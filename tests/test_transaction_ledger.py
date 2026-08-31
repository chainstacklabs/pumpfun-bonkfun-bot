from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.transaction_ledger import LedgerConflict, TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus


def _record_submission(
    ledger: TransactionLedger,
    *,
    intent: str = "intent",
    signature: str = "sig-1",
    blockhash: str = "blockhash",
    last_valid_block_height: int = 100,
    state: str = "submitted",
    wire_bytes: bytes | None = None,
    receipt_destinations: tuple[str, ...] | None = None,
) -> str:
    ledger.record_intent(intent, "wallet", 10, 5, "a" * 64)
    return ledger.record_submission(
        intent,
        signature,
        blockhash,
        last_valid_block_height,
        state=state,
        wire_bytes=wire_bytes,
        receipt_destinations=receipt_destinations,
    )


def test_prepared_wire_reservation_is_durable_and_exact(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    wire = b"\x01signed-transaction\xff"

    with TransactionLedger(path) as ledger:
        assert _record_submission(ledger, state="prepared", wire_bytes=wire) == "sig-1"
        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
                state="prepared",
                wire_bytes=b"different-transaction",
            )
            == "sig-1"
        )

    with TransactionLedger(path) as recovered:
        active = recovered.get_active_submission_record("intent")
        assert active is not None
        assert active.signature == "sig-1"
        assert active.state == "prepared"
        assert active.wire_bytes == wire

        records = recovered.list_recoverable()
        assert len(records) == 1
        assert records[0].signature == "sig-1"
        assert records[0].state == "prepared"
        assert records[0].wire_bytes == wire


def test_receipt_destinations_are_bound_to_exact_submission(
    tmp_path: Path,
) -> None:
    destinations = ("primary", "protocol-fee", "creator-fee")
    path = tmp_path / "ledger.sqlite"

    with TransactionLedger(path) as ledger:
        _record_submission(
            ledger,
            state="prepared",
            wire_bytes=b"wire",
            receipt_destinations=destinations,
        )

    with TransactionLedger(path) as recovered:
        active = recovered.get_active_submission_record("intent")
        assert active is not None
        assert active.receipt_destinations == destinations
        assert recovered.get_receipt_destinations("sig-1") == destinations
        assert recovered.list_recoverable()[0].receipt_destinations == destinations


def test_signature_cannot_be_rebound_to_different_wire_bytes(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire-one")

        with pytest.raises(LedgerConflict, match="different wire bytes"):
            _record_submission(
                ledger,
                state="prepared",
                wire_bytes=b"wire-two",
            )

        assert ledger.get_active_submission_record("intent").wire_bytes == b"wire-one"


def test_cancelling_prepared_reservation_releases_intent(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"cancelled-wire")

        assert ledger.release_prepared_submission("sig-1") is True
        assert ledger.release_prepared_submission("sig-1") is False
        assert ledger.get_active_submission("intent") is None
        assert ledger.list_recoverable() == []

        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
                state="prepared",
                wire_bytes=b"replacement-wire",
            )
            == "sig-2"
        )


def test_unsubmitted_intent_can_be_replanned_after_restart(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.record_intent("intent", "wallet", 10, 5, "a" * 64)

        ledger.record_intent("intent", "wallet", 20, 7, "b" * 64)

        row = ledger.connection.execute(
            """
            SELECT quote_amount_raw, fee_lamports, message_hash
            FROM intents WHERE intent_id = 'intent'
            """
        ).fetchone()
        assert tuple(row) == ("20", "7", "b" * 64)


def test_submitted_intent_cannot_be_replanned(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire")

        with pytest.raises(LedgerConflict, match="different data"):
            ledger.record_intent("intent", "wallet", 20, 7, "b" * 64)


def test_submitted_reservation_cannot_be_cancelled(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"sent-wire")
        ledger.mark_submission_submitted("sig-1")

        assert ledger.release_prepared_submission("sig-1") is False
        active = ledger.get_active_submission_record("intent")
        assert active is not None
        assert active.state == "submitted"
        assert active.wire_bytes == b"sent-wire"


def test_unknown_outcome_blocks_duplicate_and_remains_recoverable(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, wire_bytes=b"submitted-wire")
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.UNKNOWN, "sig-1", "rpc timeout")
        )

        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
            )
            == "sig-1"
        )
        outcome = ledger.get_outcome("sig-1")
        assert outcome == TransactionOutcome(
            TransactionStatus.UNKNOWN,
            "sig-1",
            "rpc timeout",
        )
        assert [record.signature for record in ledger.list_recoverable()] == ["sig-1"]

        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.SUCCESS, "sig-1", slot=123)
        )
        assert ledger.list_recoverable() == []
        assert ledger.get_active_submission("intent") == "sig-1"


@pytest.mark.parametrize(
    "terminal_status",
    [TransactionStatus.REVERTED, TransactionStatus.EXPIRED],
)
def test_terminal_failure_allows_fresh_submission_for_same_intent(
    tmp_path: Path,
    terminal_status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(terminal_status, "sig-1"))

        assert ledger.get_active_submission("intent") is None
        assert ledger.list_recoverable() == []
        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
            )
            == "sig-2"
        )
        assert ledger.get_active_submission("intent") == "sig-2"


def test_ledger_does_not_overwrite_final_outcome(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(TransactionStatus.REVERTED, "sig-1"))

        with pytest.raises(LedgerConflict):
            ledger.record_outcome(
                TransactionOutcome(TransactionStatus.SUCCESS, "sig-1")
            )

        assert ledger.get_outcome("sig-1").status is TransactionStatus.REVERTED


@pytest.mark.parametrize(
    "terminal_status",
    [
        TransactionStatus.SUCCESS,
        TransactionStatus.REVERTED,
        TransactionStatus.EXPIRED,
    ],
)
def test_terminal_signature_cannot_be_reserved_again(
    tmp_path: Path,
    terminal_status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(terminal_status, "sig-1"))

        with pytest.raises(LedgerConflict, match="terminal outcome"):
            _record_submission(ledger)


def test_mark_submitted_rejects_missing_or_outcome_bound_submission(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(LedgerConflict, match="not tracked"):
            ledger.mark_submission_submitted("missing")

        _record_submission(ledger)
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.UNKNOWN, "sig-1", "timeout")
        )

        with pytest.raises(LedgerConflict, match="already has an outcome"):
            ledger.mark_submission_submitted("sig-1")


def test_mark_submitted_allows_idempotent_same_state_retry(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire")

        ledger.mark_submission_submitted("sig-1")
        ledger.mark_submission_submitted("sig-1")

        assert ledger.get_active_submission_record("intent").state == "submitted"


@pytest.mark.parametrize("status", list(TransactionStatus))
def test_outcome_rejects_missing_or_prepared_submission(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(LedgerConflict, match="not tracked"):
            ledger.record_outcome(TransactionOutcome(status, "missing"))

        _record_submission(
            ledger,
            signature="prepared",
            state="prepared",
            wire_bytes=b"wire",
        )
        with pytest.raises(LedgerConflict, match="not submitted"):
            ledger.record_outcome(TransactionOutcome(status, "prepared"))


@pytest.mark.parametrize("status", list(TransactionStatus))
def test_outcome_allows_idempotent_same_status_retry(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        outcome = TransactionOutcome(status, "sig-1", "evidence", slot=7)

        ledger.record_outcome(outcome)
        ledger.record_outcome(outcome)

        assert ledger.get_outcome("sig-1") == outcome


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.SUCCESS,
        TransactionStatus.REVERTED,
        TransactionStatus.EXPIRED,
    ],
)
def test_terminal_outcome_rejects_same_status_with_conflicting_evidence(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        recorded = TransactionOutcome(status, "sig-1", "first evidence", slot=7)
        ledger.record_outcome(recorded)

        with pytest.raises(LedgerConflict, match="conflicting evidence"):
            ledger.record_outcome(
                TransactionOutcome(status, "sig-1", "different evidence", slot=8)
            )

        assert ledger.get_outcome("sig-1") == recorded


def test_unknown_observations_converge_to_terminal_success(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        first = TransactionOutcome(
            TransactionStatus.UNKNOWN,
            "sig-1",
            "first timeout",
        )
        ledger.record_outcome(first)
        ledger.record_outcome(
            TransactionOutcome(
                TransactionStatus.UNKNOWN,
                "sig-1",
                "second timeout",
                slot=8,
            )
        )

        assert ledger.get_outcome("sig-1") == first

        success = TransactionOutcome(
            TransactionStatus.SUCCESS,
            "sig-1",
            slot=9,
        )
        ledger.record_outcome(success)
        assert ledger.get_outcome("sig-1") == success


def test_close_is_idempotent_and_does_not_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = TransactionLedger(path)
    _record_submission(ledger)

    ledger.close()
    ledger.close()

    with TransactionLedger(path) as reopened:
        assert reopened.get_active_submission("intent") == "sig-1"


def test_legacy_schema_is_migrated_without_losing_submission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                signer TEXT NOT NULL,
                quote_amount_raw TEXT,
                fee_lamports TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE submissions (
                signature TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                blockhash TEXT NOT NULL,
                last_valid_block_height INTEGER NOT NULL,
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE outcomes (
                signature TEXT PRIMARY KEY REFERENCES submissions(signature),
                status TEXT NOT NULL CHECK (
                    status IN ('success', 'reverted', 'expired', 'unknown')
                ),
                error TEXT,
                slot INTEGER,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO intents (
                intent_id, signer, quote_amount_raw, fee_lamports
            ) VALUES ('legacy-intent', 'legacy-wallet', '10', '5');
            INSERT INTO submissions (
                signature, intent_id, blockhash, last_valid_block_height
            ) VALUES ('legacy-sig', 'legacy-intent', 'legacy-blockhash', 99);
            """
        )

    with TransactionLedger(path) as ledger:
        active = ledger.get_active_submission_record("legacy-intent")
        assert active is not None
        assert active.signature == "legacy-sig"
        assert active.state == "submitted"
        assert active.wire_bytes is None
        assert [record.signature for record in ledger.list_recoverable()] == [
            "legacy-sig"
        ]

        assert (
            _record_submission(
                ledger,
                intent="new-intent",
                signature="new-sig",
                state="prepared",
                wire_bytes=b"new-wire",
            )
            == "new-sig"
        )

"""Durable, idempotent transaction intent and outcome ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from core.transaction_state import TransactionOutcome, TransactionStatus


class LedgerConflict(RuntimeError):
    """Raised when an idempotency key is reused for different transaction data."""


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    """A submission reservation bound to one exact signed wire transaction."""

    signature: str
    intent_id: str
    signer: str
    quote_amount_raw: int | None
    fee_lamports: int | None
    blockhash: str
    last_valid_block_height: int
    state: str
    wire_bytes: bytes | None
    receipt_destinations: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """A submission whose terminal on-chain outcome is not yet known."""

    intent_id: str
    signer: str
    quote_amount_raw: int | None
    fee_lamports: int | None
    signature: str
    blockhash: str
    last_valid_block_height: int
    submitted_at: str
    state: str
    wire_bytes: bytes | None
    receipt_destinations: tuple[str, ...] | None


class TransactionLedger:
    """Small SQLite-WAL ledger for idempotent transaction submissions."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._closed = False
        self._connection.row_factory = sqlite3.Row
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            self._connection.close()
            raise RuntimeError("transaction ledger requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for narrow operational inspection."""
        return self._connection

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    signer TEXT NOT NULL,
                    quote_amount_raw TEXT,
                    fee_lamports TEXT,
                    message_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    signature TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                    blockhash TEXT NOT NULL,
                    last_valid_block_height INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'submitted' CHECK (
                        state IN ('prepared', 'submitted')
                    ),
                    wire_bytes BLOB,
                    receipt_destinations TEXT,
                    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS submissions_intent_idx
                    ON submissions(intent_id);

                CREATE TABLE IF NOT EXISTS outcomes (
                    signature TEXT PRIMARY KEY REFERENCES submissions(signature),
                    status TEXT NOT NULL CHECK (
                        status IN ('success', 'reverted', 'expired', 'unknown')
                    ),
                    error TEXT,
                    slot INTEGER,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            intent_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(intents)"
                ).fetchall()
            }
            if "message_hash" not in intent_columns:
                self._connection.execute(
                    "ALTER TABLE intents ADD COLUMN message_hash TEXT"
                )

            submission_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(submissions)"
                ).fetchall()
            }
            if "state" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN state TEXT "
                    "NOT NULL DEFAULT 'submitted'"
                )
            if "wire_bytes" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN wire_bytes BLOB"
                )
            if "receipt_destinations" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN receipt_destinations TEXT"
                )

    @staticmethod
    def _validate_raw_amount(name: str, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative integer or None")

    @staticmethod
    def _encode_receipt_destinations(
        destinations: tuple[str, ...] | None,
    ) -> str | None:
        """Encode the exact native-quote recipients bound to a wire transaction."""
        if destinations is None:
            return None
        if not isinstance(destinations, tuple):
            raise TypeError("receipt_destinations must be a tuple or None")
        if any(not isinstance(item, str) or not item for item in destinations):
            raise ValueError("receipt_destinations must contain non-empty strings")
        if len(set(destinations)) != len(destinations):
            raise ValueError("receipt_destinations must not contain duplicates")
        return json.dumps(list(destinations), separators=(",", ":"))

    @staticmethod
    def _decode_receipt_destinations(raw_value: object) -> tuple[str, ...] | None:
        """Decode durable receipt recipients, rejecting corrupt ledger state."""
        if raw_value is None:
            return None
        if not isinstance(raw_value, str):
            raise LedgerConflict("receipt destinations are not stored as JSON text")
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise LedgerConflict("receipt destinations contain invalid JSON") from exc
        if (
            not isinstance(decoded, list)
            or any(not isinstance(item, str) or not item for item in decoded)
            or len(set(decoded)) != len(decoded)
        ):
            raise LedgerConflict("receipt destinations contain invalid values")
        return tuple(decoded)

    def record_intent(
        self,
        intent_id: str,
        signer: str,
        quote_amount_raw: int | None,
        fee_lamports: int | None,
        message_hash: str | None = None,
    ) -> None:
        """Create an intent, or accept an exact replay of the same intent."""
        if not intent_id or not signer:
            raise ValueError("intent_id and signer are required")
        self._validate_raw_amount("quote_amount_raw", quote_amount_raw)
        self._validate_raw_amount("fee_lamports", fee_lamports)
        if message_hash is not None and (
            len(message_hash) != 64
            or any(character not in "0123456789abcdef" for character in message_hash)
        ):
            raise ValueError("message_hash must be a lowercase SHA-256 digest")

        values = (
            intent_id,
            signer,
            None if quote_amount_raw is None else str(quote_amount_raw),
            None if fee_lamports is None else str(fee_lamports),
            message_hash,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT
                        intent_id, signer, quote_amount_raw, fee_lamports, message_hash
                    FROM intents WHERE intent_id = ?
                    """,
                    (intent_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        """
                        INSERT INTO intents (
                            intent_id, signer, quote_amount_raw, fee_lamports, message_hash
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                elif tuple(row) != values:
                    has_submission = self._connection.execute(
                        "SELECT 1 FROM submissions WHERE intent_id = ? LIMIT 1",
                        (intent_id,),
                    ).fetchone()
                    if has_submission is not None:
                        raise LedgerConflict(
                            f"intent id {intent_id!r} is already bound to different data"
                        )
                    self._connection.execute(
                        """
                        UPDATE intents
                        SET signer = ?, quote_amount_raw = ?, fee_lamports = ?,
                            message_hash = ?
                        WHERE intent_id = ?
                        """,
                        (
                            signer,
                            str(quote_amount_raw)
                            if quote_amount_raw is not None
                            else None,
                            str(fee_lamports) if fee_lamports is not None else None,
                            message_hash,
                            intent_id,
                        ),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_active_submission_record(self, intent_id: str) -> SubmissionRecord | None:
        """Return the safest reusable submission for an intent."""
        if not intent_id:
            raise ValueError("intent_id is required")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    s.signature,
                    s.intent_id,
                    i.signer,
                    i.quote_amount_raw,
                    i.fee_lamports,
                    s.blockhash,
                    s.last_valid_block_height,
                    s.state,
                    s.wire_bytes,
                    s.receipt_destinations
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN outcomes AS o ON o.signature = s.signature
                WHERE s.intent_id = ?
                  AND (
                      o.status IS NULL
                      OR o.status IN ('unknown', 'success')
                  )
                ORDER BY
                    CASE o.status
                        WHEN 'success' THEN 0
                        WHEN 'unknown' THEN 1
                        ELSE 2
                    END,
                    s.submitted_at DESC,
                    s.rowid DESC
                LIMIT 1
                """,
                (intent_id,),
            ).fetchone()
        if row is None:
            return None
        wire_bytes = row["wire_bytes"]
        return SubmissionRecord(
            signature=str(row["signature"]),
            intent_id=str(row["intent_id"]),
            blockhash=str(row["blockhash"]),
            last_valid_block_height=int(row["last_valid_block_height"]),
            state=str(row["state"]),
            signer=str(row["signer"]),
            quote_amount_raw=(
                None
                if row["quote_amount_raw"] is None
                else int(row["quote_amount_raw"])
            ),
            fee_lamports=(
                None if row["fee_lamports"] is None else int(row["fee_lamports"])
            ),
            wire_bytes=None if wire_bytes is None else bytes(wire_bytes),
            receipt_destinations=self._decode_receipt_destinations(
                row["receipt_destinations"]
            ),
        )

    def get_active_submission(self, intent_id: str) -> str | None:
        """Return a submission that is successful or not yet resolved."""
        record = self.get_active_submission_record(intent_id)
        return None if record is None else record.signature

    def get_receipt_destinations(self, signature: str) -> tuple[str, ...] | None:
        """Return exact native-quote receipt destinations for a submission."""
        if not signature:
            raise ValueError("signature is required")
        with self._lock:
            row = self._connection.execute(
                "SELECT receipt_destinations FROM submissions WHERE signature = ?",
                (signature,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_receipt_destinations(row["receipt_destinations"])

    @staticmethod
    def _validate_submission_state(state: str) -> None:
        if state not in {"prepared", "submitted"}:
            raise ValueError("state must be 'prepared' or 'submitted'")

    def record_submission(
        self,
        intent_id: str,
        signature: str,
        blockhash: str,
        last_valid_block_height: int,
        *,
        wire_bytes: bytes | None = None,
        state: str = "submitted",
        receipt_destinations: tuple[str, ...] | None = None,
    ) -> str:
        """Atomically reserve one reusable exact-wire submission."""
        if not intent_id or not signature or not blockhash:
            raise ValueError("intent_id, signature, and blockhash are required")
        if (
            isinstance(last_valid_block_height, bool)
            or not isinstance(last_valid_block_height, int)
            or last_valid_block_height < 0
        ):
            raise ValueError("last_valid_block_height must be a non-negative integer")
        self._validate_submission_state(state)
        if wire_bytes is not None and not isinstance(
            wire_bytes, (bytes, bytearray, memoryview)
        ):
            raise TypeError("wire_bytes must be bytes-like or None")
        normalized_wire = None if wire_bytes is None else bytes(wire_bytes)
        normalized_destinations = self._encode_receipt_destinations(
            receipt_destinations
        )

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                active = self._connection.execute(
                    """
                    SELECT s.signature
                    FROM submissions AS s
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.intent_id = ?
                      AND (
                          o.status IS NULL
                          OR o.status IN ('unknown', 'success')
                      )
                    ORDER BY
                        CASE o.status
                            WHEN 'success' THEN 0
                            WHEN 'unknown' THEN 1
                            ELSE 2
                        END,
                        s.submitted_at DESC,
                        s.rowid DESC
                    LIMIT 1
                    """,
                    (intent_id,),
                ).fetchone()
                if active is not None and str(active["signature"]) != signature:
                    self._connection.commit()
                    return str(active["signature"])

                self._connection.execute(
                    """
                    INSERT INTO submissions (
                        signature, intent_id, blockhash, last_valid_block_height,
                        state, wire_bytes, receipt_destinations
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(signature) DO NOTHING
                    """,
                    (
                        signature,
                        intent_id,
                        blockhash,
                        last_valid_block_height,
                        state,
                        normalized_wire,
                        normalized_destinations,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT
                        s.signature,
                        s.intent_id,
                        s.blockhash,
                        s.last_valid_block_height,
                        s.state,
                        s.wire_bytes,
                        s.receipt_destinations,
                        o.status AS outcome_status
                    FROM submissions AS s
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.signature = ?
                    """,
                    (signature,),
                ).fetchone()
                if row is None:
                    raise LedgerConflict(
                        f"submission {signature!r} disappeared during reservation"
                    )
                outcome_status = row["outcome_status"]
                if outcome_status in {
                    TransactionStatus.SUCCESS.value,
                    TransactionStatus.REVERTED.value,
                    TransactionStatus.EXPIRED.value,
                }:
                    raise LedgerConflict(
                        f"signature {signature!r} already has terminal outcome "
                        f"{outcome_status!r}"
                    )
                if (
                    str(row["intent_id"]) != intent_id
                    or str(row["blockhash"]) != blockhash
                    or int(row["last_valid_block_height"]) != last_valid_block_height
                ):
                    raise LedgerConflict(
                        f"signature {signature!r} is already bound to different data"
                    )
                existing_wire = row["wire_bytes"]
                if (
                    normalized_wire is not None
                    and existing_wire is not None
                    and bytes(existing_wire) != normalized_wire
                ):
                    raise LedgerConflict(
                        f"signature {signature!r} is bound to different wire bytes"
                    )
                existing_destinations = row["receipt_destinations"]
                if normalized_destinations is not None:
                    if existing_destinations is None:
                        raise LedgerConflict(
                            f"signature {signature!r} has no durable receipt context"
                        )
                    if self._decode_receipt_destinations(existing_destinations) != (
                        receipt_destinations
                    ):
                        raise LedgerConflict(
                            f"signature {signature!r} is bound to different "
                            "receipt destinations"
                        )
                existing_state = str(row["state"])
                if existing_state == "submitted" and state == "prepared":
                    raise LedgerConflict(
                        f"submission {signature!r} cannot return to prepared state"
                    )
                if (existing_state == "prepared" and state == "submitted") or (
                    existing_wire is None and normalized_wire is not None
                ):
                    cursor = self._connection.execute(
                        """
                        UPDATE submissions
                        SET state = CASE
                                WHEN state = 'prepared' AND ? = 'submitted'
                                THEN 'submitted'
                                ELSE state
                            END,
                            wire_bytes = COALESCE(wire_bytes, ?)
                        WHERE signature = ?
                        """,
                        (state, normalized_wire, signature),
                    )
                    if cursor.rowcount != 1:
                        raise LedgerConflict(
                            f"submission {signature!r} changed during reservation"
                        )
                self._connection.commit()
                return signature
            except Exception:
                self._connection.rollback()
                raise

    def mark_submission_submitted(self, signature: str) -> None:
        """Mark a prepared wire transaction as eligible for recovery."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE submissions SET state = 'submitted'
                    WHERE signature = ? AND state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes
                          WHERE signature = submissions.signature
                      )
                    """,
                    (signature,),
                )
                if cursor.rowcount == 1:
                    self._connection.commit()
                    return

                row = self._connection.execute(
                    """
                    SELECT s.state, o.status AS outcome_status
                    FROM submissions AS s
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.signature = ?
                    """,
                    (signature,),
                ).fetchone()
                if row is None:
                    raise LedgerConflict(f"submission {signature!r} is not tracked")
                if row["outcome_status"] is not None:
                    raise LedgerConflict(
                        f"submission {signature!r} already has an outcome"
                    )
                if str(row["state"]) != "submitted":
                    raise LedgerConflict(f"submission {signature!r} is not prepared")
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def release_prepared_submission(self, signature: str) -> bool:
        """Remove a reservation only when no network send was attempted."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                prepared = self._connection.execute(
                    """
                    SELECT intent_id FROM submissions
                    WHERE signature = ? AND state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes
                          WHERE signature = submissions.signature
                      )
                    """,
                    (signature,),
                ).fetchone()
                cursor = self._connection.execute(
                    """
                    DELETE FROM submissions
                    WHERE signature = ? AND state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes WHERE signature = submissions.signature
                      )
                    """,
                    (signature,),
                )
                if cursor.rowcount == 1 and prepared is not None:
                    self._connection.execute(
                        """
                        DELETE FROM intents
                        WHERE intent_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM submissions
                              WHERE intent_id = intents.intent_id
                          )
                        """,
                        (str(prepared["intent_id"]),),
                    )
                self._connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self._connection.rollback()
                raise

    def record_outcome(
        self,
        outcome: TransactionOutcome,
        *,
        allow_prepared: bool = False,
    ) -> None:
        """Atomically record evidence without replacing a final status."""
        if not isinstance(allow_prepared, bool):
            raise TypeError("allow_prepared must be a boolean")
        values = (
            outcome.signature,
            outcome.status.value,
            outcome.error,
            outcome.slot,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                submission = self._connection.execute(
                    """
                    UPDATE submissions SET state = 'submitted'
                    WHERE signature = ?
                      AND (
                          state = 'submitted'
                          OR (? = 1 AND state = 'prepared')
                      )
                    """,
                    (outcome.signature, int(allow_prepared)),
                )
                if submission.rowcount != 1:
                    row = self._connection.execute(
                        "SELECT state FROM submissions WHERE signature = ?",
                        (outcome.signature,),
                    ).fetchone()
                    if row is None:
                        raise LedgerConflict(
                            f"submission {outcome.signature!r} is not tracked"
                        )
                    raise LedgerConflict(
                        f"submission {outcome.signature!r} is not submitted"
                    )

                existing = self._connection.execute(
                    """
                    SELECT status, error, slot
                    FROM outcomes WHERE signature = ?
                    """,
                    (outcome.signature,),
                ).fetchone()
                if existing is None:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO outcomes (signature, status, error, slot)
                        VALUES (?, ?, ?, ?)
                        """,
                        values,
                    )
                    if cursor.rowcount != 1:
                        raise LedgerConflict(
                            f"could not record outcome for {outcome.signature!r}"
                        )
                    self._connection.commit()
                    return

                existing_status = TransactionStatus(existing["status"])
                if (
                    existing_status is TransactionStatus.UNKNOWN
                    and outcome.status is TransactionStatus.UNKNOWN
                ):
                    # UNKNOWN is an observation, not final evidence. Retain the
                    # first diagnostic while allowing later polls to converge.
                    self._connection.commit()
                    return
                if existing_status is outcome.status:
                    if (
                        existing["error"] != outcome.error
                        or existing["slot"] != outcome.slot
                    ):
                        raise LedgerConflict(
                            f"outcome for {outcome.signature!r} has "
                            "conflicting evidence"
                        )
                    self._connection.commit()
                    return
                if existing_status is not TransactionStatus.UNKNOWN:
                    raise LedgerConflict(
                        f"final outcome for {outcome.signature!r} cannot be replaced"
                    )
                cursor = self._connection.execute(
                    """
                    UPDATE outcomes
                    SET status = ?, error = ?, slot = ?,
                        observed_at = CURRENT_TIMESTAMP
                    WHERE signature = ? AND status = 'unknown'
                    """,
                    (
                        outcome.status.value,
                        outcome.error,
                        outcome.slot,
                        outcome.signature,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LedgerConflict(
                        f"outcome for {outcome.signature!r} changed concurrently"
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_outcome(self, signature: str) -> TransactionOutcome | None:
        """Return the latest recorded outcome for a signature."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT status, error, slot FROM outcomes WHERE signature = ?
                """,
                (signature,),
            ).fetchone()
        if row is None:
            return None
        return TransactionOutcome(
            status=TransactionStatus(row["status"]),
            signature=signature,
            error=row["error"],
            slot=row["slot"],
        )

    def get_last_valid_block_height(self, signature: str) -> int | None:
        """Return the validity height retained for a submitted signature."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_valid_block_height
                FROM submissions WHERE signature = ?
                """,
                (signature,),
            ).fetchone()
        return None if row is None else int(row["last_valid_block_height"])

    def list_recoverable(self, limit: int = 100) -> list[RecoveryRecord]:
        """List unresolved submissions, including prepared exact-wire sends."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    i.intent_id,
                    i.signer,
                    i.quote_amount_raw,
                    i.fee_lamports,
                    s.signature,
                    s.blockhash,
                    s.last_valid_block_height,
                    s.submitted_at,
                    s.state,
                    s.wire_bytes,
                    s.receipt_destinations
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN outcomes AS o ON o.signature = s.signature
                WHERE o.signature IS NULL OR o.status = 'unknown'
                ORDER BY s.submitted_at, s.rowid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            RecoveryRecord(
                intent_id=str(row["intent_id"]),
                signer=str(row["signer"]),
                quote_amount_raw=(
                    None
                    if row["quote_amount_raw"] is None
                    else int(row["quote_amount_raw"])
                ),
                fee_lamports=(
                    None if row["fee_lamports"] is None else int(row["fee_lamports"])
                ),
                signature=str(row["signature"]),
                blockhash=str(row["blockhash"]),
                last_valid_block_height=int(row["last_valid_block_height"]),
                submitted_at=str(row["submitted_at"]),
                state=str(row["state"]),
                wire_bytes=(
                    None if row["wire_bytes"] is None else bytes(row["wire_bytes"])
                ),
                receipt_destinations=self._decode_receipt_destinations(
                    row["receipt_destinations"]
                ),
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the ledger connection idempotently."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> TransactionLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

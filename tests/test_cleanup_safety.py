from __future__ import annotations

import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from cleanup.manager import AccountCleanupManager, CleanupResult, CleanupStatus
from cleanup.modes import handle_cleanup_after_sell, handle_cleanup_post_session
from core.client import (
    TransactionOutcome,
    TransactionStatus,
    TransactionSubmissionUnknown,
)
from core.pubkeys import SystemAddresses


class _FakeWallet:
    keypair = object()
    pubkey = Pubkey.new_unique()

    def get_associated_token_address(self, mint: Pubkey, program: Pubkey) -> Pubkey:
        return Pubkey.new_unique()


class _FakeClient:
    execution_policy = SimpleNamespace(validate_force_burn=lambda requested: None)

    async def get_account_info(self, address: Pubkey):
        return SimpleNamespace(owner=SystemAddresses.TOKEN_PROGRAM)

    async def get_token_account_balance(self, address: Pubkey) -> int:
        return 0


class _FakeFees:
    async def calculate_priority_fee(self, addresses):
        return 0


@pytest.mark.asyncio
async def test_cleanup_does_not_close_unowned_zero_balance_ata(monkeypatch) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    result = await AccountCleanupManager(
        _FakeClient(), wallet, _FakeFees()
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)
    assert result.status is CleanupStatus.OWNERSHIP_UNPROVEN


@pytest.mark.asyncio
async def test_cleanup_does_not_unwrap_unowned_positive_wsol(monkeypatch) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    client = _FakeClient()
    client.get_token_account_balance = _positive_balance
    result = await AccountCleanupManager(client, wallet, _FakeFees()).cleanup_ata(
        SystemAddresses.WSOL_MINT, SystemAddresses.TOKEN_PROGRAM
    )
    assert result.status is CleanupStatus.OWNERSHIP_UNPROVEN


@pytest.mark.asyncio
async def test_force_burn_rejects_third_party_deposit_after_confirmed_sell(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    client = _CleanupClient(balance=15)
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=100,
    )
    AccountCleanupManager.record_confirmed_sell_delta(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        sold_raw=90,
    )

    result = await AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        force_burn=True,
        journal_path=tmp_path / "cleanup.json",
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert result.status is CleanupStatus.OWNERSHIP_UNPROVEN
    assert client.submissions == 0


@pytest.mark.asyncio
async def test_force_burn_allows_exact_confirmed_residual(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    client = _CleanupClient(
        balance=10,
        outcomes=[TransactionOutcome(TransactionStatus.SUCCESS, "cleanup-signature")],
    )
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=100,
    )
    AccountCleanupManager.record_confirmed_sell_delta(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        sold_raw=90,
    )

    result = await AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        force_burn=True,
        journal_path=tmp_path / "cleanup.json",
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert result.status is CleanupStatus.CONFIRMED
    assert client.submissions == 1
    assert len(client.submitted_instructions) == 2


@pytest.mark.asyncio
async def test_confirmation_exception_remains_durable_and_unresolved(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    client = _CleanupClient(
        balance=0,
        outcomes=[RuntimeError("rpc offline")],
    )

    result = await AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert result.status is CleanupStatus.UNRESOLVED
    assert result.tx_signature == "cleanup-signature"
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    entry = next(iter(persisted["entries"].values()))
    assert entry["status"] == CleanupStatus.UNRESOLVED.value


@pytest.mark.asyncio
async def test_signature_bearing_submission_ambiguity_remains_recoverable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    first_client = _CleanupClient(
        balance=0,
        submission_error=TransactionSubmissionUnknown(
            "ambiguous-cleanup-signature", "RPC response lost"
        ),
    )

    first_result = await AccountCleanupManager(
        first_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert first_result.status is CleanupStatus.UNRESOLVED
    assert first_result.tx_signature == "ambiguous-cleanup-signature"
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    entry = next(iter(persisted["entries"].values()))
    assert entry["status"] == CleanupStatus.UNRESOLVED.value
    assert entry["tx_signature"] == "ambiguous-cleanup-signature"
    assert entry["generation"] in entry["intent_id"]

    AccountCleanupManager._pending_signatures.clear()
    recovery_client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(TransactionStatus.SUCCESS, "ambiguous-cleanup-signature")
        ],
    )
    recovered = await AccountCleanupManager(
        recovery_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert recovered.status is CleanupStatus.CONFIRMED
    assert recovered.tx_signature == "ambiguous-cleanup-signature"
    assert recovery_client.confirmed_signatures == ["ambiguous-cleanup-signature"]
    assert recovery_client.submissions == 0


@pytest.mark.asyncio
async def test_presend_cleanup_failure_remains_retryable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    failed_client = _CleanupClient(
        balance=0,
        submission_error=RuntimeError("blockhash endpoint unavailable"),
    )

    unresolved = await AccountCleanupManager(
        failed_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert unresolved.status is CleanupStatus.UNRESOLVED
    assert unresolved.tx_signature is None

    recovered_client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(TransactionStatus.SUCCESS, "retry-cleanup-signature")
        ],
        signatures=["retry-cleanup-signature"],
    )
    recovered = await AccountCleanupManager(
        recovered_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert recovered.status is CleanupStatus.CONFIRMED
    assert recovered.tx_signature == "retry-cleanup-signature"
    assert recovered_client.submissions == 1


@pytest.mark.asyncio
async def test_startup_worker_consumes_staged_cleanup(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    token_program = SystemAddresses.TOKEN_PROGRAM
    journal_path = tmp_path / "cleanup.json"
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        token_program,
        baseline_raw=0,
        acquired_raw=1,
    )
    AccountCleanupManager(
        _CleanupClient(balance=0),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).stage_confirmed_sell_cleanup(mint, token_program, sold_raw=1)
    key = (str(wallet.pubkey), str(mint), str(token_program))
    AccountCleanupManager._ownership_records.pop(key, None)
    AccountCleanupManager._pending_signatures.pop(key, None)
    recovery_client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(TransactionStatus.SUCCESS, "resumed-cleanup-signature")
        ],
        signatures=["resumed-cleanup-signature"],
    )

    results = await AccountCleanupManager(
        recovery_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).resume_pending_cleanups()

    assert [result.status for result in results] == [CleanupStatus.CONFIRMED]
    assert recovery_client.submissions == 1
    assert json.loads(journal_path.read_text(encoding="utf-8"))["entries"] == {}


@pytest.mark.asyncio
async def test_unresolved_cleanup_survives_restart_and_terminal_failure_persists(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    first_client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(
                TransactionStatus.UNKNOWN,
                "cleanup-signature",
                error="rpc timeout",
            )
        ],
    )

    first_result = await AccountCleanupManager(
        first_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert first_result.status is CleanupStatus.UNRESOLVED
    assert first_result.tx_signature == "cleanup-signature"
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["entries"]
    first_entry = next(iter(persisted["entries"].values()))
    first_generation = first_entry["generation"]
    first_intent_id = first_entry["intent_id"]

    AccountCleanupManager._pending_signatures.clear()
    second_client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(
                TransactionStatus.REVERTED,
                "cleanup-signature",
                error="custom program error",
            )
        ],
    )
    second_result = await AccountCleanupManager(
        second_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert second_result.status is CleanupStatus.FAILED
    assert second_result.tx_signature == "cleanup-signature"
    assert second_client.confirmed_signatures == ["cleanup-signature"]
    assert second_client.submissions == 0
    persisted_after_restart = json.loads(journal_path.read_text(encoding="utf-8"))
    restarted_entry = next(iter(persisted_after_restart["entries"].values()))
    assert restarted_entry["generation"] == first_generation
    assert restarted_entry["intent_id"] == first_intent_id

    AccountCleanupManager._pending_signatures.clear()
    third_client = _CleanupClient(balance=0)
    third_result = await AccountCleanupManager(
        third_client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert third_result.status is CleanupStatus.FAILED
    assert third_result.tx_signature == "cleanup-signature"
    assert third_client.confirmed_signatures == []
    assert third_client.submissions == 0


def test_legacy_cleanup_journal_preserves_intent_during_migration(tmp_path) -> None:
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    ata = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    journal_key = ":".join(
        (
            str(wallet.pubkey),
            str(mint),
            str(SystemAddresses.TOKEN_PROGRAM),
        )
    )
    legacy_intent = f"cleanup:{wallet.pubkey}:{mint}:{ata}"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet.pubkey),
                "entries": {
                    journal_key: {
                        "status": CleanupStatus.UNRESOLVED.value,
                        "mint": str(mint),
                        "ata": str(ata),
                        "token_program_id": str(SystemAddresses.TOKEN_PROGRAM),
                        "balance_raw": 0,
                        "tx_signature": "legacy-cleanup-signature",
                        "error": "confirmation pending",
                        "intent_id": legacy_intent,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    AccountCleanupManager(
        _CleanupClient(balance=0),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )

    migrated = json.loads(journal_path.read_text(encoding="utf-8"))
    migrated_entry = migrated["entries"][journal_key]
    assert migrated["version"] == 3
    assert migrated_entry["intent_id"] == legacy_intent
    assert migrated_entry["tx_signature"] == "legacy-cleanup-signature"
    assert migrated_entry["generation"]


def test_staged_sell_cleanup_restores_ownership_without_double_counting(
    tmp_path,
) -> None:
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    token_program = SystemAddresses.TOKEN_PROGRAM
    ata = Pubkey.new_unique()
    wallet.get_associated_token_address = lambda _mint, _program: ata
    journal_path = tmp_path / "cleanup.json"
    key = (str(wallet.pubkey), str(mint), str(token_program))
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        token_program,
        baseline_raw=0,
        acquired_raw=100,
    )
    first_manager = AccountCleanupManager(
        _CleanupClient(balance=10),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )

    staged = first_manager.stage_confirmed_sell_cleanup(
        mint,
        token_program,
        sold_raw=90,
    )

    assert staged.status is CleanupStatus.UNRESOLVED
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    entry = next(iter(payload["entries"].values()))
    assert entry["ownership"]["confirmed_sold_raw"] == 90

    AccountCleanupManager._ownership_records.pop(key, None)
    restarted_manager = AccountCleanupManager(
        _CleanupClient(balance=10),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )
    repeated = restarted_manager.stage_confirmed_sell_cleanup(
        mint,
        token_program,
        sold_raw=90,
    )

    assert repeated.status is CleanupStatus.UNRESOLVED
    assert AccountCleanupManager._ownership_records[key].confirmed_sold_raw == 90


def test_cleanup_journal_mutations_are_serialized_across_managers(
    monkeypatch, tmp_path
) -> None:
    wallet = _FakeWallet()
    journal_path = tmp_path / "cleanup.json"
    first_manager = AccountCleanupManager(
        _CleanupClient(balance=0),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )
    second_manager = AccountCleanupManager(
        _CleanupClient(balance=0),
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )
    first_mint = Pubkey.new_unique()
    second_mint = Pubkey.new_unique()
    first_key = (
        str(wallet.pubkey),
        str(first_mint),
        str(SystemAddresses.TOKEN_PROGRAM),
    )
    second_key = (
        str(wallet.pubkey),
        str(second_mint),
        str(SystemAddresses.TOKEN_PROGRAM),
    )
    first_result = CleanupResult(
        CleanupStatus.UNRESOLVED,
        first_mint,
        Pubkey.new_unique(),
        SystemAddresses.TOKEN_PROGRAM,
        balance_raw=0,
        tx_signature="first-signature",
    )
    second_result = CleanupResult(
        CleanupStatus.UNRESOLVED,
        second_mint,
        Pubkey.new_unique(),
        SystemAddresses.TOKEN_PROGRAM,
        balance_raw=0,
        tx_signature="second-signature",
    )
    first_write_started = Event()
    release_first_write = Event()
    second_started = Event()
    second_done = Event()
    errors: list[BaseException] = []
    original_first_write = first_manager._write_journal

    def blocked_first_write() -> None:
        first_write_started.set()
        release_first_write.wait(timeout=2)
        original_first_write()

    def persist_first() -> None:
        try:
            first_manager._persist_result(
                first_key,
                first_result,
                intent_id="first-intent",
                generation="first-generation",
            )
        except BaseException as exc:
            errors.append(exc)

    def persist_second() -> None:
        second_started.set()
        try:
            second_manager._persist_result(
                second_key,
                second_result,
                intent_id="second-intent",
                generation="second-generation",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_done.set()

    monkeypatch.setattr(first_manager, "_write_journal", blocked_first_write)
    first_thread = Thread(target=persist_first)
    second_thread = Thread(target=persist_second)
    first_thread.start()
    assert first_write_started.wait(timeout=1)
    second_thread.start()
    assert second_started.wait(timeout=1)
    try:
        assert not second_done.wait(timeout=0.1)
    finally:
        release_first_write.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert set(persisted["entries"]) == {
        ":".join(first_key),
        ":".join(second_key),
    }


@pytest.mark.asyncio
async def test_reacquisition_after_terminal_failure_starts_a_new_cleanup_attempt(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    ata = Pubkey.new_unique()
    monkeypatch.setattr(
        wallet,
        "get_associated_token_address",
        lambda mint, program: ata,
    )
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(
                TransactionStatus.REVERTED,
                "failed-cleanup-signature",
                error="custom program error",
            ),
            TransactionOutcome(TransactionStatus.SUCCESS, "retry-cleanup-signature"),
        ],
        signatures=["failed-cleanup-signature", "retry-cleanup-signature"],
    )
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    manager = AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    )

    failed = await manager.cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=2,
    )
    recovered = await manager.cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert failed.status is CleanupStatus.FAILED
    assert recovered.status is CleanupStatus.CONFIRMED
    assert recovered.tx_signature == "retry-cleanup-signature"
    assert client.submissions == 2
    assert client.submitted_intent_ids[0] != client.submitted_intent_ids[1]


@pytest.mark.asyncio
async def test_older_ambiguous_cleanup_cannot_delete_new_ownership_generation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    mint = Pubkey.new_unique()
    token_program = SystemAddresses.TOKEN_PROGRAM
    client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(TransactionStatus.UNKNOWN, "first-cleanup-signature"),
            TransactionOutcome(TransactionStatus.SUCCESS, "first-cleanup-signature"),
            TransactionOutcome(TransactionStatus.SUCCESS, "second-cleanup-signature"),
        ],
        signatures=["first-cleanup-signature", "second-cleanup-signature"],
    )
    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        token_program,
        baseline_raw=0,
        acquired_raw=1,
    )
    manager = AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        journal_path=tmp_path / "cleanup.json",
    )
    unresolved = await manager.cleanup_ata(mint, token_program)

    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        token_program,
        baseline_raw=0,
        acquired_raw=2,
    )
    recovered = await manager.cleanup_ata(mint, token_program)

    assert unresolved.status is CleanupStatus.UNRESOLVED
    assert recovered.status is CleanupStatus.CONFIRMED
    assert recovered.tx_signature == "second-cleanup-signature"
    assert client.submissions == 2


@pytest.mark.asyncio
async def test_reacquired_ata_uses_a_new_cleanup_intent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cleanup.manager.asyncio.sleep", _no_sleep)
    wallet = _FakeWallet()
    ata = Pubkey.new_unique()
    monkeypatch.setattr(
        wallet,
        "get_associated_token_address",
        lambda mint, program: ata,
    )
    mint = Pubkey.new_unique()
    journal_path = tmp_path / "cleanup.json"
    client = _CleanupClient(
        balance=0,
        outcomes=[
            TransactionOutcome(TransactionStatus.SUCCESS, "first-cleanup-signature"),
            TransactionOutcome(TransactionStatus.SUCCESS, "second-cleanup-signature"),
        ],
        signatures=["first-cleanup-signature", "second-cleanup-signature"],
    )

    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=1,
    )
    first = await AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    AccountCleanupManager.record_bot_owned_balance(
        wallet.pubkey,
        mint,
        SystemAddresses.TOKEN_PROGRAM,
        baseline_raw=0,
        acquired_raw=2,
    )
    second = await AccountCleanupManager(
        client,
        wallet,
        _FakeFees(),
        journal_path=journal_path,
    ).cleanup_ata(mint, SystemAddresses.TOKEN_PROGRAM)

    assert first.status is CleanupStatus.CONFIRMED
    assert second.status is CleanupStatus.CONFIRMED
    assert first.tx_signature == "first-cleanup-signature"
    assert second.tx_signature == "second-cleanup-signature"
    assert client.submissions == 2
    assert len(client.submitted_intent_ids) == 2
    assert client.submitted_intent_ids[0] != client.submitted_intent_ids[1]


@pytest.mark.asyncio
async def test_cleanup_dispatchers_return_nonterminal_results(monkeypatch) -> None:
    mint = Pubkey.new_unique()
    token_program = SystemAddresses.TOKEN_PROGRAM
    unresolved = CleanupResult(
        CleanupStatus.UNRESOLVED,
        mint,
        token_program_id=token_program,
        tx_signature="cleanup-signature",
    )

    class _ResultManager:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def cleanup_ata(self, requested_mint, requested_program):
            assert requested_mint == mint
            assert requested_program == token_program
            return unresolved

    monkeypatch.setattr("cleanup.modes.AccountCleanupManager", _ResultManager)
    wallet = _FakeWallet()

    after_sell = await handle_cleanup_after_sell(
        object(),
        wallet,
        mint,
        token_program,
        _FakeFees(),
        "after_sell",
        False,
        False,
    )
    post_session = await handle_cleanup_post_session(
        object(),
        wallet,
        [mint],
        [token_program],
        _FakeFees(),
        "post_session",
        False,
        False,
    )

    assert after_sell is unresolved
    assert post_session == [unresolved]


@pytest.mark.asyncio
async def test_post_session_mode_records_confirmed_sell_before_skipping_cleanup(
    monkeypatch,
) -> None:
    mint = Pubkey.new_unique()
    token_program = SystemAddresses.TOKEN_PROGRAM
    recorded: list[tuple[Pubkey, Pubkey, Pubkey, int]] = []

    class _ResultManager:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("post-session mode must not construct a manager")

        @staticmethod
        def record_confirmed_sell_delta(
            wallet_pubkey: Pubkey,
            requested_mint: Pubkey,
            requested_program: Pubkey,
            *,
            sold_raw: int,
        ) -> None:
            recorded.append(
                (wallet_pubkey, requested_mint, requested_program, sold_raw)
            )

    monkeypatch.setattr("cleanup.modes.AccountCleanupManager", _ResultManager)
    wallet = _FakeWallet()

    result = await handle_cleanup_after_sell(
        object(),
        wallet,
        mint,
        token_program,
        _FakeFees(),
        "post_session",
        False,
        False,
        confirmed_sold_raw=7,
    )

    assert result is None
    assert recorded == [(wallet.pubkey, mint, token_program, 7)]


async def _no_sleep(seconds: float) -> None:
    return None


async def _positive_balance(address: Pubkey) -> int:
    return 10


class _CleanupClient:
    execution_policy = SimpleNamespace(validate_force_burn=lambda requested: None)

    def __init__(
        self,
        *,
        balance: int,
        outcomes=None,
        submission_error: Exception | None = None,
        signatures=None,
    ) -> None:
        self.balance = balance
        self.outcomes = list(outcomes or [])
        self.submission_error = submission_error
        self.signatures = list(signatures or [])
        self.submissions = 0
        self.confirmed_signatures: list[str] = []
        self.submitted_instructions = []
        self.submitted_intent_ids: list[str] = []

    async def get_account_info(self, address: Pubkey):
        return SimpleNamespace(owner=SystemAddresses.TOKEN_PROGRAM)

    async def get_token_account_balance(self, address: Pubkey) -> int:
        return self.balance

    async def build_and_send_transaction(self, instructions, keypair, **kwargs):
        self.submissions += 1
        self.submitted_instructions = list(instructions)
        self.submitted_intent_ids.append(kwargs["intent_id"])
        if self.submission_error is not None:
            raise self.submission_error
        if self.signatures:
            return self.signatures.pop(0)
        return "cleanup-signature"

    async def confirm_transaction_outcome(self, signature: str):
        self.confirmed_signatures.append(signature)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

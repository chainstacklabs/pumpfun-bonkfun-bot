from __future__ import annotations

import asyncio
import hashlib
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from solana.exceptions import SolanaRpcException
from solana.rpc.core import RPCException
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from core import client as client_module
from core.client import SolanaClient
from core.execution_policy import ExecutionBlocked, ExecutionPolicy, TradeLimitExceeded
from core.pubkeys import WSOL_MINT
from core.transaction_ledger import TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus


def _live_client(
    tmp_path, rpc: object
) -> tuple[SolanaClient, TransactionLedger, Keypair]:
    signer = Keypair()
    policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=100_000,
        allow_skip_preflight=True,
    )
    ledger = TransactionLedger(tmp_path / "transactions.sqlite3")
    client = SolanaClient(
        "http://offline.invalid", execution_policy=policy, ledger=ledger
    )
    client._client = rpc
    context = client_module._BlockhashContext(Hash.default(), 100)

    async def cached_blockhash() -> object:
        return context

    async def not_expired(_context) -> bool:
        return False

    async def height_not_exceeded(
        _last_valid_height: int,
        *,
        commitment: str,
    ) -> bool:
        return False

    client._get_cached_blockhash_context = cached_blockhash
    client._blockhash_is_expired = not_expired
    client._current_block_height_exceeds = height_not_exceeded
    return client, ledger, signer


def _instruction() -> Instruction:
    return Instruction(Pubkey.new_unique(), b"trade", [])


def _unknown_error_type() -> type[RuntimeError]:
    return getattr(client_module, "TransactionSubmissionUnknown", RuntimeError)


@pytest.mark.asyncio
async def test_caller_compute_budget_instruction_is_rejected(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(client_module.ExecutionBlocked):
        await client.build_and_send_transaction(
            [set_compute_unit_limit(100_000), _instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="injected-compute-budget",
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_newly_signed_transaction_retries_are_disabled(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="max_retries must be 1"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            max_retries=2,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="unsafe-retry-count",
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_compute_unit_limit_cannot_exceed_protocol_cap(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="compute_unit_limit"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            compute_unit_limit=1_400_001,
            intent_id="oversized-compute-budget",
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_priority_fee_cannot_exceed_wire_encoding_cap(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="priority_fee"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            priority_fee=2**64,
            intent_id="oversized-priority-fee",
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.parametrize(
    "transport_error",
    [
        aiohttp.ClientConnectionError("lost"),
        SolanaRpcException(Exception("lost"), lambda value: value, None, object()),
        RuntimeError("client wrapper failed after dispatch"),
    ],
    ids=["aiohttp", "solana-rpc", "generic-wrapper"],
)
@pytest.mark.asyncio
async def test_transport_failure_is_unknown_and_raises_with_signature(
    tmp_path,
    transport_error: BaseException,
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock(side_effect=transport_error))
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="ambiguous-send",
        )

    signature = caught.value.signature
    outcome = ledger.get_outcome(signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN
    assert ledger.get_active_submission_record("ambiguous-send").state == "submitted"

    with pytest.raises(_unknown_error_type()):
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="ambiguous-send",
        )
    assert rpc.send_transaction.await_count == 1


@pytest.mark.asyncio
async def test_response_signature_mismatch_is_unknown_not_success(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(
            return_value=SimpleNamespace(value=Signature.default())
        )
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="mismatched-response",
        )

    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.parametrize(
    "malformed_response",
    [None, SimpleNamespace()],
    ids=["none", "missing-value"],
)
@pytest.mark.asyncio
async def test_malformed_send_response_is_signature_bearing_unknown(
    tmp_path, malformed_response
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock(return_value=malformed_response))
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id=f"malformed-response-{id(malformed_response)}",
        )

    assert caught.value.signature
    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_rpc_error_after_send_started_raises_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=RPCException("node unhealthy"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="rpc-error-send",
        )

    assert caught.value.signature
    assert (
        ledger.get_outcome(caught.value.signature).status is TransactionStatus.UNKNOWN
    )


@pytest.mark.asyncio
async def test_blockhash_rpc_error_does_not_prove_expiry(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=RPCException("Blockhash not found"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    async def not_proven(
        _signature: Signature,
        _last_valid_height: int,
        _commitment: str,
    ) -> bool:
        return False

    client._prove_transaction_expired = not_proven

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="unproven-blockhash-error",
            max_retries=1,
        )

    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_wire_rpc_error_raises_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_raw_transaction=AsyncMock(side_effect=RPCException("node unhealthy"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-rpc-error",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-rpc-error",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="prepared-rpc-error",
        )

    assert caught.value.signature == signature
    assert ledger.get_outcome(signature).status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_malformed_response_is_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock(return_value=None))
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-malformed-response",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-malformed-response",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="prepared-malformed-response",
        )

    assert caught.value.signature == signature
    outcome = ledger.get_outcome(signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_recovery_submits_the_exact_stored_wire_bytes(tmp_path) -> None:
    captured: list[bytes] = []

    async def send_raw_transaction(wire: bytes, _opts) -> object:
        captured.append(wire)
        return SimpleNamespace(value=prepared_signature)

    rpc = SimpleNamespace(send_raw_transaction=send_raw_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    prepared_wire = bytes(transaction)
    prepared_signature = transaction.signatures[0]
    message_hash = hashlib.sha256(bytes(message)).hexdigest()
    ledger.record_intent(
        "prepared-recovery",
        str(signer.pubkey()),
        10,
        5_000,
        message_hash,
    )
    ledger.record_submission(
        "prepared-recovery",
        str(prepared_signature),
        str(Hash.default()),
        100,
        wire_bytes=prepared_wire,
        state="prepared",
    )

    returned = await client.build_and_send_transaction(
        [instruction],
        signer,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="prepared-recovery",
    )

    assert returned == prepared_signature
    assert captured == [prepared_wire]
    assert ledger.get_active_submission_record("prepared-recovery").state == "submitted"


@pytest.mark.asyncio
async def test_prepared_recovery_revalidates_current_budget_policy(tmp_path) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    message = Message([_instruction()], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-over-current-budget",
        str(signer.pubkey()),
        100,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-over-current-budget",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )
    client.execution_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=50,
        max_total_fee_lamports=100_000,
        allow_skip_preflight=True,
    )

    with pytest.raises(TradeLimitExceeded, match="quote amount"):
        await client.recover_active_submission("prepared-over-current-budget")

    rpc.send_raw_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_recovery_rejects_wire_signer_mismatch(tmp_path) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    other_signer = Keypair()
    message = Message([_instruction()], other_signer.pubkey())
    transaction = Transaction([other_signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-wrong-wire-signer",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-wrong-wire-signer",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )

    with pytest.raises(ExecutionBlocked, match="does not match"):
        await client.recover_active_submission("prepared-wrong-wire-signer")

    rpc.send_raw_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_recovery_ignores_rebuilt_message_and_receipt_context(
    tmp_path,
) -> None:
    captured: list[bytes] = []

    async def send_raw_transaction(wire: bytes, _opts) -> object:
        captured.append(wire)
        return SimpleNamespace(value=prepared_signature)

    rpc = SimpleNamespace(send_raw_transaction=send_raw_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    original_instruction = _instruction()
    original_message = Message([original_instruction], signer.pubkey())
    transaction = Transaction([signer], original_message, Hash.default())
    prepared_wire = bytes(transaction)
    prepared_signature = transaction.signatures[0]
    original_destinations = tuple(str(Pubkey.new_unique()) for _ in range(3))
    rebuilt_destinations = tuple(str(Pubkey.new_unique()) for _ in range(3))
    ledger.record_intent(
        "changed-prepared-recovery",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(original_message)).hexdigest(),
    )
    ledger.record_submission(
        "changed-prepared-recovery",
        str(prepared_signature),
        str(Hash.default()),
        100,
        wire_bytes=prepared_wire,
        state="prepared",
        receipt_destinations=original_destinations,
    )

    returned = await client.build_and_send_transaction(
        [_instruction()],
        signer,
        priority_fee=1,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="changed-prepared-recovery",
        receipt_destinations=rebuilt_destinations,
    )

    assert returned == prepared_signature
    assert captured == [prepared_wire]
    assert await client.get_submission_receipt_destinations(
        prepared_signature
    ) == tuple(Pubkey.from_string(item) for item in original_destinations)


@pytest.mark.asyncio
async def test_expired_prepared_wire_remains_unknown_without_terminal_evidence(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_raw_transaction=AsyncMock(),
        send_transaction=AsyncMock(),
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    stale_blockhash = Hash.new_unique()
    stale_transaction = Transaction([signer], message, stale_blockhash)
    stale_signature = stale_transaction.signatures[0]
    ledger.record_intent(
        "stale-prepared",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "stale-prepared",
        str(stale_signature),
        str(stale_blockhash),
        10,
        wire_bytes=bytes(stale_transaction),
        state="prepared",
    )

    async def height_exceeded(
        _last_valid_height: int,
        *,
        commitment: str,
    ) -> bool:
        assert commitment == "finalized"
        return True

    client._current_block_height_exceeds = height_exceeded

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="stale-prepared",
        )

    assert caught.value.signature == str(stale_signature)
    assert ledger.get_active_submission_record("stale-prepared") is not None
    rpc.send_raw_transaction.assert_not_awaited()
    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_before_send_releases_only_prepared_reservation(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    client._rate_limiter.acquire = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-before-send",
        )

    assert ledger.get_active_submission_record("cancel-before-send") is None


@pytest.mark.asyncio
async def test_cancellation_during_send_keeps_submitted_reservation(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=asyncio.CancelledError())
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(asyncio.CancelledError):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-during-send",
        )

    record = ledger.get_active_submission_record("cancel-during-send")
    assert record is not None
    assert record.state == "submitted"


@pytest.mark.asyncio
async def test_cancellation_during_post_send_ledger_mark_waits_for_transition(
    tmp_path,
) -> None:
    async def send_transaction(transaction: Transaction, _opts) -> object:
        return SimpleNamespace(value=transaction.signatures[0])

    rpc = SimpleNamespace(send_transaction=send_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    mark_started = Event()
    release_mark = Event()
    original_mark = ledger.mark_submission_submitted

    def blocking_mark(signature: str) -> None:
        mark_started.set()
        release_mark.wait()
        original_mark(signature)

    ledger.mark_submission_submitted = blocking_mark
    submission = asyncio.create_task(
        client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-during-ledger-mark",
        )
    )
    await asyncio.wait_for(asyncio.to_thread(mark_started.wait), timeout=1)

    submission.cancel()
    await asyncio.sleep(0)
    assert not submission.done()
    release_mark.set()

    with pytest.raises(asyncio.CancelledError):
        await submission

    record = ledger.get_active_submission_record("cancel-during-ledger-mark")
    assert record is not None
    assert record.state == "submitted"
    assert ledger.get_outcome(record.signature).status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_confirmation_does_not_promote_lower_commitment_status_to_success() -> (
    None
):
    lower_status = SimpleNamespace(
        err=None,
        slot=7,
        confirmation_status=0,
    )
    rpc = SimpleNamespace(
        confirm_transaction=AsyncMock(
            return_value=SimpleNamespace(value=[lower_status])
        )
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    async def no_transaction(_signature, *, commitment: str = "confirmed") -> None:
        return None

    async def not_expired(
        _signature: Signature,
        _last_valid_height: int,
        _requested_commitment: str,
    ) -> bool:
        return False

    client._get_transaction_result = no_transaction
    client._prove_transaction_expired = not_expired

    outcome = await client.confirm_transaction_outcome(
        Signature.default(),
        commitment="confirmed",
        timeout_seconds=1,
        last_valid_block_height=10,
    )

    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_unknown_poll_reuses_stored_terminal_outcome(tmp_path) -> None:
    rpc = SimpleNamespace(
        confirm_transaction=AsyncMock(return_value=SimpleNamespace(value=[]))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    transaction = Transaction(
        [signer],
        Message([_instruction()], signer.pubkey()),
        Hash.default(),
    )
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "confirmed-before-restart",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(transaction.message)).hexdigest(),
    )
    ledger.record_submission(
        "confirmed-before-restart",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="submitted",
    )
    stored = TransactionOutcome(TransactionStatus.SUCCESS, signature, slot=7)
    ledger.record_outcome(stored)
    client._get_transaction_result = AsyncMock(return_value=None)
    client._prove_transaction_expired = AsyncMock(return_value=False)

    outcome = await client.confirm_transaction_outcome(
        signature,
        timeout_seconds=1,
    )

    assert outcome == stored
    assert ledger.get_outcome(signature) == stored


@pytest.mark.asyncio
async def test_processed_confirmation_is_rejected_as_nonterminal() -> None:
    client = SolanaClient("http://offline.invalid")

    with pytest.raises(ValueError, match="confirmed or finalized"):
        await client.confirm_transaction_outcome(
            Signature.default(),
            commitment="processed",
        )


@pytest.mark.asyncio
async def test_transaction_result_requires_explicit_meta_error_field() -> None:
    client = SolanaClient("http://offline.invalid")

    async def partial_response(_body, **_kwargs) -> dict:
        return {"result": {"slot": 7, "meta": {}}}

    client.post_rpc = partial_response

    assert await client._get_transaction_result(Signature.default()) is None
    assert not await client.verify_transaction_succeeded(Signature.default())


@pytest.mark.asyncio
async def test_prunable_history_absence_does_not_prove_expiry() -> None:
    client = SolanaClient("http://offline.invalid")
    height_commitments: list[str] = []
    history_commitments: list[str] = []

    async def height(_last_valid_height: int, *, commitment: str) -> bool:
        height_commitments.append(commitment)
        return True

    async def status(_signature: Signature) -> tuple[bool, None]:
        return True, None

    async def history(_signature: str, commitment: str) -> tuple[bool, bool]:
        history_commitments.append(commitment)
        return True, False

    client._current_block_height_exceeds = height
    client._read_signature_status = status
    client._read_transaction_presence = history

    assert not await client._prove_transaction_expired(
        Signature.default(), 10, "confirmed"
    )
    assert height_commitments == ["finalized", "finalized"]
    assert history_commitments == ["finalized", "finalized"]


@pytest.mark.asyncio
async def test_cached_blockhash_expiry_uses_finalized_height() -> None:
    rpc = SimpleNamespace(
        get_block_height=AsyncMock(return_value=SimpleNamespace(value=11))
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    assert await client._blockhash_is_expired(
        client_module._BlockhashContext(Hash.default(), 10)
    )
    rpc.get_block_height.assert_awaited_once_with(commitment="finalized")


@pytest.mark.asyncio
async def test_read_rpc_calls_retry_transport_failures_within_bound() -> None:
    rpc = SimpleNamespace(
        get_account_info=AsyncMock(
            side_effect=[
                aiohttp.ClientConnectionError("temporary"),
                SimpleNamespace(value={"data": "ok"}),
            ]
        )
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    result = await client.get_account_info(Pubkey.new_unique())

    assert result == {"data": "ok"}
    assert rpc.get_account_info.await_count == 2


@pytest.mark.asyncio
async def test_buy_receipt_attributes_token_and_quote_deltas_to_trade_accounts() -> (
    None
):
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    buyer_token = Pubkey.new_unique()
    buyer_token_two = Pubkey.new_unique()
    buyer_quote = Pubkey.new_unique()
    quote_vault = Pubkey.new_unique()
    unrelated = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 2,
                    "mint": str(mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 3,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 4,
                    "mint": str(quote_mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 5,
                    "mint": str(quote_mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "100"},
                },
                {
                    "accountIndex": 6,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                },
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "25"},
                },
                {
                    "accountIndex": 2,
                    "mint": str(mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "999"},
                },
                {
                    "accountIndex": 3,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "40"},
                },
                {
                    "accountIndex": 4,
                    "mint": str(quote_mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "888"},
                },
                {
                    "accountIndex": 5,
                    "mint": str(quote_mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "60"},
                },
                {
                    "accountIndex": 6,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "5"},
                },
            ],
            "preBalances": [0, 0, 0, 0, 0, 0, 0],
            "postBalances": [0, 0, 0, 0, 0, 0, 0],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(buyer_token),
                    str(unrelated),
                    str(quote_vault),
                    str(unrelated),
                    str(buyer_quote),
                    str(buyer_token_two),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        quote_vault,
        quote_mint,
    )

    assert tokens_received == 30
    assert quote_spent == 40


def test_token_delta_attribution_rejects_duplicate_account_indexes() -> None:
    owner = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    meta = {
        "preTokenBalances": [],
        "postTokenBalances": [
            {
                "accountIndex": 1,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": "10"},
            },
            {
                "accountIndex": 1,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": "20"},
            },
        ],
    }

    assert (
        SolanaClient._extract_positive_token_diff(
            meta,
            mint,
            owner=owner,
            account_count=2,
        )
        is None
    )


@pytest.mark.asyncio
async def test_quote_receipt_rejects_duplicate_destination_account_keys() -> None:
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    quote_vault = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "40"},
                }
            ],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(quote_vault),
                    str(quote_vault),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result
    _tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        quote_vault,
        quote_mint,
    )

    assert quote_spent is None


@pytest.mark.asyncio
async def test_buy_sol_receipt_sums_primary_and_fee_destinations() -> None:
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    buyer_token = Pubkey.new_unique()
    primary_destination = Pubkey.new_unique()
    fee_destination = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "5"},
                }
            ],
            "preBalances": [10_000, 100, 200, 300],
            "postBalances": [9_000, 100, 270, 325],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(buyer_token),
                    str(primary_destination),
                    str(fee_destination),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        primary_destination,
        WSOL_MINT,
        quote_destinations=[fee_destination],
    )

    assert tokens_received == 5
    assert quote_spent == 95


@pytest.mark.asyncio
async def test_sell_sol_receipt_adds_transaction_fee_to_owner_delta() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "fee": 5,
            "preBalances": [10_000, 200],
            "postBalances": [10_100, 195],
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
        "transaction": {
            "message": {"accountKeys": [str(owner), str(Pubkey.new_unique())]}
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(Signature.default(), WSOL_MINT, owner)
        == 105
    )


@pytest.mark.asyncio
async def test_sell_spl_quote_receipt_uses_owner_token_delta() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    quote_account = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preBalances": [0, 0],
            "postBalances": [0, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "50"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "75"},
                }
            ],
        },
        "transaction": {"message": {"accountKeys": [str(owner), str(quote_account)]}},
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(
            Signature.default(), quote_mint, owner
        )
        == 25
    )


def test_receipt_amount_parsing_rejects_noncanonical_numeric_values() -> None:
    owner = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    malformed = {
        "preTokenBalances": [],
        "postTokenBalances": [
            {
                "accountIndex": 0,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": 10},
            }
        ],
    }

    assert (
        SolanaClient._extract_positive_token_diff(
            malformed,
            mint,
            owner=owner,
            account_count=1,
        )
        is None
    )
    assert (
        SolanaClient._validated_lamport_balances(
            {"preBalances": [0], "postBalances": [True]}, 1
        )
        is None
    )


@pytest.mark.asyncio
async def test_transaction_token_balance_rejects_malformed_amount() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    result = {
        "slot": 1,
        "meta": {"err": None, "postTokenBalances": []},
        "transaction": {
            "signatures": [str(Signature.default())],
            "message": {"accountKeys": [str(owner)]},
        },
    }
    result["meta"]["postTokenBalances"] = [
        {
            "accountIndex": 0,
            "mint": str(mint),
            "owner": str(owner),
            "uiTokenAmount": {"amount": 10},
        }
    ]

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_transaction_token_balance(Signature.default(), owner, mint)
        is None
    )


def test_canonical_transaction_requires_requested_primary_signature() -> None:
    primary = Keypair().sign_message(b"primary")
    secondary = Keypair().sign_message(b"secondary")
    result = {
        "slot": 1,
        "meta": {"err": None},
        "transaction": {
            "signatures": [str(primary), str(secondary)],
            "message": {"accountKeys": [str(Pubkey.new_unique())]},
        },
    }

    assert SolanaClient._is_canonical_transaction_result(result, str(primary))
    assert not SolanaClient._is_canonical_transaction_result(result, str(secondary))

    result["transaction"]["signatures"][1] = "not-a-signature"
    assert not SolanaClient._is_canonical_transaction_result(result, str(primary))

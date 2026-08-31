from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.pubkey import Pubkey

from core.client import TransactionStatus, TransactionSubmissionUnknown
from core.execution_policy import ExecutionBlocked, ExecutionPolicy
from core.pubkeys import SystemAddresses
from interfaces.core import Platform, TokenInfo
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from trading.universal_trader import UniversalTrader


def _token(platform: Platform) -> TokenInfo:
    mint = Pubkey.new_unique()
    pool = mint if platform is Platform.LETS_BONK else Pubkey.new_unique()
    return TokenInfo(
        name="Token",
        symbol="TOK",
        uri="",
        mint=mint,
        platform=platform,
        bonding_curve=pool if platform is Platform.PUMP_FUN else None,
        pool_state=pool if platform is Platform.LETS_BONK else None,
        quote_mint=SystemAddresses.WSOL_MINT,
        quote_token_program_id=SystemAddresses.TOKEN_PROGRAM,
        token_program_id=SystemAddresses.TOKEN_PROGRAM,
        base_decimals=6,
        quote_decimals=9,
    )


def test_recovery_token_round_trip_preserves_creation_timestamp() -> None:
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = 123.5

    payload = UniversalTrader._token_to_dict(token)
    recovered = UniversalTrader._token_from_dict(payload)

    assert payload["creation_timestamp"] == 123.5
    assert recovered.creation_timestamp == 123.5


@pytest.mark.parametrize(
    "malformed_timestamp",
    ["123.5", True, 123, float("nan"), float("inf")],
)
def test_recovery_token_rejects_malformed_creation_timestamp(
    malformed_timestamp: object,
) -> None:
    payload = UniversalTrader._token_to_dict(_token(Platform.LETS_BONK))
    payload["creation_timestamp"] = malformed_timestamp

    with pytest.raises(ValueError, match="creation_timestamp"):
        UniversalTrader._token_from_dict(payload)


def test_recovery_token_serialization_rejects_malformed_creation_timestamp() -> None:
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = "123.5"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="creation_timestamp"):
        UniversalTrader._token_to_dict(token)


def test_malformed_journal_does_not_partially_activate_positions(tmp_path) -> None:
    trader = object.__new__(UniversalTrader)
    wallet = Pubkey.new_unique()
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="buy-signature",
    )
    pending_payload = UniversalTrader._token_to_dict(_token(Platform.LETS_BONK))
    pending_payload["creation_timestamp"] = "not-a-timestamp"
    journal_path = tmp_path / "positions.json"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.LETS_BONK.value,
                "positions": {
                    token_key: {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": position.to_dict(),
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [pending_payload],
            }
        ),
        encoding="utf-8",
    )
    trader.wallet = SimpleNamespace(pubkey=wallet)
    trader.platform = Platform.LETS_BONK
    trader._journal_path = journal_path
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader.traded_mints = set()
    trader.traded_token_programs = {}

    with pytest.raises(RuntimeError, match="Cannot safely load recovery journal"):
        trader._load_recovery_journal()

    assert trader._active_positions == {}
    assert trader._unresolved_buys == {}
    assert trader._pending_recovery_tokens == []
    assert trader._reserved_mints == set()
    assert trader.traded_mints == set()
    assert trader.traded_token_programs == {}


def test_recovery_journal_rejects_cross_platform_active_position(
    tmp_path,
) -> None:
    trader = object.__new__(UniversalTrader)
    wallet = Pubkey.new_unique()
    token = _token(Platform.PUMP_FUN)
    token_key = str(token.mint)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="buy-signature",
    )
    journal_path = tmp_path / "positions.json"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.LETS_BONK.value,
                "positions": {
                    token_key: {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": position.to_dict(),
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [],
            }
        ),
        encoding="utf-8",
    )
    trader.wallet = SimpleNamespace(pubkey=wallet)
    trader.platform = Platform.LETS_BONK
    trader._journal_path = journal_path
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader.traded_mints = set()
    trader.traded_token_programs = {}

    with pytest.raises(RuntimeError, match="Cannot safely load recovery journal"):
        trader._load_recovery_journal()

    assert trader._active_positions == {}
    assert trader._reserved_mints == set()


@pytest.mark.asyncio
async def test_restart_joins_pending_buy_and_sell_intents_before_rebuild() -> None:
    trader = object.__new__(UniversalTrader)
    buy_token = _token(Platform.LETS_BONK)
    sell_token = _token(Platform.LETS_BONK)
    sell_position = Position.create_from_buy_result(
        mint=sell_token.mint,
        symbol=sell_token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="confirmed-buy",
    )
    sell_position.mark_exit_intent("sell-intent", ExitReason.STOP_LOSS, 0.8)
    buy_intent = UniversalTrader._buy_intent_id(buy_token)
    records = {
        buy_intent: SimpleNamespace(signature="buy-signature"),
        "sell-intent": SimpleNamespace(signature="sell-signature"),
    }
    trader.transaction_ledger = SimpleNamespace(
        get_active_submission_record=lambda intent_id: records.get(intent_id)
    )
    trader._pending_recovery_tokens = [buy_token]
    trader._unresolved_buys = {}
    trader._active_positions = {str(sell_token.mint): (sell_token, sell_position)}
    trader._reserved_mints = set()
    journal_writes: list[bool] = []
    trader._write_recovery_journal = lambda: journal_writes.append(True)

    trader._hydrate_submission_recovery()

    assert trader._pending_recovery_tokens == []
    assert trader._unresolved_buys[str(buy_token.mint)]["signature"] == "buy-signature"
    assert sell_position.pending_exit_signature == "sell-signature"
    assert journal_writes == [True]

    trader.solana_client = SimpleNamespace(
        recover_active_submission=AsyncMock(
            side_effect=[
                TransactionSubmissionUnknown("buy-signature", "response lost"),
                "sell-signature",
            ]
        )
    )
    await trader._resume_ledger_bound_submissions()

    assert [
        call.args[0]
        for call in trader.solana_client.recover_active_submission.await_args_list
    ] == [buy_intent, "sell-intent"]


class _AddressProviderWithoutCreatorVault:
    derive_creator_vault = None

    def derive_pool_address(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return mint

    def derive_base_vault(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return Pubkey.find_program_address(
            [b"base-vault", bytes(mint)], Pubkey.default()
        )[0]

    def derive_quote_vault(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return Pubkey.find_program_address(
            [b"quote-vault", bytes(mint)], Pubkey.default()
        )[0]


class _InstructionBuilder:
    async def build_sell_instruction(self, *args, **kwargs) -> list:
        return []

    def get_required_accounts_for_sell(self, *args, **kwargs) -> list:
        return []

    def get_sell_compute_unit_limit(self, override: int | None) -> int | None:
        return override

    async def build_buy_instruction(self, *args, **kwargs) -> list:
        return []

    def get_required_accounts_for_buy(self, *args, **kwargs) -> list:
        return []

    def get_buy_compute_unit_limit(self, override: int | None) -> int | None:
        return override


class _CurveManager:
    async def get_pool_state_and_token_program(
        self, pool: Pubkey, mint: Pubkey, commitment: str | None = None
    ) -> tuple[dict, Pubkey]:
        provider = _AddressProviderWithoutCreatorVault()
        quote_mint = SystemAddresses.WSOL_MINT
        return (
            {
                "complete": False,
                "is_tradeable": True,
                "status_name": "funding",
                "quote_mint": quote_mint,
                "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
                "base_mint": mint,
                "base_token_program": SystemAddresses.TOKEN_PROGRAM,
                "pool_address": pool,
                "base_vault": provider.derive_base_vault(mint, quote_mint),
                "quote_vault": provider.derive_quote_vault(mint, quote_mint),
                "global_config": Pubkey.new_unique(),
                "platform_config": Pubkey.new_unique(),
                "creator": Pubkey.new_unique(),
                "base_decimals": 6,
                "quote_decimals": 9,
            },
            SystemAddresses.TOKEN_PROGRAM,
        )

    async def calculate_sell_amount_out(self, pool: Pubkey, amount: int) -> int:
        return 1_000_000_000


class _Client:
    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        return "sell-signature"

    async def confirm_transaction_outcome(self, signature: str) -> SimpleNamespace:
        return SimpleNamespace(
            status=TransactionStatus.SUCCESS,
            slot=123,
            error=None,
        )

    async def get_sell_transaction_details(
        self, signature: str, quote_mint: Pubkey, owner: Pubkey
    ) -> int:
        return 1_000_000_000


class _SuccessfulBuyClient(_Client):
    def __init__(self) -> None:
        self.submission_kwargs: dict = {}

    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        self.submission_kwargs = kwargs
        return "buy-signature"

    async def get_buy_transaction_details(
        self,
        signature: str,
        mint: Pubkey,
        destination: Pubkey,
        **kwargs,
    ) -> tuple[int, int]:
        return 2_000_000, 1_000_000


class _UnknownSubmissionClient(_Client):
    async def get_account_info(self, address: Pubkey) -> None:
        raise ValueError("account does not exist")

    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        raise TransactionSubmissionUnknown(
            "deterministic-signature", "RPC response lost"
        )


class _PriorityFees:
    async def calculate_priority_fee(self, accounts: list) -> int:
        return 0


def test_expected_wallet_is_validated_before_client_setup(monkeypatch) -> None:
    actual_wallet = Pubkey.new_unique()
    expected_wallet = Pubkey.new_unique()
    client_creations: list[bool] = []
    monkeypatch.setattr(
        "trading.universal_trader.Wallet",
        lambda private_key: SimpleNamespace(pubkey=actual_wallet),
    )

    def create_client(*args, **kwargs):
        client_creations.append(True)
        raise AssertionError("client setup must not run for a mismatched wallet")

    monkeypatch.setattr("trading.universal_trader.SolanaClient", create_client)

    with pytest.raises(ExecutionBlocked, match="does not match signer wallet"):
        UniversalTrader(
            rpc_endpoint="offline",
            wss_endpoint="offline",
            private_key="unused",
            buy_amount=0.1,
            buy_slippage=0.1,
            sell_slippage=0.1,
            execution_policy=ExecutionPolicy(expected_wallet=str(expected_wallet)),
        )

    assert client_creations == []


@pytest.mark.asyncio
async def test_letsbonk_sell_does_not_require_creator_vault_capability(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    provider = _AddressProviderWithoutCreatorVault()
    implementations = SimpleNamespace(
        address_provider=provider,
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    seller = PlatformAwareSeller(
        _Client(),
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.success is True
    assert token.creator is not None
    assert token.creator_vault is None


@pytest.mark.asyncio
async def test_pumpfun_sell_fails_clearly_without_required_creator_vault_capability(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    monkeypatch.setattr(
        "trading.platform_aware._read_pool_state_with_retry",
        AsyncMock(
            return_value=(
                {
                    "creator": str(Pubkey.new_unique()),
                    "complete": False,
                    "quote_mint": SystemAddresses.WSOL_MINT,
                    "base_decimals": 6,
                    "quote_decimals": 9,
                },
                SystemAddresses.TOKEN_PROGRAM,
            )
        ),
    )
    seller = PlatformAwareSeller(
        _Client(),
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.success is False
    assert "requires creator-vault derivation" in result.error_message


@pytest.mark.asyncio
async def test_unknown_sell_submission_preserves_signature_and_raw_amount(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    seller = PlatformAwareSeller(
        _UnknownSubmissionClient(),
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.unresolved is True
    assert result.tx_signature == "deterministic-signature"
    assert result.amount_raw == 1_000_000


@pytest.mark.asyncio
async def test_unknown_buy_submission_preserves_signature_and_recovery_accounting(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    token.state_from_event = True
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
    )
    buyer = PlatformAwareBuyer(
        _UnknownSubmissionClient(),
        wallet,
        _PriorityFees(),
        amount=0.1,
        slippage=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )

    result = await buyer.execute(token)

    assert result.unresolved is True
    assert result.tx_signature == "deterministic-signature"
    assert result.amount_raw == 2_000_000
    assert result.quote_amount_raw == 110_000_000
    assert result.account_balance_baseline_raw == 0


@pytest.mark.asyncio
async def test_pumpfun_usdc_buy_has_no_native_receipt_destinations(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    token.quote_mint = SystemAddresses.USDC_MINT
    token.quote_decimals = 6
    token.state_from_event = True
    token.curve_complete = False
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _SuccessfulBuyClient()
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
    )
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        _PriorityFees(),
        amount=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
        quote_amounts={SystemAddresses.USDC_MINT: 1.0},
    )

    result = await buyer.execute(token)

    assert result.success is True
    assert client.submission_kwargs["receipt_destinations"] is None


def _lifecycle_trader(*, yolo_mode: bool) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader.platform = Platform.LETS_BONK
    trader.exit_strategy = "manual"
    trader.yolo_mode = yolo_mode
    trader.match_string = None
    trader.bro_address = None
    trader.token_wait_timeout = 1
    trader.token_queue = asyncio.Queue()
    trader._queue_lock = asyncio.Lock()
    trader._shutdown_event = asyncio.Event()
    trader._position_tasks = set()
    trader._position_monitor_tasks = set()
    trader._fatal_monitor_errors = asyncio.Queue()
    trader._active_positions = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader._unresolved_buys = {}
    trader._inflight_tokens = {}
    trader.processed_tokens = set()
    trader.token_timestamps = {}
    trader.solana_client = SimpleNamespace(get_health=AsyncMock(return_value="ok"))
    trader._write_recovery_journal = lambda: None

    async def process_queue() -> None:
        await asyncio.Event().wait()

    async def reconcile() -> None:
        return None

    trader._process_token_queue = process_queue
    trader._reconcile_unresolved_buys = reconcile
    return trader


@pytest.mark.asyncio
async def test_start_propagates_fatal_reconciliation_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def reconcile() -> None:
        raise RuntimeError("reconciliation failed")

    async def listen(*args, **kwargs) -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader._reconcile_unresolved_buys = reconcile
    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_reconciliation_failure_interrupts_stalled_queue_drain() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    events: list[str] = []

    async def reconcile() -> None:
        raise RuntimeError("reconciliation failed during drain")

    async def cleanup() -> None:
        events.append("cleanup")

    trader._reconcile_unresolved_buys = reconcile
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="reconciliation failed during drain"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_single_token_listener_failure_propagates_without_waiting_for_timeout() -> (
    None
):
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_wait_timeout = 60

    async def listen(*args, **kwargs) -> None:
        raise RuntimeError("single listener failed")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    with pytest.raises(RuntimeError, match="single listener failed"):
        await asyncio.wait_for(trader._wait_for_token(), timeout=0.1)


@pytest.mark.asyncio
async def test_recovered_token_keeps_original_age_when_queued() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    trader.max_token_age = 10
    trader._handle_token = AsyncMock()
    trader._process_token_queue = UniversalTrader._process_token_queue.__get__(trader)
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = monotonic() - 100

    queued = await trader._queue_token(token, recovered=True)
    assert trader.token_timestamps[str(token.mint)] == token.creation_timestamp
    processor_task = asyncio.create_task(trader._process_token_queue())
    await asyncio.wait_for(trader.token_queue.join(), timeout=0.1)
    processor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await processor_task

    assert queued is True
    assert trader.token_timestamps.get(str(token.mint)) is None
    trader._handle_token.assert_not_awaited()
    assert str(token.mint) in trader.processed_tokens
    assert str(token.mint) not in trader._reserved_mints


@pytest.mark.asyncio
async def test_single_token_listener_reserves_only_first_matching_token() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    first_token = _token(Platform.LETS_BONK)
    second_token = _token(Platform.LETS_BONK)

    async def listen(callback, *args, **kwargs) -> None:
        await callback(first_token)
        await callback(second_token)
        await asyncio.Event().wait()

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    found_token = await trader._wait_for_token()

    assert found_token is first_token
    assert trader._reserved_mints == {str(first_token.mint)}
    assert set(trader.token_timestamps) == {str(first_token.mint)}


@pytest.mark.asyncio
async def test_single_token_listener_normal_return_is_lifecycle_failure() -> None:
    trader = _lifecycle_trader(yolo_mode=False)

    async def listen(*args, **kwargs) -> None:
        return None

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    with pytest.raises(
        RuntimeError,
        match="Token listener stopped before detecting a token",
    ):
        await trader._wait_for_token()


@pytest.mark.asyncio
async def test_unresolved_buy_reconciliation_propagates_fatal_error() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buys = {
        str(token.mint): {
            "token": token,
            "signature": "buy-signature",
            "baseline_raw": 0,
        }
    }
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            side_effect=RuntimeError("confirmation failed")
        )
    )
    trader.price_check_interval = 1

    with pytest.raises(RuntimeError, match="confirmation failed"):
        await asyncio.wait_for(
            trader._reconcile_unresolved_buys(),
            timeout=0.1,
        )


@pytest.mark.asyncio
async def test_unresolved_buy_recovery_uses_exact_durable_receipt_destinations() -> (
    None
):
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    primary = Pubkey.new_unique()
    protocol_fee = Pubkey.new_unique()
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buys = {
        token_key: {
            "token": token,
            "signature": "buy-signature",
            "baseline_raw": 0,
        }
    }

    async def read_receipt(*args, **kwargs) -> tuple[int, int]:
        trader._shutdown_event.set()
        return 2_000_000, 1_050_000_000

    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.SUCCESS,
                slot=123,
            )
        ),
        get_submission_receipt_destinations=AsyncMock(
            return_value=(primary, protocol_fee)
        ),
        get_buy_transaction_details=AsyncMock(side_effect=read_receipt),
    )
    trader._handle_successful_buy = AsyncMock()
    trader.processed_tokens = set()
    trader._reserved_mints = {token_key}
    trader.price_check_interval = 1

    await trader._reconcile_unresolved_buys()

    trader.solana_client.get_buy_transaction_details.assert_awaited_once_with(
        "buy-signature",
        token.mint,
        primary,
        quote_mint=SystemAddresses.WSOL_MINT,
        quote_destinations=[protocol_fee],
    )
    recovered_result = trader._handle_successful_buy.await_args.args[1]
    assert recovered_result.quote_amount_raw == 1_050_000_000


@pytest.mark.asyncio
async def test_cleanup_attempts_all_resources_and_raises_first_failure(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    events: list[str] = []

    async def blocked_background_task() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("background cancelled")

    background_task = asyncio.create_task(blocked_background_task())
    await asyncio.sleep(0)
    trader._pending_recovery_tokens = []
    trader._inflight_tokens = {}
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader.token_queue = asyncio.Queue()
    trader._write_recovery_journal = lambda: events.append("journal")
    trader._position_tasks = {background_task}
    trader.traded_mints = {token.mint}
    trader.traded_token_programs = {str(token.mint): token.token_program_id}
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.priority_fee_manager = object()
    trader.cleanup_mode = object()
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False

    async def cleanup_post_session(*args, **kwargs) -> None:
        events.append("post-session cleanup")
        raise RuntimeError("post-session cleanup failed")

    async def close_client() -> None:
        events.append("client close")
        raise RuntimeError("client close failed")

    def close_ledger() -> None:
        events.append("ledger close")

    trader.solana_client = SimpleNamespace(close=close_client)
    trader.transaction_ledger = SimpleNamespace(close=close_ledger)
    trader._journal_lock_handle = None
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_post_session",
        cleanup_post_session,
    )

    with pytest.raises(RuntimeError, match="post-session cleanup failed"):
        await asyncio.wait_for(trader._cleanup_resources(), timeout=0.1)

    assert events == [
        "journal",
        "background cancelled",
        "post-session cleanup",
        "client close",
        "ledger close",
    ]


@pytest.mark.asyncio
async def test_start_propagates_fatal_listener_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        raise RuntimeError("listener failed")

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="listener failed"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_yolo_listener_normal_return_is_lifecycle_failure() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        return None

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="Token listener stopped unexpectedly"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_start_cancellation_still_cleans_up_and_propagates() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    listener_started = asyncio.Event()
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        listener_started.set()
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup
    start_task = asyncio.create_task(trader.start())
    await listener_started.wait()

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_yolo_start_propagates_fatal_monitor_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    await trader._fatal_monitor_errors.put(RuntimeError("monitor crashed"))
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="monitor crashed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_single_start_propagates_monitor_error_while_waiting_for_token() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    await trader._fatal_monitor_errors.put(RuntimeError("monitor crashed"))
    events: list[str] = []

    async def wait_for_token() -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="monitor crashed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_monitor_error_winning_after_listener_is_still_propagated() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        return None

    async def drain(
        processor_task: asyncio.Task,
        lifecycle_failure_task: asyncio.Task,
    ) -> None:
        trader._fatal_monitor_errors.put_nowait(RuntimeError("late monitor crash"))
        await asyncio.sleep(0)

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._await_queue_drain = drain
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="late monitor crash"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_start_finalizes_reservation_and_propagates_fatal_trade_error() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    events: list[tuple[str, bool] | str] = []

    async def wait_for_token() -> TokenInfo:
        trader._reserved_mints.add(str(token.mint))
        return token

    async def handle_token(token_info: TokenInfo) -> bool:
        raise RuntimeError("trade failed")

    def finish_token(token_info: TokenInfo, handled: bool) -> None:
        events.append(("finish", handled))

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = finish_token
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="trade failed"):
        await trader.start()

    assert events == [("finish", False), "cleanup"]


@pytest.mark.asyncio
async def test_handle_token_propagates_fatal_buy_exception_and_keeps_recovery() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader.platform = Platform.LETS_BONK
    trader.allowed_quote_mints = None
    trader.quote_amounts = {SystemAddresses.WSOL_MINT: 1.0}
    trader.extreme_fast_mode = True
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader.buyer = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("submission crashed"))
    )

    with pytest.raises(RuntimeError, match="submission crashed"):
        await trader._handle_token(token)

    assert trader._pending_recovery_tokens == [token]


@pytest.mark.asyncio
async def test_queue_processor_propagates_unexpected_trading_exception() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader.token_queue = asyncio.Queue()
    trader.token_queue.put_nowait(token)
    trader._inflight_tokens = {}
    trader.token_timestamps = {}
    trader.max_token_age = 60
    trader._handle_token = AsyncMock(side_effect=RuntimeError("worker crashed"))
    trader._finish_token_reservation = lambda token_info, handled: None

    with pytest.raises(RuntimeError, match="worker crashed"):
        await asyncio.wait_for(trader._process_token_queue(), timeout=0.1)

    assert trader.token_queue.empty()
    await asyncio.wait_for(trader.token_queue.join(), timeout=0.1)


@pytest.mark.asyncio
async def test_start_notices_queue_processor_failure_before_queue_drain() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    trader.max_token_age = 60
    trader._process_token_queue = UniversalTrader._process_token_queue.__get__(trader)
    trader._handle_token = AsyncMock(side_effect=RuntimeError("queue trade failed"))
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._wait_for_token = AsyncMock(
        side_effect=AssertionError("must not wait after processor failure")
    )
    cleanup_calls: list[bool] = []

    async def cleanup() -> None:
        cleanup_calls.append(True)

    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="queue trade failed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert cleanup_calls == [True]


@pytest.mark.asyncio
async def test_single_token_start_awaits_automatic_monitor_before_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    events: list[str] = []

    async def wait_for_token() -> TokenInfo:
        return token

    async def monitor() -> None:
        await asyncio.sleep(0.01)
        events.append("monitor")

    async def handle_token(token_info: TokenInfo) -> bool:
        task = asyncio.create_task(monitor())
        trader._position_tasks.add(task)
        trader._position_monitor_tasks.add(task)
        return True

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._cleanup_resources = cleanup

    await trader.start()

    assert events == ["monitor", "cleanup"]


def test_failed_pending_recovery_keeps_reservation() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    trader._write_recovery_journal = lambda: None

    trader._finish_token_reservation(token, handled=False)

    assert trader._pending_recovery_tokens == [token]
    assert token_key in trader._reserved_mints
    assert token_key not in trader.processed_tokens


def test_handled_recovery_clears_pending_state_and_reservation() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    trader._write_recovery_journal = lambda: None

    trader._finish_token_reservation(token, handled=True)

    assert trader._pending_recovery_tokens == []
    assert token_key not in trader._reserved_mints
    assert token_key in trader.processed_tokens


@pytest.mark.parametrize("resolved_state", ["active", "unresolved"])
def test_recovery_token_is_cleared_when_durable_state_owns_reservation(
    resolved_state: str,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = (
        {token_key: (token, object())} if resolved_state == "active" else {}
    )
    trader._unresolved_buys = (
        {token_key: {"token": token, "signature": "buy-signature"}}
        if resolved_state == "unresolved"
        else {}
    )
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    journal_writes: list[bool] = []
    trader._write_recovery_journal = lambda: journal_writes.append(True)

    trader._finish_token_reservation(token, handled=False)

    assert trader._pending_recovery_tokens == []
    assert token_key in trader._reserved_mints
    assert token_key not in trader.processed_tokens
    assert journal_writes == [True]


@pytest.mark.asyncio
async def test_position_monitor_propagates_unexpected_error() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            calculate_price=AsyncMock(side_effect=RuntimeError("price monitor crashed"))
        )
    )
    trader._get_pool_address = lambda token_info: token_info.pool_state

    with pytest.raises(RuntimeError, match="price monitor crashed"):
        await asyncio.wait_for(
            trader._monitor_position_until_exit(token, position),
            timeout=0.1,
        )


@pytest.mark.asyncio
async def test_confirmed_pending_sell_stages_cleanup_before_position_removal(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        position_id="buy-signature",
        account_balance_baseline_raw=0,
    )
    position.mark_exit_intent("sell:buy-signature:1", ExitReason.TAKE_PROFIT, 2.0)
    position.mark_exit_pending("pending-sell", ExitReason.TAKE_PROFIT)
    events: list[str] = []

    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    calculate_price = AsyncMock(
        side_effect=AssertionError("price must not gate pending sell reconciliation")
    )
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_price=calculate_price)
    )
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.SUCCESS,
                slot=123,
                error=None,
            )
        )
    )
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = "after_sell"
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._log_trade = lambda *args, **kwargs: events.append("log")
    trader._remove_position = lambda mint: events.append("remove")
    staged_manager = SimpleNamespace()
    monkeypatch.setattr(
        "trading.universal_trader.stage_cleanup_after_sell",
        lambda *args, **kwargs: events.append("stage") or staged_manager,
    )
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    await trader._monitor_position_until_exit(token, position)

    assert events == ["stage", "log", "remove"]
    assert cleanup_after_sell.await_args.kwargs["confirmed_sold_raw"] is None
    assert cleanup_after_sell.await_args.kwargs["staged_manager"] is staged_manager

    calculate_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_sell_passes_raw_delta_to_cleanup(monkeypatch) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        account_balance_baseline_raw=0,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_price=AsyncMock(return_value=2.0))
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.LETS_BONK,
                tx_signature="sell-signature",
                amount=1.5,
                amount_raw=1_500_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    await trader._monitor_position_until_exit(token, position)

    assert cleanup_after_sell.await_args.kwargs["confirmed_sold_raw"] == 1_500_000


@pytest.mark.asyncio
async def test_monitor_cancellation_drains_inflight_sell_before_propagating(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        account_balance_baseline_raw=0,
        position_id="buy-signature",
    )
    sell_started = asyncio.Event()
    release_sell = asyncio.Event()
    events: list[str] = []

    async def sell(*args, **kwargs) -> TradeResult:
        sell_started.set()
        await release_sell.wait()
        return TradeResult(
            success=True,
            platform=Platform.LETS_BONK,
            tx_signature="sell-signature",
            amount=2.0,
            amount_raw=2_000_000,
            price=2.0,
            status=TransactionStatus.SUCCESS.value,
        )

    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_price=AsyncMock(return_value=2.0))
    )
    trader.seller = SimpleNamespace(execute=sell)
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: events.append("log")
    trader._remove_position = lambda mint: events.append("remove")
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    monitor_task = asyncio.create_task(
        trader._monitor_position_until_exit(token, position)
    )
    await sell_started.wait()
    monitor_task.cancel()
    await asyncio.sleep(0)
    completed_before_sell = monitor_task.done()
    release_sell.set()

    with pytest.raises(asyncio.CancelledError):
        await monitor_task

    assert completed_before_sell is False
    assert events == ["log", "remove"]
    cleanup_after_sell.assert_awaited_once()

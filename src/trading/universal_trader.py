"""
Universal trading coordinator that works with any platform.
Cleaned up to remove all platform-specific hardcoding.
"""

import asyncio

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows requires an explicit lock backend
    fcntl = None
import json
import sys
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import monotonic

from solders.pubkey import Pubkey

from cleanup.manager import AccountCleanupManager
from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
    stage_cleanup_after_sell,
)
from core.client import (
    SolanaClient,
    TransactionStatus,
    TransactionSubmissionUnknown,
)
from core.execution_policy import ExecutionMode, ExecutionPolicy
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
    resolve_quote_amounts,
    resolve_quote_mint,
)
from core.transaction_ledger import TransactionLedger
from core.wallet import Wallet
from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from utils.durable_file import atomic_write_text
from utils.logger import get_logger

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    # Standard asyncio is fine, just slightly slower
    pass

logger = get_logger(__name__)

# Exit sells are attempted in bounded bursts. Positions remain journaled and
# monitored after a burst, rather than being abandoned.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3


def _resolve_quote_config(
    buy_amount: float,
    quote_amounts: dict[str, float] | None,
    allowed_quote_mints: list[str] | None,
) -> tuple[dict[Pubkey, float], set[Pubkey] | None]:
    """Resolve quote-asset configuration into per-mint amounts and an allowlist.

    Keys may be mint addresses or the aliases "sol"/"usdc". SOL always falls
    back to trade.buy_amount, so a config that never mentions quote assets
    keeps its existing SOL-only behaviour.

    Args:
        buy_amount: SOL amount per buy from trade.buy_amount
        quote_amounts: Optional map of quote mint -> amount in whole units
        allowed_quote_mints: Optional list of quote mints permitted to trade

    Returns:
        Tuple of (amount per quote mint, allowed quote mints or None for any)
    """
    amounts = {WSOL_MINT: buy_amount, **resolve_quote_amounts(quote_amounts)}
    allowed = (
        {resolve_quote_mint(mint) for mint in allowed_quote_mints}
        if allowed_quote_mints
        else None
    )
    return amounts, allowed


def _validate_exit_config(
    exit_strategy: str,
    take_profit_percentage: float | None,
    stop_loss_percentage: float | None,
    max_hold_time: int | None,
    price_check_interval: int,
    max_exit_sell_attempts: int,
) -> str:
    """Validate exit configuration before any network resources are created."""
    if not isinstance(exit_strategy, str):
        raise ValueError("exit_strategy must be a string")
    strategy = exit_strategy.lower()
    if strategy not in {"tp_sl", "time_based", "manual"}:
        raise ValueError("exit_strategy must be one of: tp_sl, time_based, manual")
    if (
        isinstance(price_check_interval, bool)
        or not isinstance(price_check_interval, int)
        or price_check_interval <= 0
    ):
        raise ValueError("price_check_interval must be a positive integer")
    if (
        isinstance(max_exit_sell_attempts, bool)
        or not isinstance(max_exit_sell_attempts, int)
        or max_exit_sell_attempts <= 0
    ):
        raise ValueError("max_exit_sell_attempts must be a positive integer")
    if take_profit_percentage is not None:
        if (
            isinstance(take_profit_percentage, bool)
            or not isinstance(take_profit_percentage, int | float)
            or not isfinite(take_profit_percentage)
            or take_profit_percentage <= 0
        ):
            raise ValueError("take_profit_percentage must be finite and positive")
    if stop_loss_percentage is not None:
        if (
            isinstance(stop_loss_percentage, bool)
            or not isinstance(stop_loss_percentage, int | float)
            or not isfinite(stop_loss_percentage)
            or stop_loss_percentage <= 0
            or stop_loss_percentage >= 1
        ):
            raise ValueError("stop_loss_percentage must be finite and between 0 and 1")
    if max_hold_time is not None and (
        isinstance(max_hold_time, bool)
        or not isinstance(max_hold_time, int)
        or max_hold_time <= 0
    ):
        raise ValueError("max_hold_time must be a positive integer")
    if strategy == "tp_sl" and all(
        value is None
        for value in (
            take_profit_percentage,
            stop_loss_percentage,
            max_hold_time,
        )
    ):
        raise ValueError("tp_sl exit strategy requires at least one exit condition")
    return strategy


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # Platform configuration
        platform: Platform | str = Platform.PUMP_FUN,
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
        execution_policy: ExecutionPolicy | None = None,
        # Quote asset configuration (pump.fun non-SOL pairs)
        quote_amounts: dict[str, float] | None = None,
        allowed_quote_mints: list[str] | None = None,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        price_check_interval: int = 10,
        max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        # Retry and timeout settings
        max_retries: int = 1,
        wait_time_after_creation: int = 15,
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 0.001,
        token_wait_timeout: int = 30,
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        # Compute unit configuration
        compute_units: dict | None = None,
        # Queue and recovery configuration
        max_rps: float = 25.0,
        token_queue_size: int = 128,
        position_journal_path: str | Path | None = None,
        transaction_ledger_path: str | Path | None = None,
    ):
        """Initialize the universal trader."""
        self.exit_strategy = _validate_exit_config(
            exit_strategy,
            take_profit_percentage,
            stop_loss_percentage,
            max_hold_time,
            price_check_interval,
            max_exit_sell_attempts,
        )
        if (
            isinstance(token_queue_size, bool)
            or not isinstance(token_queue_size, int)
            or token_queue_size <= 0
        ):
            raise ValueError("token_queue_size must be a positive integer")
        if self.exit_strategy == "time_based" and (
            isinstance(wait_time_after_buy, bool)
            or not isinstance(wait_time_after_buy, int)
            or wait_time_after_buy <= 0
        ):
            raise ValueError(
                "wait_time_after_buy must be a positive integer for time_based exit"
            )
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.wallet = Wallet(private_key)
        self.execution_policy.validate_wallet(self.wallet.pubkey)
        self.platform = Platform(platform) if isinstance(platform, str) else platform
        self.transaction_ledger: TransactionLedger | None = None
        if self.execution_policy.mode is ExecutionMode.LIVE:
            ledger_path = (
                Path(transaction_ledger_path)
                if transaction_ledger_path
                else (
                    Path(".state")
                    / "transaction-ledgers"
                    / f"{self.wallet.pubkey}-{self.platform.value}.sqlite3"
                )
            )
            self.transaction_ledger = TransactionLedger(ledger_path)
        self.solana_client = SolanaClient(
            rpc_endpoint,
            max_rps=max_rps,
            execution_policy=self.execution_policy,
            ledger=self.transaction_ledger,
        )
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )
        logger.info(f"Initialized Universal Trader for platform: {self.platform.value}")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Get platform-specific implementations
        self.platform_implementations = get_platform_implementations(
            self.platform, self.solana_client
        )

        # Store compute unit and quote-asset configuration
        self.compute_units = compute_units or {}
        self.quote_amounts, self.allowed_quote_mints = _resolve_quote_config(
            buy_amount, quote_amounts, allowed_quote_mints
        )

        # Create platform-aware traders
        self.buyer, self.seller = (
            PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                buy_amount,
                buy_slippage,
                max_retries,
                extreme_fast_token_amount,
                extreme_fast_mode,
                compute_units=self.compute_units,
                quote_amounts=self.quote_amounts,
                curve_refresh_budget=curve_refresh_budget,
                trust_create_event=trust_create_event,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
            ),
        )

        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            pumpportal_url=pumpportal_url,
            platforms=[self.platform],  # Only listen for our platform
        )

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_time = max_hold_time
        self.price_check_interval = price_check_interval
        self.max_exit_sell_attempts = max_exit_sell_attempts

        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout

        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[str, Pubkey] = {}
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue(
            maxsize=token_queue_size
        )
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        self._reserved_mints: set[str] = set()
        self._inflight_tokens: dict[str, TokenInfo] = {}
        self._queue_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._position_tasks: set[asyncio.Task] = set()
        self._position_monitor_tasks: set[asyncio.Task] = set()
        self._fatal_monitor_errors: asyncio.Queue[BaseException] = asyncio.Queue()
        self._active_positions: dict[str, tuple[TokenInfo, Position]] = {}
        self._unresolved_buys: dict[str, dict] = {}
        self._pending_recovery_tokens: list[TokenInfo] = []
        self._journal_path = (
            Path(position_journal_path)
            if position_journal_path is not None
            else Path(".state")
            / "positions"
            / f"{self.wallet.pubkey}-{self.platform.value}.json"
        )
        self._journal_lock_handle = None
        if self.execution_policy.mode is ExecutionMode.LIVE:
            if fcntl is None:
                if self.transaction_ledger is not None:
                    self.transaction_ledger.close()
                raise RuntimeError(
                    "Live recovery journal locking is unavailable on this platform"
                )
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._journal_path.with_suffix(
                f"{self._journal_path.suffix}.lock"
            )
            self._journal_lock_handle = lock_path.open("a+b")
            try:
                fcntl.flock(
                    self._journal_lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                self._journal_lock_handle.close()
                if self.transaction_ledger is not None:
                    self.transaction_ledger.close()
                self._journal_lock_handle = None
                raise RuntimeError(
                    f"Another live trader owns recovery journal {self._journal_path}"
                ) from exc
        try:
            self._load_recovery_journal()
            self._hydrate_submission_recovery()
        except BaseException:
            for stage, error in self._release_persistence_resources():
                logger.error(
                    "Failure during constructor cleanup: %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise

    def _release_persistence_resources(
        self,
    ) -> list[tuple[str, BaseException]]:
        """Release the ledger and journal lock, attempting both on failure."""
        failures: list[tuple[str, BaseException]] = []

        ledger = self.transaction_ledger
        self.transaction_ledger = None
        if ledger is not None:
            try:
                ledger.close()
            except BaseException as exc:
                failures.append(("transaction ledger close", exc))

        lock_handle = self._journal_lock_handle
        self._journal_lock_handle = None
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except BaseException as exc:
                failures.append(("recovery journal unlock", exc))
            try:
                lock_handle.close()
            except BaseException as exc:
                failures.append(("recovery journal lock close", exc))

        return failures

    @staticmethod
    def _validate_creation_timestamp(value: object) -> float | None:
        """Accept only finite timestamps matching TokenInfo's declared type."""
        if value is None:
            return None
        if not isinstance(value, float) or not isfinite(value):
            raise ValueError(
                "Recovery token creation_timestamp must be a finite float or null"
            )
        return value

    @staticmethod
    def _token_to_dict(token_info: TokenInfo) -> dict:
        """Serialize only authoritative TokenInfo fields needed for recovery."""
        pubkey_fields = (
            "mint",
            "bonding_curve",
            "associated_bonding_curve",
            "pool_state",
            "base_vault",
            "quote_vault",
            "global_config",
            "platform_config",
            "user",
            "creator",
            "creator_vault",
            "token_program_id",
            "quote_mint",
            "quote_token_program_id",
        )
        payload = {
            "name": token_info.name,
            "symbol": token_info.symbol,
            "uri": token_info.uri,
            "platform": token_info.platform.value,
            "is_mayhem_mode": token_info.is_mayhem_mode,
            "is_cashback_coin": token_info.is_cashback_coin,
            "virtual_quote_reserves": token_info.virtual_quote_reserves,
            "state_from_event": token_info.state_from_event,
            "curve_complete": token_info.curve_complete,
            "pool_tradeable": token_info.pool_tradeable,
            "pool_status": token_info.pool_status,
            "base_decimals": token_info.base_decimals,
            "quote_decimals": token_info.quote_decimals,
            "source": token_info.source,
            "signature": token_info.signature,
            "slot": token_info.slot,
            "commitment": token_info.commitment,
            "transaction_index": token_info.transaction_index,
            "inner_instruction_index": token_info.inner_instruction_index,
            "metadata_verified": token_info.metadata_verified,
            "creation_timestamp": UniversalTrader._validate_creation_timestamp(
                token_info.creation_timestamp
            ),
        }
        for field_name in pubkey_fields:
            value = getattr(token_info, field_name)
            payload[field_name] = str(value) if value is not None else None
        return payload

    @staticmethod
    def _token_from_dict(payload: dict) -> TokenInfo:
        """Restore TokenInfo without guessing missing protocol metadata."""
        if not isinstance(payload, dict):
            raise ValueError("Recovery token record must be an object")
        pubkey_fields = (
            "mint",
            "bonding_curve",
            "associated_bonding_curve",
            "pool_state",
            "base_vault",
            "quote_vault",
            "global_config",
            "platform_config",
            "user",
            "creator",
            "creator_vault",
            "token_program_id",
            "quote_mint",
            "quote_token_program_id",
        )
        values = {
            field_name: (
                Pubkey.from_string(payload[field_name])
                if payload.get(field_name)
                else None
            )
            for field_name in pubkey_fields
        }
        boolean_fields = (
            "is_mayhem_mode",
            "is_cashback_coin",
            "state_from_event",
            "metadata_verified",
        )
        for field_name in boolean_fields:
            value = payload.get(field_name, False)
            if not isinstance(value, bool):
                raise ValueError(f"Recovery token {field_name} must be a boolean")
        for field_name in ("curve_complete", "pool_tradeable"):
            value = payload.get(field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(
                    f"Recovery token {field_name} must be a boolean or null"
                )
        pool_status = payload.get("pool_status")
        if pool_status is not None and not isinstance(pool_status, str):
            raise ValueError("Recovery token pool_status must be a string or null")
        return TokenInfo(
            name=str(payload["name"]),
            symbol=str(payload["symbol"]),
            uri=str(payload.get("uri", "")),
            platform=Platform(payload["platform"]),
            is_mayhem_mode=payload.get("is_mayhem_mode", False),
            is_cashback_coin=payload.get("is_cashback_coin", False),
            virtual_quote_reserves=payload.get("virtual_quote_reserves"),
            state_from_event=payload.get("state_from_event", False),
            curve_complete=payload.get("curve_complete"),
            pool_tradeable=payload.get("pool_tradeable"),
            pool_status=payload.get("pool_status"),
            creation_timestamp=UniversalTrader._validate_creation_timestamp(
                payload.get("creation_timestamp")
            ),
            base_decimals=payload.get("base_decimals"),
            quote_decimals=payload.get("quote_decimals"),
            source=payload.get("source"),
            signature=payload.get("signature"),
            slot=payload.get("slot"),
            commitment=payload.get("commitment"),
            transaction_index=payload.get("transaction_index"),
            inner_instruction_index=payload.get("inner_instruction_index"),
            metadata_verified=payload.get("metadata_verified", False),
            **values,
        )

    def _load_recovery_journal(self) -> None:
        """Load active positions and unresolved work, failing closed on corruption."""
        if not self._journal_path.exists():
            return
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("recovery journal must contain an object")
            if payload.get("version") != 1:
                raise ValueError("unsupported journal version")
            if payload.get("wallet") != str(self.wallet.pubkey):
                raise ValueError("recovery journal belongs to a different wallet")
            if payload.get("platform") != self.platform.value:
                raise ValueError("recovery journal belongs to a different platform")

            position_records = payload.get("positions", {})
            unresolved_records = payload.get("unresolved_buys", {})
            pending_records = payload.get("pending_tokens", [])
            if not isinstance(position_records, dict):
                raise ValueError("recovery journal positions must be an object")
            if not isinstance(unresolved_records, dict):
                raise ValueError("recovery journal unresolved_buys must be an object")
            if not isinstance(pending_records, list):
                raise ValueError("recovery journal pending_tokens must be an array")

            active_positions = dict(self._active_positions)
            unresolved_buys = dict(self._unresolved_buys)
            reserved_mints = set(self._reserved_mints)
            traded_mints = set(self.traded_mints)
            traded_token_programs = dict(self.traded_token_programs)
            cleanup_records: list[tuple[TokenInfo, Position]] = []

            for token_key, record in position_records.items():
                if not isinstance(record, dict):
                    raise ValueError("position journal record must be an object")
                token_info = self._token_from_dict(record["token"])
                position = Position.from_dict(record["position"])
                if str(position.mint) != token_key or position.mint != token_info.mint:
                    raise ValueError("position journal mint mismatch")
                if token_info.platform is not self.platform:
                    raise ValueError("position journal platform mismatch")
                if position.is_active:
                    active_positions[token_key] = (token_info, position)
                    reserved_mints.add(token_key)
                    traded_mints.add(token_info.mint)
                    if token_info.token_program_id is not None:
                        traded_token_programs[token_key] = token_info.token_program_id
                        if (
                            position.account_balance_baseline_raw is not None
                            and position.quantity_raw is not None
                        ):
                            cleanup_records.append((token_info, position))

            for token_key, record in unresolved_records.items():
                if not isinstance(record, dict):
                    raise ValueError("unresolved buy journal record must be an object")
                token_info = self._token_from_dict(record["token"])
                signature = record.get("signature")
                if (
                    str(token_info.mint) != token_key
                    or not isinstance(signature, str)
                    or not signature
                ):
                    raise ValueError("invalid unresolved buy journal record")
                if token_info.platform is not self.platform:
                    raise ValueError("unresolved buy journal platform mismatch")
                unresolved_buys[token_key] = {
                    "token": token_info,
                    "signature": signature,
                    "baseline_raw": record.get("baseline_raw"),
                }
                reserved_mints.add(token_key)

            pending_recovery_tokens: list[TokenInfo] = []
            for item in pending_records:
                if not isinstance(item, dict):
                    raise ValueError("pending recovery token record must be an object")
                pending_token = self._token_from_dict(item)
                if pending_token.platform is not self.platform:
                    raise ValueError("pending recovery token platform mismatch")
                pending_recovery_tokens.append(pending_token)

            for token_info, position in cleanup_records:
                AccountCleanupManager.record_bot_owned_balance(
                    self.wallet.pubkey,
                    token_info.mint,
                    token_info.token_program_id,
                    baseline_raw=position.account_balance_baseline_raw,
                    acquired_raw=position.quantity_raw,
                    ownership_id=position.position_id,
                )

            self._active_positions = active_positions
            self._unresolved_buys = unresolved_buys
            self._pending_recovery_tokens = pending_recovery_tokens
            self._reserved_mints = reserved_mints
            self.traded_mints = traded_mints
            self.traded_token_programs = traded_token_programs
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot safely load recovery journal {self._journal_path}"
            ) from exc

    @staticmethod
    def _buy_intent_id(token_info: TokenInfo) -> str:
        """Return the stable ledger intent used by the buyer."""
        return f"buy:{token_info.platform.value}:{token_info.mint}"

    def _hydrate_submission_recovery(self) -> None:
        """Join journaled work to exact ledger signatures before age/price gates."""
        if self.transaction_ledger is None:
            return
        changed = False
        remaining_pending: list[TokenInfo] = []
        for token_info in self._pending_recovery_tokens:
            token_key = str(token_info.mint)
            intent_id = self._buy_intent_id(token_info)
            record = self.transaction_ledger.get_active_submission_record(intent_id)
            if record is None:
                remaining_pending.append(token_info)
                continue
            self._unresolved_buys[token_key] = {
                "token": token_info,
                "signature": record.signature,
                "baseline_raw": None,
                "intent_id": intent_id,
            }
            self._reserved_mints.add(token_key)
            changed = True
        if len(remaining_pending) != len(self._pending_recovery_tokens):
            self._pending_recovery_tokens = remaining_pending

        for token_key, (token_info, position) in self._active_positions.items():
            intent_id = position.pending_exit_intent_id
            if intent_id is None or position.pending_exit_signature is not None:
                continue
            record = self.transaction_ledger.get_active_submission_record(intent_id)
            if record is None:
                continue
            if position.pending_exit_reason is None:
                raise RuntimeError(
                    f"Position {token_key} has a pending exit intent without a reason"
                )
            position.mark_exit_pending(record.signature, position.pending_exit_reason)
            self._active_positions[token_key] = (token_info, position)
            changed = True

        if changed:
            self._write_recovery_journal()

    async def _resume_ledger_bound_submissions(self) -> None:
        """Resume prepared exact wires before listeners, age checks, or prices."""
        if getattr(self, "transaction_ledger", None) is None:
            return
        intents: dict[str, str] = {}
        for record in self._unresolved_buys.values():
            token_info = record["token"]
            intent_id = record.get("intent_id") or self._buy_intent_id(token_info)
            intents[intent_id] = record["signature"]
        for _token_info, position in self._active_positions.values():
            if (
                position.pending_exit_intent_id is not None
                and position.pending_exit_signature is not None
            ):
                intents[position.pending_exit_intent_id] = (
                    position.pending_exit_signature
                )

        for intent_id, expected_signature in intents.items():
            try:
                recovered = await self.solana_client.recover_active_submission(
                    intent_id,
                    skip_preflight=True,
                )
            except TransactionSubmissionUnknown as exc:
                recovered_signature = exc.signature
            else:
                recovered_signature = None if recovered is None else str(recovered)
            if (
                recovered_signature is not None
                and recovered_signature != expected_signature
            ):
                raise RuntimeError(
                    f"Ledger recovery signature mismatch for intent {intent_id}"
                )

    async def _resume_staged_cleanups(self) -> None:
        """Consume durable post-sell cleanup work before accepting new tokens."""
        if getattr(self, "cleanup_mode", None) not in {"after_sell", "post_session"}:
            return
        manager = AccountCleanupManager(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )
        results = await manager.resume_pending_cleanups()
        for result in results:
            if not result.success:
                logger.warning(
                    "Recovered cleanup remains %s for mint %s",
                    result.status.value,
                    result.mint,
                )

    def _write_recovery_journal(self) -> None:
        """Atomically persist every active or unfinished unit of work."""
        payload = {
            "version": 1,
            "wallet": str(self.wallet.pubkey),
            "platform": self.platform.value,
            "updated_at": datetime.now(UTC).isoformat(),
            "positions": {
                token_key: {
                    "token": self._token_to_dict(token_info),
                    "position": position.to_dict(),
                }
                for token_key, (token_info, position) in self._active_positions.items()
                if position.is_active
            },
            "unresolved_buys": {
                token_key: {
                    "token": self._token_to_dict(record["token"]),
                    "signature": record["signature"],
                    "baseline_raw": record.get("baseline_raw"),
                }
                for token_key, record in self._unresolved_buys.items()
            },
            "pending_tokens": [
                self._token_to_dict(token_info)
                for token_info in self._pending_recovery_tokens
            ],
        }
        atomic_write_text(
            self._journal_path,
            json.dumps(payload, indent=2, sort_keys=True),
        )

    def _persist_position(self, token_info: TokenInfo, position: Position) -> None:
        """Record an active holding before monitoring or returning control."""
        token_key = str(token_info.mint)
        self._active_positions[token_key] = (token_info, position)
        self._reserved_mints.add(token_key)
        self.traded_mints.add(token_info.mint)
        if token_info.token_program_id is not None:
            self.traded_token_programs[token_key] = token_info.token_program_id
            if (
                position.account_balance_baseline_raw is not None
                and position.quantity_raw is not None
            ):
                AccountCleanupManager.record_bot_owned_balance(
                    self.wallet.pubkey,
                    token_info.mint,
                    token_info.token_program_id,
                    baseline_raw=position.account_balance_baseline_raw,
                    acquired_raw=position.quantity_raw,
                    ownership_id=position.position_id,
                )
        self._write_recovery_journal()

    def _remove_position(self, mint: Pubkey) -> None:
        """Remove only a confirmed-closed position from the active journal."""
        token_key = str(mint)
        self._active_positions.pop(token_key, None)
        self._reserved_mints.discard(token_key)
        self.processed_tokens.add(token_key)
        self._write_recovery_journal()

    def _schedule_position_monitor(
        self, token_info: TokenInfo, position: Position
    ) -> asyncio.Task | None:
        """Start one monitor task for an active automatic-exit position."""
        if not position.is_active or (
            position.take_profit_price is None
            and position.stop_loss_price is None
            and position.max_hold_time is None
        ):
            return None
        task = asyncio.create_task(
            self._monitor_position_until_exit(token_info, position)
        )
        self._position_tasks.add(task)
        self._position_monitor_tasks.add(task)

        def _task_finished(done_task: asyncio.Task) -> None:
            self._position_tasks.discard(done_task)
            self._position_monitor_tasks.discard(done_task)
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error is not None:
                self._fatal_monitor_errors.put_nowait(error)
                logger.error(
                    "Position monitor stopped unexpectedly for %s",
                    token_info.mint,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_task_finished)
        return task

    async def _await_queue_drain(
        self,
        processor_task: asyncio.Task,
        lifecycle_failure_task: asyncio.Task,
    ) -> None:
        """Wait for queued work unless its processor or lifecycle fails."""
        drain_task = asyncio.create_task(self.token_queue.join())
        try:
            done, _ = await asyncio.wait(
                {drain_task, processor_task, lifecycle_failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lifecycle_failure_task in done:
                raise await lifecycle_failure_task
            if processor_task in done:
                await processor_task
                raise RuntimeError("Token queue processor stopped unexpectedly")
            await drain_task
            if lifecycle_failure_task.done():
                raise await lifecycle_failure_task
        finally:
            if not drain_task.done():
                drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

    async def start(self) -> None:
        """Start trading and propagate fatal failures after orderly cleanup."""
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        logger.info(f"Exit strategy: {self.exit_strategy}")
        processor_task: asyncio.Task | None = None
        listener_task: asyncio.Task | None = None
        monitor_failure_task: asyncio.Task | None = None
        monitor_group_task: asyncio.Future | None = None
        token_wait_task: asyncio.Task | None = None
        primary_error: BaseException | None = None
        primary_traceback = None

        try:
            await self._resume_ledger_bound_submissions()
            await self._resume_staged_cleanups()
            processor_task = asyncio.create_task(self._process_token_queue())
            reconciliation_task = asyncio.create_task(self._reconcile_unresolved_buys())
            self._position_tasks.add(reconciliation_task)

            def _reconciliation_finished(done_task: asyncio.Task) -> None:
                self._position_tasks.discard(done_task)
                if done_task.cancelled():
                    return
                error = done_task.exception()
                if error is not None:
                    self._fatal_monitor_errors.put_nowait(error)
                    logger.error(
                        "Unresolved-buy reconciliation stopped unexpectedly",
                        exc_info=(type(error), error, error.__traceback__),
                    )

            reconciliation_task.add_done_callback(_reconciliation_finished)
            monitor_failure_task = asyncio.create_task(self._fatal_monitor_errors.get())

            for token_info, position in tuple(self._active_positions.values()):
                self._schedule_position_monitor(token_info, position)
            for token_info in tuple(self._pending_recovery_tokens):
                queued = await self._queue_token(token_info, recovered=True)
                token_key = str(token_info.mint)
                if not queued and (
                    token_key in self._active_positions
                    or token_key in self._unresolved_buys
                ):
                    self._finish_token_reservation(token_info, handled=False)
            self._write_recovery_journal()

            try:
                health_resp = await self.solana_client.get_health()
                logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
            except Exception as exc:
                logger.warning(f"RPC warm-up failed: {exc!s}")

            if not self.yolo_mode:
                await self._await_queue_drain(
                    processor_task,
                    monitor_failure_task,
                )
                token_wait_task = asyncio.create_task(self._wait_for_token())
                done, _ = await asyncio.wait(
                    {token_wait_task, monitor_failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if monitor_failure_task in done:
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                token_info = await token_wait_task
                if token_info is not None:
                    handled = False
                    try:
                        handled = await self._handle_token(token_info)
                    finally:
                        self._finish_token_reservation(token_info, handled)
                if monitor_failure_task.done():
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                automatic_monitors = tuple(self._position_monitor_tasks)
                if automatic_monitors:
                    monitor_group_task = asyncio.gather(*automatic_monitors)
                    done, _ = await asyncio.wait(
                        {monitor_group_task, monitor_failure_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if monitor_group_task in done:
                        await monitor_group_task
                    else:
                        monitor_error = await monitor_failure_task
                        raise monitor_error
            else:
                listener_task = asyncio.create_task(
                    self.token_listener.listen_for_tokens(
                        lambda token: self._queue_token(token),
                        self.match_string,
                        self.bro_address,
                    )
                )
                done, _ = await asyncio.wait(
                    {listener_task, processor_task, monitor_failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if monitor_failure_task in done:
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                if processor_task in done:
                    await processor_task
                    raise RuntimeError("Token queue processor stopped unexpectedly")
                await listener_task
                await self._await_queue_drain(
                    processor_task,
                    monitor_failure_task,
                )
                if monitor_failure_task.done():
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                raise RuntimeError("Token listener stopped unexpectedly")
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
        finally:
            self._shutdown_event.set()
            shutdown_errors: list[tuple[str, BaseException]] = []
            if token_wait_task is not None:
                if not token_wait_task.done():
                    token_wait_task.cancel()
                try:
                    await token_wait_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token wait", exc))

            if monitor_group_task is not None:
                if not monitor_group_task.done():
                    monitor_group_task.cancel()
                try:
                    await monitor_group_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("position monitor group", exc))

            if listener_task is not None:
                if not listener_task.done():
                    listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token listener", exc))

            if processor_task is not None:
                if not processor_task.done():
                    processor_task.cancel()
                try:
                    await processor_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token queue processor", exc))

            try:
                await self._cleanup_resources()
            except BaseException as exc:
                shutdown_errors.append(("resource cleanup", exc))

            await asyncio.sleep(0)
            monitor_errors: list[BaseException] = []
            if monitor_failure_task is not None:
                if not monitor_failure_task.done():
                    monitor_failure_task.cancel()
                try:
                    monitor_error = await monitor_failure_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    monitor_errors.append(exc)
                else:
                    monitor_errors.append(monitor_error)
            while not self._fatal_monitor_errors.empty():
                monitor_errors.append(self._fatal_monitor_errors.get_nowait())
            for monitor_error in monitor_errors:
                if primary_error is None:
                    primary_error = monitor_error
                    primary_traceback = monitor_error.__traceback__
                elif monitor_error is not primary_error:
                    shutdown_errors.append(("position monitor", monitor_error))

            if primary_error is None and shutdown_errors:
                _, primary_error = shutdown_errors.pop(0)
                primary_traceback = primary_error.__traceback__
            for stage, error in shutdown_errors:
                logger.error(
                    "Secondary failure during %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            logger.info("Universal Trader has shut down")

        if primary_error is not None:
            raise primary_error.with_traceback(primary_traceback)

    async def _wait_for_token(self) -> TokenInfo | None:
        """Wait for and atomically reserve a single token mint."""
        token_found = asyncio.Event()
        found_token: TokenInfo | None = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)
            async with self._queue_lock:
                if (
                    found_token is not None
                    or token_key in self.processed_tokens
                    or token_key in self._reserved_mints
                ):
                    return
                self._reserved_mints.add(token_key)
                self.token_timestamps[token_key] = monotonic()
                found_token = token
                token_found.set()

        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                token_callback,
                self.match_string,
                self.bro_address,
            )
        )
        token_found_task = asyncio.create_task(token_found.wait())
        try:
            done, _ = await asyncio.wait(
                {token_found_task, listener_task},
                timeout=self.token_wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if listener_task in done:
                await listener_task
                if found_token is None:
                    raise RuntimeError(
                        "Token listener stopped before detecting a token"
                    )
            if token_found_task in done or found_token is not None:
                return found_token
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
            return None
        finally:
            for task in (token_found_task, listener_task):
                if not task.done():
                    task.cancel()
            for task in (token_found_task, listener_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _cleanup_resources(self) -> None:
        """Persist unfinished work, then attempt every independent cleanup."""
        failures: list[tuple[str, BaseException]] = []

        def record_failure(stage: str, error: BaseException) -> None:
            failures.append((stage, error))

        try:
            pending_tokens = {
                str(token_info.mint): token_info
                for token_info in self._pending_recovery_tokens
            }
            pending_tokens.update(
                {
                    token_key: token_info
                    for token_key, token_info in self._inflight_tokens.items()
                    if token_key not in self._active_positions
                    and token_key not in self._unresolved_buys
                }
            )
            while True:
                try:
                    token_info = self.token_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                pending_tokens[str(token_info.mint)] = token_info
                self.token_queue.task_done()
            self._pending_recovery_tokens = list(pending_tokens.values())
            self._write_recovery_journal()
        except BaseException as exc:
            record_failure("recovery journal", exc)

        if self._position_tasks:
            background_tasks = tuple(self._position_tasks)
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            try:
                task_results = await asyncio.gather(
                    *background_tasks,
                    return_exceptions=True,
                )
            except BaseException as exc:
                record_failure("background tasks", exc)
            else:
                for result in task_results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        record_failure("background task", result)

        try:
            protected_mints = {
                Pubkey.from_string(token_key)
                for token_key in (
                    self._active_positions.keys() | self._unresolved_buys.keys()
                )
            }
            cleanup_mints = self.traded_mints - protected_mints
        except BaseException as exc:
            record_failure("cleanup planning", exc)
            cleanup_mints = set()

        if cleanup_mints:
            try:
                mints_list = list(cleanup_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except BaseException as exc:
                record_failure("post-session cleanup", exc)

        try:
            await self.solana_client.close()
        except BaseException as exc:
            record_failure("Solana client close", exc)

        failures.extend(self._release_persistence_resources())

        if failures:
            _, first_error = failures[0]
            for stage, error in failures[1:]:
                logger.error(
                    "Secondary failure during %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise first_error.with_traceback(first_error.__traceback__)

    async def _queue_token(
        self, token_info: TokenInfo, *, recovered: bool = False
    ) -> bool:
        """Atomically coalesce duplicate mints into a bounded queue."""
        token_key = str(token_info.mint)
        async with self._queue_lock:
            if token_key in self.processed_tokens or token_key in self._reserved_mints:
                return False
            self._reserved_mints.add(token_key)
            queued_at = monotonic()
            if recovered:
                creation_timestamp = self._validate_creation_timestamp(
                    token_info.creation_timestamp
                )
                if creation_timestamp is None or creation_timestamp > queued_at:
                    creation_timestamp = queued_at - max(self.max_token_age, 0) - 1
                queued_at = creation_timestamp
            self.token_timestamps[token_key] = queued_at
            try:
                self.token_queue.put_nowait(token_info)
            except asyncio.QueueFull:
                self._reserved_mints.discard(token_key)
                self.token_timestamps.pop(token_key, None)
                logger.warning(
                    f"Token queue full; dropped {token_info.symbol} ({token_info.mint})"
                )
                return False
        logger.info(
            f"Queued {'recovered ' if recovered else ''}token: "
            f"{token_info.symbol} ({token_info.mint})"
        )
        return True

    def _finish_token_reservation(self, token_info: TokenInfo, handled: bool) -> None:
        """Release a mint only when it is safe for another callback to claim."""
        token_key = str(token_info.mint)
        has_durable_state = (
            token_key in self._active_positions or token_key in self._unresolved_buys
        )
        pending_count = len(self._pending_recovery_tokens)
        if handled:
            self.processed_tokens.add(token_key)
        if handled or has_durable_state:
            self._pending_recovery_tokens = [
                pending
                for pending in self._pending_recovery_tokens
                if str(pending.mint) != token_key
            ]
        pending_changed = len(self._pending_recovery_tokens) != pending_count
        has_pending_recovery = any(
            str(pending.mint) == token_key for pending in self._pending_recovery_tokens
        )
        if not has_durable_state and not has_pending_recovery:
            self._reserved_mints.discard(token_key)
        self.token_timestamps.pop(token_key, None)
        if handled or pending_changed:
            self._write_recovery_journal()

    async def _process_token_queue(self) -> None:
        """Process claimed queue items and acknowledge only actual claims."""
        while True:
            claimed = False
            token_info: TokenInfo | None = None
            handled = False
            try:
                token_info = await self.token_queue.get()
                claimed = True
                token_key = str(token_info.mint)
                self._inflight_tokens[token_key] = token_info
                token_age = monotonic() - self.token_timestamps.get(
                    token_key, monotonic()
                )
                if token_age > self.max_token_age:
                    logger.info(
                        f"Skipping stale token {token_info.symbol} "
                        f"({token_age:.1f}s > {self.max_token_age}s)"
                    )
                    handled = True
                else:
                    handled = await self._handle_token(token_info)
            except asyncio.CancelledError:
                if (
                    claimed
                    and token_info is not None
                    and str(token_info.mint) not in self._active_positions
                    and str(token_info.mint) not in self._unresolved_buys
                    and all(
                        pending.mint != token_info.mint
                        for pending in self._pending_recovery_tokens
                    )
                ):
                    self._pending_recovery_tokens.append(token_info)
                    self._write_recovery_journal()
                logger.info("Token queue processor was cancelled")
                raise
            except Exception:
                logger.exception("Fatal error in token queue processor")
                raise
            finally:
                if claimed and token_info is not None:
                    self._inflight_tokens.pop(str(token_info.mint), None)
                    self._finish_token_reservation(token_info, handled)
                    self.token_queue.task_done()

    async def _handle_token(self, token_info: TokenInfo) -> bool:
        """Handle a token, returning true only after resolved handling."""
        try:
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return True

            # Skip coins paired against a quote asset we are not set up to
            # trade. Cheaper to drop here than to fail a buy on-chain.
            token_quote_mint = normalize_quote_mint(token_info.quote_mint)
            if (
                self.allowed_quote_mints is not None
                and token_quote_mint not in self.allowed_quote_mints
            ):
                logger.info(
                    f"Skipping {token_info.symbol} - quote mint {token_quote_mint} "
                    f"not in allowed_quote_mints"
                )
                return True
            if token_quote_mint not in self.quote_amounts:
                logger.info(
                    f"Skipping {token_info.symbol} - no buy amount configured for "
                    f"quote mint {token_quote_mint}"
                )
                return True
            if not self.extreme_fast_mode:
                await self._save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds "
                    "for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)

            logger.info(
                f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                f"{token_quote_mint} worth of {token_info.symbol} "
                f"on {token_info.platform.value}..."
            )
            token_key = str(token_info.mint)
            if all(
                str(pending.mint) != token_key
                for pending in self._pending_recovery_tokens
            ):
                self._pending_recovery_tokens.append(token_info)
                self._write_recovery_journal()
            buy_result: TradeResult = await self.buyer.execute(token_info)
            if buy_result.success:
                await self._handle_successful_buy(token_info, buy_result)
                handled = True
            else:
                handled = await self._handle_failed_buy(token_info, buy_result)
            self._pending_recovery_tokens = [
                pending
                for pending in self._pending_recovery_tokens
                if str(pending.mint) != token_key
            ]
            self._write_recovery_journal()
            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)
            return handled

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")
            raise

    async def _handle_successful_buy(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        *,
        replace_unresolved: bool = False,
    ) -> None:
        """Journal a confirmed holding before starting any exit monitor."""
        if (
            buy_result.amount is None
            or buy_result.amount <= 0
            or buy_result.price is None
            or buy_result.price <= 0
        ):
            raise ValueError("Successful buy result is missing receipt accounting")
        self._log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
        )
        take_profit = (
            self.take_profit_percentage if self.exit_strategy == "tp_sl" else None
        )
        stop_loss = self.stop_loss_percentage if self.exit_strategy == "tp_sl" else None
        if self.exit_strategy == "tp_sl":
            max_hold_time = self.max_hold_time
        elif self.exit_strategy == "time_based":
            max_hold_time = self.wait_time_after_buy
        else:
            max_hold_time = None
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            take_profit_percentage=take_profit,
            stop_loss_percentage=stop_loss,
            max_hold_time=max_hold_time,
            quantity_raw=buy_result.amount_raw,
            quote_amount_raw=buy_result.quote_amount_raw,
            account_balance_baseline_raw=(buy_result.account_balance_baseline_raw),
            position_id=buy_result.tx_signature
            or f"{token_info.platform.value}:{token_info.mint}",
        )
        token_key = str(token_info.mint)
        unresolved_record = (
            self._unresolved_buys.pop(token_key, None) if replace_unresolved else None
        )
        try:
            self._persist_position(token_info, position)
        except Exception:
            self._active_positions.pop(token_key, None)
            if unresolved_record is not None:
                self._unresolved_buys[token_key] = unresolved_record
            raise
        logger.info(f"Journaled active position: {position}")
        if self.marry_mode or self.exit_strategy == "manual":
            logger.info("Position retained for manual exit")
            return
        self._schedule_position_monitor(token_info, position)

    async def _handle_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> bool:
        """Keep unknown buys unresolved; clean up only terminal failures."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        if (
            buy_result.unresolved
            or buy_result.status == TransactionStatus.SUCCESS.value
        ):
            if not buy_result.tx_signature:
                raise RuntimeError("Unresolved buy has no transaction signature")
            token_key = str(token_info.mint)
            self._unresolved_buys[token_key] = {
                "token": token_info,
                "signature": buy_result.tx_signature,
                "baseline_raw": buy_result.account_balance_baseline_raw,
            }
            self._reserved_mints.add(token_key)
            self._write_recovery_journal()
            logger.warning(
                f"Buy outcome unresolved for {token_info.symbol}; "
                "cleanup and duplicate submission are blocked"
            )
            return False

        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )
        return True

    async def _reconcile_unresolved_buys(self) -> None:
        """Resolve prior signatures without submitting a duplicate buy."""
        while not self._shutdown_event.is_set():
            for token_key, record in tuple(self._unresolved_buys.items()):
                try:
                    signature = record["signature"]
                    outcome = await self.solana_client.confirm_transaction_outcome(
                        signature
                    )
                    if outcome.status is TransactionStatus.UNKNOWN:
                        continue
                    if outcome.status is not TransactionStatus.SUCCESS:
                        self._unresolved_buys.pop(token_key, None)
                        self._reserved_mints.discard(token_key)
                        self.processed_tokens.add(token_key)
                        self._write_recovery_journal()
                        continue

                    token_info = record["token"]
                    quote_mint = normalize_quote_mint(token_info.quote_mint)
                    quote_destinations: list[Pubkey] | None = None
                    if is_sol_paired(quote_mint):
                        durable_destinations = await self.solana_client.get_submission_receipt_destinations(
                            signature
                        )
                        if not durable_destinations:
                            if not record.get("missing_receipt_context_logged"):
                                logger.error(
                                    "Cannot reconcile native buy %s without exact "
                                    "durable receipt destinations",
                                    signature,
                                )
                                record["missing_receipt_context_logged"] = True
                            continue
                        destination = durable_destinations[0]
                        quote_destinations = list(durable_destinations[1:])
                    else:
                        implementations = get_platform_implementations(
                            token_info.platform, self.solana_client
                        )
                        destination = self.buyer._get_sol_destination(
                            token_info, implementations.address_provider
                        )
                    (
                        tokens_raw,
                        quote_spent_raw,
                    ) = await self.solana_client.get_buy_transaction_details(
                        signature,
                        token_info.mint,
                        destination,
                        quote_mint=quote_mint,
                        quote_destinations=quote_destinations,
                    )
                    if (
                        isinstance(tokens_raw, bool)
                        or not isinstance(tokens_raw, int)
                        or tokens_raw <= 0
                        or isinstance(quote_spent_raw, bool)
                        or not isinstance(quote_spent_raw, int)
                        or quote_spent_raw <= 0
                    ):
                        continue
                    base_decimals = token_info.base_decimals
                    if base_decimals is None:
                        if token_info.platform is not Platform.PUMP_FUN:
                            raise ValueError(
                                "Cannot reconcile position without base decimals"
                            )
                        base_decimals = TOKEN_DECIMALS
                    if (
                        isinstance(base_decimals, bool)
                        or not isinstance(base_decimals, int)
                        or not 0 <= base_decimals <= 18
                    ):
                        raise ValueError("Invalid base decimals in recovery token")
                    token_info.base_decimals = base_decimals
                    token_amount = tokens_raw / 10**base_decimals
                    quote_unit = quote_units_per_token(quote_mint)
                    average_price = (quote_spent_raw / quote_unit) / token_amount
                    await self._handle_successful_buy(
                        token_info,
                        TradeResult(
                            success=True,
                            platform=token_info.platform,
                            tx_signature=signature,
                            amount=token_amount,
                            price=average_price,
                            amount_raw=tokens_raw,
                            quote_amount_raw=quote_spent_raw,
                            account_balance_baseline_raw=record.get("baseline_raw"),
                            slot=outcome.slot,
                            status=outcome.status.value,
                        ),
                        replace_unresolved=True,
                    )
                    self.processed_tokens.add(token_key)
                except Exception:
                    logger.exception(
                        f"Failed to reconcile unresolved buy for {token_key}"
                    )
                    raise
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=max(1, self.price_check_interval),
                )
            except TimeoutError:
                pass

    async def _sleep_until_shutdown(self, seconds: float) -> bool:
        """Sleep interruptibly, returning true when shutdown was requested."""
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor until confirmed exit; unknown sells are reconciled in place."""
        pool_address = self._get_pool_address(token_info)
        curve_manager = self.platform_implementations.curve_manager
        exit_sell_attempts = 0

        while position.is_active and not self._shutdown_event.is_set():
            try:
                if position.pending_exit_signature is not None:
                    outcome = await self.solana_client.confirm_transaction_outcome(
                        position.pending_exit_signature
                    )
                    if outcome.status is TransactionStatus.UNKNOWN:
                        await self._sleep_until_shutdown(self.price_check_interval)
                        continue
                    if outcome.status is TransactionStatus.SUCCESS:
                        exit_reason = position.pending_exit_reason or ExitReason.MANUAL
                        exit_price = position.pending_exit_price or position.entry_price
                        pending_signature = position.pending_exit_signature
                        sold_raw = (
                            position.quantity_raw
                            if (
                                position.account_balance_baseline_raw is not None
                                and token_info.token_program_id is not None
                            )
                            else None
                        )
                        staged_cleanup = (
                            stage_cleanup_after_sell(
                                self.solana_client,
                                self.wallet,
                                token_info.mint,
                                token_info.token_program_id,
                                self.priority_fee_manager,
                                self.cleanup_mode,
                                self.cleanup_with_priority_fee,
                                self.cleanup_force_close_with_burn,
                                sold_raw,
                            )
                            if sold_raw is not None
                            else None
                        )
                        position.close_position(exit_price, exit_reason)
                        self._log_trade(
                            "sell",
                            token_info,
                            exit_price,
                            position.quantity,
                            pending_signature,
                        )
                        self._remove_position(token_info.mint)
                        await handle_cleanup_after_sell(
                            self.solana_client,
                            self.wallet,
                            token_info.mint,
                            token_info.token_program_id,
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn,
                            confirmed_sold_raw=(
                                sold_raw if staged_cleanup is None else None
                            ),
                            staged_manager=staged_cleanup,
                        )
                        break
                    position.clear_pending_exit()
                    self._persist_position(token_info, position)

                current_price = await curve_manager.calculate_price(pool_address)
                if current_price <= 0:
                    raise ValueError("Platform returned an invalid current price")

                should_exit, exit_reason = position.should_exit(current_price)
                if should_exit and exit_reason:
                    exit_sell_attempts += 1
                    if position.pending_exit_intent_id is None:
                        attempt_sequence = position.next_exit_attempt()
                        sell_intent_id = (
                            f"sell:{position.position_id}:{attempt_sequence}"
                        )
                        position.mark_exit_intent(
                            sell_intent_id,
                            exit_reason,
                            current_price,
                        )
                        self._persist_position(token_info, position)
                    else:
                        sell_intent_id = position.pending_exit_intent_id
                        exit_reason = position.pending_exit_reason or exit_reason
                    sell_task = asyncio.create_task(
                        self.seller.execute(
                            token_info,
                            token_amount=position.quantity,
                            token_price=current_price,
                            token_amount_raw=position.quantity_raw,
                            intent_id=sell_intent_id,
                        )
                    )
                    sell_cancellation: asyncio.CancelledError | None = None
                    while True:
                        try:
                            sell_result = await asyncio.shield(sell_task)
                            break
                        except asyncio.CancelledError as exc:
                            if sell_cancellation is None:
                                sell_cancellation = exc
                            if sell_task.done():
                                sell_result = await sell_task
                                break
                    if sell_result.success:
                        exit_price = sell_result.price or current_price
                        sold_raw = (
                            sell_result.amount_raw
                            if (
                                position.account_balance_baseline_raw is not None
                                and token_info.token_program_id is not None
                            )
                            else None
                        )
                        staged_cleanup = (
                            stage_cleanup_after_sell(
                                self.solana_client,
                                self.wallet,
                                token_info.mint,
                                token_info.token_program_id,
                                self.priority_fee_manager,
                                self.cleanup_mode,
                                self.cleanup_with_priority_fee,
                                self.cleanup_force_close_with_burn,
                                sold_raw,
                            )
                            if sold_raw is not None
                            else None
                        )
                        position.close_position(exit_price, exit_reason)
                        self._log_trade(
                            "sell",
                            token_info,
                            exit_price,
                            sell_result.amount or position.quantity,
                            sell_result.tx_signature,
                        )
                        self._remove_position(token_info.mint)
                        await handle_cleanup_after_sell(
                            self.solana_client,
                            self.wallet,
                            token_info.mint,
                            token_info.token_program_id,
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn,
                            confirmed_sold_raw=(
                                sold_raw if staged_cleanup is None else None
                            ),
                            staged_manager=staged_cleanup,
                        )
                        if sell_cancellation is not None:
                            raise sell_cancellation
                        break
                    if sell_result.unresolved and sell_result.tx_signature:
                        position.mark_exit_pending(
                            sell_result.tx_signature, exit_reason
                        )
                        self._persist_position(token_info, position)
                        logger.warning(
                            f"Sell outcome unresolved for {token_info.symbol}; "
                            "monitor will reconcile the same signature"
                        )
                    else:
                        position.clear_pending_exit()
                        self._persist_position(token_info, position)
                        logger.error(
                            f"Exit sell failed ({exit_sell_attempts}/"
                            f"{self.max_exit_sell_attempts}): "
                            f"{sell_result.error_message}"
                        )
                        if sell_cancellation is not None:
                            raise sell_cancellation
                        if exit_sell_attempts >= self.max_exit_sell_attempts:
                            logger.error(
                                f"Exit burst exhausted for {token_info.symbol}; "
                                "position remains journaled and monitored"
                            )
                            exit_sell_attempts = 0
                            if await self._sleep_until_shutdown(
                                max(30, self.price_check_interval)
                            ):
                                break
                            continue
                    if sell_cancellation is not None:
                        raise sell_cancellation
                else:
                    exit_sell_attempts = 0

                if await self._sleep_until_shutdown(self.price_check_interval):
                    break
            except Exception:
                logger.exception(f"Fatal error monitoring position {token_info.symbol}")
                raise

    def _get_pool_address(self, token_info: TokenInfo) -> Pubkey:
        """Get the pool/curve address for price monitoring using platform-agnostic method."""
        address_provider = self.platform_implementations.address_provider

        # Use platform-specific logic to get the appropriate address
        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            return token_info.bonding_curve
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            return token_info.pool_state
        else:
            # Fallback to deriving the address using platform provider
            return address_provider.derive_pool_address(token_info.mint)

    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token information to a file."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            file_path = trades_dir / f"{token_info.mint}.txt"

            # Convert to dictionary for saving - platform-agnostic
            token_dict = {
                "name": token_info.name,
                "symbol": token_info.symbol,
                "uri": token_info.uri,
                "mint": str(token_info.mint),
                "platform": token_info.platform.value,
                "user": str(token_info.user) if token_info.user else None,
                "creator": str(token_info.creator) if token_info.creator else None,
                "creation_timestamp": token_info.creation_timestamp,
            }

            # Add platform-specific fields only if they exist
            platform_fields = {
                "bonding_curve": token_info.bonding_curve,
                "associated_bonding_curve": token_info.associated_bonding_curve,
                "creator_vault": token_info.creator_vault,
                "pool_state": token_info.pool_state,
                "base_vault": token_info.base_vault,
                "quote_vault": token_info.quote_vault,
            }

            for field_name, field_value in platform_fields.items():
                if field_value is not None:
                    token_dict[field_name] = str(field_value)

            file_path.write_text(json.dumps(token_dict, indent=2))

            logger.info(f"Token information saved to {file_path}")
        except OSError:
            logger.exception("Failed to save token information")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
    ) -> None:
        """Log trade information."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            log_entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "action": action,
                "platform": token_info.platform.value,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

            log_file_path = trades_dir / "trades.log"
            with log_file_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except OSError:
            logger.exception("Failed to log trade information")


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility

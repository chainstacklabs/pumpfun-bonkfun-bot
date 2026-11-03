"""Universal trading coordinator that works with any platform.

The trader wires three parts together and owns the session around them:
`TokenIntake` finds coins, the buyer and seller in `platform_aware` trade them,
and `PositionManager` holds each one until its exit strategy closes it. Coins
are bought one after another; each position then runs as its own task, and the
next coin is taken once fewer than `max_open_positions` are open. Positions a
previous run left open are resumed first and count toward that limit.
"""

import asyncio
import sys

from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    WSOL_MINT,
    normalize_quote_mint,
    quote_decimals,
    resolve_quote_amounts,
    resolve_quote_mint,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from platforms import get_platform_implementations
from platforms.launchlab_presets import resolve_platform_configs
from trading import trade_log
from trading.base import TradeResult
from trading.intake import TokenIntake
from trading.migration import MigrationResolver
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position_manager import PositionManager
from trading.position_store import PositionStore
from trading.settings import TraderSettings, entry_error
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


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    #: Seconds shutdown cleanup may take before the process exits regardless.
    #: Cleanup settles for 15s per account before reading it, so this has to
    #: cover a session's worth of traded coins, not one.
    _CLEANUP_SHUTDOWN_BUDGET = 120.0

    def __init__(self, settings: TraderSettings) -> None:
        """Initialize the universal trader.

        Raises:
            ValueError: If `entry` is unknown, or is "migration" with
                `extreme_fast_mode` on
        """
        error = entry_error(
            settings.entry, extreme_fast_mode=settings.extreme_fast_mode
        )
        if error:
            raise ValueError(error)
        self.settings = settings
        self.entry = settings.entry
        # Core components
        self.solana_client = SolanaClient(
            settings.rpc_endpoint, max_rps=settings.max_rps
        )
        self.wallet = Wallet(settings.private_key)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=settings.enable_dynamic_priority_fee,
            enable_fixed_fee=settings.enable_fixed_priority_fee,
            fixed_fee=settings.fixed_priority_fee,
            extra_fee=settings.extra_priority_fee,
            hard_cap=settings.hard_cap_prior_fee,
        )

        # Platform setup
        if isinstance(settings.platform, str):
            self.platform = Platform(settings.platform)
        else:
            self.platform = settings.platform

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
        compute_units = settings.compute_units or {}
        self.quote_amounts, allowed_quote_mints = _resolve_quote_config(
            settings.buy_amount, settings.quote_amounts, settings.allowed_quote_mints
        )

        # Execution: platform-aware buyer and seller
        self.buyer, self.seller = (
            PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                settings.buy_amount,
                settings.buy_slippage,
                settings.max_retries,
                settings.extreme_fast_token_amount,
                settings.extreme_fast_mode,
                compute_units=compute_units,
                quote_amounts=self.quote_amounts,
                curve_refresh_budget=settings.curve_refresh_budget,
                trust_create_event=settings.trust_create_event,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                settings.sell_slippage,
                settings.max_retries,
                compute_units=compute_units,
            ),
        )

        # Intake: the listener, filtered to our platform, and the token queue
        self.intake = TokenIntake(
            settings,
            ListenerFactory.create_listener(
                listener_type=settings.listener_type,
                wss_endpoint=settings.wss_endpoint,
                geyser_endpoint=settings.geyser_endpoint,
                geyser_api_token=settings.geyser_api_token,
                geyser_auth_type=settings.geyser_auth_type,
                platforms=[self.platform],  # Only listen for our platform
                platform_configs=resolve_platform_configs(
                    self.platform, settings.platform_configs
                ),
            ),
            self.quote_amounts,
            allowed_quote_mints,
            pool_resolver=(
                MigrationResolver(self.solana_client)
                if self.entry == "migration"
                else None
            ),
        )

        self.positions = PositionManager(
            settings,
            seller=self.seller,
            solana_client=self.solana_client,
            wallet=self.wallet,
            priority_fee_manager=self.priority_fee_manager,
            platform_implementations=self.platform_implementations,
            store=(
                PositionStore(settings.positions_file)
                if settings.positions_file
                else None
            ),
        )

        self.extreme_fast_mode = settings.extreme_fast_mode
        self.wait_time_after_creation = settings.wait_time_after_creation
        self.wait_time_before_new_token = settings.wait_time_before_new_token
        self.yolo_mode = settings.yolo_mode

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[
            str, Pubkey
        ] = {}  # Maps mint (as string) to token_program_id

    async def _resolve_quote_token_programs(self) -> None:
        """Warm the quote-mint token-program and decimals caches at startup.

        `extreme_fast_mode` submits a buy with zero RPC calls between detection
        and submission, so a quote mint's token program -- and its decimals,
        which size `max_sol_cost`/`min_sol_output` -- must already be known. The
        set of quote mints is fixed at startup, so both facts are cached here
        from the one mint-account fetch `resolve_quote_token_program` makes; the
        hot path only reads those caches.

        Raises:
            ValueError: If a configured quote mint's owner is neither SPL Token
                nor Token-2022, or its decimals cannot be read. Left uncaught
                deliberately: trading a quote mint the bot cannot size is worse
                than refusing to start.
        """
        for quote_mint in self.quote_amounts:
            token_program = await resolve_quote_token_program(
                quote_mint, self.solana_client.get_account_info
            )
            logger.info(
                f"Resolved quote mint {quote_mint} -> token program "
                f"{token_program}, decimals {quote_decimals(quote_mint)}"
            )

    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        intake, positions = self.intake, self.positions
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        logger.info(f"Entry: {self.entry}")
        logger.info(
            f"Match filter: {intake.match_string if intake.match_string else 'None'}"
        )
        logger.info(
            f"Creator filter: {intake.bro_address if intake.bro_address else 'None'}"
        )
        logger.info(f"Marry mode: {positions.marry_mode}")
        logger.info(f"YOLO mode: {self.yolo_mode}")
        logger.info(f"Exit strategy: {positions.exit_strategy}")
        logger.info(f"Max open positions: {positions.max_open_positions}")

        if positions.exit_strategy == "tp_sl":
            logger.info(
                f"Take profit: {positions.take_profit_percentage * 100 if positions.take_profit_percentage else 'None'}%"
            )
            logger.info(
                f"Stop loss: {positions.stop_loss_percentage * 100 if positions.stop_loss_percentage else 'None'}%"
            )
            logger.info(
                f"Max hold time: {positions.max_hold_time if positions.max_hold_time else 'None'} seconds"
            )
            logger.info(f"Max exit sell attempts: {positions.max_exit_sell_attempts}")

        logger.info(f"Max token age: {intake.max_token_age} seconds")

        try:
            health_resp = await self.solana_client.get_health()
            logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
        except Exception as e:
            logger.warning(f"RPC warm-up failed: {e!s}")

        # Outside the try/except above: an RPC health check is best-effort, but a
        # quote mint that cannot be resolved to a token program derives a
        # non-existent ATA and reverts every trade -- fail startup instead.
        await self._resolve_quote_token_programs()

        try:
            for token_info in await positions.resume():
                self._track_traded(token_info)
                # Held already: the intake must not buy it again, e.g. when a
                # coin resumed from its curve migrates under `entry: migration`.
                intake.processed_tokens.add(str(token_info.mint))
            # Resumed positions hold slots like any other.
            await positions.wait_for_free_slot()

            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                token_info = await (
                    intake.wait_for_migrated_token()
                    if self.entry == "migration"
                    else intake.wait_for_token()
                )
                if token_info:
                    await self._handle_token(token_info)
                    await positions.wait_all()
                    logger.info("Finished processing single token. Exiting...")
                else:
                    # Resumed positions still run to their exit.
                    await positions.wait_all()
                    logger.info(
                        f"No suitable token found within timeout period ({intake.token_wait_timeout}s). Exiting..."
                    )
            else:
                # Continuous mode: process tokens until interrupted
                logger.info(
                    "Running in continuous mode - will process tokens until interrupted"
                )
                if self.entry == "migration":
                    await intake.run_migrations(self._handle_token)
                else:
                    await intake.run(self._handle_token)
                # The listener stopped: positions still open run to their exit
                # before cleanup.
                await positions.wait_all()

        except Exception:
            logger.exception("Trading stopped due to error")

        finally:
            # Shutdown or an error: stop what is still open and wait for each
            # position task to end, so cleanup never races a sell.
            await positions.stop()
            await self._cleanup_without_interruption()
            logger.info("Universal Trader has shut down")

    async def _cleanup_without_interruption(self) -> None:
        """Run cleanup to completion even though shutdown arrives as a cancellation.

        Shutdown reaches this coroutine as a cancellation, and `_cleanup_resources`
        awaits once per account — a settle wait, then an RPC read. Awaiting it
        directly meant the first of those re-raised `CancelledError` and every
        account after it kept its rent, silently: the run ends without even
        logging that it shut down. Running it as its own task and absorbing the
        cancellation lets it finish.

        Bounded by `_CLEANUP_SHUTDOWN_BUDGET` so a stuck RPC cannot hold the
        process open. Rent left behind when the budget expires is not lost — it
        is reclaimed by `tools/cleanup_accounts.py <MINT>` — so the warning names
        that rather than retrying here.
        """
        task = asyncio.create_task(self._cleanup_resources())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._CLEANUP_SHUTDOWN_BUDGET

        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                task.cancel()
                logger.warning(
                    f"Cleanup did not finish within {self._CLEANUP_SHUTDOWN_BUDGET}s. "
                    f"Accounts still open keep their rent; reclaim it with "
                    f"tools/cleanup_accounts.py <MINT>."
                )
                break
            try:
                # asyncio.wait neither cancels the task on timeout nor re-raises
                # what it failed with, so the loop owns both outcomes.
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # The cancellation that started this shutdown, arriving while
                # cleanup runs. Absorbing it is the point: this is already the
                # terminal path, and re-raising would strand the rent that the
                # remaining accounts hold.
                continue

        if task.done() and not task.cancelled() and task.exception() is not None:
            logger.error("Cleanup failed", exc_info=task.exception())

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        settings = self.settings
        if self.traded_mints:
            try:
                logger.info(f"Cleaning up {len(self.traded_mints)} traded token(s)...")
                # Build parallel lists of mints and token_program_ids
                mints_list = list(self.traded_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    settings.cleanup_mode,
                    settings.cleanup_with_priority_fee,
                    settings.cleanup_force_close_with_burn,
                )
            except Exception:
                logger.exception("Error during cleanup")

        self.intake.forget_unprocessed()

        await self.solana_client.close()

    async def _handle_token(self, token_info: TokenInfo) -> None:
        """Handle a new token creation event."""
        try:
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return

            skip_reason = self.intake.skip_reason(token_info)
            if skip_reason:
                logger.info(f"Skipping {token_info.symbol} - {skip_reason}")
                return
            token_quote_mint = normalize_quote_mint(token_info.quote_mint)

            # Wait for pool/curve to stabilize (unless in extreme fast mode)
            if not self.extreme_fast_mode:
                trade_log.save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)

            # Buy token
            logger.info(
                f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                f"{token_quote_mint} worth of {token_info.symbol} "
                f"on {token_info.platform.value}..."
            )
            buy_result: TradeResult = await self.buyer.execute(token_info)

            if buy_result.success:
                await self._handle_successful_buy(token_info, buy_result)
            else:
                await self._handle_failed_buy(token_info, buy_result)

            # The next coin is taken once a position slot is free: with the
            # default of one, once this position has exited.
            await self.positions.wait_for_free_slot()

            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")

    async def _handle_successful_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle successful token purchase."""
        logger.info(
            f"Successfully bought {token_info.symbol} on {token_info.platform.value}"
        )
        trade_log.log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
        )
        self._track_traded(token_info)
        self.positions.open(token_info, buy_result)

    def _track_traded(self, token_info: TokenInfo) -> None:
        """Remember a coin held this session, for post-session cleanup."""
        self.traded_mints.add(token_info.mint)
        if token_info.token_program_id:
            self.traded_token_programs[str(token_info.mint)] = (
                token_info.token_program_id
            )

    async def _handle_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle failed token purchase."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        settings = self.settings
        # Close ATA if enabled
        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            settings.cleanup_mode,
            settings.cleanup_with_priority_fee,
            settings.cleanup_force_close_with_burn,
        )


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility

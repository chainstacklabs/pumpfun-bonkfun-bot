"""Position manager: holds a bought coin until its exit strategy closes it.

Monitoring, the exit strategies, bounded exit retries and post-exit cleanup.
Each position runs as its own task, up to `trade.max_open_positions` at once.
With a `PositionStore`, each monitored position is on disk from its buy until
its sell, and `resume` picks those up again after a restart.
"""

import asyncio
import struct
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from cleanup.modes import handle_cleanup_after_sell
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    normalize_quote_mint,
    quote_symbol,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from interfaces.core import (
    ConfirmationStatus,
    CurveGraduatedError,
    TokenInfo,
    TradeFailureReason,
)
from platforms import PlatformImplementations
from trading import trade_log
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareSeller
from trading.position import ExitReason, Position
from trading.position_store import PositionStore, StoredPosition
from trading.settings import TraderSettings
from utils.logger import get_logger

if TYPE_CHECKING:
    from solders.pubkey import Pubkey

logger = get_logger(__name__)


def _utc_now() -> datetime:
    """Naive UTC, the form `Position.entry_time` uses."""
    return datetime.now(UTC).replace(tzinfo=None)


class ExitSellVerdict(Enum):
    """What to do after an exit sell came back unsuccessful.

    An exit sell is not idempotent, so "it did not succeed" is not enough to act
    on: a reverted sell changed nothing and should be retried, while one whose
    confirmation never arrived may already have emptied the position.

    `_classify_failed_exit_sell` only returns the three failure verdicts; SOLD is
    reported by the attempt itself.
    """

    SOLD = "sold"  # confirmed on this attempt, the ordinary happy path
    LATE_SUCCESS = "late_success"  # it landed after all — stop, do not resell
    RETRY = "retry"  # provably nothing happened on chain
    STOP = "stop"  # still unknown — a blind resell is the worse risk


def _exit_sell_verdict_for(reason: TradeFailureReason | None) -> ExitSellVerdict | None:
    """Map a failure reason to a verdict, where one can be decided offline.

    Args:
        reason: The reported failure reason, or None if the seller did not set
            one — which means "unknown", never "reverted".

    Returns:
        The verdict, or None when the signature has to be re-checked first.
    """
    if reason is TradeFailureReason.REVERTED:
        # The program errored and the tokens are still held.
        return ExitSellVerdict.RETRY
    if reason is TradeFailureReason.SUBMIT_FAILED:
        # Nothing was confirmed as sent and there is no signature to re-check.
        # Stranding the position is the worse of the two risks.
        return ExitSellVerdict.RETRY
    return None


class PositionManager:
    """Runs the configured exit strategy for each coin the buyer bought."""

    #: Seconds `stop` waits for the cancelled position tasks to end.
    _STOP_BUDGET = 30.0
    #: Where positions are kept across restarts; None keeps them in memory only.
    store: PositionStore | None = None

    def __init__(  # noqa: PLR0913
        self,
        settings: TraderSettings,
        *,
        seller: PlatformAwareSeller,
        solana_client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        platform_implementations: PlatformImplementations,
        store: PositionStore | None = None,
    ) -> None:
        """Set up the manager; without a `store` nothing is written to disk."""
        self.seller = seller
        self.solana_client = solana_client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.platform_implementations = platform_implementations

        self.exit_strategy = settings.exit_strategy.lower()
        self.take_profit_percentage = settings.take_profit_percentage
        self.stop_loss_percentage = settings.stop_loss_percentage
        self.max_hold_time = settings.max_hold_time
        # The attempt cap is clamped: below 1 would mean "never try to sell".
        self.price_check_interval, self.max_exit_sell_attempts = (
            settings.price_check_interval,
            max(1, settings.max_exit_sell_attempts),
        )
        self.wait_time_after_buy = settings.wait_time_after_buy
        self.marry_mode = settings.marry_mode

        self.cleanup_mode = settings.cleanup_mode
        self.cleanup_force_close_with_burn = settings.cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = settings.cleanup_with_priority_fee

        # Coins whose graduation has been logged, so it is said once.
        self._graduated_mints: set[Pubkey] = set()

        # Clamped like the attempt cap: below 1 no coin would ever be taken.
        self.max_open_positions = max(1, settings.max_open_positions)
        # One task per position still running its exit strategy.
        self._open: set[asyncio.Task] = set()
        self.store = store

    @property
    def monitors(self) -> bool:
        """Whether positions are held to an exit; not under manual or marry mode."""
        return not self.marry_mode and self.exit_strategy in ("tp_sl", "time_based")

    @property
    def open_count(self) -> int:
        """Positions whose exit strategy is still running."""
        return sum(not task.done() for task in self._open)

    def open(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime | None = None,
    ) -> asyncio.Task:
        """Start holding a bought coin in its own task and return at once.

        A coin bought just now (`entry_time` None) is written to the store
        first, when its position is monitored. The task never raises: an error
        in the exit strategy is logged and ends that position only.

        Args:
            entry_time: When a resumed position was bought, naive UTC
        """
        if entry_time is None and self.store is not None and self.monitors:
            self.store.add(token_info, buy_result, _utc_now())
        task = asyncio.create_task(
            self._hold(token_info, buy_result, entry_time),
            name=f"position-{token_info.mint}",
        )
        self._open.add(task)
        task.add_done_callback(self._open.discard)
        return task

    async def _hold(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime | None,
    ) -> None:
        try:
            await self.manage(token_info, buy_result, entry_time=entry_time)
        except Exception:
            logger.exception(f"Error managing position in {token_info.symbol}")

    async def wait_for_free_slot(self) -> None:
        """Return once fewer than `max_open_positions` positions are open."""
        if self.open_count >= self.max_open_positions > 1:
            logger.info(
                f"{self.open_count} positions open (max_open_positions "
                f"{self.max_open_positions}); waiting for one to close"
            )
        while self.open_count >= self.max_open_positions:
            await asyncio.wait(set(self._open), return_when=asyncio.FIRST_COMPLETED)

    async def wait_all(self) -> None:
        """Wait until every open position has run its exit strategy to the end."""
        while self.open_count:
            await asyncio.wait(set(self._open))

    async def stop(self) -> None:
        """Stop every open position and wait until each task has ended.

        Shutdown cleanup closes token accounts, burning what is left with
        `force_close_with_burn`, so it must not start while a position may
        still sell. Runs to the end through a cancellation of its caller, bounded
        by `_STOP_BUDGET`; the coins stay in the wallet.
        """
        tasks = {task for task in self._open if not task.done()}
        if not tasks:
            return
        logger.info(
            f"Stopping {len(tasks)} open position(s); their tokens stay in the wallet"
        )
        for task in tasks:
            task.cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._STOP_BUDGET
        while not all(task.done() for task in tasks):
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    f"{sum(not t.done() for t in tasks)} position task(s) did not "
                    f"stop within {self._STOP_BUDGET}s"
                )
                return
            try:
                await asyncio.wait(tasks, timeout=remaining)
            except asyncio.CancelledError:
                # The shutdown arriving again; the tasks are already cancelled
                # and cleanup still has to run after them.
                continue

    async def resume(self) -> list[TokenInfo]:
        """Hold again each position a previous run left in the store.

        The wallet is the truth: a position whose coin the wallet no longer
        holds was sold (or moved) while the bot was down and is cleared, never
        sold again; one holding less than was bought sells what is left. A
        position whose balance or quote mint cannot be read stays in the file
        and is not monitored this run.

        Returns:
            The coins now held again, each in its own task
        """
        if self.store is None:
            return []
        stored = self.store.load()
        if stored and not self.monitors:
            logger.warning(
                f"{len(stored)} position(s) in {self.store.path} are not resumed: "
                f"exit strategy {self.exit_strategy}"
                f"{' with marry mode' if self.marry_mode else ''} sells nothing"
            )
            return []
        resumed = []
        for position in stored:
            if await self._resume_one(position):
                resumed.append(position.token_info)
        return resumed

    async def _resume_one(self, stored: StoredPosition) -> bool:
        token_info, buy = stored.token_info, stored.buy_result
        label = f"{token_info.symbol} ({token_info.mint})"
        held = await self._held_raw(token_info)
        if held is None:
            logger.error(
                f"Could not read the wallet's balance of {label}; its position "
                f"stays in {self.store.path} and is not monitored this run"
            )
            return False
        if held == 0:
            logger.info(
                f"Clearing position in {label}: the wallet no longer holds it, "
                f"so it was sold or moved while the bot was down"
            )
            self.store.remove(token_info.mint)
            return False
        try:
            await resolve_quote_token_program(
                normalize_quote_mint(token_info.quote_mint),
                self.solana_client.get_account_info,
            )
        except Exception:
            logger.exception(
                f"Could not resolve the quote mint of {label}; its position "
                f"stays in {self.store.path} and is not monitored this run"
            )
            return False

        bought_raw = (
            buy.amount_raw
            if buy.amount_raw is not None
            else round(buy.amount * 10**TOKEN_DECIMALS)
        )
        if held < bought_raw:
            logger.warning(
                f"The wallet holds {held} raw of {label}, less than the "
                f"{bought_raw} bought; the position sells what is held"
            )
            buy = TradeResult(
                success=True,
                platform=buy.platform,
                tx_signature=buy.tx_signature,
                amount=held / 10**TOKEN_DECIMALS,
                amount_raw=held,
                price=buy.price,
            )
        logger.info(
            f"Resuming position in {label}: {buy.amount} tokens bought at "
            f"{buy.price} on {stored.entry_time.isoformat()} UTC"
        )
        self.open(token_info, buy, entry_time=stored.entry_time)
        return True

    async def _held_raw(self, token_info: TokenInfo) -> int | None:
        """Raw amount of the coin in the wallet's account; None when unreadable."""
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1.0)
            try:
                program = token_info.token_program_id
                if program is None:
                    program = (
                        await self.solana_client.get_account_info(token_info.mint)
                    ).owner
                account = self.wallet.get_associated_token_address(
                    token_info.mint, program
                )
                try:
                    data = (await self.solana_client.get_account_info(account)).data
                except ValueError:
                    return 0  # no account: nothing held
                # SPL token account: mint (32), owner (32), amount (u64).
                return struct.unpack_from("<Q", bytes(data), 64)[0]
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Balance read for {token_info.symbol} failed "
                    f"(attempt {attempt + 1}/3): {e}"
                )
        return None

    async def manage(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime | None = None,
    ) -> None:
        """Hold a bought coin until its exit strategy is done with it.

        Args:
            entry_time: When a resumed position was bought, naive UTC; the hold
                time and `wait_time_after_buy` count from it. None: bought now.
        """
        # Choose exit strategy
        if not self.marry_mode:
            if self.exit_strategy == "tp_sl":
                await self._handle_tp_sl_exit(token_info, buy_result, entry_time)
            elif self.exit_strategy == "time_based":
                await self._handle_time_based_exit(token_info, buy_result, entry_time)
            elif self.exit_strategy == "manual":
                logger.info("Manual exit strategy - position will remain open")
        else:
            logger.info("Marry mode enabled. Skipping sell operation.")

    async def _handle_tp_sl_exit(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime | None = None,
    ) -> None:
        """Handle take profit/stop loss exit strategy."""
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            quantity_raw=buy_result.amount_raw,
            take_profit_percentage=self.take_profit_percentage,
            stop_loss_percentage=self.stop_loss_percentage,
            max_hold_time=self.max_hold_time,
        )
        if entry_time is not None:
            position.entry_time = entry_time

        logger.info(f"Created position: {position}")
        quote_label = quote_symbol(normalize_quote_mint(token_info.quote_mint))
        if position.take_profit_price:
            logger.info(
                f"Take profit target: {position.take_profit_price:.8f} {quote_label}"
            )
        if position.stop_loss_price:
            logger.info(
                f"Stop loss target: {position.stop_loss_price:.8f} {quote_label}"
            )

        # Monitor position until exit condition is met
        await self._monitor_position_until_exit(token_info, position)

    async def _handle_time_based_exit(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime | None = None,
    ) -> None:
        """Handle legacy time-based exit strategy.

        Args:
            buy_result: Result from the buy operation (contains token amount)
            entry_time: When a resumed position was bought; the wait counts
                from it
        """
        wait = self.wait_time_after_buy
        if entry_time is not None:
            held_for = (_utc_now() - entry_time).total_seconds()
            wait = max(0.0, wait - held_for)
        logger.info(f"Waiting for {wait} seconds before selling...")
        await asyncio.sleep(wait)

        logger.info(f"Selling {token_info.symbol}...")
        # The first attempt prices off the buy: no price has been read since,
        # and reading one would cost an RPC call on the happy path.
        token_price = buy_result.price

        for attempt in range(1, self.max_exit_sell_attempts + 1):
            sell_result: TradeResult = await self.seller.execute(
                token_info,
                token_amount=buy_result.amount,
                token_price=token_price,
                token_amount_raw=buy_result.amount_raw,
            )

            if sell_result.success:
                logger.info(f"Successfully sold {token_info.symbol}")
                self._forget(token_info)
                trade_log.log_trade(
                    "sell",
                    token_info,
                    sell_result.price,
                    sell_result.amount,
                    sell_result.tx_signature,
                )
                # Close ATA if enabled
                await self._cleanup_after_exit(token_info)
                return

            logger.error(
                f"Failed to sell {token_info.symbol} (attempt "
                f"{attempt}/{self.max_exit_sell_attempts}): "
                f"{sell_result.error_message}"
            )

            verdict = await self._classify_failed_exit_sell(token_info, sell_result)
            if verdict is ExitSellVerdict.LATE_SUCCESS:
                # The sell landed; only its confirmation was late. Run the same
                # cleanup a first-try success would, minus the trade log: the
                # amounts were never read back.
                logger.info(f"Sell of {token_info.symbol} confirmed late")
                self._forget(token_info)
                await self._cleanup_after_exit(token_info)
                return
            if verdict is ExitSellVerdict.STOP:
                logger.error(
                    f"Stopping exit attempts for {token_info.symbol} after "
                    f"{attempt} attempt(s) - the last sell could not be "
                    f"resolved. Position stays open and is no longer monitored."
                )
                return

            if attempt >= self.max_exit_sell_attempts:
                break

            # The seller turns token_price into the slippage floor, and a revert
            # usually means that floor no longer matches the market, so re-read
            # the price rather than repeating the same unpayable ask.
            token_price = await self._current_price_or(token_info, token_price)

        logger.error(
            f"Giving up on selling {token_info.symbol} after "
            f"{self.max_exit_sell_attempts} attempts. Position stays open "
            f"and is no longer monitored - tokens are still held."
        )

    async def _classify_failed_exit_sell(
        self, token_info: TokenInfo, sell_result: TradeResult
    ) -> ExitSellVerdict:
        """Decide whether another exit sell is safe after one came back failed.

        A reverted sell changed nothing; one whose confirmation never arrived may
        already have closed the position, and resending spends a fee to sell a
        balance that is no longer there.

        A merely unconfirmed signature is re-checked first: `getTransaction`
        keeps answering long after signature statuses age out of the RPC's recent
        history, so a sell that turns out to have landed is reported as a late
        success rather than retried.

        Args:
            token_info: Token information, for logging
            sell_result: The unsuccessful result from the seller

        Returns:
            LATE_SUCCESS, RETRY, or STOP
        """
        verdict = _exit_sell_verdict_for(sell_result.failure_reason)
        if verdict is not None:
            return verdict

        signature = sell_result.tx_signature
        if not signature:
            # No signature to re-check and no reason recorded: treat as unknown.
            logger.error(
                f"Exit sell for {token_info.symbol} failed without a signature "
                f"to re-check; not sending another one"
            )
            return ExitSellVerdict.STOP

        logger.warning(
            f"Exit sell {signature[:16]}... for {token_info.symbol} was not "
            f"confirmed; re-checking before deciding whether to sell again"
        )
        try:
            status = await self.solana_client.verify_transaction_status(signature)
        except Exception:
            # The re-check is all that stands between an unresolved sell and a
            # second one, so it must not throw past the decision: in the monitor
            # loop an escape lands in the outer handler, which can call straight
            # back into another exit attempt.
            logger.exception(
                f"Could not verify exit sell {signature[:16]}...; "
                f"leaving it unresolved rather than selling again"
            )
            return ExitSellVerdict.STOP

        if status is ConfirmationStatus.SUCCESS:
            logger.info(
                f"Exit sell {signature[:16]}... did land after all — "
                f"position is closed, not retrying"
            )
            return ExitSellVerdict.LATE_SUCCESS
        if status is ConfirmationStatus.REVERTED:
            logger.warning(
                f"Exit sell {signature[:16]}... reverted on chain; "
                f"tokens are still held, retrying"
            )
            return ExitSellVerdict.RETRY

        logger.error(
            f"Exit sell {signature[:16]}... is still unconfirmed. Not sending "
            f"another sell: it may already have closed the position. Verify "
            f"this signature before acting on {token_info.symbol}."
        )
        return ExitSellVerdict.STOP

    async def _read_price(self, token_info: TokenInfo) -> float:
        """The coin's price where it trades now: its curve, or after graduation its AMM pool.

        Returns:
            Whole quote units per whole token; 0.0 for a pool with nothing in it

        Raises:
            ValueError: If a graduated coin's pool cannot be priced yet — it
                lands a moment after graduation
        """
        implementations = self.platform_implementations
        try:
            return await implementations.curve_market.get_price(token_info)
        except CurveGraduatedError:
            market = implementations.graduated_market
            if market is None:
                raise ValueError(  # noqa: TRY003
                    f"{token_info.symbol} has graduated and "
                    f"{token_info.platform.value} has no post-graduation market"
                ) from None
            if token_info.mint not in self._graduated_mints:
                self._graduated_mints.add(token_info.mint)
                logger.info(f"{token_info.symbol} has graduated; pricing off its pool")
            return await market.get_price(token_info)

    async def _current_price_or(self, token_info: TokenInfo, fallback: float) -> float:
        """Read the current price, falling back to the last known one.

        Args:
            token_info: Token information used to locate the pool/curve
            fallback: Price to keep if the read fails

        Returns:
            The freshly read price, or `fallback` if it could not be read
        """
        try:
            price = await self._read_price(token_info)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Could not re-read price for {token_info.symbol} ({e}); "
                f"retrying against {fallback:.8f}"
            )
            return fallback
        if price and price > 0:
            return price
        logger.warning(
            f"Re-read price for {token_info.symbol} was {price}; "
            f"retrying against {fallback:.8f}"
        )
        return fallback

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor a position until exit conditions are met."""
        logger.info(
            f"Starting position monitoring for {token_info.symbol} "
            f"(check interval: {self.price_check_interval}s)"
        )

        exit_sell_attempts = 0
        # Every price below is denominated in the coin's quote asset, which is
        # not always SOL.
        quote_label = quote_symbol(normalize_quote_mint(token_info.quote_mint))

        # The last price actually read, always positive. A max_hold_time exit
        # firing with the price feed down still needs a slippage floor, and
        # before the first successful read the entry price is all there is.
        last_known_price = position.entry_price

        while position.is_active:
            try:
                current_price = await self._read_price(token_info)
                # A curve with no virtual token reserves prices at 0.0 rather
                # than raising, and the seller rejects a non-positive price
                # before its own try block, so a stored 0.0 would escape the
                # bounded exit handling next time the feed went down.
                if current_price > 0:
                    last_known_price = current_price

                # Check if position should be exited
                should_exit, exit_reason = position.should_exit(current_price)

                if should_exit and exit_reason:
                    logger.info(
                        f"Exit condition met for {token_info.symbol}: "
                        f"{exit_reason.value}"
                    )
                    logger.info(f"Current price: {current_price:.8f} {quote_label}")

                    # 0.0 satisfies the stop-loss comparison, so the exit can
                    # fire on a price the sell cannot be floored against.
                    exit_price = (
                        current_price if current_price > 0 else last_known_price
                    )
                    if exit_price != current_price:
                        logger.warning(
                            f"Curve priced {token_info.symbol} at 0; flooring the "
                            f"exit sell at the last real price "
                            f"{exit_price:.8f} {quote_label}"
                        )

                    exit_sell_attempts += 1
                    if await self._run_exit_attempt(
                        token_info,
                        position,
                        exit_reason,
                        exit_price,
                        exit_sell_attempts,
                    ):
                        break
                    # Keep monitoring: the next iteration re-reads the price and
                    # retries the sell with a floor that matches the market.
                else:
                    # Log current status
                    exit_sell_attempts = 0
                    pnl = position.get_pnl(current_price)
                    logger.debug(
                        f"Position status for {token_info.symbol}: "
                        f"{current_price:.8f} {quote_label} "
                        f"({pnl['price_change_pct']:+.2f}%)"
                    )

                # Wait before next price check
                await asyncio.sleep(self.price_check_interval)

            except Exception:
                logger.exception(f"Error monitoring position in {token_info.symbol}")

                # max_hold_time is the one exit condition that needs no price,
                # and should_exit cannot be asked without one. Ask the time-only
                # question here so the deadline is enforced with the feed down.
                if position.should_exit_on_time():
                    logger.warning(
                        f"Max hold time reached for {token_info.symbol} while "
                        f"the price is unreadable - exiting against the last "
                        f"known price {last_known_price:.8f} {quote_label}"
                    )
                    exit_sell_attempts += 1
                    if await self._run_exit_attempt(
                        token_info,
                        position,
                        ExitReason.MAX_HOLD_TIME,
                        last_known_price,
                        exit_sell_attempts,
                    ):
                        break

                await asyncio.sleep(
                    self.price_check_interval
                )  # Continue monitoring despite errors

    async def _run_exit_attempt(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
        attempt: int,
    ) -> bool:
        """Make one bounded attempt to close the position.

        Args:
            position: The open position
            exit_reason: Why the exit fired
            price: Price the sell is floored against
            attempt: 1-based attempt counter, for the bound and the logs

        Returns:
            True when monitoring should stop, whether because the position is
            closed, because the outcome could not be resolved, or because the
            attempt budget is spent.
        """
        verdict = await self._attempt_position_exit(
            token_info, position, exit_reason, price
        )
        if verdict is not ExitSellVerdict.RETRY:
            return True

        if attempt >= self.max_exit_sell_attempts:
            logger.error(
                f"Giving up on exiting {token_info.symbol} after "
                f"{attempt} attempts. Position stays open "
                f"and is no longer monitored - tokens are still held."
            )
            return True
        return False

    async def _attempt_position_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
    ) -> ExitSellVerdict:
        """Sell the position once and report what came of it.

        Args:
            position: The open position, closed in place on success
            exit_reason: Why the exit fired
            price: Price the sell is floored against. The seller turns it into
                `min_quote_output`, so it must be a price the pool can pay — an
                exit fires precisely because the price left the entry price.

        Returns:
            SOLD, LATE_SUCCESS, RETRY, or STOP
        """
        # Log PnL before exit
        pnl = position.get_pnl(price)
        logger.info(
            f"Position PnL: {pnl['price_change_pct']:.2f}% "
            f"({pnl['unrealized_pnl_quote']:.6f} "
            f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))})"
        )

        sell_result = await self.seller.execute(
            token_info,
            token_amount=position.quantity,
            token_price=price,
            token_amount_raw=position.quantity_raw,
        )

        if sell_result.success:
            # Close position with actual exit price
            position.close_position(sell_result.price, exit_reason)
            self._forget(token_info)

            logger.info(
                f"Successfully exited position: {exit_reason.value} "
                f"({token_info.symbol})"
            )
            trade_log.log_trade(
                "sell",
                token_info,
                sell_result.price,
                sell_result.amount,
                sell_result.tx_signature,
            )

            final_pnl = position.get_pnl()
            logger.info(
                f"Final PnL: {final_pnl['price_change_pct']:.2f}% "
                f"({final_pnl['unrealized_pnl_quote']:.6f} "
                f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))})"
            )
            await self._cleanup_after_exit(token_info)
            return ExitSellVerdict.SOLD

        logger.error(f"Failed to exit position: {sell_result.error_message}")

        verdict = await self._classify_failed_exit_sell(token_info, sell_result)
        if verdict is ExitSellVerdict.LATE_SUCCESS:
            # The sell landed, only its confirmation was late. Close against the
            # price it was floored at; the real fill was never read back.
            position.close_position(price, exit_reason)
            self._forget(token_info)
            logger.info(f"Position for {token_info.symbol} closed by a late fill")
            await self._cleanup_after_exit(token_info)
        elif verdict is ExitSellVerdict.STOP:
            logger.error(
                f"Stopping exit attempts for {token_info.symbol} - the last "
                f"sell could not be resolved. Position stays open and is no "
                f"longer monitored."
            )
        return verdict

    def _forget(self, token_info: TokenInfo) -> None:
        """Take a sold position out of the store.

        Only a sale does: a position stopped at shutdown, or one whose exit
        gave up or could not be resolved, still holds its coin and stays for
        the next run, which checks the wallet before selling.
        """
        if self.store is not None:
            self.store.remove(token_info.mint)

    async def _cleanup_after_exit(self, token_info: TokenInfo) -> None:
        """Run the configured post-sell cleanup for a closed position.

        Args:
            token_info: Token whose account may now be closable
        """
        await handle_cleanup_after_sell(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )

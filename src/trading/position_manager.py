"""Position manager: holds a bought coin until its exit strategy closes it.

Monitoring, the exit strategies, bounded exit retries and post-exit cleanup.
Each position runs as its own task, up to `trade.max_open_positions` at once.
With a `PositionStore`, each monitored position is on disk from the moment its
buy is sent until its sell, and `resume` picks those up again after a restart.
"""

import asyncio
import dataclasses
import struct
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from enum import Enum

from solders.pubkey import Pubkey

from cleanup.modes import handle_cleanup_after_sell, handle_cleanup_cleared
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    normalize_quote_mint,
    quote_symbol,
    quote_units_per_token,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from interfaces.core import (
    ConfirmationStatus,
    CurveGraduatedError,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from platforms import PlatformImplementations
from trading import trade_log
from trading.base import PendingBuy, TradeResult
from trading.platform_aware import (
    PlatformAwareSeller,
    landed_sale,
    log_deltas,
    read_deltas,
)
from trading.pnl import (
    CostBasis,
    Realised,
    cost_basis,
    format_price,
    pnl_text,
    sale_pnl,
)
from trading.position import ExitReason, Position
from trading.position_store import PositionStore, StoredPosition
from trading.settings import TraderSettings
from utils.logger import get_logger

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
    PARTIAL = "partial"  # part of it sold; the rest is still held and monitored
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
    #: Seconds between looks at a buy sent but not resolved.
    _PENDING_RECHECK = 2.0
    #: Seconds after its send a buy with no recorded block height is taken to
    #: be past landing; a blockhash lives about 60 to 90.
    _PENDING_EXPIRY = 120.0
    #: Seconds an unresolved exit sell is waited on for its blockhash to expire
    #: before the exit gives up on it.
    _SELL_EXPIRY_WAIT = 180.0
    #: Seconds between warnings while a position waits for its first price.
    _PRICE_WARN_EVERY = 60.0
    #: Where positions are kept across restarts; None keeps them in memory only.
    #: Also a class default: verifiers build a manager without `__init__`.
    store: PositionStore | None = None
    #: Account closes running in their own task, after a sale or for a
    #: position cleared because the wallet no longer holds its coin. They hold
    #: no position slot; shutdown lets them finish rather than stopping them.
    #: Rebound, never mutated, so the class default serves a manager built
    #: without `__init__` too.
    _closes: frozenset[asyncio.Task] = frozenset()

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
        # The platform `platform_implementations` price; a stored position
        # from another one cannot be priced here.
        self.platform = Platform(settings.platform)

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

    def hold_pending(
        self, token_info: TokenInfo, sent: TradeResult, pending: PendingBuy
    ) -> None:
        """Keep a buy that was just sent in the store until it is resolved.

        A shutdown or crash before the buy confirms leaves it there, with its
        signature, and the next start resolves it (`open_pending`). `open`
        replaces it once the buy confirms; `resolve_failed_buy` drops it once
        the buy provably did not land, or resolves it in this run. Only for
        monitored positions, like `open`.

        Args:
            sent: The signature, and the expected amount and price
        """
        if self.store is not None and self.monitors:
            self.store.add(
                token_info, sent, _utc_now(), self.wallet.pubkey, pending=pending
            )

    def resolve_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> bool:
        """Settle the pending record of a buy that did not come back successful.

        A buy that reverted, or never reached the chain, holds nothing and its
        record is dropped. Any other failure may have delivered the coin: the
        record stays and a task resolves it from its signature (`open_pending`).

        Returns:
            Whether the record stays, so the coin may be held and must not be
            cleaned up as a failed buy
        """
        if self.store is None or str(token_info.mint) not in self.store.pending_mints():
            return False
        if buy_result.failure_reason in (
            TradeFailureReason.REVERTED,
            TradeFailureReason.SUBMIT_FAILED,
        ):
            self.store.remove(token_info.mint)
            return False
        logger.warning(
            f"The buy of {token_info.symbol} ({buy_result.tx_signature}) could "
            f"not be confirmed; resolving it from its signature"
        )
        stored = self.store.pending(token_info.mint)
        if stored is not None:
            self.open_pending(stored)
        return True

    def open_pending(self, stored: StoredPosition) -> asyncio.Task:
        """Resolve a buy sent but not settled, then hold it if it landed.

        Runs as a position task: it takes a slot and `stop` cancels it, which
        leaves the record pending for the next start. The signature decides:
        a landed buy is held at the amounts and price its transaction shows, a
        reverted one is cleared. One no node knows is looked at again until
        its blockhash has expired; then the wallet's balance decides, capped
        at what the buy can have delivered.
        """
        task = asyncio.create_task(
            self._hold_pending(stored), name=f"pending-{stored.token_info.mint}"
        )
        self._open.add(task)
        task.add_done_callback(self._open.discard)
        return task

    async def _hold_pending(self, stored: StoredPosition) -> None:
        token_info = stored.token_info
        try:
            buy = await self._resolve_buy(stored)
            if buy is not None:
                await self.manage(token_info, buy, entry_time=stored.entry_time)
        except Exception:
            logger.exception(f"Error resolving the buy of {token_info.symbol}")

    async def _resolve_buy(self, stored: StoredPosition) -> TradeResult | None:
        """What a pending buy delivered, or None when it holds nothing (yet).

        A landed buy is rewritten as an ordinary position and logged to the
        trade log. None leaves the record cleared, or still pending when the
        wallet's balance cannot be read.
        """
        token_info, sent = stored.token_info, stored.buy_result
        pending = stored.pending or PendingBuy()
        label = f"{token_info.symbol} ({token_info.mint})"
        signature = sent.tx_signature
        first = True
        revert: list[str] = []
        loop = asyncio.get_running_loop()
        height_warned_at: float | None = None

        def height_unreadable(error: Exception) -> None:
            # Said at once, then once every `_PRICE_WARN_EVERY` while it lasts.
            nonlocal height_warned_at
            line = (
                f"Could not read the block height to settle the buy of {label} "
                f"({error}); asking again"
            )
            now = loop.time()
            if height_warned_at is None or now - height_warned_at >= (
                self._PRICE_WARN_EVERY
            ):
                height_warned_at = now
                logger.warning(line)
            else:
                logger.debug(line)

        while True:
            # Every balance read below names the slot this decision rests on:
            # a node behind it errors, which keeps the record, rather than
            # answering "not found", which would clear it.
            outcome, slot = await self._signature_outcome(
                signature,
                first=first,
                on_revert=revert.append,
                program=self._venue_program(pending.venue),
            )
            first = False
            if outcome is ConfirmationStatus.SUCCESS:
                buy = await self._landed_buy(token_info, sent, pending, label, slot)
                break
            if outcome is ConfirmationStatus.REVERTED:
                error = f": {revert[-1]}" if revert else ""
                logger.info(
                    f"The buy of {label} ({signature}) reverted{error}; clearing it"
                )
                self.store.remove(token_info.mint)
                return None
            expired, slot = await self._past_landing(stored, height_unreadable)
            if expired:
                buy = await self._buy_from_balance(
                    token_info, sent, pending, label, slot
                )
                break
            await asyncio.sleep(self._PENDING_RECHECK)
        if buy is None:
            return None
        self.store.add(token_info, buy, stored.entry_time, self.wallet.pubkey)
        trade_log.log_trade(
            "buy",
            token_info,
            buy.price,
            buy.amount,
            signature,
            deltas=buy.deltas,
            venue=pending.venue,
        )
        return buy

    def _venue_program(self, venue: str | None) -> Pubkey | None:
        """The program of the venue a pending buy was sent to (`Market.name`).

        None for a record without one, or naming no venue of this platform.
        """
        markets = (
            self.platform_implementations.curve_market,
            self.platform_implementations.graduated_market,
        )
        for market in markets:
            if market is not None and market.name == venue:
                return market.program_id
        return None

    async def _signature_outcome(
        self,
        signature: str | None,
        *,
        first: bool,
        on_revert: Callable[[str], None] | None = None,
        program: Pubkey | None = None,
    ) -> tuple[ConfirmationStatus | None, int | None]:
        """The signature's status and slot; UNCONFIRMED when the lookup fails.

        Only the first lookup searches the whole history: a buy sent before a
        restart may have aged out of the recent status cache, while one still
        unknown after that lands, if ever, inside it.
        """
        if not signature:
            return None, None
        try:
            return await self.solana_client.get_signature_outcome(
                signature,
                search_history=first,
                on_revert=on_revert,
                program=program,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not look up {signature[:16]}...: {e}")
            return ConfirmationStatus.UNCONFIRMED, None

    async def _past_landing(
        self,
        stored: StoredPosition,
        on_height_error: Callable[[Exception], None],
    ) -> tuple[bool, int | None]:
        """Whether the buy can no longer land, and the slot that was seen at.

        A recorded block height decides: when the height cannot be read, the
        buy is taken as not expired yet and asked about again next time. The
        clock decides only for a record with no block height; the slot is then
        None.

        Args:
            on_height_error: Told of each failed height read
        """
        last_valid = stored.pending.last_valid_block_height if stored.pending else None
        if last_valid is not None:
            try:
                height, slot = await self.solana_client.get_block_height_and_slot()
            except Exception as e:  # noqa: BLE001
                on_height_error(e)
                return False, None
            return height > last_valid, slot
        held_for = (_utc_now() - stored.entry_time).total_seconds()
        return held_for > self._PENDING_EXPIRY, None

    async def _landed_buy(
        self,
        token_info: TokenInfo,
        sent: TradeResult,
        pending: PendingBuy,
        label: str,
        slot: int | None,
    ) -> TradeResult | None:
        """The amounts and fill price a landed buy's transaction shows.

        The wallet is still the truth: a coin sold or moved since (while the
        bot was down) is cleared, one partly sold is held at what is left, and
        an unreadable balance leaves the record pending. Falls back to the
        balance alone when the transaction's amounts cannot be read; the price
        is then unknown (None). The fill price is the transaction's, whatever
        is left of it.

        Args:
            slot: The slot the buy landed in; balance reads refuse a node behind it
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        deltas = await self.solana_client.get_trade_deltas(
            sent.tx_signature,
            self.wallet.pubkey,
            token_info.mint,
            quote_mint,
            pending.quote_destination,
        )
        if deltas is None or deltas.tokens_raw <= 0:
            return await self._buy_from_balance(token_info, sent, pending, label, slot)
        log_deltas("Buy", token_info, deltas)
        tokens_raw = deltas.tokens_raw
        spent = (
            deltas.venue_quote_raw
            if deltas.venue_quote_raw is not None
            else pending.spend_raw
        )
        price = None
        if spent:
            try:
                price = (
                    spent
                    / quote_units_per_token(quote_mint)
                    / (tokens_raw / 10**TOKEN_DECIMALS)
                )
            except ValueError:
                logger.warning(f"Could not price the fill of {label}")
        held = await self._held_raw(token_info, slot)
        if held is None:
            logger.error(
                f"Could not confirm the wallet's balance of {label}; its buy "
                f"{sent.tx_signature} stays pending in {self.store.path}"
            )
            return None
        if held == 0:
            logger.info(
                f"Clearing position in {label}: its buy landed but the wallet no "
                f"longer holds it, so it was sold or moved while the bot was down"
            )
            self.store.remove(token_info.mint)
            self._close_cleared(token_info)
            return None
        tokens_raw = min(tokens_raw, held)
        amount = tokens_raw / 10**TOKEN_DECIMALS
        logger.info(
            f"The buy of {label} ({sent.tx_signature}) landed: {tokens_raw} raw "
            f"at {price if price is not None else 'an unknown price'}"
        )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature=sent.tx_signature,
            amount=amount,
            amount_raw=tokens_raw,
            price=price,
            deltas=deltas,
        )

    async def _buy_from_balance(
        self,
        token_info: TokenInfo,
        sent: TradeResult,
        pending: PendingBuy,
        label: str,
        slot: int | None,
    ) -> TradeResult | None:
        """A buy known only by the wallet's balance, never more than it can hold.

        Args:
            slot: The slot the decision rests on; balance reads refuse a node
                behind it

        Returns:
            The position at the balance held, capped at what the buy can have
            delivered, with no price; None when nothing is held (the record is
            cleared) or the balance cannot be read (it stays pending)
        """
        held = await self._held_raw(token_info, slot)
        if held is None:
            logger.error(
                f"Could not confirm the wallet's balance of {label}; its buy "
                f"{sent.tx_signature} stays pending in {self.store.path}"
            )
            return None
        if held == 0:
            logger.info(
                f"Clearing position in {label}: the wallet holds none, so its "
                f"buy {sent.tx_signature} never landed"
            )
            self.store.remove(token_info.mint)
            self._close_cleared(token_info)
            return None
        cap = pending.max_amount_raw
        amount_raw = held if cap is None else min(held, cap)
        logger.info(
            f"The buy of {label} ({sent.tx_signature}) is known only by the "
            f"wallet's balance: holding {amount_raw} raw of the {held} held"
        )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature=sent.tx_signature,
            amount=amount_raw / 10**TOKEN_DECIMALS,
            amount_raw=amount_raw,
            price=None,
        )

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
            self.store.add(token_info, buy_result, _utc_now(), self.wallet.pubkey)
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
        """Wait until every open position has run its exit strategy to the end.

        Then until every account close those exits started has finished.
        """
        while self.open_count:
            await asyncio.wait(set(self._open))
        while closes := {t for t in self._closes if not t.done()}:
            await asyncio.wait(closes)

    async def stop(self) -> None:
        """Stop every open position and wait until each task has ended.

        Shutdown cleanup closes token accounts, burning what is left with
        `force_close_with_burn`, so it must not start while a position may
        still sell. A position already sold is not stopped: the close of its
        empty account runs to the end, and so does that of a position cleared
        because the wallet no longer holds its coin. Runs to the end through a
        cancellation of its caller, bounded by `_STOP_BUDGET`; the coins stay in
        the wallet.
        """
        self._report_pending()
        tasks = {t for t in self._open | self._closes if not t.done()}
        if not tasks:
            return
        held, closing = tasks - self._closes, tasks & self._closes
        if held:
            logger.info(
                f"Stopping {len(held)} open position(s); their tokens stay in "
                f"the wallet"
            )
        if closing:
            what = (
                "close their token account"
                if self.cleanup_mode == "after_sell"
                else "read their sale back"
            )
            logger.info(f"Waiting for {len(closing)} position(s) to {what}")
        for task in held:
            task.cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._STOP_BUDGET
        while not all(task.done() for task in tasks):
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._give_up_stopping(tasks)
                return
            try:
                await asyncio.wait(tasks, timeout=remaining)
            except asyncio.CancelledError:
                # The shutdown arriving again; the tasks are already cancelled
                # and cleanup still has to run after them.
                continue

    def _give_up_stopping(self, tasks: set[asyncio.Task]) -> None:
        """Log what `stop` leaves running, and cancel the account closes.

        A close still running would race the client closing at shutdown; the
        rent it leaves is reclaimed by `tools/cleanup_accounts.py <MINT>`.
        """
        closes = {t for t in tasks & self._closes if not t.done()}
        for task in closes:
            task.cancel()
        reports = sum(t.get_name().startswith("report-") for t in closes)
        if len(closes) > reports:
            logger.warning(
                f"{len(closes) - reports} token account close(s) did not finish "
                f"within {self._STOP_BUDGET}s; reclaim the rent with "
                f"tools/cleanup_accounts.py <MINT>"
            )
        if reports:
            logger.warning(
                f"{reports} sale report(s) did not finish within "
                f"{self._STOP_BUDGET}s; their trades.log rows and PnL lines are "
                f"missing"
            )
        positions = sum(not t.done() for t in tasks - closes)
        if positions:
            logger.warning(
                f"{positions} position task(s) did not stop within {self._STOP_BUDGET}s"
            )

    def _report_pending(self) -> None:
        """Name the buys sent but never settled, kept for the next start."""
        pending = set() if self.store is None else self.store.pending_mints()
        if pending:
            logger.warning(
                f"{len(pending)} buy(s) sent but not confirmed stay in "
                f"{self.store.path}: {', '.join(sorted(pending))}. The next start "
                f"settles each by its signature; one still unknown once its blockhash "
                f"has expired is settled by the wallet's balance"
            )

    def stored_mints(self) -> set[str]:
        """Every mint in the store, resumed or not; empty without a store."""
        return set() if self.store is None else self.store.mints()

    async def resume(self) -> list[TokenInfo]:
        """Hold again each position a previous run left in the store.

        The wallet is the truth: a position whose coin the wallet no longer
        holds was sold (or moved) while the bot was down and is cleared, never
        sold again; one holding less than was bought sells what is left. A
        position whose balance or quote mint cannot be read, that is on
        another platform than the bot's, or that another wallet bought, stays
        in the file and is not monitored this run. Every position held is
        resumed, even past `max_open_positions`; the trader takes no new coin
        until below it.

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

    def _not_ours(self, stored: StoredPosition, label: str) -> bool:
        """Whether another platform or wallet holds the position (logged)."""
        token_info = stored.token_info
        if token_info.platform != self.platform:
            logger.warning(
                f"Position in {label} is on {token_info.platform.value}, this bot "
                f"trades {self.platform.value}; it stays in {self.store.path} and "
                f"is not monitored this run"
            )
            return True
        if stored.wallet is not None and stored.wallet != str(self.wallet.pubkey):
            logger.warning(
                f"Position in {label} was bought by wallet {stored.wallet}, this "
                f"bot trades from {self.wallet.pubkey}; it stays in "
                f"{self.store.path} and is not monitored this run"
            )
            return True
        return False

    async def _resume_pending(self, stored: StoredPosition, label: str) -> bool:
        """Start resolving a buy the last run sent but never settled."""
        if not await self._quote_resolved(stored.token_info, label):
            return False
        logger.info(
            f"Resolving the buy of {label} sent before the bot stopped "
            f"({stored.buy_result.tx_signature}) from its signature"
        )
        self.open_pending(stored)
        return True

    async def _resume_one(self, stored: StoredPosition) -> bool:
        token_info, buy = stored.token_info, stored.buy_result
        label = f"{token_info.symbol} ({token_info.mint})"
        if self._not_ours(stored, label):
            return False
        if stored.pending is not None:
            return await self._resume_pending(stored, label)
        held = await self._held_raw(token_info)
        if held is None:
            logger.error(
                f"Could not confirm the wallet's balance of {label}; its position "
                f"stays in {self.store.path} and is not monitored this run"
            )
            return False
        if held == 0:
            logger.info(
                f"Clearing position in {label}: the wallet no longer holds it, "
                f"so it was sold or moved while the bot was down"
            )
            self.store.remove(token_info.mint)
            self._close_cleared(token_info)
            return False
        if not await self._quote_resolved(token_info, label):
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
            # The buy's deltas stay: its cost per token is unchanged. The file
            # keeps the smaller amount, so a later start never sells more.
            buy = dataclasses.replace(
                buy, amount=held / 10**TOKEN_DECIMALS, amount_raw=held
            )
            self.store.reduce(token_info.mint, buy.amount, held)
        logger.info(
            f"Resuming position in {label}: {buy.amount} tokens bought at "
            f"{buy.price} on {stored.entry_time.isoformat()} UTC"
        )
        self.open(token_info, buy, entry_time=stored.entry_time)
        return True

    def _close_cleared(self, token_info: TokenInfo) -> None:
        """Close the account of a cleared position's coin, under `after_sell`.

        The wallet holds none of the coin, so its account is empty, or gone.
        It closes in its own task, which `stop` lets finish like the close
        after a sell; nothing is burned. Under `post_session` the sweep at the
        end of the session closes it with every other empty account.
        """
        if self.cleanup_mode != "after_sell":
            return
        self._start_close(self._close_cleared_account(token_info), token_info)

    def _start_close(
        self, close: Coroutine, token_info: TokenInfo, *, closes: bool = True
    ) -> asyncio.Task:
        """Run an account close, or a sale's report, in its own task in `_closes`.

        Args:
            closes: The task closes an account; False for a sale's report that
                only reads it back
        """
        kind = "close" if closes else "report"
        task = asyncio.create_task(close, name=f"{kind}-{token_info.mint}")
        self._closes |= {task}
        task.add_done_callback(self._closed)
        return task

    def _closed(self, task: asyncio.Task) -> None:
        self._closes -= {task}

    async def _close_cleared_account(self, token_info: TokenInfo) -> None:
        try:
            program = token_info.token_program_id
            if program is None:
                program = (
                    await self.solana_client.get_account_info(
                        token_info.mint, commitment="confirmed"
                    )
                ).owner
            await handle_cleanup_cleared(
                self.solana_client,
                self.wallet,
                token_info.mint,
                program,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Could not close the token account of {token_info.symbol} "
                f"({token_info.mint}): {e!s}"
            )

    async def _quote_resolved(self, token_info: TokenInfo, label: str) -> bool:
        """Resolve the coin's quote mint; False (logged) when it cannot be."""
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
        return True

    async def _held_raw(
        self, token_info: TokenInfo, min_context_slot: int | None = None
    ) -> int | None:
        """Raw amount of the coin in the wallet's account; None when unsure.

        A balance above zero is taken at once. Zero clears the position for
        good, so every read must agree: an RPC node lagging behind may not
        show yet the account a buy just made. A read that fails leaves the
        answer None, which keeps the position. Reads are at "confirmed": the
        client's default, "finalized", runs seconds behind a fresh buy. With
        `min_context_slot`, a node behind that slot fails the read.
        """
        at_slot = (
            {} if min_context_slot is None else {"min_context_slot": min_context_slot}
        )
        reads, nothing_held = 3, 0
        for attempt in range(reads):
            if attempt:
                await asyncio.sleep(1.0)
            try:
                program = token_info.token_program_id
                if program is None:
                    program = (
                        await self.solana_client.get_account_info(
                            token_info.mint, commitment="confirmed", **at_slot
                        )
                    ).owner
                account = self.wallet.get_associated_token_address(
                    token_info.mint, program
                )
                try:
                    data = (
                        await self.solana_client.get_account_info(
                            account, commitment="confirmed", **at_slot
                        )
                    ).data
                except ValueError:
                    nothing_held += 1  # no account seen
                    continue
                # SPL token account: mint (32), owner (32), amount (u64).
                held = struct.unpack_from("<Q", bytes(data), 64)[0]
                if held:
                    return held
                nothing_held += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Balance read for {token_info.symbol} failed "
                    f"(attempt {attempt + 1}/{reads}): {e}"
                )
        return 0 if nothing_held == reads else None

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
        entry_price = buy_result.price
        if entry_price is None:
            entry_price = await self._first_price(token_info)
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=entry_price,
            quantity=buy_result.amount,
            quantity_raw=buy_result.amount_raw,
            take_profit_percentage=self.take_profit_percentage,
            stop_loss_percentage=self.stop_loss_percentage,
            max_hold_time=self.max_hold_time,
        )
        if entry_time is not None:
            position.entry_time = entry_time
        position.cost = cost_basis(
            buy_result, normalize_quote_mint(token_info.quote_mint)
        ) or CostBasis(entry_price, from_chain=False)

        logger.info(f"Created position: {position}")
        quote_label = quote_symbol(normalize_quote_mint(token_info.quote_mint))
        if position.take_profit_price:
            logger.info(
                f"Take profit target: {format_price(position.take_profit_price)} "
                f"{quote_label}"
            )
        if position.stop_loss_price:
            logger.info(
                f"Stop loss target: {format_price(position.stop_loss_price)} "
                f"{quote_label}"
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
        # A fresh position's first attempt prices off the buy: no price has been
        # read since, and reading one would cost an RPC call on the happy path.
        # A resumed one was bought before the restart, so its buy price is stale.
        token_price = buy_result.price
        if token_price is None:
            token_price = await self._first_price(token_info)
        elif entry_time is not None:
            token_price = await self._current_price_or(token_info, token_price)
        cost = cost_basis(
            buy_result, normalize_quote_mint(token_info.quote_mint)
        ) or CostBasis(token_price, from_chain=False)
        realised = Realised()

        # Set once part of it sold: the curve paid out all it had, so later
        # attempts on the rest wait `price_check_interval` for it to refill.
        curve_short = False
        attempt = 0
        while attempt < self.max_exit_sell_attempts:
            attempt += 1
            sell_result: TradeResult = await self.seller.execute(
                token_info,
                token_amount=buy_result.amount,
                token_price=token_price,
                token_amount_raw=buy_result.amount_raw,
            )
            verdict, left = await self._settle_time_based_sell(
                token_info,
                sell_result,
                buy_result,
                attempt,
                cost=cost,
                price=token_price,
                realised=realised,
            )
            if verdict in (ExitSellVerdict.SOLD, ExitSellVerdict.STOP):
                return
            if verdict is ExitSellVerdict.PARTIAL:
                # What is left gets a fresh attempt budget.
                buy_result = dataclasses.replace(
                    buy_result, amount=left / 10**TOKEN_DECIMALS, amount_raw=left
                )
                attempt, curve_short = 0, True
            elif attempt >= self.max_exit_sell_attempts:
                break
            if curve_short:
                await asyncio.sleep(self.price_check_interval)
            # The seller turns token_price into the slippage floor, and a revert
            # usually means that floor no longer matches the market, so re-read
            # the price rather than repeating the same unpayable ask.
            token_price = await self._current_price_or(token_info, token_price)

        logger.error(
            f"Giving up on selling {token_info.symbol} after "
            f"{self.max_exit_sell_attempts} attempts. Position stays open "
            f"and is no longer monitored - tokens are still held."
        )

    async def _settle_time_based_sell(  # noqa: PLR0913
        self,
        token_info: TokenInfo,
        sell_result: TradeResult,
        held: TradeResult,
        attempt: int,
        *,
        cost: CostBasis,
        price: float,
        realised: Realised,
    ) -> tuple[ExitSellVerdict, int]:
        """Act on one time-based exit sell: settle it, then report it and clean up.

        Args:
            held: What the sell was asked to sell, as `amount`/`amount_raw`
            cost: The position's cost basis
            price: The seller's reference price
            realised: The position's sales so far; this one is added

        Returns:
            The verdict, SOLD once all of it is sold (late or not), and the raw
            tokens left when the verdict is PARTIAL (0 otherwise)
        """
        if not sell_result.success:
            logger.error(
                f"Failed to sell {token_info.symbol} (attempt "
                f"{attempt}/{self.max_exit_sell_attempts}): "
                f"{sell_result.error_message}"
            )
            verdict = await self._classify_failed_exit_sell(token_info, sell_result)
            if verdict is ExitSellVerdict.STOP:
                logger.error(
                    f"Stopping exit attempts for {token_info.symbol} after "
                    f"{attempt} attempt(s) - the last sell could not be "
                    f"resolved. Position stays open and is no longer monitored."
                )
            if verdict is not ExitSellVerdict.LATE_SUCCESS:
                return verdict, 0
            # The sell landed; only its confirmation was late. Settle it like a
            # first-try success.
            logger.info(f"Sell of {token_info.symbol} confirmed late")

        left = self._settle_sale(
            token_info, sell_result.amount_raw, held.amount, held.amount_raw
        )
        self._after_sale(
            token_info,
            sell_result,
            (held.amount, held.amount_raw),
            cost=cost,
            price=price,
            realised=realised,
            last=not left,
        )
        if left:
            return ExitSellVerdict.PARTIAL, left
        logger.info(
            f"Successfully sold {token_info.symbol} (venue: {sell_result.venue})"
        )
        return ExitSellVerdict.SOLD, 0

    async def _classify_failed_exit_sell(  # noqa: PLR0911 - one return per outcome
        self, token_info: TokenInfo, sell_result: TradeResult
    ) -> ExitSellVerdict:
        """Decide whether another exit sell is safe after one came back failed.

        A reverted sell changed nothing; one whose confirmation never arrived may
        already have closed the position, and resending spends a fee to sell a
        balance that is no longer there.

        A merely unconfirmed signature is re-checked first: `getTransaction`
        keeps answering long after signature statuses age out of the RPC's recent
        history, so a sell that turns out to have landed is reported as a late
        success rather than retried. One still unknown that carries its last
        valid block height is then waited on until it lands or can no longer
        land (`_sell_after_expiry`).

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

        if sell_result.last_valid_block_height is not None:
            return await self._sell_after_expiry(
                token_info,
                signature,
                sell_result.last_valid_block_height,
                sell_result.amount_raw,
            )
        logger.error(
            f"Exit sell {signature[:16]}... is still unconfirmed. Not sending "
            f"another sell: it may already have closed the position. Verify "
            f"this signature before acting on {token_info.symbol}."
        )
        return ExitSellVerdict.STOP

    async def _sell_after_expiry(
        self,
        token_info: TokenInfo,
        signature: str,
        last_valid: int,
        asked_raw: int | None,
    ) -> ExitSellVerdict:
        """Wait until an unresolved exit sell has landed or can no longer land.

        Each look reads the block height and slot first, then the signature:
        a sell cannot land once the height is past `last_valid`, so a lookup
        made after that read sees it if it ever landed. Another sell goes out
        only when the height is past `last_valid`, no node knows the signature,
        and the wallet, read at "confirmed" from a node at least at that slot,
        still holds the `asked_raw` the sell asked for. A smaller balance means
        it landed after all; a node behind the slot, a signature seen but not
        confirmed, or a failed read is waited on. Nothing is resent before the
        expiry. Bounded by `_SELL_EXPIRY_WAIT`.

        Returns:
            LATE_SUCCESS, RETRY (reverted, or expired unknown with the coin
            still held), or STOP when still unresolved at the bound
        """
        logger.warning(
            f"Waiting for exit sell {signature[:16]}... of {token_info.symbol} to "
            f"land or expire (block height {last_valid}) before selling again"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._SELL_EXPIRY_WAIT
        while True:
            try:
                height, slot = await self.solana_client.get_block_height_and_slot()
            except Exception:  # noqa: BLE001
                height, slot = None, None
            try:
                outcome, seen_at = await self.solana_client.get_signature_outcome(
                    signature, search_history=True, quiet=True
                )
            except Exception:  # noqa: BLE001
                outcome, seen_at = ConfirmationStatus.UNCONFIRMED, None
            if outcome is ConfirmationStatus.SUCCESS:
                logger.info(f"Exit sell {signature[:16]}... landed after all")
                return ExitSellVerdict.LATE_SUCCESS
            if outcome is ConfirmationStatus.REVERTED:
                return ExitSellVerdict.RETRY
            expired = height is not None and height > last_valid
            if expired and outcome is None and seen_at is None and asked_raw:
                held = await self._held_raw(token_info, slot)
                if held is not None and held < asked_raw:
                    logger.info(
                        f"Exit sell {signature[:16]}... landed after all: the "
                        f"wallet holds {held} of the {asked_raw} raw it sold"
                    )
                    return ExitSellVerdict.LATE_SUCCESS
                if held is not None:
                    logger.info(
                        f"Exit sell {signature[:16]}... expired without landing; "
                        f"selling {token_info.symbol} again"
                    )
                    return ExitSellVerdict.RETRY
            if loop.time() >= deadline:
                logger.error(
                    f"Exit sell {signature[:16]}... is still unresolved after "
                    f"{self._SELL_EXPIRY_WAIT:g}s. Not sending another sell: it "
                    f"may already have closed the position. Verify this "
                    f"signature before acting on {token_info.symbol}."
                )
                return ExitSellVerdict.STOP
            await asyncio.sleep(self._PENDING_RECHECK)

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

    async def _first_price(self, token_info: TokenInfo) -> float:
        """A real price for a position whose fill price was never read.

        Its buy price would be an estimate, which in `extreme_fast_mode` can be
        far enough off to fire a stop loss at once; no exit runs until a price
        has been read. Re-read every `price_check_interval` until one above
        zero is, warning every `_PRICE_WARN_EVERY` seconds meanwhile; the price
        is then kept in the store as the position's entry price.
        """
        logger.warning(
            f"The fill price of {token_info.symbol} is unknown; its exit waits for "
            f"a price read and counts profit and loss from it"
        )
        loop = asyncio.get_running_loop()
        warned_at = loop.time()
        while True:
            try:
                price = await self._read_price(token_info)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Price read for {token_info.symbol} failed: {e}")
            else:
                if price > 0:
                    break
            if loop.time() - warned_at >= self._PRICE_WARN_EVERY:
                warned_at = loop.time()
                logger.warning(f"Still no price for {token_info.symbol}")
            await asyncio.sleep(self.price_check_interval)
        # Kept, so a restart counts from the same baseline.
        if self.store is not None:
            self.store.set_price(token_info.mint, price)
        logger.info(
            f"Entry price of {token_info.symbol} taken as {format_price(price)}"
        )
        return price

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
                f"retrying against {format_price(fallback)}"
            )
            return fallback
        if price and price > 0:
            return price
        logger.warning(
            f"Re-read price for {token_info.symbol} was {price}; "
            f"retrying against {format_price(fallback)}"
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
                    logger.info(
                        f"Current price: {format_price(current_price)} {quote_label}"
                    )

                    # 0.0 satisfies the stop-loss comparison, so the exit can
                    # fire on a price the sell cannot be floored against.
                    exit_price = (
                        current_price if current_price > 0 else last_known_price
                    )
                    if exit_price != current_price:
                        logger.warning(
                            f"Curve priced {token_info.symbol} at 0; flooring the "
                            f"exit sell at the last real price "
                            f"{format_price(exit_price)} {quote_label}"
                        )

                    exit_sell_attempts = await self._run_exit_attempt(
                        token_info,
                        position,
                        exit_reason,
                        exit_price,
                        exit_sell_attempts + 1,
                    )
                    if exit_sell_attempts is None:
                        break
                    # Keep monitoring: the next iteration re-reads the price and
                    # retries the sell with a floor that matches the market.
                else:
                    # Log current status
                    exit_sell_attempts = 0
                    pnl = position.get_pnl(current_price)
                    logger.debug(
                        f"Position status for {token_info.symbol}: "
                        f"{format_price(current_price)} {quote_label} "
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
                        f"known price {format_price(last_known_price)} {quote_label}"
                    )
                    exit_sell_attempts = await self._run_exit_attempt(
                        token_info,
                        position,
                        ExitReason.MAX_HOLD_TIME,
                        last_known_price,
                        exit_sell_attempts + 1,
                    )
                    if exit_sell_attempts is None:
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
    ) -> int | None:
        """Make one bounded attempt to close the position.

        Args:
            position: The open position
            exit_reason: Why the exit fired
            price: Price the sell is floored against
            attempt: 1-based attempt counter, for the bound and the logs

        Returns:
            The attempts spent so far, 0 after a partial sale (what is left gets
            a fresh budget), or None when monitoring should stop: the position
            is closed, the outcome could not be resolved, or the attempt budget
            is spent.
        """
        verdict = await self._attempt_position_exit(
            token_info, position, exit_reason, price
        )
        if verdict is ExitSellVerdict.PARTIAL:
            return 0
        if verdict is not ExitSellVerdict.RETRY:
            return None

        if attempt >= self.max_exit_sell_attempts:
            logger.error(
                f"Giving up on exiting {token_info.symbol} after "
                f"{attempt} attempts. Position stays open "
                f"and is no longer monitored - tokens are still held."
            )
            return None
        return attempt

    async def _attempt_position_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
    ) -> ExitSellVerdict:
        """Sell the position once and report what came of it.

        Args:
            position: The open position, closed in place once all of it sold,
                reduced to what is left when only part of it did
            exit_reason: Why the exit fired
            price: Price the sell is floored against. The seller turns it into
                `min_quote_output`, so it must be a price the pool can pay — an
                exit fires precisely because the price left the entry price.

        Returns:
            SOLD, PARTIAL, LATE_SUCCESS, RETRY, or STOP
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

        if not sell_result.success:
            logger.error(f"Failed to exit position: {sell_result.error_message}")
            verdict = await self._classify_failed_exit_sell(token_info, sell_result)
            if verdict is not ExitSellVerdict.LATE_SUCCESS:
                if verdict is ExitSellVerdict.STOP:
                    logger.error(
                        f"Stopping exit attempts for {token_info.symbol} - the "
                        f"last sell could not be resolved. Position stays open "
                        f"and is no longer monitored."
                    )
                return verdict
            # The sell landed, only its confirmation was late.
            logger.info(f"The exit sell of {token_info.symbol} landed; confirmed late")

        asked = (position.quantity, position.quantity_raw)
        partial = self._settle_position(token_info, position, sell_result)
        self._after_sale(
            token_info,
            sell_result,
            asked,
            cost=position.cost or CostBasis(position.entry_price, from_chain=False),
            price=price,
            realised=position.realised,
            last=not partial,
        )
        if partial:
            return ExitSellVerdict.PARTIAL
        position.close_position(price, exit_reason)
        logger.info(
            f"Successfully exited position: {exit_reason.value} "
            f"({token_info.symbol}, venue: {sell_result.venue})"
        )
        return ExitSellVerdict.SOLD

    def _after_sale(  # noqa: PLR0913
        self,
        token_info: TokenInfo,
        sell_result: TradeResult,
        asked: tuple[float, int | None],
        *,
        cost: CostBasis,
        price: float,
        realised: Realised,
        last: bool,
    ) -> None:
        """Report a settled sale, then close the account once all of it is sold.

        Runs in its own task in `_closes`, started once the store is settled:
        the position's slot is free while the transaction is read, `stop` and
        `wait_all` wait for it like a close, and nothing it does can undo the
        settlement. The parts of a position sold in parts report in order.

        Args:
            asked: Whole and raw tokens the sell was asked to sell
            price: The seller's reference price
            last: All of the position is sold: its line is `Final PnL`, with
                the whole position's total, and the account closes after it
        """
        earlier = list(realised.reports)
        task = self._start_close(
            self._report_sale(
                token_info,
                sell_result,
                asked,
                cost=cost,
                price=price,
                realised=realised,
                earlier=earlier,
                last=last,
            ),
            token_info,
            closes=last and self.cleanup_mode == "after_sell",
        )
        realised.reports.append(task)

    async def _report_sale(  # noqa: PLR0913
        self,
        token_info: TokenInfo,
        sell_result: TradeResult,
        asked: tuple[float, int | None],
        *,
        cost: CostBasis,
        price: float,
        realised: Realised,
        earlier: list[asyncio.Task],
        last: bool,
    ) -> None:
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        pnl = None
        try:
            sale = await self._read_sale(token_info, sell_result, price, *asked)
            pnl = sale_pnl(cost, sale, quote_mint, price)
        except Exception:
            logger.exception(
                f"Could not price the sale of {token_info.symbol}; it is settled"
            )
        # Whatever happened above, the parts are totalled in order.
        if earlier:
            await asyncio.gather(*earlier, return_exceptions=True)
        if pnl is None:
            realised.missing += 1
        else:
            realised.add(pnl)
        try:
            quote_label = quote_symbol(quote_mint)
            if pnl is not None:
                trade_log.log_trade(
                    "sell",
                    token_info,
                    sale.price,
                    sale.amount,
                    sale.tx_signature,
                    deltas=sale.deltas,
                    pnl=pnl,
                    venue=sale.venue,
                )
                label = "Final PnL" if last else "PnL on the part sold"
                total = realised.suffix(quote_label) if last else ""
                logger.info(pnl_text(label, pnl, quote_label) + total)
            elif last:
                logger.info(
                    f"Final PnL: unknown, this sale could not be priced"
                    f"{realised.suffix(quote_label)}"
                )
        except Exception:
            logger.exception(
                f"Could not report the sale of {token_info.symbol}; it is settled"
            )
        if last:
            await self._close_after_sale(token_info)

    async def _read_sale(
        self,
        token_info: TokenInfo,
        sell_result: TradeResult,
        price: float,
        quantity: float,
        quantity_raw: int | None,
    ) -> TradeResult:
        """A landed sale, read back from its transaction.

        Args:
            price: The seller's reference price, the sale's price if it can't
                be read
            quantity, quantity_raw: What it was asked to sell, for a sale that
                does not say
        """
        try:
            deltas = await read_deltas(
                self.solana_client,
                sell_result.tx_signature,
                self.wallet.pubkey,
                token_info,
            )
        except Exception as e:
            logger.warning(f"Could not read back the sale of {token_info.symbol}: {e}")
            logger.debug("Reading the sale back failed", exc_info=True)
            deltas = None
        amount_raw = sell_result.amount_raw
        if amount_raw is None:
            amount_raw = (
                quantity_raw
                if quantity_raw is not None
                else int(quantity * 10**TOKEN_DECIMALS)
            )
        sale = landed_sale(
            token_info, sell_result.tx_signature, amount_raw, price, deltas
        )
        sale.venue = sell_result.venue
        return sale

    def _settle_position(
        self, token_info: TokenInfo, position: Position, sell_result: TradeResult
    ) -> bool:
        """Settle a landed exit sell against `position`.

        Returns:
            Whether part of it is left: `position` now holds only that part
        """
        left = self._settle_sale(
            token_info, sell_result.amount_raw, position.quantity, position.quantity_raw
        )
        if left:
            position.quantity, position.quantity_raw = left / 10**TOKEN_DECIMALS, left
        return bool(left)

    def _settle_sale(
        self,
        token_info: TokenInfo,
        sold_raw: int | None,
        quantity: float,
        quantity_raw: int | None,
    ) -> int:
        """Take a landed exit sell off the position in the store.

        A sell the curve could pay for only in part leaves the rest held: the
        store keeps the position with what is left, and no cleanup may run
        until that is sold too.

        Args:
            sold_raw: Raw tokens the sell sold; None when the seller did not
                say, taken as all of it
            quantity: Whole tokens the sell was asked to sell
            quantity_raw: The same in raw units, None when only `quantity` is
                known (the seller then sells `int(quantity * 10**6)`)

        Returns:
            Raw tokens left unsold; 0 once the position is all sold
        """
        asked = (
            quantity_raw
            if quantity_raw is not None
            else int(quantity * 10**TOKEN_DECIMALS)
        )
        if sold_raw is None or sold_raw >= asked:
            self._forget(token_info)
            return 0
        left = asked - sold_raw
        logger.warning(
            f"Sold {sold_raw} of {asked} raw {token_info.symbol}: the curve could "
            f"not pay for the rest. {left} raw stay held; the position stays open "
            f"and sells them on a later attempt"
        )
        if self.store is not None:
            self.store.reduce(token_info.mint, left / 10**TOKEN_DECIMALS, left)
        return left

    def _forget(self, token_info: TokenInfo) -> None:
        """Take a sold position out of the store.

        Only a sale does: a position stopped at shutdown, or one whose exit
        gave up or could not be resolved, still holds its coin and stays for
        the next run, which checks the wallet before selling.
        """
        if self.store is not None:
            self.store.remove(token_info.mint)

    async def _close_after_sale(self, token_info: TokenInfo) -> None:
        """Run the configured post-sell cleanup for a position all sold.

        Part of the sale's own task (`_after_sale`), so the position's slot is
        free however long a dropped close takes.
        """
        if self.cleanup_mode != "after_sell":
            return
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

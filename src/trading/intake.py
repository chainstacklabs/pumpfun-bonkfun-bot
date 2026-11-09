"""Token intake: the listener, the queue, freshness and the configured filters.

With `trade.entry: migration` the intake takes the listener's migrations
instead of its launches: each is resolved to a coin, filtered, and handed on
once its pool is readable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from solders.pubkey import Pubkey

from core.slot_clock import SlotClock
from interfaces.core import PoolReady, TokenInfo
from monitoring.base_listener import BaseTokenListener
from trading.checks import CheckContext, CheckPipeline, ConfiguredCheck
from trading.checks.builtin import (
    CreatorCheck,
    NameMatchCheck,
    QuoteAllowedCheck,
    QuoteAmountCheck,
    TransferFeeCheck,
)
from trading.migration import MigrationResolver, PoolNotTradable
from trading.settings import TraderSettings
from utils.logger import get_logger

logger = get_logger(__name__)

TokenHandler = Callable[[TokenInfo], Awaitable[None]]


class TokenIntake:
    """Turns the listener's detections into coins worth handing to the buyer."""

    #: Set under `filters.age_from: chain`; None counts from detection.
    slot_clock: SlotClock | None = None

    def __init__(  # noqa: PLR0913
        self,
        settings: TraderSettings,
        token_listener: BaseTokenListener,
        quote_amounts: dict[Pubkey, float],
        allowed_quote_mints: set[Pubkey] | None,
        *,
        pool_resolver: MigrationResolver | None = None,
        slot_clock: SlotClock | None = None,
        checks: list[ConfiguredCheck] | None = None,
        check_context: CheckContext | None = None,
    ) -> None:
        """Set up the intake.

        Args:
            pool_resolver: Required with `trade.entry: migration`; unused otherwise
            slot_clock: Required with `filters.age_from: chain`, where a
                launch's age counts from the slot its create landed in
            checks: The `filters.checks` the bot YAML enables; the intake runs
                the ones that read nothing, after the `filters` settings
        """
        self.token_listener = token_listener
        self.entry = settings.entry
        if self.entry == "migration" and pool_resolver is None:
            raise ValueError("trade.entry: migration needs a pool resolver")  # noqa: TRY003
        self.pool_resolver = pool_resolver
        self.match_string = settings.match_string
        self.bro_address = settings.bro_address
        self.max_token_age = settings.max_token_age
        self.slot_clock = slot_clock if settings.age_from == "chain" else None
        if settings.age_from == "chain" and slot_clock is None:
            raise ValueError("filters.age_from: chain needs a slot clock")  # noqa: TRY003
        self.token_wait_timeout = settings.token_wait_timeout
        self.quote_amounts = quote_amounts
        self.allowed_quote_mints = allowed_quote_mints
        self.max_transfer_fee_bps = settings.max_transfer_fee_bps

        # The `filters` settings as checks. A launch's name and creator are
        # filtered by the listener; a migration's here, since no launch was seen.
        settings_checks = [
            ConfiguredCheck(QuoteAllowedCheck(allowed_quote_mints), named=False),
            ConfiguredCheck(QuoteAmountCheck(quote_amounts), named=False),
            ConfiguredCheck(TransferFeeCheck(self.max_transfer_fee_bps), named=False),
        ]
        identity_checks = [
            ConfiguredCheck(NameMatchCheck(self.match_string), named=False),
            ConfiguredCheck(CreatorCheck(self.bro_address), named=False),
        ]
        configured = list(checks or [])
        context = check_context or CheckContext(client=None)
        self._launch_checks = CheckPipeline(settings_checks + configured, context)
        self._migration_checks = CheckPipeline(
            identity_checks + settings_checks + configured, context
        )
        # Each coin's answer, by mint: a coin is asked about on detection and
        # again before the buy, and a check may count what it has seen.
        self._verdicts: dict[str, str | None] = {}

        self.token_queue: asyncio.Queue = asyncio.Queue()
        # Migration entry: (report, monotonic time it was queued).
        self.pool_queue: asyncio.Queue[tuple[PoolReady, float]] = asyncio.Queue()
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        # Set when the listener ended on an error rather than a stop.
        self.stopped_on_error = False

    async def skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the `filters` settings or a check that reads nothing refuse the coin.

        None to trade it. Checked on detection as well as before the buy:
        single-token mode would otherwise spend its one token on a coin it was
        never going to buy.
        """
        return await self._verdict(self._launch_checks, token_info)

    async def _verdict(self, pipeline: CheckPipeline, coin: TokenInfo) -> str | None:
        key = str(coin.mint)
        if key not in self._verdicts:
            self._verdicts[key] = await pipeline.reason(coin, reads_chain=False)
        return self._verdicts[key]

    async def wait_for_token(self, *, timeout: float | None = None) -> TokenInfo | None:
        """Wait for the first coin the filters accept, up to `token_wait_timeout`.

        `timeout` overrides it, in seconds, for a wait already partly spent.

        Returns:
            The coin, or None if none arrived in time
        """
        limit = self.token_wait_timeout if timeout is None else timeout
        # Create a one-time event to signal when a token is found
        token_found = asyncio.Event()
        found_token = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)

            if token_key in self.processed_tokens:
                return
            self.processed_tokens.add(token_key)
            if self.slot_clock is not None:
                self.slot_clock.observe(token.creation_slot)
            skip_reason = await self.skip_reason(token) or self._too_old(
                token, monotonic()
            )
            if skip_reason:
                logger.info(f"Skipping {token.symbol} - {skip_reason}; still waiting")
                return
            # Record when the token was discovered
            self.token_timestamps[token_key] = monotonic()
            found_token = token
            token_found.set()

        self._start_slot_clock()
        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                token_callback,
                self.match_string,
                self.bro_address,
            )
        )

        # Wait for a token with a timeout
        try:
            logger.info(f"Waiting for a suitable token (timeout: {limit:.0f}s)...")
            await asyncio.wait_for(token_found.wait(), timeout=limit)
        except TimeoutError:
            logger.info(f"Timed out after waiting {limit:.0f}s for a token")
            return None
        else:
            logger.info(f"Found token: {found_token.symbol} ({found_token.mint})")
            return found_token
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
            await self._stop_slot_clock()

    async def run(self, handle_token: TokenHandler) -> None:
        """Queue every detection and hand each fresh one to `handle_token`, one at a time.

        Returns when the listener stops; the queue processor is cancelled then.
        """
        processor_task = asyncio.create_task(self._process_token_queue(handle_token))
        self._start_slot_clock()

        try:
            await self.token_listener.listen_for_tokens(
                self._queue_token,
                self.match_string,
                self.bro_address,
            )
        except Exception:
            logger.exception("Token listening stopped due to error")
            self.stopped_on_error = True
        finally:
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass
            await self._stop_slot_clock()

    def _start_slot_clock(self) -> None:
        if self.slot_clock is not None:
            self.slot_clock.start()

    async def _stop_slot_clock(self) -> None:
        if self.slot_clock is not None:
            await self.slot_clock.stop()

    def _age(self, token_info: TokenInfo, detected_at: float) -> tuple[float, str]:
        """The coin's age in seconds, and what it counts from.

        Under `age_from: chain` it counts from the slot its create landed in;
        a coin whose slot is unknown, or read before the clock has a current
        slot, counts from when it was detected.
        """
        if self.slot_clock is not None:
            age = self.slot_clock.age(token_info.creation_slot)
            if age is not None:
                return age, "since created on chain"
        return monotonic() - detected_at, "since detected"

    def stale_reason(self, token_info: TokenInfo) -> str | None:
        """Why a coin found a while ago is too old to take up now; None while fresh.

        For a coin found by `wait_for_token`, which recorded when; a coin with
        no such record counts as fresh.
        """
        detected_at = self.token_timestamps.get(str(token_info.mint))
        if detected_at is None:
            return None
        return self._too_old(token_info, detected_at)

    def _too_old(self, token_info: TokenInfo, detected_at: float) -> str | None:
        """Why the coin is past `max_token_age`, or None while it is fresh."""
        age, since = self._age(token_info, detected_at)
        if age > self.max_token_age:
            return f"too old ({age:.1f}s {since} > max_token_age {self.max_token_age}s)"
        return None

    def forget_unprocessed(self) -> None:
        """Drop discovery times of coins that were queued but never processed."""
        old_keys = {k for k in self.token_timestamps if k not in self.processed_tokens}
        for key in old_keys:
            self.token_timestamps.pop(key, None)

    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)

        if token_key in self.processed_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
            return

        # The first discovery counts: a repeated report of a coin still queued
        # must not make the copy ahead of it look fresh.
        self.token_timestamps.setdefault(token_key, monotonic())
        if self.slot_clock is not None:
            self.slot_clock.observe(token_info.creation_slot)

        await self.token_queue.put(token_info)
        logger.info(
            f"Queued new token: {token_info.symbol} ({token_info.mint}) on {token_info.platform.value}"
        )

    async def _process_token_queue(self, handle_token: TokenHandler) -> None:
        """Continuously process tokens from the queue, only if they're fresh.

        `task_done()` is balanced against `get()`: cancelling this task is how
        yolo mode shuts down, and the cancellation usually lands while parked on
        `get()`, with no item taken. Calling `task_done()` on that path raises
        ValueError, which escapes `run()`, so a clean shutdown ends up logged as
        `Trading stopped due to error`.
        """
        while True:
            took_item = False
            try:
                token_info = await self.token_queue.get()
                took_item = True
                token_key = str(token_info.mint)
                if token_key in self.processed_tokens:
                    # A repeated report queued before the first was processed.
                    logger.debug(f"Token {token_info.symbol} already processed")
                    continue

                # Check if token is still "fresh"
                current_time = monotonic()
                detected_at = self.token_timestamps.get(token_key, current_time)
                too_old = self._too_old(token_info, detected_at)
                if too_old:
                    logger.info(f"Skipping token {token_info.symbol} - {too_old}")
                    continue

                self.processed_tokens.add(token_key)

                age, since = self._age(token_info, detected_at)
                logger.info(
                    f"Processing fresh token: {token_info.symbol} "
                    f"(age: {age:.1f}s {since})"
                )
                await handle_token(token_info)

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
            finally:
                # The stale-token `continue` above took an item, so it still
                # needs this; a cancellation parked on `get()` did not.
                if took_item:
                    self.token_queue.task_done()

    # Migration entry. The listener still reports launches; they are dropped.

    async def migration_skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the filters refuse a migrated coin, or None to buy it.

        The name filter reads the coin's metadata; the creator filter compares
        the creator its curve records, since no launch was seen.
        """
        return await self._verdict(self._migration_checks, token_info)

    async def _ignore_launch(self, _token_info: TokenInfo) -> None:
        return None

    async def _queue_pool(self, pool: PoolReady) -> None:
        if str(pool.mint) in self.processed_tokens:
            logger.debug(f"Migrated coin {pool.mint} already processed. Skipping...")
            return
        await self.pool_queue.put((pool, monotonic()))
        logger.info(f"Queued migrated coin {pool.mint} (pool {pool.pool})")

    async def _prepare_pool(self, pool: PoolReady) -> TokenInfo | None:
        """The coin behind a migration, once filtered and its pool readable.

        A coin is processed once. The exceptions: a pre-execution report
        (shreds) that leaves nothing to buy -- a curve not yet complete, a
        quote mint the curve does not use, a pool that never appears --
        because another attempt for the same mint may still land; and any
        report whose curve the chain did not yet show as completed, which a
        lagging node causes as well as an attempt that cannot land.

        Returns:
            The coin to buy, or None when it is skipped
        """
        key = str(pool.mint)
        if key in self.processed_tokens:
            return None
        retry_allowed = pool.pre_execution
        try:
            token_info = await self.pool_resolver.resolve(pool)
        except PoolNotTradable as e:
            logger.info(f"Skipping migrated coin {pool.mint} - {e}")
            if not (retry_allowed or e.not_yet_visible):
                self.processed_tokens.add(key)
            return None
        reason = await self.migration_skip_reason(token_info)
        if reason:
            logger.info(f"Skipping {token_info.symbol} ({token_info.mint}) - {reason}")
            self.processed_tokens.add(key)
            return None
        if not await self.pool_resolver.wait_for_pool(token_info):
            if not retry_allowed:
                self.processed_tokens.add(key)
            return None
        self.processed_tokens.add(key)
        return token_info

    async def wait_for_migrated_token(
        self, *, timeout: float | None = None
    ) -> TokenInfo | None:
        """`wait_for_token` for migrations: the first migrated coin the filters accept.

        Returns:
            The coin, its pool readable, or None if none arrived in time
        """
        limit = self.token_wait_timeout if timeout is None else timeout

        async def first_accepted() -> TokenInfo:
            while True:
                pool, queued_at = await self.pool_queue.get()
                # A report that waited behind another's pool wait ages too.
                age = monotonic() - queued_at
                if age > self.max_token_age:
                    logger.info(
                        f"Skipping migrated coin {pool.mint} - too old "
                        f"({age:.1f}s since its migration was detected > "
                        f"max_token_age {self.max_token_age}s)"
                    )
                    continue
                token_info = await self._prepare_pool(pool)
                if token_info is not None:
                    return token_info

        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                self._ignore_launch, pool_callback=self._queue_pool
            )
        )
        try:
            logger.info(f"Waiting for a suitable migration (timeout: {limit:.0f}s)...")
            found = await asyncio.wait_for(first_accepted(), timeout=limit)
        except TimeoutError:
            logger.info(f"Timed out after waiting {limit:.0f}s for a migration")
            return None
        else:
            logger.info(f"Found token: {found.symbol} ({found.mint})")
            return found
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    async def run_migrations(self, handle_token: TokenHandler) -> None:
        """`run` for migrations: queue each, hand each fresh one's coin to `handle_token`."""
        processor_task = asyncio.create_task(self._process_pool_queue(handle_token))
        try:
            await self.token_listener.listen_for_tokens(
                self._ignore_launch, pool_callback=self._queue_pool
            )
        except Exception:
            logger.exception("Token listening stopped due to error")
            self.stopped_on_error = True
        finally:
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass

    async def _process_pool_queue(self, handle_token: TokenHandler) -> None:
        """Hand each fresh migration's coin to `handle_token`, one at a time.

        Freshness is the report's age when it is taken off the queue, as for
        launches. `task_done()` is balanced as in `_process_token_queue`.
        """
        while True:
            took_item = False
            try:
                pool, queued_at = await self.pool_queue.get()
                took_item = True
                age = monotonic() - queued_at
                if age > self.max_token_age:
                    logger.info(
                        f"Skipping migrated coin {pool.mint} - too old "
                        f"({age:.1f}s since its migration was detected > "
                        f"max_token_age {self.max_token_age}s)"
                    )
                    continue
                token_info = await self._prepare_pool(pool)
                if token_info is None:
                    continue
                logger.info(
                    f"Processing migrated coin: {token_info.symbol} (age: {age:.1f}s)"
                )
                await handle_token(token_info)
            except asyncio.CancelledError:
                logger.info("Migration queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in migration queue processor")
            finally:
                if took_item:
                    self.pool_queue.task_done()

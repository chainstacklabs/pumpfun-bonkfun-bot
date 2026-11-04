"""Token intake: the listener, the queue, freshness and the configured filters.

With `trade.entry: migration` the intake takes the listener's migrations
instead of its launches: each is resolved to a coin, filtered, and handed on
once its pool is readable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from solders.pubkey import Pubkey

from core.pubkeys import normalize_quote_mint
from interfaces.core import PoolReady, TokenInfo
from monitoring.base_listener import BaseTokenListener
from trading.migration import MigrationResolver, PoolNotTradable
from trading.settings import TraderSettings
from utils.logger import get_logger

logger = get_logger(__name__)

TokenHandler = Callable[[TokenInfo], Awaitable[None]]


class TokenIntake:
    """Turns the listener's detections into coins worth handing to the buyer."""

    def __init__(
        self,
        settings: TraderSettings,
        token_listener: BaseTokenListener,
        quote_amounts: dict[Pubkey, float],
        allowed_quote_mints: set[Pubkey] | None,
        pool_resolver: MigrationResolver | None = None,
    ) -> None:
        """Set up the intake.

        Args:
            pool_resolver: Required with `trade.entry: migration`; unused otherwise
        """
        self.token_listener = token_listener
        self.entry = settings.entry
        if self.entry == "migration" and pool_resolver is None:
            raise ValueError("trade.entry: migration needs a pool resolver")  # noqa: TRY003
        self.pool_resolver = pool_resolver
        self.match_string = settings.match_string
        self.bro_address = settings.bro_address
        self.max_token_age = settings.max_token_age
        self.token_wait_timeout = settings.token_wait_timeout
        self.quote_amounts = quote_amounts
        self.allowed_quote_mints = allowed_quote_mints
        self.max_transfer_fee_bps = settings.max_transfer_fee_bps

        self.token_queue: asyncio.Queue = asyncio.Queue()
        # Migration entry: (report, monotonic time it was queued).
        self.pool_queue: asyncio.Queue[tuple[PoolReady, float]] = asyncio.Queue()
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        # Set when the listener ended on an error rather than a stop.
        self.stopped_on_error = False

    def skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the configured filters refuse this coin, or None to trade it.

        Checked on detection as well as before the buy: single-token mode would
        otherwise spend its one token on a coin it was never going to buy.
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        if (
            self.allowed_quote_mints is not None
            and quote_mint not in self.allowed_quote_mints
        ):
            return f"quote mint {quote_mint} not in allowed_quote_mints"
        if quote_mint not in self.quote_amounts:
            return f"no buy amount configured for quote mint {quote_mint}"
        if (
            self.max_transfer_fee_bps is not None
            and token_info.transfer_fee_bps > self.max_transfer_fee_bps
        ):
            return (
                f"transfer fee {token_info.transfer_fee_bps} bps exceeds "
                f"max_transfer_fee_bps {self.max_transfer_fee_bps}"
            )
        return None

    async def wait_for_token(self) -> TokenInfo | None:
        """Wait for the first coin the filters accept, up to `token_wait_timeout`.

        Returns:
            The coin, or None if none arrived in time
        """
        # Create a one-time event to signal when a token is found
        token_found = asyncio.Event()
        found_token = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)

            if token_key in self.processed_tokens:
                return
            self.processed_tokens.add(token_key)
            skip_reason = self.skip_reason(token)
            if skip_reason:
                logger.info(f"Skipping {token.symbol} - {skip_reason}; still waiting")
                return
            # Record when the token was discovered
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

        # Wait for a token with a timeout
        try:
            logger.info(
                f"Waiting for a suitable token (timeout: {self.token_wait_timeout}s)..."
            )
            await asyncio.wait_for(token_found.wait(), timeout=self.token_wait_timeout)
        except TimeoutError:
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
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

    async def run(self, handle_token: TokenHandler) -> None:
        """Queue every detection and hand each fresh one to `handle_token`, one at a time.

        Returns when the listener stops; the queue processor is cancelled then.
        """
        processor_task = asyncio.create_task(self._process_token_queue(handle_token))

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
                token_age = current_time - self.token_timestamps.get(
                    token_key, current_time
                )

                if token_age > self.max_token_age:
                    logger.info(
                        f"Skipping token {token_info.symbol} - too old ({token_age:.1f}s > {self.max_token_age}s)"
                    )
                    continue

                self.processed_tokens.add(token_key)

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
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

    def migration_skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the filters refuse a migrated coin, or None to buy it.

        The name filter reads the coin's metadata; the creator filter compares
        the creator its curve records, since no launch was seen.
        """
        if self.match_string and not (
            self.match_string.lower() in token_info.name.lower()
            or self.match_string.lower() in token_info.symbol.lower()
        ):
            return f"does not match filter '{self.match_string}'"
        if self.bro_address and str(token_info.creator) != self.bro_address:
            return f"not created by {self.bro_address}"
        return self.skip_reason(token_info)

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
        reason = self.migration_skip_reason(token_info)
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

    async def wait_for_migrated_token(self) -> TokenInfo | None:
        """`wait_for_token` for migrations: the first migrated coin the filters accept.

        Returns:
            The coin, its pool readable, or None if none arrived in time
        """

        async def first_accepted() -> TokenInfo:
            while True:
                pool, queued_at = await self.pool_queue.get()
                # A report that waited behind another's pool wait ages too.
                age = monotonic() - queued_at
                if age > self.max_token_age:
                    logger.info(
                        f"Skipping migrated coin {pool.mint} - too old "
                        f"({age:.1f}s > {self.max_token_age}s)"
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
            logger.info(
                f"Waiting for a suitable migration (timeout: {self.token_wait_timeout}s)..."
            )
            found = await asyncio.wait_for(
                first_accepted(), timeout=self.token_wait_timeout
            )
        except TimeoutError:
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a migration"
            )
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
                        f"({age:.1f}s > {self.max_token_age}s)"
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

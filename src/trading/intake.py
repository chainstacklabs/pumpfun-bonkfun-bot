"""Token intake: the listener, the queue, freshness and the configured filters."""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from solders.pubkey import Pubkey

from core.pubkeys import normalize_quote_mint
from interfaces.core import TokenInfo
from monitoring.base_listener import BaseTokenListener
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
    ) -> None:
        self.token_listener = token_listener
        self.match_string = settings.match_string
        self.bro_address = settings.bro_address
        self.max_token_age = settings.max_token_age
        self.token_wait_timeout = settings.token_wait_timeout
        self.quote_amounts = quote_amounts
        self.allowed_quote_mints = allowed_quote_mints
        self.max_transfer_fee_bps = settings.max_transfer_fee_bps

        self.token_queue: asyncio.Queue = asyncio.Queue()
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}

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

        # Record timestamp when token was discovered
        self.token_timestamps[token_key] = monotonic()

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

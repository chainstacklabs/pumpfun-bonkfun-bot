"""Platform-agnostic base class for WebSocket token listeners."""

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.pubkeys import is_sol_paired
from interfaces.core import Platform, PoolReady, TokenInfo
from utils.logger import get_logger

if TYPE_CHECKING:
    from solders.pubkey import Pubkey

logger = get_logger(__name__)

PoolCallback = Callable[[PoolReady], Awaitable[None]]

# Migrations already reported, kept so a racing duplicate migration or a
# redelivered frame reports a pool once.
_SEEN_POOLS_LIMIT = 10_000
# Launches already reported, kept so a coin is reported once; see `_new_launch`.
_SEEN_LAUNCHES_LIMIT = 10_000


def describe_launch(token_info: TokenInfo, *, pre_execution: bool = False) -> str:
    """The one line a detected coin is logged with.

    Names the mint, so two coins with the same name read apart, and what tells
    where it launched: its curve, a LaunchLab coin's platform config, the quote
    asset when the listener read it, and the creator.
    """
    stage = " pre-execution" if pre_execution else ""
    parts = [f"mint {token_info.mint}"]
    if token_info.platform_config is not None:
        parts.append(f"platform config {token_info.platform_config}")
    curve = token_info.bonding_curve or token_info.pool_state
    if curve is not None:
        parts.append(f"curve {curve}")
    if token_info.quote_mint is not None:
        quote = token_info.quote_mint
        parts.append(f"quote {'SOL' if is_sol_paired(quote) else quote}")
    creator = token_info.creator or token_info.user
    if creator is not None:
        parts.append(f"creator {creator}")
    return (
        f"New token detected{stage}: {token_info.name} ({token_info.symbol}) "
        f"on {token_info.platform.value}: {', '.join(parts)}"
    )


def reraise_if_cancelled() -> None:
    """Re-raise CancelledError if the running task is being cancelled.

    Call this first inside a broad `except Exception` that guards a websocket
    read. Cancelling a task parked in `websockets`' `recv()` does not reliably
    surface as `CancelledError`: when the cancellation lands while the library
    is assembling frames it raises `AssertionError: cannot reset() while queue
    isn't empty` instead. That is an ordinary `Exception`, so a broad handler
    swallows the shutdown request, and the loop it guards never stops — while
    the connection's frame state is left corrupt, so every later read asserts
    too.

    Raises:
        asyncio.CancelledError: If the current task has a pending cancellation.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


class BaseTokenListener(ABC):
    """Base abstract class for token listeners - now platform-agnostic."""

    def __init__(self, platform: Platform | None = None):
        """Initialize the listener with optional platform specification.

        Args:
            platform: Platform to monitor (if None, monitor all platforms)
        """
        self.platform = platform
        # Set by listen_for_tokens; None leaves migrations unparsed.
        self.pool_callback: PoolCallback | None = None
        self._seen_pools: OrderedDict[Pubkey | tuple[Pubkey, str], None] = OrderedDict()
        self._seen_launches: OrderedDict[Pubkey, None] = OrderedDict()

    @abstractmethod
    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
        pool_callback: PoolCallback | None = None,
    ) -> None:
        """Listen for new token creations.

        Args:
            token_callback: Callback function for new tokens
            match_string: Optional string to match in token name/symbol
            creator_address: Optional creator address to filter by
            pool_callback: Called once per mint whose curve migrated to a
                pool, or on shreds once per migrate transaction; None (the
                default) looks for no migrations. The name and creator
                filters do not apply to it.
        """
        pass

    async def _emit_pools(self, pools: list[PoolReady]) -> None:
        """Hand each newly migrated pool to `pool_callback`.

        An executed migration is reported once per mint: a curve migrates
        once. A pre-execution one is reported once per transaction and mint,
        because nothing has run yet: an earlier attempt that will fail, or
        names a pool the coin does not use, must not hide the one that lands.
        A callback that raises is logged and does not stop the listener.
        """
        if self.pool_callback is None:
            return
        for pool in pools:
            key = (pool.mint, pool.signature) if pool.pre_execution else pool.mint
            if key in self._seen_pools:
                continue
            self._seen_pools[key] = None
            if len(self._seen_pools) > _SEEN_POOLS_LIMIT:
                self._seen_pools.popitem(last=False)
            stage = "submitted (pre-execution)" if pool.pre_execution else "done"
            logger.info(
                f"Migration {stage}: {pool.mint} -> pool {pool.pool} "
                f"(quote {pool.quote_mint}) on {pool.platform.value}"
            )
            try:
                await self.pool_callback(pool)
            except Exception:
                reraise_if_cancelled()
                logger.exception(f"Pool callback failed for {pool.mint}")

    def _new_launch(
        self, token_info: TokenInfo, *, pre_execution: bool = False
    ) -> bool:
        """Log a detected coin once; False when its mint was reported already.

        A mint is created once, so a second report is the same coin: a frame
        delivered again (a `processed` notification from a fork that was
        dropped, say) or, before execution, another transaction sending the
        same create. The intake would not buy it again either, so it is not
        reported to the callback again.
        """
        if token_info.mint in self._seen_launches:
            logger.debug(f"{token_info.mint} reported again; already detected")
            return False
        self._seen_launches[token_info.mint] = None
        if len(self._seen_launches) > _SEEN_LAUNCHES_LIMIT:
            self._seen_launches.popitem(last=False)
        logger.info(describe_launch(token_info, pre_execution=pre_execution))
        return True

    def should_process_token(self, token_info: TokenInfo) -> bool:
        """Check if a token should be processed based on platform filter.

        Returns:
            True if token should be processed
        """
        if self.platform is None:
            return True  # Process all platforms
        return token_info.platform == self.platform

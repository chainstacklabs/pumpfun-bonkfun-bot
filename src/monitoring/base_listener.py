"""Platform-agnostic base class for WebSocket token listeners."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from interfaces.core import Platform, TokenInfo


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

    @abstractmethod
    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for new token creations.

        Args:
            token_callback: Callback function for new tokens
            match_string: Optional string to match in token name/symbol
            creator_address: Optional creator address to filter by
        """
        pass

    def should_process_token(self, token_info: TokenInfo) -> bool:
        """Check if a token should be processed based on platform filter.

        Returns:
            True if token should be processed
        """
        if self.platform is None:
            return True  # Process all platforms
        return token_info.platform == self.platform

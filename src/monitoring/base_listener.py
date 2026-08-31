"""
Base class for WebSocket token listeners - now platform-agnostic.
"""

import asyncio
import math
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from interfaces.core import Platform, TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)


class ReconnectLimitError(ConnectionError):
    """Raised after the configured number of consecutive reconnect failures."""


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Finite exponential reconnect policy with capped proportional jitter."""

    max_attempts: int = 8
    initial_delay: float = 1.0
    max_delay: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise TypeError("max_attempts must be a positive integer")
        for name, value in (
            ("initial_delay", self.initial_delay),
            ("max_delay", self.max_delay),
            ("jitter_ratio", self.jitter_ratio),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
            ):
                raise TypeError(f"{name} must be a finite number")
        if self.initial_delay <= 0 or self.max_delay < self.initial_delay:
            raise ValueError("Reconnect delays must be positive and ordered")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")

    def delay(self, attempt: int, *, jitter: float | None = None) -> float:
        """Return a bounded delay or raise once the retry budget is exhausted."""
        if attempt < 1:
            raise ValueError("Reconnect attempt must be positive")
        if attempt > self.max_attempts:
            raise ReconnectLimitError(
                f"Reconnect limit exhausted after {self.max_attempts} attempts"
            )
        bounded = min(self.initial_delay * (2 ** (attempt - 1)), self.max_delay)
        jitter_value = (
            random.uniform(-1.0, 1.0)  # noqa: S311 - non-security retry jitter
            if jitter is None
            else jitter
        )
        if not -1.0 <= jitter_value <= 1.0:
            raise ValueError("jitter must be between -1 and 1")
        return min(
            self.max_delay,
            max(0.0, bounded * (1.0 + self.jitter_ratio * jitter_value)),
        )


class BaseTokenListener(ABC):
    """Base abstract class for token listeners - now platform-agnostic."""

    def __init__(
        self,
        platform: Platform | None = None,
        *,
        reconnect_policy: ReconnectPolicy | None = None,
    ) -> None:
        """Initialize the listener with optional platform and reconnect policy."""
        self.platform = platform
        self.reconnect_policy = reconnect_policy or ReconnectPolicy()
        self.subscription_timeout = 10.0
        self.receive_timeout = 30.0

    @abstractmethod
    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """
        Listen for new token creations.

        Args:
            token_callback: Callback function for new tokens
            match_string: Optional string to match in token name/symbol
            creator_address: Optional creator address to filter by
        """
        raise NotImplementedError

    def should_process_token(self, token_info: TokenInfo) -> bool:
        """Check if a token should be processed based on platform filter.

        Args:
            token_info: Token information

        Returns:
            True if token should be processed
        """
        if self.platform is None:
            return True  # Process all platforms
        return token_info.platform == self.platform

    async def dispatch_token(
        self,
        token_info: TokenInfo,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        *,
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> bool:
        """Apply common filters and deliver one normalized token event."""
        if not self.should_process_token(token_info):
            return False
        if match_string and not (
            match_string.casefold() in token_info.name.casefold()
            or match_string.casefold() in token_info.symbol.casefold()
        ):
            logger.info("Token does not match filter %r. Skipping.", match_string)
            return False
        if creator_address:
            creators = {
                str(value)
                for value in (token_info.creator, token_info.user)
                if value is not None
            }
            if creator_address not in creators:
                logger.info("Token not created by %s. Skipping.", creator_address)
                return False
        await token_callback(token_info)
        return True

    async def cancel_task(self, task: asyncio.Task[object] | None) -> None:
        """Cancel and await a background task without leaking cancellation."""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def wait_before_reconnect(self, attempt: int, reason: BaseException) -> None:
        """Sleep using the bounded retry policy and keep failures observable."""
        delay = self.reconnect_policy.delay(attempt)
        logger.warning(
            "Listener reconnect %d/%d in %.2fs after %s",
            attempt,
            self.reconnect_policy.max_attempts,
            delay,
            reason,
        )
        await asyncio.sleep(delay)

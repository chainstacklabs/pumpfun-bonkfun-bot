"""Bounded token-bucket rate limiter for Solana RPC requests."""

import asyncio
import math
import time

from utils.logger import get_logger

logger = get_logger(__name__)


class TokenBucketRateLimiter:
    """Token bucket rate limiter for controlling RPC request rate.

    Implements a token bucket algorithm that replenishes tokens at a
    fixed rate. Each RPC call consumes one token. When the bucket is
    empty, callers wait until a token becomes available.

    Args:
        max_rps: Maximum requests per second (bucket refill rate).
        burst_size: Maximum burst size (bucket capacity). Defaults to max_rps.
        max_waiters: Maximum number of callers allowed to wait for a token.
        acquire_timeout: Maximum seconds a caller may wait. ``None`` disables
            the timeout while retaining the waiter bound.
    """

    def __init__(
        self,
        max_rps: float,
        burst_size: int | None = None,
        max_waiters: int = 256,
        acquire_timeout: float | None = 30.0,
    ) -> None:
        if isinstance(max_rps, bool) or not isinstance(max_rps, (int, float)):
            raise TypeError("max_rps must be a number")
        try:
            numeric_max_rps = float(max_rps)
        except OverflowError as exc:
            raise ValueError("max_rps is too large") from exc
        if not math.isfinite(numeric_max_rps) or numeric_max_rps <= 0:
            raise ValueError(f"max_rps must be finite and positive, got {max_rps}")
        if burst_size is not None and (
            isinstance(burst_size, bool) or not isinstance(burst_size, int)
        ):
            raise TypeError("burst_size must be an integer")
        if isinstance(max_waiters, bool) or not isinstance(max_waiters, int):
            raise TypeError("max_waiters must be an integer")
        if max_waiters <= 0:
            raise ValueError(f"max_waiters must be positive, got {max_waiters}")
        numeric_timeout: float | None = None
        if acquire_timeout is not None:
            if isinstance(acquire_timeout, bool) or not isinstance(
                acquire_timeout, (int, float)
            ):
                raise TypeError("acquire_timeout must be a number or None")
            try:
                numeric_timeout = float(acquire_timeout)
            except OverflowError as exc:
                raise ValueError("acquire_timeout is too large") from exc
            if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
                raise ValueError(
                    "acquire_timeout must be finite and positive when supplied"
                )

        self._max_rps = numeric_max_rps
        self._burst_size = (
            burst_size if burst_size is not None else max(1, math.ceil(numeric_max_rps))
        )
        if self._burst_size <= 0:
            raise ValueError(f"burst_size must be positive, got {burst_size}")
        try:
            initial_tokens = float(self._burst_size)
        except OverflowError as exc:
            raise ValueError("burst_size is too large") from exc
        if not math.isfinite(initial_tokens):
            raise ValueError("burst_size is too large")
        self._max_waiters = max_waiters
        self._acquire_timeout = numeric_timeout
        self._waiting = 0
        self._tokens = initial_tokens
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token within the configured queue and time bounds.

        Raises:
            RuntimeError: If the bounded waiter queue is full.
            TimeoutError: If no token is available before ``acquire_timeout``.
        """
        if self._waiting >= self._max_waiters:
            raise RuntimeError("RPC rate limiter waiter queue is full")

        self._waiting += 1
        try:
            waiter = self._wait_for_token()
            if self._acquire_timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=self._acquire_timeout)
        finally:
            self._waiting -= 1

    @property
    def pending_waiters(self) -> int:
        """Return the number of callers currently acquiring a token."""
        return self._waiting

    async def _wait_for_token(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_time = (1.0 - self._tokens) / self._max_rps

            await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._burst_size,
            self._tokens + elapsed * self._max_rps,
        )
        self._last_refill = now

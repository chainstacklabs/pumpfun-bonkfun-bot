"""Token bucket rate limiter for Solana RPC requests."""

import asyncio
import time

from utils.logger import get_logger

logger = get_logger(__name__)


class TokenBucketRateLimiter:
    """Token bucket rate limiter for controlling RPC request rate.

    Implements a token bucket algorithm that replenishes tokens at a
    fixed rate. Each RPC call consumes one token. When the bucket is
    empty, callers wait until a token becomes available.

    Args:
        max_rps: Maximum requests per second (bucket refill rate). Must be positive.
        burst_size: Maximum burst size (bucket capacity). Defaults to max_rps.
    """

    def __init__(self, max_rps: float, burst_size: int | None = None) -> None:
        if max_rps <= 0:
            raise ValueError(f"max_rps must be positive, got {max_rps}")
        self._max_rps = max_rps
        self._burst_size = burst_size if burst_size is not None else max(1, int(max_rps))
        self._tokens = float(self._burst_size)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token, waiting if the bucket is empty."""
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

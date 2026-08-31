"""
Universal PumpPortal listener that works with multiple platforms.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from monitoring.event_normalization import (
    NormalizationError,
    attach_event_context,
    normalize_pumpportal_event,
)
from monitoring.subscription import (
    SubscriptionRejected,
    decode_json_frame,
    subscribe_pumpportal,
)
from utils.logger import get_logger

logger = get_logger(__name__)

WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024

PUMPPORTAL_SUPPORTED_PLATFORMS = frozenset({Platform.PUMP_FUN})
PUMPPORTAL_FAILURE_STATUSES = frozenset(
    {"error", "failed", "failure", "rejected", "unsuccessful"}
)


def _reject_failure_envelope(
    envelope: dict[str, Any],
    *,
    context: str,
) -> None:
    """Reject explicit provider failure signals before token processing."""
    status = envelope.get("status")
    status_failed = status is False or (
        isinstance(status, str)
        and status.strip().lower() in PUMPPORTAL_FAILURE_STATUSES
    )
    if isinstance(status, dict):
        status_failed = (
            status.get("success") is False
            or status.get("ok") is False
            or any(status.get(key) is not None for key in ("error", "err", "Err"))
            or any(bool(status.get(key)) for key in PUMPPORTAL_FAILURE_STATUSES)
        )
    error_failed = any(
        envelope.get(key) is not None for key in ("error", "err", "transactionError")
    )
    if error_failed or envelope.get("success") is False or status_failed:
        raise NormalizationError(
            f"PumpPortal {context} reports a failure: {envelope!r}"
        )


def _validate_token_envelope(token_data: dict[str, Any]) -> None:
    """Require PumpPortal's routing and correlation fields before dispatch."""
    invalid_fields = [
        field
        for field in ("signature", "mint", "pool")
        if not isinstance(token_data.get(field), str) or not token_data[field].strip()
    ]
    if invalid_fields:
        raise NormalizationError(
            "PumpPortal token object requires non-empty string fields: "
            f"{invalid_fields}"
        )


class UniversalPumpPortalListener(BaseTokenListener):
    """Universal PumpPortal listener that works with multiple platforms."""

    def __init__(
        self,
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        platforms: list[Platform] | None = None,
    ):
        """Initialize universal PumpPortal listener.

        Args:
            pumpportal_url: PumpPortal WebSocket URL
            platforms: List of platforms to monitor (if None, monitor all supported platforms)
        """
        super().__init__()
        self.pumpportal_url = pumpportal_url
        self.ping_interval = 20  # seconds
        self._pending_frames: deque[str | bytes] = deque()
        self._subscription_ids: frozenset[int] = frozenset()

        from platforms.pumpfun.pumpportal_processor import PumpFunPumpPortalProcessor

        selected_platforms = [Platform.PUMP_FUN] if platforms is None else platforms
        if not selected_platforms:
            raise ValueError(
                "At least one platform is required for PumpPortal listener"
            )
        unsupported = [
            platform
            for platform in selected_platforms
            if platform not in PUMPPORTAL_SUPPORTED_PLATFORMS
        ]
        if unsupported:
            unsupported_names = [
                platform.value if isinstance(platform, Platform) else repr(platform)
                for platform in unsupported
            ]
            raise ValueError(
                "PumpPortal does not support platforms: "
                f"{unsupported_names}. Supported platforms: "
                f"{[Platform.PUMP_FUN.value]}"
            )

        self.processors = [PumpFunPumpPortalProcessor()]

        self.pool_to_processors: dict[str, list] = {}
        for processor in self.processors:
            for pool_name in processor.supported_pool_names:
                self.pool_to_processors.setdefault(pool_name, []).append(processor)

        logger.info(
            f"Initialized Universal PumpPortal listener for platforms: {[p.platform.value for p in self.processors]}"
        )
        logger.info(f"Monitoring pools: {list(self.pool_to_processors.keys())}")

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for acknowledged, successful PumpPortal creation events."""
        reconnect_attempt = 0
        while True:
            ping_task: asyncio.Task[object] | None = None
            try:
                async with websockets.connect(
                    self.pumpportal_url,
                    max_size=WEBSOCKET_MAX_MESSAGE_BYTES,
                ) as websocket:
                    self._pending_frames.clear()
                    await self._subscribe_to_new_tokens(websocket)
                    reconnect_attempt = 0
                    ping_task = asyncio.create_task(self._ping_loop(websocket))
                    try:
                        while True:
                            token_info = await self._wait_for_token_creation(websocket)
                            if token_info is None:
                                continue
                            logger.info(
                                "New token detected: %s (%s) on %s",
                                token_info.name,
                                token_info.symbol,
                                token_info.platform.value,
                            )
                            await self.dispatch_token(
                                token_info,
                                token_callback,
                                match_string=match_string,
                                creator_address=creator_address,
                            )
                    finally:
                        await self.cancel_task(ping_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reconnect_attempt += 1
                await self.wait_before_reconnect(reconnect_attempt, exc)

    async def _subscribe_to_new_tokens(self, websocket: Any) -> None:
        """Subscribe and require PumpPortal's explicit success response."""
        result = await subscribe_pumpportal(
            websocket,
            request_id=1,
            timeout=self.subscription_timeout,
        )
        self._subscription_ids = result.subscription_ids
        self._pending_frames.extend(result.pending_frames)
        logger.info("Confirmed PumpPortal new-token subscription")

    async def _ping_loop(self, websocket: Any) -> None:
        """Keep connection alive with pings.

        Args:
            websocket: Active WebSocket connection
        """
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=10)
                except TimeoutError:
                    logger.warning("Ping timeout - PumpPortal server not responding")
                    # Force reconnection
                    await websocket.close()
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ping error")
            await websocket.close()

    async def _next_frame(self, websocket: Any) -> str | bytes:
        if self._pending_frames:
            return self._pending_frames.popleft()
        return await asyncio.wait_for(websocket.recv(), timeout=self.receive_timeout)

    async def _wait_for_token_creation(self, websocket: Any) -> TokenInfo | None:
        """Wait for one normalized PumpPortal creation event."""
        try:
            data = decode_json_frame(await self._next_frame(websocket))
            _reject_failure_envelope(data, context="event envelope")

            token_data: dict[str, Any] | None = None
            if data.get("method") == "newToken":
                params = data.get("params")
                if (
                    not isinstance(params, list)
                    or not params
                    or not isinstance(params[0], dict)
                ):
                    raise NormalizationError(
                        "PumpPortal newToken params must contain a token object"
                    )
                token_data = params[0]
            elif {"signature", "mint", "pool"}.issubset(data):
                token_data = data
            if token_data is None:
                return None

            _reject_failure_envelope(token_data, context="token envelope")
            _validate_token_envelope(token_data)

            pool_name = str(token_data.get("pool", "")).lower()
            processors = self.pool_to_processors.get(pool_name)
            if not processors:
                logger.debug("Ignoring token from unsupported pool: %s", pool_name)
                return None

            for processor in processors:
                try:
                    if not processor.can_process(token_data):
                        continue
                    event = normalize_pumpportal_event(
                        token_data,
                        platform=processor.platform,
                    )
                    token_info = processor.process_token_data(token_data)
                except NormalizationError:
                    raise
                except Exception:
                    logger.exception(
                        "PumpPortal processor error for %s at signature %s",
                        processor.platform.value,
                        token_data.get("signature"),
                    )
                    continue
                if token_info is not None:
                    return attach_event_context(token_info, event)
            return None
        except TimeoutError:
            logger.debug(
                "No data received from PumpPortal for %.0f seconds",
                self.receive_timeout,
            )
        except ConnectionClosed:
            raise
        except SubscriptionRejected:
            raise
        except NormalizationError as exc:
            logger.warning("Rejected PumpPortal notification: %s", exc)
        except Exception:
            logger.exception("Error processing PumpPortal WebSocket message")
        return None

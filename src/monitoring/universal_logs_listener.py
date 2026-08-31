"""
Universal logs listener that works with any platform through the interface system.
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
    normalize_logs_notification,
)
from monitoring.parser_dispatch import parse_normalized_event
from monitoring.subscription import decode_json_frame, subscribe_json_rpc
from utils.logger import get_logger

logger = get_logger(__name__)

# Solana logsSubscribe / blockSubscribe frames routinely exceed the websockets
# library's 1 MiB default, which closes the connection with code 1009
# ("message too big"). Reconnecting recovers, but every dropped frame is a
# missed token, so raise the ceiling instead of eating the disconnects.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class UniversalLogsListener(BaseTokenListener):
    """Universal logs listener that works with any platform."""

    def __init__(
        self,
        wss_endpoint: str,
        platforms: list[Platform] | None = None,
    ):
        """Initialize universal logs listener.

        Args:
            wss_endpoint: WebSocket endpoint URL
            platforms: List of platforms to monitor (if None, monitor all supported platforms)
        """
        super().__init__()
        self.wss_endpoint = wss_endpoint
        self.ping_interval = 20  # seconds
        self._pending_frames: deque[str | bytes] = deque()
        self._subscription_ids: frozenset[int] = frozenset()

        # Import platform factory and get supported platforms
        from platforms import platform_factory

        if platforms is None:
            # Monitor all supported platforms
            self.platforms = platform_factory.get_supported_platforms()
        else:
            self.platforms = platforms

        # Get event parsers for all platforms
        self.platform_parsers = {}
        self.platform_program_ids = []

        for platform in self.platforms:
            try:
                # Create a simple dummy client that doesn't start blockhash updater
                from core.client import SolanaClient

                # Create a mock client class to avoid network operations
                class DummyClient(SolanaClient):
                    def __init__(self):
                        # Skip SolanaClient.__init__ to avoid starting blockhash updater
                        self.rpc_endpoint = "http://dummy"
                        self._client = None
                        self._cached_blockhash = None
                        self._blockhash_lock = None
                        self._blockhash_updater_task = None

                dummy_client = DummyClient()

                implementations = platform_factory.create_for_platform(
                    platform, dummy_client
                )
                parser = implementations.event_parser
                self.platform_parsers[platform] = parser
                self.platform_program_ids.append(str(parser.get_program_id()))

                logger.info(
                    f"Registered platform {platform.value} with program ID {parser.get_program_id()}"
                )

            except Exception as e:
                logger.warning(f"Could not register platform {platform.value}: {e}")

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for successful token creations using correlated logs subscriptions."""
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        reconnect_attempt = 0
        while True:
            ping_task: asyncio.Task[object] | None = None
            try:
                async with websockets.connect(
                    self.wss_endpoint,
                    max_size=WEBSOCKET_MAX_MESSAGE_BYTES,
                ) as websocket:
                    self._pending_frames.clear()
                    await self._subscribe_to_logs(websocket)
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

    async def _subscribe_to_logs(self, websocket: Any) -> None:
        """Subscribe to every program and require correlated acknowledgements."""
        requests = [
            {
                "jsonrpc": "2.0",
                "id": index + 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [program_id]},
                    {"commitment": "processed"},
                ],
            }
            for index, program_id in enumerate(self.platform_program_ids)
        ]
        result = await subscribe_json_rpc(
            websocket,
            requests,
            timeout=self.subscription_timeout,
        )
        self._subscription_ids = result.subscription_ids
        self._pending_frames.extend(result.pending_frames)
        logger.info(
            "Confirmed %d logs subscriptions: %s",
            len(result.subscription_ids),
            sorted(result.subscription_ids),
        )

    async def _ping_loop(self, websocket) -> None:
        """Keep connection alive with pings."""
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=10)
                except TimeoutError:
                    logger.warning("Ping timeout - server not responding")
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
        """Wait for one successful, correlated logs notification."""
        try:
            frame = await self._next_frame(websocket)
            data = decode_json_frame(frame)
            event = normalize_logs_notification(
                data,
                subscription_ids=self._subscription_ids,
                commitment="processed",
            )
            if event is None:
                return None
            tokens = parse_normalized_event(event, self.platform_parsers)
            if len(tokens) > 1:
                logger.error(
                    "Ambiguous logs notification %s matched %d creations; rejecting",
                    event.signature,
                    len(tokens),
                )
                return None
            return tokens[0] if tokens else None
        except TimeoutError:
            logger.debug("No data received for %.0f seconds", self.receive_timeout)
        except ConnectionClosed:
            raise
        except NormalizationError as exc:
            logger.warning("Rejected logs notification: %s", exc)
        except Exception:
            logger.exception("Error processing logs WebSocket message")
        return None

"""
Universal logs listener that works with any platform through the interface system.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
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
        """Listen for new token creations using logsSubscribe.

        Args:
            token_callback: Callback function for new tokens
            match_string: Optional string to match in token name/symbol
            creator_address: Optional creator address to filter by
        """
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        while True:
            try:
                async with websockets.connect(
                    self.wss_endpoint, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
                ) as websocket:
                    await self._subscribe_to_logs(websocket)
                    ping_task = asyncio.create_task(self._ping_loop(websocket))

                    try:
                        while True:
                            token_info = await self._wait_for_token_creation(websocket)
                            if not token_info:
                                continue

                            logger.info(
                                f"New token detected: {token_info.name} ({token_info.symbol}) on {token_info.platform.value}"
                            )

                            # Apply filters
                            if match_string and not (
                                match_string.lower() in token_info.name.lower()
                                or match_string.lower() in token_info.symbol.lower()
                            ):
                                logger.info(
                                    f"Token does not match filter '{match_string}'. Skipping..."
                                )
                                continue

                            if (
                                creator_address
                                and str(token_info.user) != creator_address
                            ):
                                logger.info(
                                    f"Token not created by {creator_address}. Skipping..."
                                )
                                continue

                            await token_callback(token_info)

                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("WebSocket connection closed. Reconnecting...")
                        ping_task.cancel()

            except Exception:
                logger.exception("WebSocket connection error")
                logger.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def _subscribe_to_logs(self, websocket) -> None:
        """Subscribe to logs mentioning any of the monitored program IDs.

        Args:
            websocket: Active WebSocket connection
        """
        # Subscribe to logs for all monitored platforms
        for i, program_id in enumerate(self.platform_program_ids):
            subscription_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [program_id]},
                        {"commitment": "processed"},
                    ],
                }
            )

            await websocket.send(subscription_message)
            logger.info(f"Subscribed to logs mentioning program: {program_id}")

            # Wait for subscription confirmation
            response = await websocket.recv()
            response_data = json.loads(response)
            if "result" in response_data:
                logger.info(
                    f"Subscription confirmed with ID: {response_data['result']}"
                )
            else:
                logger.warning(f"Unexpected subscription response: {response}")

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

    async def _wait_for_token_creation(self, websocket) -> TokenInfo | None:
        """Wait for token creation events from any platform."""
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)

            if "method" not in data or data["method"] != "logsNotification":
                return None

            log_data = data["params"]["result"]["value"]
            logs = log_data.get("logs", [])
            signature = log_data.get("signature", "unknown")

            # Try each platform's event parser
            for platform, parser in self.platform_parsers.items():
                token_info = parser.parse_token_creation_from_logs(logs, signature)
                if token_info:
                    return token_info

            return None

        except TimeoutError:
            logger.debug("No data received for 30 seconds")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            raise
        except Exception:
            logger.exception("Error processing WebSocket message")

        return None

"""Universal PumpPortal listener that works with multiple platforms."""

import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from utils.logger import get_logger

logger = get_logger(__name__)


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

        # Get platform-specific processors
        from platforms.letsbonk.pumpportal_processor import LetsBonkPumpPortalProcessor
        from platforms.pumpfun.pumpportal_processor import PumpFunPumpPortalProcessor

        # Create processor instances
        all_processors = [
            PumpFunPumpPortalProcessor(),
            LetsBonkPumpPortalProcessor(),
        ]

        # Filter processors based on requested platforms
        if platforms is None:
            self.processors = all_processors
        else:
            self.processors = [p for p in all_processors if p.platform in platforms]

        # Build mapping of pool names to processors for quick lookup
        self.pool_to_processors: dict[str, list] = {}
        for processor in self.processors:
            for pool_name in processor.supported_pool_names:
                if pool_name not in self.pool_to_processors:
                    self.pool_to_processors[pool_name] = []
                self.pool_to_processors[pool_name].append(processor)

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
        """Listen for new token creations using PumpPortal WebSocket.

        Args:
            token_callback: Callback function for new tokens
            match_string: Optional string to match in token name/symbol
            creator_address: Optional creator address to filter by
        """
        while True:
            try:
                async with websockets.connect(self.pumpportal_url) as websocket:
                    await self._subscribe_to_new_tokens(websocket)
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

                            if creator_address:
                                creator_str = (
                                    str(token_info.creator)
                                    if token_info.creator
                                    else ""
                                )
                                user_str = (
                                    str(token_info.user) if token_info.user else ""
                                )
                                if creator_address not in [creator_str, user_str]:
                                    logger.info(
                                        f"Token not created by {creator_address}. Skipping..."
                                    )
                                    continue

                            await token_callback(token_info)

                    except websockets.exceptions.ConnectionClosed:
                        logger.warning(
                            "PumpPortal WebSocket connection closed. Reconnecting..."
                        )
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except Exception:
                logger.exception("PumpPortal WebSocket connection error")
                logger.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def _subscribe_to_new_tokens(self, websocket) -> None:
        """Subscribe to new token events from PumpPortal.

        Args:
            websocket: Active WebSocket connection
        """
        subscription_message = json.dumps({"method": "subscribeNewToken", "params": []})

        await websocket.send(subscription_message)
        logger.info("Subscribed to PumpPortal new token events")

    async def _ping_loop(self, websocket) -> None:
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

    async def _wait_for_token_creation(self, websocket) -> TokenInfo | None:
        """Wait for token creation event from PumpPortal.

        Args:
            websocket: Active WebSocket connection

        Returns:
            TokenInfo if a token creation is found, None otherwise
        """
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)

            # Handle different message formats from PumpPortal
            token_data = None
            if "method" in data and data["method"] == "newToken":
                # Standard newToken method format
                params = data.get("params", [])
                if params and len(params) > 0:
                    token_data = params[0]
            elif "signature" in data and "mint" in data and "pool" in data:
                # Direct token data format
                token_data = data

            if not token_data:
                return None

            # Get pool name to determine which processor to use
            pool_name = token_data.get("pool", "").lower()
            if pool_name not in self.pool_to_processors:
                logger.debug(f"Ignoring token from unsupported pool: {pool_name}")
                return None

            # Try each processor that supports this pool
            for processor in self.pool_to_processors[pool_name]:
                if processor.can_process(token_data):
                    token_info = processor.process_token_data(token_data)
                    if token_info:
                        logger.debug(
                            f"Successfully processed token using {processor.platform.value} processor"
                        )
                        return token_info

            logger.debug(f"No processor could handle token data from pool {pool_name}")
            return None

        except TimeoutError:
            logger.debug("No data received from PumpPortal for 30 seconds")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("PumpPortal WebSocket connection closed")
            raise
        except json.JSONDecodeError:
            logger.exception("Failed to decode PumpPortal message")
        except Exception:
            logger.exception("Error processing PumpPortal WebSocket message")

        return None

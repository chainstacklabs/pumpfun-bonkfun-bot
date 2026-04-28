"""
Universal block listener that works with any platform through the interface system.
"""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import websockets
from solders.transaction import VersionedTransaction

from core.client import SolanaClient
from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from platforms import get_platform_implementations, platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)


class UniversalBlockListener(BaseTokenListener):
    """Universal block listener that works with any platform."""

    def __init__(
        self,
        wss_endpoint: str,
        platforms: list[Platform] | None = None,
    ) -> None:
        """Initialize universal block listener.

        Args:
            wss_endpoint: WebSocket endpoint URL
            platforms: List of platforms to monitor (if None, monitor all supported platforms)
        """
        super().__init__()
        self.wss_endpoint = wss_endpoint
        self.ping_interval = 20  # seconds

        # Get supported platforms
        if platforms is None:
            # Monitor all supported platforms
            self.platforms = platform_factory.get_supported_platforms()
        else:
            self.platforms = platforms

        # Get event parsers for all platforms
        self.platform_parsers = {}
        self.platform_program_ids = []
        # Map program IDs to their parsers for faster lookup
        self.program_id_to_parser = {}

        for platform in self.platforms:
            try:
                # Create a mock client class to avoid network operations
                class DummyClient(SolanaClient):
                    def __init__(self) -> None:
                        # Skip SolanaClient.__init__ to avoid starting blockhash updater
                        self.rpc_endpoint = "http://dummy"
                        self._client = None
                        self._cached_blockhash = None
                        self._blockhash_lock = None
                        self._blockhash_updater_task = None

                dummy_client = DummyClient()

                implementations = get_platform_implementations(platform, dummy_client)
                parser = implementations.event_parser
                self.platform_parsers[platform] = parser
                program_id_str = str(parser.get_program_id())
                self.platform_program_ids.append(program_id_str)
                self.program_id_to_parser[program_id_str] = (platform, parser)

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
        """Listen for new token creations using blockSubscribe.

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
                async with websockets.connect(self.wss_endpoint) as websocket:
                    await self._subscribe_to_programs(websocket)
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

    async def _subscribe_to_programs(
        self, websocket: websockets.WebSocketServerProtocol
    ) -> None:
        """Subscribe to blocks mentioning any of the monitored program IDs.

        Args:
            websocket: Active WebSocket connection
        """
        # For block subscriptions, we can use mentionsAccountOrProgram to monitor multiple programs
        # We'll create separate subscriptions for each program to be more specific
        for i, program_id in enumerate(self.platform_program_ids):
            subscription_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentionsAccountOrProgram": program_id},
                        {
                            "commitment": "confirmed",
                            "encoding": "base64",
                            "showRewards": False,
                            "transactionDetails": "full",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                }
            )

            await websocket.send(subscription_message)
            logger.info(f"Subscribed to blocks mentioning program: {program_id}")

    async def _ping_loop(self, websocket: websockets.WebSocketServerProtocol) -> None:
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
                    logger.warning("Ping timeout - server not responding")
                    # Force reconnection
                    await websocket.close()
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ping error")

    async def _wait_for_token_creation(
        self, websocket: websockets.WebSocketServerProtocol
    ) -> TokenInfo | None:
        """Wait for token creation event from any platform.

        Args:
            websocket: Active WebSocket connection

        Returns:
            TokenInfo if a token creation is found, None otherwise
        """
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)

            # Handle subscription errors
            if "error" in data:
                logger.error(f"Block subscription error: {data['error']}")
                return None
            elif "result" in data:
                # Subscription confirmation - continue waiting for notifications
                return None

            if "method" not in data or data["method"] != "blockNotification":
                return None

            if "params" not in data or "result" not in data["params"]:
                return None

            block_data = data["params"]["result"]
            if "value" not in block_data or "block" not in block_data["value"]:
                return None

            block = block_data["value"]["block"]
            if "transactions" not in block:
                return None

            # Process all transactions in the block for token creations
            return self._process_block_transactions(block["transactions"])

        except TimeoutError:
            logger.debug("No data received for 30 seconds")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            raise
        except Exception:
            logger.exception("Error processing WebSocket message")

        return None

    def _process_block_transactions(self, transactions: list) -> TokenInfo | None:
        """Process all transactions in a block looking for token creations.

        Args:
            transactions: List of transaction data from block

        Returns:
            TokenInfo if a token creation is found, None otherwise
        """
        for tx in transactions:
            if not isinstance(tx, dict) or "transaction" not in tx:
                continue

            # Skip failed txs — sniping a failed create yields a non-existent
            # mint and the buy-side tx then fails with InvalidMint.
            meta = tx.get("meta")
            if isinstance(meta, dict) and meta.get("err") is not None:
                continue

            tx_data = tx["transaction"]

            # Handle base64 encoded transaction data
            if isinstance(tx_data, list) and len(tx_data) > 0:
                token_info = self._parse_encoded_transaction(tx, tx_data[0])
                if token_info:
                    return token_info

            # Handle already decoded transaction data (shouldn't happen in blockSubscribe)
            elif isinstance(tx_data, dict) and "message" in tx_data:
                token_info = self._parse_decoded_transaction(tx, tx_data)
                if token_info:
                    return token_info

        return None

    def _parse_encoded_transaction(
        self, tx: dict, encoded_data: str
    ) -> TokenInfo | None:
        """Parse base64 encoded transaction data.

        Args:
            tx: Transaction wrapper from block
            encoded_data: Base64 encoded transaction data

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            tx_bytes = base64.b64decode(encoded_data)
            transaction = VersionedTransaction.from_bytes(tx_bytes)

            # Check if any of the instructions use our monitored programs
            for instruction in transaction.message.instructions:
                program_id = str(
                    transaction.message.account_keys[instruction.program_id_index]
                )

                # Check if this program ID is one we're monitoring
                if program_id in self.program_id_to_parser:
                    platform, parser = self.program_id_to_parser[program_id]

                    # Try to parse with the appropriate parser
                    try:
                        if hasattr(parser, "parse_token_creation_from_block"):
                            token_info = parser.parse_token_creation_from_block(
                                {"transactions": [tx]}
                            )
                            if token_info:
                                return token_info
                    except Exception:
                        # Expected for non-creation transactions
                        continue

        except Exception:
            # Failed to decode transaction - skip it
            pass

        return None

    def _parse_decoded_transaction(self, tx: dict, tx_data: dict) -> TokenInfo | None:
        """Parse already decoded transaction data.

        Args:
            tx: Transaction wrapper from block
            tx_data: Decoded transaction data

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        message = tx_data["message"]
        if "instructions" not in message or "accountKeys" not in message:
            return None

        for ix in message["instructions"]:
            if "programIdIndex" not in ix:
                continue

            program_idx = ix["programIdIndex"]
            if program_idx >= len(message["accountKeys"]):
                continue

            program_id = message["accountKeys"][program_idx]

            # Check if this program ID is one we're monitoring
            if program_id in self.program_id_to_parser:
                platform, parser = self.program_id_to_parser[program_id]

                try:
                    if hasattr(parser, "parse_token_creation_from_block"):
                        token_info = parser.parse_token_creation_from_block(
                            {"transactions": [tx]}
                        )
                        if token_info:
                            return token_info
                except Exception:
                    # Expected for non-creation transactions
                    continue

        return None

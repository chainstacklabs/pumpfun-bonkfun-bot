"""
Universal block listener that works with any platform through the interface system.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from core.client import SolanaClient
from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from monitoring.event_normalization import (
    NormalizationError,
    normalize_block_notification,
    normalize_block_transaction,
)
from monitoring.parser_dispatch import parse_normalized_event
from monitoring.subscription import (
    SubscriptionCorrelationError,
    decode_json_frame,
    subscribe_json_rpc,
)
from platforms import get_platform_implementations, platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)

# Solana logsSubscribe / blockSubscribe frames routinely exceed the websockets
# library's 1 MiB default, which closes the connection with code 1009
# ("message too big"). Reconnecting recovers, but every dropped frame is a
# missed token, so raise the ceiling instead of eating the disconnects.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024

MAX_RECENT_CREATIONS = 4096
CreationKey = tuple[str, int | None, int | None, str]


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
        self._pending_frames: deque[str | bytes] = deque()
        self._subscription_platforms: dict[int, Platform] = {}
        self._recent_creation_order: deque[CreationKey] = deque()
        self._recent_creation_keys: set[CreationKey] = set()

        # Get supported platforms, preserving configuration order while ensuring
        # one subscription per platform.
        configured_platforms = (
            platform_factory.get_supported_platforms()
            if platforms is None
            else platforms
        )
        self.platforms = list(dict.fromkeys(configured_platforms))

        # Get event parsers for all platforms.
        self.platform_parsers: dict[Platform, Any] = {}
        self.platform_program_ids: dict[Platform, str] = {}

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
                program_id_str = str(parser.get_program_id())
                self.platform_parsers[platform] = parser
                self.platform_program_ids[platform] = program_id_str

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
        """Listen for every successful creation in correlated block updates."""
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
                    await self._subscribe_to_programs(websocket)
                    reconnect_attempt = 0
                    ping_task = asyncio.create_task(self._ping_loop(websocket))
                    try:
                        while True:
                            token_infos = await self._wait_for_token_creation(websocket)
                            for token_info in token_infos:
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

    async def _subscribe_to_programs(self, websocket: Any) -> None:
        """Subscribe once per platform and retain acknowledgement correlation."""
        subscription_platforms: dict[int, Platform] = {}
        pending_frames: list[str | bytes] = []
        for request_id, (platform, program_id) in enumerate(
            self.platform_program_ids.items(),
            start=1,
        ):
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
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
            result = await subscribe_json_rpc(
                websocket,
                [request],
                timeout=self.subscription_timeout,
            )
            if len(result.subscription_ids) != 1:
                raise SubscriptionCorrelationError(
                    f"blockSubscribe request {request_id} was not uniquely acknowledged"
                )
            subscription_id = next(iter(result.subscription_ids))
            if subscription_id in subscription_platforms:
                raise SubscriptionCorrelationError(
                    f"blockSubscribe returned duplicate subscription ID {subscription_id}"
                )
            subscription_platforms[subscription_id] = platform
            pending_frames.extend(result.pending_frames)

        self._subscription_platforms = subscription_platforms
        self._pending_frames.extend(pending_frames)
        logger.info(
            "Confirmed %d correlated block subscriptions: %s",
            len(subscription_platforms),
            sorted(subscription_platforms),
        )

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
                    logger.warning("Ping timeout - server not responding")
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

    async def _wait_for_token_creation(self, websocket: Any) -> list[TokenInfo]:
        """Wait for a block and return every valid token creation it contains."""
        try:
            frame = await self._next_frame(websocket)
            data = decode_json_frame(frame)
            notification = normalize_block_notification(
                data,
                subscription_ids=frozenset(self._subscription_platforms),
            )
            if notification is None:
                return []
            params = data.get("params")
            subscription_id = (
                params.get("subscription") if isinstance(params, dict) else None
            )
            platform = self._subscription_platforms.get(subscription_id)
            if platform is None:
                raise NormalizationError(
                    f"Block notification has unknown subscription {subscription_id!r}"
                )
            slot, transactions = notification
            return self._process_block_transactions(
                transactions,
                platform=platform,
                slot=slot,
                commitment="confirmed",
            )
        except TimeoutError:
            logger.debug("No data received for %.0f seconds", self.receive_timeout)
        except ConnectionClosed:
            raise
        except NormalizationError as exc:
            logger.warning("Rejected block notification: %s", exc)
        except Exception:
            logger.exception("Error processing block WebSocket message")
        return []

    def _process_block_transactions(
        self,
        transactions: list[dict[str, Any]],
        *,
        platform: Platform,
        slot: int | None = None,
        commitment: str = "confirmed",
    ) -> list[TokenInfo]:
        """Parse a block only with the parser correlated to its subscription."""
        parser = self.platform_parsers.get(platform)
        if parser is None:
            logger.error(
                "No parser registered for correlated platform %s",
                platform.value,
            )
            return []

        tokens: list[TokenInfo] = []
        for transaction_index, tx_wrapper in enumerate(transactions):
            try:
                event = normalize_block_transaction(
                    tx_wrapper,
                    slot=slot,
                    commitment=commitment,
                    transaction_index=transaction_index,
                )
            except NormalizationError as exc:
                logger.warning(
                    "Rejected block transaction tx=%d slot=%s: %s",
                    transaction_index,
                    slot,
                    exc,
                )
                continue
            try:
                tokens.extend(parse_normalized_event(event, {platform: parser}))
            except Exception:
                logger.exception(
                    "Parser dispatch failed for block transaction tx=%d slot=%s",
                    transaction_index,
                    slot,
                )
        return self._deduplicate_creations(tokens)

    @staticmethod
    def _creation_key(token: TokenInfo) -> CreationKey | None:
        monitoring = (
            token.additional_data.get("monitoring")
            if isinstance(token.additional_data, dict)
            else None
        )
        if not isinstance(monitoring, dict):
            return None
        signature = token.signature
        instruction_index = monitoring.get("instruction_index")
        inner_index = monitoring.get("inner_index")
        mint = str(token.mint)
        if not isinstance(signature, str) or not signature or not mint:
            return None
        if instruction_index is not None and type(instruction_index) is not int:
            return None
        if inner_index is not None and type(inner_index) is not int:
            return None
        return signature, instruction_index, inner_index, mint

    def _deduplicate_creations(self, tokens: list[TokenInfo]) -> list[TokenInfo]:
        unique: list[TokenInfo] = []
        for token in tokens:
            key = self._creation_key(token)
            if key is None:
                logger.warning(
                    "Rejected block creation without stable coordinates for mint %s",
                    token.mint,
                )
                continue
            if key in self._recent_creation_keys:
                logger.debug("Suppressed duplicate block creation %s", key)
                continue
            if len(self._recent_creation_order) >= MAX_RECENT_CREATIONS:
                expired = self._recent_creation_order.popleft()
                self._recent_creation_keys.remove(expired)
            self._recent_creation_order.append(key)
            self._recent_creation_keys.add(key)
            unique.append(token)
        return unique

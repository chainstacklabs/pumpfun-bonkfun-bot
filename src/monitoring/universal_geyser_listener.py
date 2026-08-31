"""
Universal Geyser listener that works with any platform through the interface system.
"""

import asyncio
from collections.abc import Awaitable, Callable

import grpc

from geyser.generated import geyser_pb2, geyser_pb2_grpc
from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from monitoring.event_normalization import NormalizationError, normalize_geyser_update
from monitoring.parser_dispatch import parse_normalized_event
from platforms import platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)


class UniversalGeyserListener(BaseTokenListener):
    """Universal Geyser listener that works with any platform."""

    def __init__(
        self,
        geyser_endpoint: str,
        geyser_api_token: str,
        geyser_auth_type: str,
        platforms: list[Platform] | None = None,
    ):
        """Initialize universal Geyser listener."""
        super().__init__()
        self.geyser_endpoint = geyser_endpoint
        self.geyser_api_token = geyser_api_token

        valid_auth_types = {"x-token", "basic"}
        self.auth_type: str = (geyser_auth_type or "x-token").lower()
        if self.auth_type not in valid_auth_types:
            raise ValueError(
                f"Unsupported auth_type={self.auth_type!r}. "
                f"Expected one of {valid_auth_types}"
            )

        if platforms is None:
            self.platforms = platform_factory.get_supported_platforms()
        else:
            self.platforms = platforms

        # Get event parsers for all platforms
        self.platform_parsers = {}
        self.platform_program_ids = set()

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
                self.platform_program_ids.add(parser.get_program_id())

                logger.info(
                    f"Registered platform {platform.value} with program ID {parser.get_program_id()}"
                )

            except Exception as e:
                logger.warning(f"Could not register platform {platform.value}: {e}")

    async def _create_geyser_connection(self):
        """Establish a secure connection to the Geyser endpoint."""

        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("x-token", self.geyser_api_token),), None
                )
            )
        else:  # Default to basic auth
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("authorization", f"Basic {self.geyser_api_token}"),), None
                )
            )
        creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
        channel = grpc.aio.secure_channel(self.geyser_endpoint, creds)

        return geyser_pb2_grpc.GeyserStub(channel), channel

    def _create_subscription_request(self):
        """Create a subscription request for all monitored platforms."""

        request = geyser_pb2.SubscribeRequest()

        # Add all platform program IDs to the filter
        for program_id in self.platform_program_ids:
            filter_name = f"platform_filter_{program_id}"
            request.transactions[filter_name].account_include.append(str(program_id))
            request.transactions[filter_name].failed = False

        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return request

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for successful creations on an acknowledged Geyser stream."""
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        reconnect_attempt = 0
        while True:
            channel = None
            call = None
            failure: Exception | None = None
            try:
                stub, channel = await self._create_geyser_connection()
                request = self._create_subscription_request()
                call = stub.Subscribe(iter([request]))
                try:
                    await asyncio.wait_for(
                        call.initial_metadata(),
                        timeout=self.subscription_timeout,
                    )
                except TimeoutError as exc:
                    raise TimeoutError(
                        "Geyser subscription acknowledgement timed out"
                    ) from exc

                reconnect_attempt = 0
                logger.info("Connected to Geyser endpoint: %s", self.geyser_endpoint)
                logger.info(
                    "Monitoring platforms: %s",
                    [platform.value for platform in self.platforms],
                )
                logger.info(
                    "Monitoring program IDs: %s",
                    [str(program_id) for program_id in self.platform_program_ids],
                )

                async for update in call:
                    token_infos = self._process_update_events(update)
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
                raise ConnectionError("Geyser subscription stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
                if isinstance(exc, grpc.aio.AioRpcError):
                    logger.error("Geyser RPC error: %s", exc.details())
                reconnect_attempt += 1
            finally:
                if call is not None:
                    call.cancel()
                if channel is not None:
                    await channel.close()
            if failure is not None:
                await self.wait_before_reconnect(reconnect_attempt, failure)

    def _process_update_events(self, update: object) -> list[TokenInfo]:
        """Normalize one Geyser transaction and return all valid creations."""
        try:
            event = normalize_geyser_update(update, commitment="processed")
            if event is None:
                return []
            return parse_normalized_event(event, self.platform_parsers)
        except NormalizationError as exc:
            logger.warning("Rejected Geyser update: %s", exc)
        except Exception:
            logger.exception("Error processing Geyser update")
        return []

    async def _process_update(self, update: object) -> TokenInfo | None:
        """Compatibility wrapper returning the first normalized creation."""
        token_infos = self._process_update_events(update)
        return token_infos[0] if token_infos else None

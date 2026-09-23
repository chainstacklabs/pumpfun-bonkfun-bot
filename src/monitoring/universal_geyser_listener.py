"""Universal Geyser gRPC listener, platform-agnostic via the interfaces."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import grpc

from geyser.generated import geyser_pb2, geyser_pb2_grpc
from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from platforms import platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)


def _describe_envelope(update: geyser_pb2.SubscribeUpdate) -> str:
    """Summarise the transaction format a coin was created in, for the log.

    Transaction v1 (SIMD-0385) carries its compute budget inline on the message
    as `config` rather than as ComputeBudget instructions, and geyser sets that
    field only for v1, so its presence is the version test — `versioned` is true
    for v0 and v1 alike. Diagnostic only; detection stays on `meta.log_messages`.

    Args:
        update: The geyser update a TokenInfo was just parsed out of

    Returns:
        A one-line summary of the version, inline budget and reported cost
    """
    transaction = update.transaction.transaction
    message = transaction.transaction.message

    if not message.HasField("config"):
        return "v0" if message.versioned else "legacy"

    config = message.config
    # Every field has presence, and an unset one means zero rather than a
    # runtime default, so report "unset" rather than implying a fallback.
    parts = [
        f"priority_fee={config.priority_fee} lamports"
        if config.HasField("priority_fee")
        else "priority_fee=unset",
        f"cu_limit={config.compute_unit_limit}"
        if config.HasField("compute_unit_limit")
        else "cu_limit=unset",
        f"data_size={config.loaded_accounts_data_size_limit}"
        if config.HasField("loaded_accounts_data_size_limit")
        else "data_size=unset",
    ]
    if config.HasField("heap_size"):
        parts.append(f"heap={config.heap_size}")
    if transaction.meta.HasField("cost_units"):
        parts.append(f"cost_units={transaction.meta.cost_units}")

    return f"v1 ({', '.join(parts)})"


def _log_envelope(update: geyser_pb2.SubscribeUpdate) -> None:
    """Report the creating transaction's format, when debug logging is on.

    The summary is built only if it will be printed: this sits between detection
    and submission, which extreme_fast_mode keeps free of avoidable work.

    Args:
        update: The geyser update a TokenInfo was just parsed out of
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Creating transaction envelope: {_describe_envelope(update)}")


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
        """Listen for new token creations using Geyser subscription."""
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        while True:
            try:
                stub, channel = await self._create_geyser_connection()
                request = self._create_subscription_request()

                logger.info(f"Connected to Geyser endpoint: {self.geyser_endpoint}")
                logger.info(
                    f"Monitoring platforms: {[p.value for p in self.platforms]}"
                )
                logger.info(
                    f"Monitoring program IDs: {[str(pid) for pid in self.platform_program_ids]}"
                )

                try:
                    async for update in stub.Subscribe(iter([request])):
                        token_info = await self._process_update(update)
                        if not token_info:
                            continue

                        logger.info(
                            f"New token detected: {token_info.name} ({token_info.symbol}) on {token_info.platform.value}"
                        )
                        _log_envelope(update)

                        # Apply filters
                        if match_string and not (
                            match_string.lower() in token_info.name.lower()
                            or match_string.lower() in token_info.symbol.lower()
                        ):
                            logger.info(
                                f"Token does not match filter '{match_string}'. Skipping..."
                            )
                            continue

                        if creator_address and str(token_info.user) != creator_address:
                            logger.info(
                                f"Token not created by {creator_address}. Skipping..."
                            )
                            continue

                        await token_callback(token_info)

                except Exception as e:
                    if isinstance(e, grpc.aio.AioRpcError):
                        logger.exception(f"gRPC error: {e.details()}")
                    else:
                        logger.exception("Geyser error occurred")
                    await asyncio.sleep(5)

                finally:
                    await channel.close()

            except Exception:
                logger.exception("Geyser connection error")
                logger.info("Reconnecting in 10 seconds...")
                await asyncio.sleep(10)

    async def _process_update(self, update) -> TokenInfo | None:
        """Process a Geyser update and extract token creation info.

        Delegates to each platform parser's geyser method rather than decoding
        instructions here: the parser prefers the CreateEvent from
        meta.log_messages, which carries the canonical creator (instruction
        args.creator is user-supplied) and marks the TokenInfo
        state_from_event so extreme_fast_mode can buy with zero RPC calls.
        Each parser filters on its own program id internally.
        """
        try:
            if not update.HasField("transaction"):
                return None

            for parser in self.platform_parsers.values():
                token_info = parser.parse_token_creation_from_geyser(update)
                if token_info:
                    return token_info

            return None

        except Exception:
            logger.exception("Error processing Geyser update")
            return None

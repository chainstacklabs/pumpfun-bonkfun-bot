"""Universal shreds listener: pre-execution token detection over SubscribeDeshred.

`SubscribeDeshred` delivers a transaction as entries form from shreds, before any
execution. That is earlier than `Subscribe`, and it costs two things.

**There is no TransactionStatusMeta**, so no `meta.log_messages` and no
CreateEvent: this listener decodes the create instruction, the fallback route
elsewhere and the only route here. It also means no outcome — a create that goes
on to revert is delivered exactly like one that lands.

**A coin created through a router is invisible.** The create reaches the program
as a CPI, and inner instructions are produced *by* execution, so a pre-execution
stream never carries them — undetectable, not dropped, and no decoding recovers
them. That is the standing cost of the mode and why it is not the default; see
bots/bot-sniper-5-shreds.yaml.

This inverts the usual CPI advice in docs/listeners-and-geyser.md: *trades* are
overwhelmingly inner instructions, *creates* overwhelmingly top-level, so
walking only top-level instructions here is correct.

Address lookup tables are resolved and reported on the update itself, so
instruction account indices resolve as they do on the executed stream.
"""

import asyncio
from collections.abc import Awaitable, Callable

import grpc
from solders.pubkey import Pubkey

from geyser.generated import geyser_pb2, geyser_pb2_grpc
from interfaces.core import EventParser, Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from platforms import platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)

# Seconds to wait before retrying a dropped stream, matching the geyser listener.
_STREAM_RETRY_SECONDS = 5
_CONNECTION_RETRY_SECONDS = 10


class UniversalShredsListener(BaseTokenListener):
    """Pre-execution token listener over the geyser deshred stream."""

    def __init__(
        self,
        geyser_endpoint: str,
        geyser_api_token: str,
        geyser_auth_type: str,
        platforms: list[Platform] | None = None,
    ):
        """Initialize the shreds listener.

        Args:
            geyser_endpoint: Geyser gRPC endpoint URL
            geyser_auth_type: Either "x-token" or "basic"
            platforms: Platforms to monitor; all supported platforms if None

        Raises:
            ValueError: If geyser_auth_type is not a supported value
        """
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

        self.platform_parsers: dict[Platform, EventParser] = {}
        self.platform_program_ids: dict[bytes, EventParser] = {}
        self.create_discriminators: dict[bytes, tuple[bytes, ...]] = {}
        # Flattened across platforms for the str.startswith prefix test.
        self.all_discriminators: tuple[bytes, ...] = ()

        for platform in self.platforms:
            try:
                from core.client import SolanaClient

                class DummyClient(SolanaClient):
                    """Client stub that skips the blockhash updater.

                    Parsers only decode here; nothing in this listener reaches
                    the network through the client.
                    """

                    def __init__(self):
                        self.rpc_endpoint = "http://dummy"
                        self._client = None
                        self._cached_blockhash = None
                        self._blockhash_lock = None
                        self._blockhash_updater_task = None

                implementations = platform_factory.create_for_platform(
                    platform, DummyClient()
                )
                parser = implementations.event_parser
                self.platform_parsers[platform] = parser
                self.platform_program_ids[bytes(parser.get_program_id())] = parser
                # Cheap prefix gate, see _process_update: the deshred stream
                # carries every pump transaction pre-execution, so most
                # instructions reaching us are trades, not creates.
                self.create_discriminators[bytes(parser.get_program_id())] = tuple(
                    parser.get_instruction_discriminators()
                )
                self.all_discriminators += tuple(
                    parser.get_instruction_discriminators()
                )

                logger.info(
                    f"Registered platform {platform.value} with program ID "
                    f"{parser.get_program_id()}"
                )

            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not register platform {platform.value}: {e}")

    async def _create_geyser_connection(self):
        """Establish a secure connection to the Geyser endpoint.

        Returns:
            A (stub, channel) pair; the caller owns closing the channel
        """
        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("x-token", self.geyser_api_token),), None
                )
            )
        else:
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("authorization", f"Basic {self.geyser_api_token}"),), None
                )
            )
        creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
        channel = grpc.aio.secure_channel(self.geyser_endpoint, creds)

        return geyser_pb2_grpc.GeyserStub(channel), channel

    def _create_subscription_request(self) -> geyser_pb2.SubscribeDeshredRequest:
        """Build the deshred subscription request for all monitored platforms.

        This is its own request type, not a SubscribeRequest: the deshred stream
        carries no commitment level, because commitment describes execution and
        nothing has executed. There is no `failed` filter for the same reason.

        Returns:
            The request to open the stream with
        """
        request = geyser_pb2.SubscribeDeshredRequest()

        for program_id, parser in self.platform_program_ids.items():
            filter_name = f"platform_filter_{Pubkey.from_bytes(program_id)}"
            deshred_filter = request.deshred_transactions[filter_name]
            # Not the program id: that matches every trade too, and the volume
            # makes a Python consumer lag until the server ends the stream.
            for account in parser.get_creation_filter_accounts():
                deshred_filter.account_include.append(str(account))
            deshred_filter.vote = False

        return request

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for new token creations on the pre-execution deshred stream.

        Args:
            token_callback: Called with each detected token
            match_string: Optional substring to require in the name or symbol
            creator_address: Optional creator address to filter by
        """
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        while True:
            try:
                stub, channel = await self._create_geyser_connection()
                request = self._create_subscription_request()

                logger.info(f"Connected to deshred stream: {self.geyser_endpoint}")
                logger.info(
                    f"Monitoring platforms: {[p.value for p in self.platforms]}"
                )
                logger.warning(
                    "Shreds mode reads transactions before execution: coins "
                    "created through a router arrive as CPIs and cannot be "
                    "detected, and a create that later reverts is "
                    "indistinguishable from one that lands."
                )

                try:
                    async for update in stub.SubscribeDeshred(iter([request])):
                        token_info = self._process_update(update)
                        if not token_info:
                            continue

                        logger.info(
                            f"New token detected pre-execution: {token_info.name} "
                            f"({token_info.symbol}) on {token_info.platform.value}"
                        )

                        if match_string and not (
                            match_string.lower() in token_info.name.lower()
                            or match_string.lower() in token_info.symbol.lower()
                        ):
                            logger.info(
                                f"Token does not match filter '{match_string}'. "
                                "Skipping..."
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
                        logger.exception("Deshred stream error occurred")
                    await asyncio.sleep(_STREAM_RETRY_SECONDS)

                finally:
                    await channel.close()

            except Exception:
                logger.exception("Deshred connection error")
                logger.info(f"Reconnecting in {_CONNECTION_RETRY_SECONDS} seconds...")
                await asyncio.sleep(_CONNECTION_RETRY_SECONDS)

    @staticmethod
    def _resolve_account_keys(transaction) -> list[bytes]:
        """Build the full account-key table for one deshred transaction.

        A v0 transaction indexes accounts past the end of `message.account_keys`
        when it uses an address lookup table. The deshred stream resolves those
        and reports them on the update itself rather than under a meta, in this
        order: static keys, writable loaded, read-only loaded. Ignoring them
        raises IndexError on the first coin that uses a lookup table.

        Args:
            transaction: A geyser SubscribeUpdateDeshredTransactionInfo

        Returns:
            Account keys as raw 32-byte values, indexable by an instruction
        """
        keys = list(transaction.transaction.message.account_keys)
        keys.extend(transaction.loaded_writable_addresses)
        keys.extend(transaction.loaded_readonly_addresses)
        return keys

    def _process_update(self, update) -> TokenInfo | None:
        """Extract token creation info from one deshred update.

        Only top-level instructions are walked, because inner instructions do
        not exist before execution. Each instruction is routed to its own
        platform's parser by program id rather than offered to every parser,
        so an unrelated program's instruction is never decoded speculatively.

        Args:
            update: A geyser SubscribeUpdate from the deshred stream

        Returns:
            TokenInfo for a detected creation, None otherwise
        """
        try:
            if not update.HasField("deshred_transaction"):
                return None

            transaction = update.deshred_transaction.transaction
            message = getattr(transaction.transaction, "message", None)
            if message is None:
                return None

            static_keys = transaction.transaction.message.account_keys
            keys = None

            for instruction in message.instructions:
                # Cheapest test first. The account filter already narrows the
                # stream to creations, but a creation transaction still carries
                # ComputeBudget, system and token instructions, and this avoids
                # both the key-table copy and a full IDL decode for each.
                if not bytes(instruction.data).startswith(self.all_discriminators):
                    continue

                program_index = instruction.program_id_index
                if program_index >= len(static_keys):
                    continue

                parser = self.platform_program_ids.get(
                    bytes(static_keys[program_index])
                )
                if parser is None:
                    continue

                # Resolve lookup-table accounts only once a real candidate is
                # in hand; the instruction's own accounts may index into them.
                if keys is None:
                    keys = self._resolve_account_keys(transaction)

                token_info = parser.parse_token_creation_from_instruction(
                    instruction.data, instruction.accounts, keys
                )
                if token_info:
                    return self._mark_trusted(token_info)

            return None

        except Exception:
            logger.exception("Error processing deshred update")
            return None

    @staticmethod
    def _mark_trusted(token_info: TokenInfo) -> TokenInfo:
        """Mark an instruction-parsed TokenInfo as safe to trade without a refresh.

        Other listeners leave `state_from_event` False on instruction-parsed
        tokens, because `args.creator` is user-supplied and the program may write
        something else into `BondingCurve.creator`. That has exactly one cause,
        which the parser resolves: a holder-reward coin takes its creator from a
        PDA of the mint, derivable without reading anything.

        Waiting for the curve is not a safer fallback here — the account does not
        exist yet, so the choice is trading on this data or not trading.

        Args:
            token_info: Token parsed from a create instruction

        Returns:
            The same TokenInfo, marked so extreme_fast_mode skips the curve read
        """
        token_info.state_from_event = True
        return token_info

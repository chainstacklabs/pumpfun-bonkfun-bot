"""
Pump.Fun implementation of EventParser interface.

This module parses pump.fun-specific token creation events from various sources
by implementing the EventParser interface with IDL-based event parsing.
"""

import base64
import struct
from time import monotonic
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.pubkeys import SystemAddresses
from interfaces.core import EventParser, Platform, TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddresses
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class PumpFunEventParser(EventParser):
    """Pump.Fun implementation of EventParser interface with IDL-based event parsing."""

    def __init__(self, idl_parser: IDLParser):
        """Initialize pump.fun event parser with injected IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for pump.fun platform
        """
        self._idl_parser = idl_parser

        event_discriminators = self._idl_parser.get_event_discriminators()
        self._create_event_discriminator_bytes = event_discriminators["CreateEvent"]
        self._create_event_discriminator = struct.unpack(
            "<Q", self._create_event_discriminator_bytes
        )[0]

        instruction_discriminators = self._idl_parser.get_instruction_discriminators()
        self._create_instruction_discriminator_bytes = instruction_discriminators[
            "create"
        ]
        self._create_instruction_discriminator = struct.unpack(
            "<Q", self._create_instruction_discriminator_bytes
        )[0]

        # Support for token2022 (create_v2 instruction)
        self._create_v2_instruction_discriminator_bytes = instruction_discriminators.get(
            "create_v2"
        )
        self._create_v2_instruction_discriminator = (
            struct.unpack("<Q", self._create_v2_instruction_discriminator_bytes)[0]
            if self._create_v2_instruction_discriminator_bytes
            else None
        )

        logger.info(
            "Pump.Fun event parser initialized with IDL-based event and instruction parsing"
        )
        logger.info(
            f"CreateEvent discriminator: {self._create_event_discriminator_bytes.hex()}"
        )
        logger.info(
            f"create instruction discriminator: {self._create_instruction_discriminator_bytes.hex()}"
        )
        if self._create_v2_instruction_discriminator_bytes:
            logger.info(
                f"create_v2 instruction discriminator: {self._create_v2_instruction_discriminator_bytes.hex()}"
            )

    @property
    def platform(self) -> Platform:
        """Get the platform this parser serves."""
        return Platform.PUMP_FUN

    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo | None:
        """Parse token creation from pump.fun transaction logs using IDL event parsing.

        Args:
            logs: List of log strings from transaction
            signature: Transaction signature

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        # Check if this is a token creation transaction (create or create_v2 for token2022)
        if not any(
            "Program log: Instruction: Create" in log
            or "Program log: Instruction: Create_v2" in log
            for log in logs
        ):
            return None

        # Skip swaps as the first condition may pass them
        if any("Program log: Instruction: CreateTokenAccount" in log for log in logs):
            return None

        logger.info(f"🔍 Parsing token creation from logs for signature: {signature}")

        # Look for event data in the logs (CreateEvent data!)
        # We need to find the Program data that comes after "Instruction: Create"
        try:
            create_instruction_found = False
            program_data_entries = []

            # First, collect all Program data entries and note when Create instruction happens
            for i, log in enumerate(logs):
                if "Program log: Instruction: Create" in log or "Program log: Instruction: Create_v2" in log:
                    create_instruction_found = True
                    instruction_type = "Create_v2" if "Create_v2" in log else "Create"
                    logger.info(f"📝 Found {instruction_type} instruction at log index {i}")
                elif "Program data:" in log:
                    # Extract base64 encoded event data
                    encoded_data = log.split("Program data: ")[1].strip()
                    program_data_entries.append((i, encoded_data, log))
                    logger.info(
                        f"📊 Found Program data at log index {i}, length: {len(encoded_data)}"
                    )

            if not create_instruction_found:
                logger.info("❌ No Create or Create_v2 instruction found in logs")
                return None

            if not program_data_entries:
                logger.info("❌ No Program data entries found in logs")
                return None

            logger.info(
                f"🔍 Found {len(program_data_entries)} Program data entries to check"
            )

            # Try each Program data entry
            for entry_idx, (log_idx, encoded_data, full_log) in enumerate(
                program_data_entries
            ):
                try:
                    logger.info(
                        f"🧪 Trying Program data entry {entry_idx + 1}/{len(program_data_entries)} (log index {log_idx})"
                    )

                    decoded_data = base64.b64decode(encoded_data)

                    if len(decoded_data) < 8:
                        logger.info(
                            f"⚠️ Program data too short: {len(decoded_data)} bytes"
                        )
                        continue

                    # Check discriminator from program data
                    discriminator = decoded_data[:8]
                    discriminator_int = struct.unpack("<Q", discriminator)[0]

                    logger.info(
                        f"🔍 Program data discriminator: {discriminator.hex()} (int: {discriminator_int})"
                    )
                    logger.info(
                        f"🎯 Expected CreateEvent discriminator: {self._create_event_discriminator_bytes.hex()} (int: {self._create_event_discriminator})"
                    )

                    # Try to decode as CreateEvent using IDL parser
                    decoded_event = self._idl_parser.decode_event_data(
                        decoded_data, "CreateEvent"
                    )

                    if not decoded_event:
                        logger.info("❌ IDL parser returned None for CreateEvent")
                        continue

                    if decoded_event.get("event_name") != "CreateEvent":
                        logger.info(
                            f"❌ Wrong event type: {decoded_event.get('event_name', 'None')}"
                        )
                        continue

                    logger.info(
                        f"✅ Successfully decoded event: {decoded_event.get('event_name', 'Unknown')}"
                    )
                    logger.info(
                        f"🔍 Event fields: {list(decoded_event.get('fields', {}).keys())}"
                    )

                    fields = decoded_event.get("fields", {})
                    if not fields:
                        logger.info("❌ No fields found in decoded event")
                        continue

                    # Validate required fields exist
                    required_fields = [
                        "mint",
                        "bonding_curve",
                        "user",
                        "creator",
                        "name",
                        "symbol",
                        "uri",
                    ]
                    missing_fields = [
                        field for field in required_fields if field not in fields
                    ]
                    if missing_fields:
                        logger.info(f"❌ Missing required fields: {missing_fields}")
                        continue

                    logger.info(
                        f"🎯 Token found: {fields.get('symbol', 'Unknown')} ({fields.get('name', 'Unknown')})"
                    )

                    # Convert string representations to Pubkey objects
                    # Note: IDL parser returns pubkeys as base58 strings
                    try:
                        mint = (
                            Pubkey.from_string(fields["mint"])
                            if isinstance(fields["mint"], str)
                            else fields["mint"]
                        )
                        bonding_curve = (
                            Pubkey.from_string(fields["bonding_curve"])
                            if isinstance(fields["bonding_curve"], str)
                            else fields["bonding_curve"]
                        )
                        user = (
                            Pubkey.from_string(fields["user"])
                            if isinstance(fields["user"], str)
                            else fields["user"]
                        )
                        creator = (
                            Pubkey.from_string(fields["creator"])
                            if isinstance(fields["creator"], str)
                            else fields["creator"]
                        )

                        logger.info(f"🔑 Mint: {mint}")
                        logger.info(f"🔑 Bonding Curve: {bonding_curve}")
                        logger.info(f"🔑 User: {user}")
                        logger.info(f"🔑 Creator: {creator}")

                    except Exception as e:
                        logger.info(f"❌ Failed to convert pubkey fields: {e}")
                        continue

                    # Derive additional addresses (default to TOKEN_2022_PROGRAM as per pump.fun's migration to create_v2)
                    # Note: As of recent pump.fun updates, all tokens are created via create_v2 instruction
                    # This is a technical limitation of logs listener - cannot distinguish create vs create_v2
                    # Risk is low since pump.fun now defaults to Token2022 for all new tokens
                    token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
                    associated_bonding_curve = self._derive_associated_bonding_curve(
                        mint, bonding_curve, token_program_id
                    )
                    creator_vault = self._derive_creator_vault(creator)

                    logger.info(
                        f"✅ Successfully parsed CreateEvent for token: {fields.get('symbol', 'Unknown')}"
                    )

                    return TokenInfo(
                        name=fields["name"],
                        symbol=fields["symbol"],
                        uri=fields["uri"],
                        mint=mint,
                        platform=Platform.PUMP_FUN,
                        bonding_curve=bonding_curve,
                        associated_bonding_curve=associated_bonding_curve,
                        user=user,
                        creator=creator,
                        creator_vault=creator_vault,
                        token_program_id=token_program_id,
                        creation_timestamp=monotonic(),
                    )

                except Exception as e:
                    logger.info(
                        f"❌ Failed to decode Program data entry {entry_idx + 1}: {e}"
                    )
                    continue

            logger.info("❌ No valid CreateEvent found in any Program data entries")
            return None

        except Exception:
            logger.exception("Failed to parse token creation from logs")
            return None

    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        """Parse token creation from pump.fun instruction data using injected IDL parser.

        Args:
            instruction_data: Raw instruction data
            accounts: List of account indices
            account_keys: List of account public keys

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        # Determine which create instruction (standard or v2 for token2022)
        is_create_v2 = False
        if instruction_data.startswith(self._create_instruction_discriminator_bytes):
            is_create_v2 = False
        elif (
            self._create_v2_instruction_discriminator_bytes
            and instruction_data.startswith(self._create_v2_instruction_discriminator_bytes)
        ):
            is_create_v2 = True
        else:
            return None

        try:
            # Helper to get account key
            def get_account_key(index):
                if index >= len(accounts):
                    return None
                account_index = accounts[index]
                if account_index >= len(account_keys):
                    return None
                return Pubkey.from_bytes(account_keys[account_index])

            # Parse instruction data using injected IDL parser
            decoded = self._idl_parser.decode_instruction(
                instruction_data, account_keys, accounts
            )
            expected_instruction_name = "create_v2" if is_create_v2 else "create"
            if not decoded or decoded["instruction_name"] != expected_instruction_name:
                return None

            args = decoded.get("args", {})

            # Extract account information based on IDL account order
            mint = get_account_key(0)
            bonding_curve = get_account_key(2)
            associated_bonding_curve = get_account_key(3)
            user = get_account_key(7)

            if not all([mint, bonding_curve, associated_bonding_curve, user]):
                return None

            # Create creator vault
            creator = (
                Pubkey.from_string(args.get("creator", str(user)))
                if args.get("creator")
                else user
            )
            creator_vault = self._derive_creator_vault(creator)

            # Determine token program based on instruction type
            token_program_id = (
                SystemAddresses.TOKEN_2022_PROGRAM
                if is_create_v2
                else SystemAddresses.TOKEN_PROGRAM
            )

            return TokenInfo(
                name=args.get("name", ""),
                symbol=args.get("symbol", ""),
                uri=args.get("uri", ""),
                mint=mint,
                platform=Platform.PUMP_FUN,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=user,
                creator=creator,
                creator_vault=creator_vault,
                token_program_id=token_program_id,
                creation_timestamp=monotonic(),
            )

        except Exception as e:
            logger.debug(f"Failed to parse create instruction: {e}")
            return None

    def parse_token_creation_from_geyser(
        self, transaction_info: Any
    ) -> TokenInfo | None:
        """Parse token creation from Geyser transaction data.

        Args:
            transaction_info: Geyser transaction information

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            if not hasattr(transaction_info, "transaction"):
                return None

            tx = transaction_info.transaction.transaction.transaction
            msg = getattr(tx, "message", None)
            if msg is None:
                return None

            for ix in msg.instructions:
                # Skip non-pump.fun program instructions
                program_idx = ix.program_id_index
                if program_idx >= len(msg.account_keys):
                    continue

                program_id = msg.account_keys[program_idx]
                if bytes(program_id) != bytes(self.get_program_id()):
                    continue

                token_info = self.parse_token_creation_from_instruction(
                    ix.data, ix.accounts, msg.account_keys
                )
                if token_info:
                    return token_info

            return None

        except Exception as e:
            logger.debug(f"Failed to parse geyser transaction: {e}")
            return None

    def get_program_id(self) -> Pubkey:
        """Get the pump.fun program ID this parser monitors.

        Returns:
            Pump.fun program ID
        """
        return PumpFunAddresses.PROGRAM

    def get_instruction_discriminators(self) -> list[bytes]:
        """Get instruction discriminators for token creation.

        Returns:
            List of discriminator bytes to match
        """
        return [self._create_instruction_discriminator_bytes]

    def get_event_discriminators(self) -> list[bytes]:
        """Get event discriminators for token creation.

        Returns:
            List of event discriminator bytes to match
        """
        return [self._create_event_discriminator_bytes]

    def parse_token_creation_from_block(self, block_data: dict) -> TokenInfo | None:
        """Parse token creation from block data (for block listener).

        Args:
            block_data: Block data from WebSocket

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            if "transactions" not in block_data:
                return None

            for tx in block_data["transactions"]:
                if not isinstance(tx, dict) or "transaction" not in tx:
                    continue

                # Decode base64 transaction data if needed
                tx_data = tx["transaction"]
                if isinstance(tx_data, list) and len(tx_data) > 0:
                    try:
                        tx_data_encoded = tx_data[0]
                        tx_data_decoded = base64.b64decode(tx_data_encoded)
                        transaction = VersionedTransaction.from_bytes(tx_data_decoded)

                        for ix in transaction.message.instructions:
                            program_id = transaction.message.account_keys[
                                ix.program_id_index
                            ]

                            # Check if instruction is from pump.fun program
                            if str(program_id) != str(self.get_program_id()):
                                continue

                            ix_data = bytes(ix.data)

                            # Check for create or create_v2 discriminator
                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                is_create = (
                                    discriminator == self._create_instruction_discriminator
                                )
                                is_create_v2 = (
                                    self._create_v2_instruction_discriminator
                                    and discriminator == self._create_v2_instruction_discriminator
                                )

                                if is_create or is_create_v2:
                                    # Token creation should have substantial data and many accounts
                                    if len(ix_data) <= 8 or len(ix.accounts) < 10:
                                        continue

                                    account_keys_bytes = [
                                        bytes(key)
                                        for key in transaction.message.account_keys
                                    ]

                                    # Parse the instruction
                                    token_info = (
                                        self.parse_token_creation_from_instruction(
                                            ix_data, ix.accounts, account_keys_bytes
                                        )
                                    )
                                    if token_info:
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse block transaction: {e}")
                        continue

                # Handle already decoded transaction data
                elif isinstance(tx_data, dict) and "message" in tx_data:
                    try:
                        message = tx_data["message"]
                        if (
                            "instructions" not in message
                            or "accountKeys" not in message
                        ):
                            continue

                        for ix in message["instructions"]:
                            if (
                                "programIdIndex" not in ix
                                or "accounts" not in ix
                                or "data" not in ix
                            ):
                                continue

                            program_idx = ix["programIdIndex"]
                            if program_idx >= len(message["accountKeys"]):
                                continue

                            program_id_str = message["accountKeys"][program_idx]
                            if program_id_str != str(self.get_program_id()):
                                continue

                            # Decode instruction data
                            ix_data = base64.b64decode(ix["data"])

                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                is_create = (
                                    discriminator == self._create_instruction_discriminator
                                )
                                is_create_v2 = (
                                    self._create_v2_instruction_discriminator
                                    and discriminator == self._create_v2_instruction_discriminator
                                )

                                if is_create or is_create_v2:
                                    if len(ix_data) <= 8 or len(ix["accounts"]) < 10:
                                        continue

                                    # Convert account keys to bytes for parsing
                                    account_keys_bytes = [
                                        Pubkey.from_string(key).to_bytes()
                                        for key in message["accountKeys"]
                                    ]

                                    token_info = (
                                        self.parse_token_creation_from_instruction(
                                            ix_data, ix["accounts"], account_keys_bytes
                                        )
                                    )
                                    if token_info:
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse decoded block transaction: {e}")
                        continue

            return None

        except Exception as e:
            logger.debug(f"Failed to parse block data: {e}")
            return None

    def _derive_creator_vault(self, creator: Pubkey) -> Pubkey:
        """Derive the creator vault for a creator.

        Args:
            creator: Creator address

        Returns:
            Creator vault address
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    def _derive_associated_bonding_curve(
        self, mint: Pubkey, bonding_curve: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive the associated bonding curve (ATA of bonding curve for the token).

        Args:
            mint: Token mint address
            bonding_curve: Bonding curve address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_PROGRAM

        Returns:
            Associated bonding curve address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_PROGRAM

        derived_address, _ = Pubkey.find_program_address(
            [
                bytes(bonding_curve),
                bytes(token_program_id),
                bytes(mint),
            ],
            SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        )
        return derived_address

    def _parse_bonding_curve_state(self, data: bytes) -> dict[str, Any] | None:
        """Parse bonding curve state from raw account data using IDL parser.

        Args:
            data: Raw bonding curve account data

        Returns:
            Dictionary with parsed bonding curve state or None if parsing fails
        """
        try:
            decoded = self._idl_parser.decode_account_data(
                data, "BondingCurve", skip_discriminator=True
            )
            if not decoded:
                return None
            return decoded
        except Exception as e:
            logger.debug(f"Failed to parse bonding curve state: {e}")
            return None

    def _get_is_mayhem_mode_from_curve(self, bonding_curve_address: Pubkey) -> bool:
        """Determine if a token is in mayhem mode based on bonding curve state.

        Note: This would require an RPC call to fetch the bonding curve account.
        For now, we return False as a default since the event parser doesn't have
        access to an RPC client. The mayhem mode flag will be set by traders
        when they fetch bonding curve state for other operations.

        Args:
            bonding_curve_address: Address of the bonding curve

        Returns:
            True if mayhem mode, False otherwise (or if parsing fails)
        """
        # Since event parser doesn't have RPC client access, we cannot fetch
        # and parse bonding curve state here. Mayhem mode will be set later
        # when traders fetch the bonding curve state.
        return False

    @property
    def verbose(self) -> bool:
        """Check if verbose logging is enabled."""
        return getattr(self, "_verbose", False)

    @verbose.setter
    def verbose(self, value: bool) -> None:
        """Set verbose logging."""
        self._verbose = value

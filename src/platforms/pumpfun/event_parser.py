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

from core.pubkeys import (
    QUOTE_TOKEN_PROGRAMS,
    SystemAddresses,
    normalize_quote_mint,
)
from interfaces.core import EventParser, Platform, TokenInfo
from platforms.pumpfun.address_provider import (
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

# create_v2's optional quote metadata is an all-or-none remaining-account
# group after its 16 IDL accounts: quote mint, bonding-curve quote ATA, and
# quote token program. See COIN_CREATION.md in pump-fun's public docs.
_CREATE_V2_QUOTE_MINT_ACCOUNT_INDEX = 16
_CREATE_V2_ASSOCIATED_QUOTE_ACCOUNT_INDEX = 17
_CREATE_V2_QUOTE_TOKEN_PROGRAM_ACCOUNT_INDEX = 18
_CREATE_V2_QUOTE_ACCOUNT_COUNT = 19

# Length of a Solana public key in bytes.
PUBKEY_BYTE_LENGTH = 32

# Canonical IDL account positions. create_v2 deliberately moved ``user`` ahead
# of the program accounts; treating it like legacy create maps the token program
# as the user.
_CREATE_USER_ACCOUNT_INDEX = 7
_CREATE_TOKEN_PROGRAM_ACCOUNT_INDEX = 8
_CREATE_ACCOUNT_COUNT = 13
_CREATE_V2_USER_ACCOUNT_INDEX = 5
_CREATE_V2_TOKEN_PROGRAM_ACCOUNT_INDEX = 7
_CREATE_V2_ACCOUNT_COUNT = 16

_SUPPORTED_TOKEN_PROGRAMS = frozenset(
    (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM)
)

_ADDRESS_PROVIDER = PumpFunAddressProvider()

# All fields in the vendored CreateEvent are required before its state is
# trusted for the zero-RPC path. IDLParser intentionally returns partial events
# when a trailing field cannot be decoded, so key presence must be checked here.
_CANONICAL_CREATE_EVENT_FIELDS = frozenset(
    (
        "name",
        "symbol",
        "uri",
        "mint",
        "bonding_curve",
        "user",
        "creator",
        "timestamp",
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "real_token_reserves",
        "token_total_supply",
        "token_program",
        "is_mayhem_mode",
        "is_cashback_enabled",
        "quote_mint",
        "virtual_quote_reserves",
    )
)


def _coerce_pubkey(value: object) -> Pubkey | None:
    """Coerce a decoded IDL pubkey field into a Pubkey.

    Args:
        value: Decoded field, which may be a str, Pubkey, bytes or None

    Returns:
        Pubkey, or None if the value cannot be interpreted
    """
    if value is None:
        return None
    if isinstance(value, Pubkey):
        return value
    if isinstance(value, str):
        try:
            return Pubkey.from_string(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, bytes | bytearray) and len(value) == PUBKEY_BYTE_LENGTH:
        return Pubkey.from_bytes(bytes(value))
    return None


def _is_u64(value: object, *, positive: bool = False) -> bool:
    """Return whether value is a valid raw unsigned 64-bit integer."""
    minimum = 1 if positive else 0
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= 0xFFFF_FFFF_FFFF_FFFF
    )


def _resolve_quote_metadata(value: object) -> tuple[Pubkey, Pubkey] | None:
    """Resolve only quote mints whose owning program is known locally."""
    raw_quote_mint = _coerce_pubkey(value)
    if raw_quote_mint is None:
        return None
    quote_mint = normalize_quote_mint(raw_quote_mint)
    quote_program = QUOTE_TOKEN_PROGRAMS.get(quote_mint)
    if quote_program is None:
        return None
    return quote_mint, quote_program


def _validate_instruction_accounts(
    account_keys: list[bytes], accounts: list[int]
) -> tuple[list[bytes], list[int]] | None:
    """Validate account key sizes and reject Python-style negative indices."""
    normalized_account_keys: list[bytes] = []
    for key in account_keys:
        if not isinstance(key, Pubkey | bytes | bytearray):
            return None
        raw_key = bytes(key)
        if len(raw_key) != PUBKEY_BYTE_LENGTH:
            return None
        normalized_account_keys.append(raw_key)

    normalized_accounts: list[int] = []
    for account_index in accounts:
        if (
            not isinstance(account_index, int)
            or isinstance(account_index, bool)
            or account_index < 0
            or account_index >= len(normalized_account_keys)
        ):
            return None
        normalized_accounts.append(account_index)

    return normalized_account_keys, normalized_accounts


def _has_complete_event_state(fields: dict[str, Any]) -> bool:
    """Validate the canonical state needed by the zero-RPC trust path."""
    if not _CANONICAL_CREATE_EVENT_FIELDS.issubset(fields):
        return False
    if not isinstance(fields["uri"], str):
        return False
    if any(
        not isinstance(fields[name], str) or not fields[name].strip()
        for name in ("name", "symbol")
    ):
        return False
    if not all(
        _coerce_pubkey(fields[name]) is not None
        for name in (
            "mint",
            "bonding_curve",
            "user",
            "creator",
            "token_program",
            "quote_mint",
        )
    ):
        return False
    positive_reserves = {
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "token_total_supply",
        "virtual_quote_reserves",
    }
    if not all(
        _is_u64(fields[name], positive=name in positive_reserves)
        for name in (
            "virtual_token_reserves",
            "virtual_sol_reserves",
            "real_token_reserves",
            "token_total_supply",
            "virtual_quote_reserves",
        )
    ):
        return False
    if isinstance(fields["timestamp"], bool) or not isinstance(
        fields["timestamp"], int
    ):
        return False
    return all(
        isinstance(fields[name], bool)
        for name in ("is_mayhem_mode", "is_cashback_enabled")
    )


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
        self._create_v2_instruction_discriminator_bytes = (
            instruction_discriminators.get("create_v2")
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

        # Program data is trusted only while the runtime invocation stack says
        # pump.fun owns the active frame. A discriminator alone is forgeable.
        try:
            create_instruction_found = False
            program_data_entries: list[tuple[int, str]] = []
            program_stack: list[str] = []
            pump_program = str(self.get_program_id())

            for i, log in enumerate(logs):
                parts = log.split()
                if len(parts) >= 3 and parts[0] == "Program" and parts[2] == "invoke":
                    program_stack.append(parts[1])
                    continue

                current_program = program_stack[-1] if program_stack else None
                if (
                    "Program log: Instruction: Create" in log
                    or "Program log: Instruction: Create_v2" in log
                ) and current_program == pump_program:
                    create_instruction_found = True
                    instruction_type = "Create_v2" if "Create_v2" in log else "Create"
                    logger.info(
                        f"📝 Found {instruction_type} instruction at log index {i}"
                    )
                elif (
                    log.startswith("Program data: ") and current_program == pump_program
                ):
                    encoded_data = log.split("Program data: ", 1)[1].strip()
                    program_data_entries.append((i, encoded_data))
                    logger.info(
                        f"📊 Found Program data at log index {i}, length: {len(encoded_data)}"
                    )

                if (
                    len(parts) >= 3
                    and parts[0] == "Program"
                    and (parts[2] == "success" or parts[2].startswith("failed"))
                ):
                    completed_program = parts[1]
                    if program_stack and program_stack[-1] == completed_program:
                        program_stack.pop()

            if not create_instruction_found:
                logger.info("❌ No Create or Create_v2 instruction found in logs")
                return None

            if not program_data_entries:
                logger.info("❌ No Program data entries found in logs")
                return None

            logger.info(
                f"🔍 Found {len(program_data_entries)} Program data entries to check"
            )

            # Every entry was observed in an active pump.fun invocation frame.
            valid_event_count = 0
            for entry_idx, (log_idx, encoded_data) in enumerate(program_data_entries):
                try:
                    logger.info(
                        f"🧪 Trying Program data entry {entry_idx + 1}/{len(program_data_entries)} (log index {log_idx})"
                    )

                    decoded_data = base64.b64decode(encoded_data, validate=True)

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

                    if discriminator != self._create_event_discriminator_bytes:
                        continue
                    valid_event_count += 1
                    if valid_event_count > 1:
                        logger.warning(
                            "Rejecting ambiguous transaction with multiple "
                            "CreateEvent payloads"
                        )
                        return None

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

                    if not _has_complete_event_state(fields):
                        logger.info("❌ CreateEvent is missing canonical state fields")
                        continue

                    logger.info(
                        f"🎯 Token found: {fields.get('symbol', 'Unknown')} ({fields.get('name', 'Unknown')})"
                    )

                    mint = _coerce_pubkey(fields["mint"])
                    bonding_curve = _coerce_pubkey(fields["bonding_curve"])
                    user = _coerce_pubkey(fields["user"])
                    creator = _coerce_pubkey(fields["creator"])
                    if None in (mint, bonding_curve, user, creator):
                        logger.info("❌ CreateEvent contains an invalid core pubkey")
                        continue

                    expected_bonding_curve = _ADDRESS_PROVIDER.derive_pool_address(mint)
                    if bonding_curve != expected_bonding_curve:
                        logger.info(
                            "❌ CreateEvent bonding curve does not match the mint PDA"
                        )
                        continue

                    token_program_id = _coerce_pubkey(fields["token_program"])
                    if token_program_id not in _SUPPORTED_TOKEN_PROGRAMS:
                        logger.info(
                            "❌ CreateEvent contains an unsupported token program"
                        )
                        continue

                    quote_metadata = _resolve_quote_metadata(fields["quote_mint"])
                    if quote_metadata is None:
                        logger.info("❌ CreateEvent contains an unsupported quote mint")
                        continue
                    quote_mint, quote_program = quote_metadata

                    associated_bonding_curve = (
                        _ADDRESS_PROVIDER.derive_associated_bonding_curve(
                            mint, bonding_curve, token_program_id
                        )
                    )
                    creator_vault = _ADDRESS_PROVIDER.derive_creator_vault(creator)

                    logger.info(
                        f"✅ Successfully parsed CreateEvent for token: {fields.get('symbol', 'Unknown')}"
                    )

                    state_from_event = True
                    virtual_quote_reserves = fields.get("virtual_quote_reserves")
                    if not _is_u64(virtual_quote_reserves, positive=True):
                        virtual_quote_reserves = None

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
                        is_mayhem_mode=(
                            fields["is_mayhem_mode"]
                            if isinstance(fields.get("is_mayhem_mode"), bool)
                            else False
                        ),
                        is_cashback_coin=(
                            fields["is_cashback_enabled"]
                            if isinstance(fields.get("is_cashback_enabled"), bool)
                            else False
                        ),
                        quote_mint=quote_mint,
                        quote_token_program_id=quote_program,
                        virtual_quote_reserves=virtual_quote_reserves,
                        state_from_event=state_from_event,
                        curve_complete=False,
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
            and instruction_data.startswith(
                self._create_v2_instruction_discriminator_bytes
            )
        ):
            is_create_v2 = True
        else:
            return None

        try:
            valid_counts = (
                {_CREATE_V2_ACCOUNT_COUNT, _CREATE_V2_QUOTE_ACCOUNT_COUNT}
                if is_create_v2
                else {_CREATE_ACCOUNT_COUNT}
            )
            if len(accounts) not in valid_counts:
                return None

            validated_accounts = _validate_instruction_accounts(account_keys, accounts)
            if validated_accounts is None:
                return None
            normalized_account_keys, normalized_accounts = validated_accounts

            def get_account_key(index: int) -> Pubkey | None:
                if index < 0 or index >= len(normalized_accounts):
                    return None
                return _coerce_pubkey(
                    normalized_account_keys[normalized_accounts[index]]
                )

            decoded = self._idl_parser.decode_instruction(
                instruction_data, normalized_account_keys, normalized_accounts
            )
            expected_instruction_name = "create_v2" if is_create_v2 else "create"
            if not decoded or decoded["instruction_name"] != expected_instruction_name:
                return None

            args = decoded.get("args", {})
            if not isinstance(args.get("uri"), str):
                return None
            if any(
                not isinstance(args.get(name), str) or not args[name].strip()
                for name in ("name", "symbol")
            ):
                return None

            user_index = (
                _CREATE_V2_USER_ACCOUNT_INDEX
                if is_create_v2
                else _CREATE_USER_ACCOUNT_INDEX
            )
            token_program_index = (
                _CREATE_V2_TOKEN_PROGRAM_ACCOUNT_INDEX
                if is_create_v2
                else _CREATE_TOKEN_PROGRAM_ACCOUNT_INDEX
            )
            mint = get_account_key(0)
            bonding_curve = get_account_key(2)
            associated_bonding_curve = get_account_key(3)
            user = get_account_key(user_index)
            token_program_id = get_account_key(token_program_index)

            if None in (
                mint,
                bonding_curve,
                associated_bonding_curve,
                user,
                token_program_id,
            ):
                return None

            expected_token_program = (
                SystemAddresses.TOKEN_2022_PROGRAM
                if is_create_v2
                else SystemAddresses.TOKEN_PROGRAM
            )
            if token_program_id != expected_token_program:
                return None
            expected_bonding_curve = _ADDRESS_PROVIDER.derive_pool_address(mint)
            expected_associated_bonding_curve = (
                _ADDRESS_PROVIDER.derive_associated_bonding_curve(
                    mint, expected_bonding_curve, expected_token_program
                )
            )
            if (
                bonding_curve != expected_bonding_curve
                or associated_bonding_curve != expected_associated_bonding_curve
            ):
                return None

            creator = _coerce_pubkey(args.get("creator"))
            if creator is None:
                return None
            creator_vault = _ADDRESS_PROVIDER.derive_creator_vault(creator)

            # Preserve the optional trailing OptionBool behavior supported by
            # IDLParser: omitted and explicit None both mean disabled.
            is_cashback_raw = args.get("is_cashback_enabled")
            is_cashback = (
                is_cashback_raw.get("field_0", False)
                if isinstance(is_cashback_raw, dict)
                else is_cashback_raw
                if isinstance(is_cashback_raw, bool)
                else False
            )

            # The optional quote metadata is one atomic remaining-account
            # group. Its absence means native SOL; if present, bind all three
            # accounts to locally verified metadata and the curve PDA.
            has_quote_accounts = (
                is_create_v2 and len(accounts) >= _CREATE_V2_QUOTE_ACCOUNT_COUNT
            )
            raw_quote_mint = (
                get_account_key(_CREATE_V2_QUOTE_MINT_ACCOUNT_INDEX)
                if has_quote_accounts
                else SystemAddresses.DEFAULT_PUBKEY
            )
            quote_metadata = _resolve_quote_metadata(raw_quote_mint)
            if quote_metadata is None:
                return None
            quote_mint, quote_program = quote_metadata

            if has_quote_accounts:
                associated_quote = get_account_key(
                    _CREATE_V2_ASSOCIATED_QUOTE_ACCOUNT_INDEX
                )
                supplied_quote_program = get_account_key(
                    _CREATE_V2_QUOTE_TOKEN_PROGRAM_ACCOUNT_INDEX
                )
                expected_associated_quote = (
                    _ADDRESS_PROVIDER.derive_quote_token_account(
                        bonding_curve, quote_mint, quote_program
                    )
                )
                if (
                    associated_quote != expected_associated_quote
                    or supplied_quote_program != quote_program
                ):
                    return None

            return TokenInfo(
                name=args["name"],
                symbol=args["symbol"],
                uri=args["uri"],
                mint=mint,
                platform=Platform.PUMP_FUN,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=user,
                creator=creator,
                creator_vault=creator_vault,
                token_program_id=token_program_id,
                is_mayhem_mode=(
                    args["is_mayhem_mode"]
                    if isinstance(args.get("is_mayhem_mode"), bool)
                    else False
                ),
                is_cashback_coin=is_cashback,
                quote_mint=quote_mint,
                quote_token_program_id=quote_program,
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

            # Prefer the CreateEvent from meta.log_messages, same as the block
            # parser: the event carries the canonical creator (instruction
            # args.creator is user-supplied and may differ post-2026-04-28)
            # plus mayhem/cashback/quote_mint, which marks the TokenInfo
            # state_from_event so extreme_fast_mode can buy with zero RPC
            # calls. Fall back to instruction decoding when logs are absent.
            tx_info = transaction_info.transaction.transaction
            meta = getattr(tx_info, "meta", None)
            log_messages = list(getattr(meta, "log_messages", []) or [])
            if log_messages:
                token_info = self.parse_token_creation_from_logs(
                    log_messages, signature=""
                )
                if token_info:
                    return token_info

            tx = tx_info.transaction
            msg = getattr(tx, "message", None)
            if msg is None:
                return None

            for ix in msg.instructions:
                # Skip non-pump.fun program instructions
                program_idx = ix.program_id_index
                if (
                    not isinstance(program_idx, int)
                    or isinstance(program_idx, bool)
                    or program_idx < 0
                    or program_idx >= len(msg.account_keys)
                ):
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
        discriminators = [self._create_instruction_discriminator_bytes]
        if self._create_v2_instruction_discriminator_bytes is not None:
            discriminators.append(self._create_v2_instruction_discriminator_bytes)
        return discriminators

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

                # Prefer parsing the CreateEvent from logs — args.creator on the
                # ix is user-supplied and post-2026-04-28 may differ from the
                # canonical BC.creator (which the program writes as a PFEE PDA
                # in some cases). The CreateEvent log carries the canonical
                # creator, so creator_vault derived from it matches the program
                # constraint.
                meta = tx.get("meta")
                if isinstance(meta, dict):
                    logs = meta.get("logMessages") or meta.get("log_messages")
                    if logs:
                        token_info = self.parse_token_creation_from_logs(
                            logs, signature=""
                        )
                        if token_info:
                            return token_info

                # Decode base64 transaction data if needed
                tx_data = tx["transaction"]
                if isinstance(tx_data, list) and len(tx_data) > 0:
                    try:
                        tx_data_encoded = tx_data[0]
                        tx_data_decoded = base64.b64decode(tx_data_encoded)
                        transaction = VersionedTransaction.from_bytes(tx_data_decoded)

                        for ix in transaction.message.instructions:
                            program_idx = ix.program_id_index
                            if (
                                not isinstance(program_idx, int)
                                or isinstance(program_idx, bool)
                                or program_idx < 0
                                or program_idx >= len(transaction.message.account_keys)
                            ):
                                continue
                            program_id = transaction.message.account_keys[program_idx]

                            # Check if instruction is from pump.fun program
                            if str(program_id) != str(self.get_program_id()):
                                continue

                            ix_data = bytes(ix.data)

                            # Check for create or create_v2 discriminator
                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                is_create = (
                                    discriminator
                                    == self._create_instruction_discriminator
                                )
                                is_create_v2 = (
                                    self._create_v2_instruction_discriminator
                                    and discriminator
                                    == self._create_v2_instruction_discriminator
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
                            if (
                                not isinstance(program_idx, int)
                                or isinstance(program_idx, bool)
                                or program_idx < 0
                                or program_idx >= len(message["accountKeys"])
                            ):
                                continue

                            raw_program_id = message["accountKeys"][program_idx]
                            if isinstance(raw_program_id, dict):
                                raw_program_id = raw_program_id.get("pubkey")
                            program_id = _coerce_pubkey(raw_program_id)
                            if program_id != self.get_program_id():
                                continue

                            # Decode instruction data
                            ix_data = base64.b64decode(ix["data"])

                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                is_create = (
                                    discriminator
                                    == self._create_instruction_discriminator
                                )
                                is_create_v2 = (
                                    self._create_v2_instruction_discriminator
                                    and discriminator
                                    == self._create_v2_instruction_discriminator
                                )

                                if is_create or is_create_v2:
                                    if len(ix_data) <= 8 or len(ix["accounts"]) < 10:
                                        continue

                                    # Normalize and validate every key before any
                                    # instruction account index is dereferenced.
                                    account_keys_bytes: list[bytes] = []
                                    for raw_key in message["accountKeys"]:
                                        if isinstance(raw_key, dict):
                                            raw_key = raw_key.get("pubkey")
                                        key = _coerce_pubkey(raw_key)
                                        if key is None:
                                            account_keys_bytes = []
                                            break
                                        account_keys_bytes.append(bytes(key))
                                    if not account_keys_bytes:
                                        continue

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

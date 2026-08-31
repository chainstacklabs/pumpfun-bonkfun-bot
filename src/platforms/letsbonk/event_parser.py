"""
LetsBonk implementation of EventParser interface.

This module parses LetsBonk-specific token creation events from various sources
by implementing the EventParser interface with IDL-based parsing.
"""

import base64
import struct
from time import monotonic
from typing import Any, cast

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.pubkeys import SystemAddresses
from interfaces.core import EventParser, Platform, TokenInfo
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class LetsBonkEventParser(EventParser):
    """LetsBonk implementation of EventParser interface with IDL-based parsing."""

    def __init__(self, idl_parser: IDLParser):
        """Initialize LetsBonk event parser with injected IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for LetsBonk platform
        """
        self.address_provider = LetsBonkAddressProvider()
        self._idl_parser = idl_parser

        # Get all initialize instruction discriminators from injected IDL parser
        # LetsBonk has multiple initialize variants: initialize, initialize_v2, initialize_with_token_2022
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._initialize_discriminator_bytes_list = [
            discriminators["initialize"],
            discriminators["initialize_v2"],
            discriminators["initialize_with_token_2022"],
        ]
        self._initialize_discriminators = {
            struct.unpack("<Q", disc_bytes)[0]
            for disc_bytes in self._initialize_discriminator_bytes_list
        }

        logger.info(
            f"LetsBonk event parser initialized with {len(self._initialize_discriminators)} "
            f"initialize instruction variants"
        )

    @property
    def platform(self) -> Platform:
        """Get the platform this parser serves."""
        return Platform.LETS_BONK

    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo | None:
        """Parse token creation from LetsBonk transaction logs.

        Args:
            logs: List of log strings from transaction
            signature: Transaction signature

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        # LetsBonk doesn't emit specific logs for token creation like pump.fun
        # Token creation is identified through instruction parsing
        return None

    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        """Parse token creation from LetsBonk instruction data using injected IDL parser.

        Args:
            instruction_data: Raw instruction data
            accounts: List of account indices
            account_keys: List of account public keys

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        # Check if instruction starts with any of the initialize discriminators
        if not any(
            instruction_data.startswith(disc_bytes)
            for disc_bytes in self._initialize_discriminator_bytes_list
        ):
            return None

        try:

            def get_account_key(index: int) -> Pubkey | None:
                if index >= len(accounts):
                    return None
                account_index = accounts[index]
                if account_index >= len(account_keys):
                    return None
                return Pubkey.from_bytes(bytes(account_keys[account_index]))

            decoded = self._idl_parser.decode_instruction(
                instruction_data, account_keys, accounts
            )
            if not decoded or decoded["instruction_name"] not in {
                "initialize",
                "initialize_v2",
                "initialize_with_token_2022",
            }:
                return None

            instruction_name = decoded["instruction_name"]
            decoded_accounts = decoded.get("accounts", {})

            def get_named_account(name: str, fallback_index: int) -> Pubkey | None:
                value = decoded_accounts.get(name)
                if value:
                    return Pubkey.from_string(value)
                return get_account_key(fallback_index)

            args = decoded.get("args", {})
            base_mint_param = args.get("base_mint_param", {})
            if not isinstance(base_mint_param, dict):
                return None
            if any(
                not isinstance(base_mint_param.get(name), str)
                or not base_mint_param[name].strip()
                for name in ("name", "symbol")
            ):
                return None

            # All supported initialize variants use payer at index 0 and the
            # authoritative pool creator at index 1. The payer may fund a pool
            # on somebody else's behalf and must not be recorded as creator.
            payer = get_named_account("payer", 0)
            creator = get_named_account("creator", 1)
            global_config = get_named_account("global_config", 2)
            platform_config = get_named_account("platform_config", 3)
            pool_state = get_named_account("pool_state", 5)
            base_mint = get_named_account("base_mint", 6)
            quote_mint = get_named_account("quote_mint", 7)
            base_vault = get_named_account("base_vault", 8)
            quote_vault = get_named_account("quote_vault", 9)

            if instruction_name == "initialize_with_token_2022":
                base_program_index, quote_program_index = 10, 11
            else:
                # initialize and initialize_v2 include metadata_account at 10.
                base_program_index, quote_program_index = 11, 12
            base_token_program = get_named_account(
                "base_token_program", base_program_index
            )
            quote_token_program = get_named_account(
                "quote_token_program", quote_program_index
            )

            required_accounts = {
                "payer": payer,
                "creator": creator,
                "global_config": global_config,
                "platform_config": platform_config,
                "pool_state": pool_state,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "base_vault": base_vault,
                "quote_vault": quote_vault,
                "base_token_program": base_token_program,
                "quote_token_program": quote_token_program,
            }
            missing = [
                name for name, value in required_accounts.items() if value is None
            ]
            if missing:
                logger.debug(
                    "Initialize instruction is missing authoritative accounts: "
                    f"{', '.join(missing)}"
                )
                return None

            known_token_programs = {
                SystemAddresses.TOKEN_PROGRAM,
                SystemAddresses.TOKEN_2022_PROGRAM,
            }
            if (
                base_token_program not in known_token_programs
                or quote_token_program not in known_token_programs
            ):
                logger.debug(
                    "Initialize instruction uses unsupported token programs: "
                    f"base={base_token_program}, quote={quote_token_program}"
                )
                return None

            curve_param = args.get("curve_param")
            curve_type = (
                curve_param.get("variant") if isinstance(curve_param, dict) else None
            )
            transfer_fee_extension_param: dict[str, int] | None = None
            if instruction_name == "initialize_with_token_2022":
                transfer_fee_extension_param = (
                    self._validated_transfer_fee_extension_param(
                        args.get("transfer_fee_extension_param")
                    )
                )
            additional_data = {
                "source": "initialize_instruction",
                "instruction_name": instruction_name,
                "curve_type": curve_type,
                "curve_param": curve_param,
            }
            if instruction_name == "initialize_with_token_2022":
                additional_data["transfer_fee_extension_param"] = (
                    transfer_fee_extension_param
                )
                additional_data["transfer_fee_metadata_scope"] = "initialization_only"
            for field in ("source", "status"):
                if field in decoded:
                    additional_data[field] = decoded[field]

            return TokenInfo(
                name=base_mint_param.get("name", ""),
                symbol=base_mint_param.get("symbol", ""),
                uri=base_mint_param.get("uri", ""),
                mint=cast("Pubkey", base_mint),
                platform=Platform.LETS_BONK,
                pool_state=cast("Pubkey", pool_state),
                base_vault=cast("Pubkey", base_vault),
                quote_vault=cast("Pubkey", quote_vault),
                global_config=cast("Pubkey", global_config),
                platform_config=cast("Pubkey", platform_config),
                user=cast("Pubkey", payer),
                creator=cast("Pubkey", creator),
                token_program_id=cast("Pubkey", base_token_program),
                quote_mint=cast("Pubkey", quote_mint),
                quote_token_program_id=cast("Pubkey", quote_token_program),
                creation_timestamp=monotonic(),
                additional_data=additional_data,
            )

        except (IndexError, KeyError, TypeError, ValueError) as e:
            logger.debug(f"Failed to parse initialize instruction: {e}")
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
                # Skip non-LetsBonk program instructions
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
                    meta = getattr(
                        transaction_info.transaction.transaction, "meta", None
                    )
                    if meta is not None and getattr(meta, "err", None) is not None:
                        return None
                    self._annotate_source(
                        token_info,
                        source="geyser",
                        status="succeeded" if meta is not None else None,
                    )
                    return token_info

            return None

        except Exception as e:
            logger.debug(f"Failed to parse geyser transaction: {e}")
            return None

    def get_program_id(self) -> Pubkey:
        """Get the Raydium LaunchLab program ID this parser monitors.

        Returns:
            Raydium LaunchLab program ID
        """
        return self.address_provider.program_id

    def get_instruction_discriminators(self) -> list[bytes]:
        """Get instruction discriminators for token creation.

        Returns:
            List of discriminator bytes to match (all initialize variants)
        """
        return self._initialize_discriminator_bytes_list

    @staticmethod
    def _validated_transfer_fee_extension_param(
        value: Any,
    ) -> dict[str, int] | None:
        """Validate initialization metadata without treating it as current state."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Transfer-fee extension metadata is not a mapping")
        basis_points = value.get("transfer_fee_basis_points")
        maximum_fee = value.get("maximum_fee")
        if (
            isinstance(basis_points, bool)
            or not isinstance(basis_points, int)
            or not 0 <= basis_points <= 10_000
        ):
            raise ValueError("Transfer-fee basis points are invalid")
        if (
            isinstance(maximum_fee, bool)
            or not isinstance(maximum_fee, int)
            or not 0 <= maximum_fee <= 2**64 - 1
        ):
            raise ValueError("Transfer-fee maximum is invalid")
        return {
            "transfer_fee_basis_points": basis_points,
            "maximum_fee": maximum_fee,
        }

    @staticmethod
    def _annotate_source(
        token_info: TokenInfo, *, source: str, status: str | None = None
    ) -> None:
        """Preserve listener provenance without replacing initialize metadata."""
        additional_data = dict(token_info.additional_data or {})
        additional_data["source"] = source
        if status is not None:
            additional_data["status"] = status
        token_info.additional_data = additional_data

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
                meta = tx.get("meta")
                if isinstance(meta, dict) and meta.get("err") is not None:
                    continue
                status = "succeeded" if meta is not None else None

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

                            # Check if instruction is from LetsBonk program
                            if str(program_id) != str(self.get_program_id()):
                                continue

                            ix_data = bytes(ix.data)

                            # Check for any initialize discriminator variant
                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                if discriminator in self._initialize_discriminators:
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
                                        self._annotate_source(
                                            token_info,
                                            source="block",
                                            status=status,
                                        )
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

                                if discriminator in self._initialize_discriminators:
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
                                        self._annotate_source(
                                            token_info,
                                            source="block",
                                            status=status,
                                        )
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse decoded block transaction: {e}")
                        continue

            return None

        except Exception as e:
            logger.debug(f"Failed to parse block data: {e}")
            return None

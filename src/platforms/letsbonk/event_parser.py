"""
LetsBonk implementation of EventParser interface.

This module parses LetsBonk-specific token creation events from various sources
by implementing the EventParser interface with IDL-based parsing.
"""

import base64
import struct
from time import monotonic
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

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
            # Accept any of the initialize instruction variants
            if not decoded or decoded["instruction_name"] not in {
                "initialize",
                "initialize_v2",
                "initialize_with_token_2022",
            }:
                return None

            args = decoded.get("args", {})

            # Extract MintParams from the decoded arguments
            base_mint_param = args.get("base_mint_param", {})
            if not base_mint_param:
                return None

            # Extract account information based on IDL account order for initialize instruction
            # From the manual example, the account order is:
            # 0: creator (signer)
            # 1: creator_ata (not needed for TokenInfo)
            # 2: global_config
            # 3: platform_config
            # 4: creator
            # 5: pool_state
            # 6: base_mint
            # 7: quote_mint (WSOL)
            # 8: base_vault
            # 9: quote_vault
            # ... other accounts

            creator = get_account_key(0)  # First signer account (creator)
            global_config = get_account_key(2)  # global_config account
            platform_config = get_account_key(3)  # platform_config account
            pool_state = get_account_key(5)  # pool_state account
            base_mint = get_account_key(6)  # base_mint account
            base_vault = get_account_key(8)  # base_vault account
            quote_vault = get_account_key(9)  # quote_vault account

            if not all(
                [
                    creator,
                    global_config,
                    platform_config,
                    pool_state,
                    base_mint,
                    base_vault,
                    quote_vault,
                ]
            ):
                logger.debug(
                    f"Missing required accounts: creator={creator}, global_config={global_config}, "
                    f"platform_config={platform_config}, pool_state={pool_state}, base_mint={base_mint}, "
                    f"base_vault={base_vault}, quote_vault={quote_vault}"
                )
                return None

            return TokenInfo(
                name=base_mint_param.get("name", ""),
                symbol=base_mint_param.get("symbol", ""),
                uri=base_mint_param.get("uri", ""),
                mint=base_mint,
                platform=Platform.LETS_BONK,
                pool_state=pool_state,
                base_vault=base_vault,
                quote_vault=quote_vault,
                global_config=global_config,
                platform_config=platform_config,
                user=creator,
                creator=creator,
                creation_timestamp=monotonic(),
            )

        except Exception as e:
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

                # Check if it's the LetsBonk platform config account
                has_platform_config = False
                for acc_idx in ix.accounts:
                    if acc_idx < len(msg.account_keys):
                        acc_key = msg.account_keys[acc_idx]
                        if bytes(acc_key) == bytes(
                            self.address_provider.get_system_addresses()[
                                "platform_config"
                            ]
                        ):
                            has_platform_config = True
                            break

                if not has_platform_config:
                    continue

                # Process instruction data
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
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse decoded block transaction: {e}")
                        continue

            return None

        except Exception as e:
            logger.debug(f"Failed to parse block data: {e}")
            return None

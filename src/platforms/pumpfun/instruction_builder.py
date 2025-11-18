"""
Pump.Fun implementation of InstructionBuilder interface.

This module builds pump.fun-specific buy and sell instructions
by implementing the InstructionBuilder interface with IDL-based discriminators.
"""

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from core.pubkeys import TOKEN_DECIMALS, SystemAddresses
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class PumpFunInstructionBuilder(InstructionBuilder):
    """Pump.Fun implementation of InstructionBuilder interface with IDL-based discriminators."""

    def __init__(self, idl_parser: IDLParser):
        """Initialize pump.fun instruction builder with injected IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for pump.fun platform
        """
        self._idl_parser = idl_parser

        # Get discriminators from injected IDL parser
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._buy_discriminator = discriminators["buy"]
        self._sell_discriminator = discriminators["sell"]

        logger.info("Pump.Fun instruction builder initialized with injected IDL parser")

    @property
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        return Platform.PUMP_FUN

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for pump.fun.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of SOL to spend (in lamports)
            minimum_amount_out: Minimum tokens expected (raw token units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        instructions = []

        # Get all required accounts (includes mayhem-mode-aware fee recipient)
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)

        # 1. Create idempotent ATA instruction (won't fail if ATA already exists)
        # Use token_program from accounts_info to ensure AddressProvider controls program selection
        ata_instruction = create_idempotent_associated_token_account(
            user,  # payer
            user,  # owner
            token_info.mint,  # mint
            accounts_info["token_program"],  # token program from AddressProvider
        )
        instructions.append(ata_instruction)

        # 2. Build buy instruction
        buy_accounts = [
            AccountMeta(
                pubkey=accounts_info["global"], is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=accounts_info["fee"], is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["bonding_curve"], is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=accounts_info["associated_bonding_curve"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["user_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(pubkey=accounts_info["user"], is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["creator_vault"], is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["global_volume_accumulator"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["user_volume_accumulator"],
                is_signer=False,
                is_writable=True,
            ),
            # Index 14: fee_config (readonly)
            AccountMeta(
                pubkey=accounts_info["fee_config"],
                is_signer=False,
                is_writable=False,
            ),
            # Index 15: fee_program (readonly)
            AccountMeta(
                pubkey=accounts_info["fee_program"],
                is_signer=False,
                is_writable=False,
            ),
        ]

        # Build instruction data: discriminator + token_amount + max_sol_cost + track_volume
        # Encode OptionBool for track_volume: [1, 1] = Some(true)
        track_volume_bytes = bytes([1, 1])
        instruction_data = (
            self._buy_discriminator
            + struct.pack("<Q", minimum_amount_out)  # token amount in raw units
            + struct.pack("<Q", amount_in)  # max SOL cost in lamports
            + track_volume_bytes  # enable volume tracking
        )

        buy_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=buy_accounts,
        )
        instructions.append(buy_instruction)

        return instructions

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for pump.fun.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of tokens to sell (raw token units)
            minimum_amount_out: Minimum SOL expected (in lamports)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        instructions = []

        # Get all required accounts (includes mayhem-mode-aware fee recipient)
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)

        # Build sell instruction accounts
        sell_accounts = [
            AccountMeta(
                pubkey=accounts_info["global"], is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=accounts_info["fee"], is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["bonding_curve"], is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=accounts_info["associated_bonding_curve"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["user_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(pubkey=accounts_info["user"], is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["creator_vault"], is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=accounts_info["token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),
            # Index 12: fee_config (readonly)
            AccountMeta(
                pubkey=accounts_info["fee_config"],
                is_signer=False,
                is_writable=False,
            ),
            # Index 13: fee_program (readonly)
            AccountMeta(
                pubkey=accounts_info["fee_program"],
                is_signer=False,
                is_writable=False,
            ),
        ]

        # Build instruction data: discriminator + token_amount + min_sol_output + track_volume
        # Encode OptionBool for track_volume: [1, 1] = Some(true)
        track_volume_bytes = bytes([1, 1])
        instruction_data = (
            self._sell_discriminator
            + struct.pack("<Q", amount_in)  # token amount in raw units
            + struct.pack("<Q", minimum_amount_out)  # min SOL output in lamports
            + track_volume_bytes  # enable volume tracking
        )

        sell_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=sell_accounts,
        )
        instructions.append(sell_instruction)

        return instructions

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for buy operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)

        return [
            accounts_info["mint"],
            accounts_info["bonding_curve"],
            accounts_info["associated_bonding_curve"],
            accounts_info["user_token_account"],
            accounts_info["fee"],
            accounts_info["creator_vault"],
            accounts_info["global_volume_accumulator"],
            accounts_info["user_volume_accumulator"],
            accounts_info["program"],
            accounts_info["fee_config"],
            accounts_info["fee_program"],
        ]

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for sell operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)

        return [
            accounts_info["mint"],
            accounts_info["bonding_curve"],
            accounts_info["associated_bonding_curve"],
            accounts_info["user_token_account"],
            accounts_info["fee"],
            accounts_info["creator_vault"],
            accounts_info["program"],
            accounts_info["fee_config"],
            accounts_info["fee_program"],
        ]

    def calculate_token_amount_raw(self, token_amount_decimal: float) -> int:
        """Convert decimal token amount to raw token units.

        Args:
            token_amount_decimal: Token amount in decimal form

        Returns:
            Token amount in raw units (adjusted for decimals)
        """
        return int(token_amount_decimal * 10**TOKEN_DECIMALS)

    def calculate_token_amount_decimal(self, token_amount_raw: int) -> float:
        """Convert raw token amount to decimal form.

        Args:
            token_amount_raw: Token amount in raw units

        Returns:
            Token amount in decimal form
        """
        return token_amount_raw / 10**TOKEN_DECIMALS

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for pump.fun buy operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for buy operations
        """
        if config_override is not None:
            return config_override
        # Buy operations: ATA creation + buy instruction
        return 100_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for pump.fun sell operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for sell operations
        """
        if config_override is not None:
            return config_override
        # Sell operations: typically just sell instruction (ATA exists)
        return 60_000

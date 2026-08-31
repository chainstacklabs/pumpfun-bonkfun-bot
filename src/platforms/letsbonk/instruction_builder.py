"""
LetsBonk implementation of InstructionBuilder interface.

This module builds LetsBonk (Raydium LaunchLab) specific buy and sell instructions
by implementing the InstructionBuilder interface with IDL-based discriminators.
"""

import secrets
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountWithSeedParams, create_account_with_seed
from spl.token.instructions import create_idempotent_associated_token_account

from core.pubkeys import (
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_DECIMALS,
    SystemAddresses,
)
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class LetsBonkInstructionBuilder(InstructionBuilder):
    """LetsBonk (Raydium LaunchLab) implementation of InstructionBuilder interface with IDL-based discriminators."""

    def __init__(self, idl_parser: IDLParser):
        """Initialize LetsBonk instruction builder with injected IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for LetsBonk platform
        """
        self._idl_parser = idl_parser

        # Get discriminators from injected IDL parser
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._buy_exact_in_discriminator = discriminators["buy_exact_in"]
        self._sell_exact_in_discriminator = discriminators["sell_exact_in"]

        logger.info("LetsBonk instruction builder initialized with injected IDL parser")

    @property
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        return Platform.LETS_BONK

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for LetsBonk using buy_exact_in.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of SOL to spend (in lamports)
            minimum_amount_out: Minimum tokens expected (raw token units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        self._validate_raw_amount(amount_in, "amount_in")
        self._validate_raw_amount(
            minimum_amount_out,
            "minimum_amount_out",
            positive=False,
        )
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)
        self._require_wsol_quote(accounts_info)
        token_program_id = accounts_info["base_token_program"]
        instructions: list[Instruction] = []

        # 1. Create idempotent ATA for base token
        ata_instruction = create_idempotent_associated_token_account(
            user,  # payer
            user,  # owner
            token_info.mint,  # mint
            token_program_id,  # token program (dynamic for token2022 support)
        )
        instructions.append(ata_instruction)

        # 2. Create WSOL account with seed (temporary account for the transaction)
        wsol_seed = self._generate_wsol_seed()
        wsol_account = address_provider.create_wsol_account_with_seed(user, wsol_seed)

        # Account creation cost + amount to spend
        account_creation_lamports = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE
        total_lamports = amount_in + account_creation_lamports

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=user,
                to_pubkey=wsol_account,
                base=user,
                seed=wsol_seed,
                lamports=total_lamports,
                space=TOKEN_ACCOUNT_SIZE,
                owner=SystemAddresses.TOKEN_PROGRAM,
            )
        )
        instructions.append(create_wsol_ix)

        # 3. Initialize WSOL account
        initialize_wsol_ix = self._create_initialize_account_instruction(
            wsol_account, SystemAddresses.SOL_MINT, user
        )
        instructions.append(initialize_wsol_ix)

        # 4. Build buy_exact_in instruction with correct account ordering
        buy_accounts = [
            AccountMeta(pubkey=user, is_signer=True, is_writable=False),  # payer
            AccountMeta(
                pubkey=accounts_info["authority"], is_signer=False, is_writable=False
            ),  # authority
            AccountMeta(
                pubkey=accounts_info["global_config"],
                is_signer=False,
                is_writable=False,
            ),  # global_config
            AccountMeta(
                pubkey=accounts_info["platform_config"],
                is_signer=False,
                is_writable=False,
            ),  # platform_config
            AccountMeta(
                pubkey=accounts_info["pool_state"], is_signer=False, is_writable=True
            ),  # pool_state
            AccountMeta(
                pubkey=accounts_info["user_base_token"],
                is_signer=False,
                is_writable=True,
            ),  # user_base_token
            AccountMeta(
                pubkey=wsol_account, is_signer=False, is_writable=True
            ),  # user_quote_token (WSOL account)
            AccountMeta(
                pubkey=accounts_info["base_vault"], is_signer=False, is_writable=True
            ),  # base_vault
            AccountMeta(
                pubkey=accounts_info["quote_vault"], is_signer=False, is_writable=True
            ),  # quote_vault
            AccountMeta(
                pubkey=accounts_info["base_token_mint"],
                is_signer=False,
                is_writable=False,
            ),  # base_token_mint
            AccountMeta(
                pubkey=accounts_info["quote_token_mint"],
                is_signer=False,
                is_writable=False,
            ),  # quote_token_mint
            AccountMeta(
                pubkey=accounts_info["base_token_program"],
                is_signer=False,
                is_writable=False,
            ),  # base_token_program
            AccountMeta(
                pubkey=accounts_info["quote_token_program"],
                is_signer=False,
                is_writable=False,
            ),  # quote_token_program
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),  # event_authority
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),  # program
        ]

        # Add remaining accounts (required by the program for fee collection)
        # These are not explicitly listed in IDL but required by the program
        buy_accounts.append(
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            )
        )  # #16: System Program
        buy_accounts.append(
            AccountMeta(
                pubkey=accounts_info["platform_fee_vault"],
                is_signer=False,
                is_writable=True,
            )
        )  # #17: Platform fee vault
        if "creator_fee_vault" in accounts_info:
            buy_accounts.append(
                AccountMeta(
                    pubkey=accounts_info["creator_fee_vault"],
                    is_signer=False,
                    is_writable=True,
                )
            )  # #18: Creator fee vault

        # Build instruction data: discriminator + amount_in + minimum_amount_out + share_fee_rate
        SHARE_FEE_RATE = 0  # No sharing fee
        instruction_data = (
            self._buy_exact_in_discriminator
            + struct.pack("<Q", amount_in)  # amount_in (u64) - SOL to spend
            + struct.pack(
                "<Q", minimum_amount_out
            )  # minimum_amount_out (u64) - min tokens
            + struct.pack("<Q", SHARE_FEE_RATE)  # share_fee_rate (u64): 0
        )

        buy_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=buy_accounts,
        )
        instructions.append(buy_instruction)

        # 5. Close WSOL account to reclaim SOL
        close_wsol_ix = self._create_close_account_instruction(wsol_account, user, user)
        instructions.append(close_wsol_ix)

        return instructions

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for LetsBonk using sell_exact_in.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of tokens to sell (raw token units)
            minimum_amount_out: Minimum SOL expected (in lamports)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        self._validate_raw_amount(amount_in, "amount_in")
        self._validate_raw_amount(
            minimum_amount_out,
            "minimum_amount_out",
            positive=False,
        )
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)
        self._require_wsol_quote(accounts_info)
        instructions: list[Instruction] = []

        # 1. Create WSOL account with seed (to receive SOL)
        wsol_seed = self._generate_wsol_seed()
        wsol_account = address_provider.create_wsol_account_with_seed(user, wsol_seed)

        # Minimal account creation cost
        account_creation_lamports = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=user,
                to_pubkey=wsol_account,
                base=user,
                seed=wsol_seed,
                lamports=account_creation_lamports,
                space=TOKEN_ACCOUNT_SIZE,
                owner=SystemAddresses.TOKEN_PROGRAM,
            )
        )
        instructions.append(create_wsol_ix)

        # 2. Initialize WSOL account
        initialize_wsol_ix = self._create_initialize_account_instruction(
            wsol_account, SystemAddresses.SOL_MINT, user
        )
        instructions.append(initialize_wsol_ix)

        # 3. Build sell_exact_in instruction with correct account ordering
        sell_accounts = [
            AccountMeta(pubkey=user, is_signer=True, is_writable=False),  # payer
            AccountMeta(
                pubkey=accounts_info["authority"], is_signer=False, is_writable=False
            ),  # authority
            AccountMeta(
                pubkey=accounts_info["global_config"],
                is_signer=False,
                is_writable=False,
            ),  # global_config
            AccountMeta(
                pubkey=accounts_info["platform_config"],
                is_signer=False,
                is_writable=False,
            ),  # platform_config
            AccountMeta(
                pubkey=accounts_info["pool_state"], is_signer=False, is_writable=True
            ),  # pool_state
            AccountMeta(
                pubkey=accounts_info["user_base_token"],
                is_signer=False,
                is_writable=True,
            ),  # user_base_token (tokens being sold)
            AccountMeta(
                pubkey=wsol_account, is_signer=False, is_writable=True
            ),  # user_quote_token (WSOL received)
            AccountMeta(
                pubkey=accounts_info["base_vault"], is_signer=False, is_writable=True
            ),  # base_vault (receives tokens)
            AccountMeta(
                pubkey=accounts_info["quote_vault"], is_signer=False, is_writable=True
            ),  # quote_vault (sends WSOL)
            AccountMeta(
                pubkey=accounts_info["base_token_mint"],
                is_signer=False,
                is_writable=False,
            ),  # base_token_mint
            AccountMeta(
                pubkey=accounts_info["quote_token_mint"],
                is_signer=False,
                is_writable=False,
            ),  # quote_token_mint
            AccountMeta(
                pubkey=accounts_info["base_token_program"],
                is_signer=False,
                is_writable=False,
            ),  # base_token_program
            AccountMeta(
                pubkey=accounts_info["quote_token_program"],
                is_signer=False,
                is_writable=False,
            ),  # quote_token_program
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),  # event_authority
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),  # program
        ]

        # Add remaining accounts (required by the program for fee collection)
        # These are not explicitly listed in IDL but required by the program
        sell_accounts.append(
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            )
        )  # #16: System Program
        sell_accounts.append(
            AccountMeta(
                pubkey=accounts_info["platform_fee_vault"],
                is_signer=False,
                is_writable=True,
            )
        )  # #17: Platform fee vault
        if "creator_fee_vault" in accounts_info:
            sell_accounts.append(
                AccountMeta(
                    pubkey=accounts_info["creator_fee_vault"],
                    is_signer=False,
                    is_writable=True,
                )
            )  # #18: Creator fee vault

        # Build instruction data: discriminator + amount_in + minimum_amount_out + share_fee_rate
        SHARE_FEE_RATE = 0  # No sharing fee
        instruction_data = (
            self._sell_exact_in_discriminator
            + struct.pack("<Q", amount_in)  # amount_in (u64) - tokens to sell
            + struct.pack(
                "<Q", minimum_amount_out
            )  # minimum_amount_out (u64) - min SOL
            + struct.pack("<Q", SHARE_FEE_RATE)  # share_fee_rate (u64): 0
        )

        sell_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=sell_accounts,
        )
        instructions.append(sell_instruction)

        # 4. Close WSOL account to reclaim SOL
        close_wsol_ix = self._create_close_account_instruction(wsol_account, user, user)
        instructions.append(close_wsol_ix)

        return instructions

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get write-locked LaunchLab accounts used for priority fee sampling."""
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)
        return self._priority_fee_accounts(accounts_info)

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get write-locked LaunchLab accounts used for priority fee sampling."""
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)
        return self._priority_fee_accounts(accounts_info)

    @staticmethod
    def _validate_raw_amount(
        amount: int,
        field: str,
        *,
        positive: bool = True,
    ) -> None:
        lower_bound = 1 if positive else 0
        if (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or amount < lower_bound
        ):
            qualifier = "positive " if positive else ""
            raise ValueError(f"{field} must be a {qualifier}raw integer amount")
        if amount > 2**64 - 1:
            raise ValueError(f"{field} exceeds the u64 range")

    @staticmethod
    def _require_wsol_quote(accounts_info: dict[str, Pubkey]) -> None:
        quote_mint = accounts_info["quote_token_mint"]
        quote_program = accounts_info["quote_token_program"]
        if quote_mint != SystemAddresses.SOL_MINT:
            raise ValueError(
                f"Unsupported LetsBonk quote mint {quote_mint}; "
                "the current instruction path only supports wrapped SOL"
            )
        if quote_program != SystemAddresses.TOKEN_PROGRAM:
            raise ValueError(f"Unsupported wrapped-SOL token program {quote_program}")

    @staticmethod
    def _priority_fee_accounts(accounts_info: dict[str, Pubkey]) -> list[Pubkey]:
        """Return stable write locks, excluding read-only programs and mints."""
        account_names = (
            "pool_state",
            "base_vault",
            "quote_vault",
            "platform_fee_vault",
            "creator_fee_vault",
        )
        accounts: list[Pubkey] = []
        for name in account_names:
            account = accounts_info.get(name)
            if not isinstance(account, Pubkey):
                raise ValueError(
                    f"LetsBonk priority-fee account {name} is not a valid public key"
                )
            if account not in accounts:
                accounts.append(account)
        return accounts

    @staticmethod
    def _generate_wsol_seed() -> str:
        """Generate a collision-resistant 32-byte-compatible account seed."""
        return secrets.token_hex(16)

    def _create_initialize_account_instruction(
        self, account: Pubkey, mint: Pubkey, owner: Pubkey
    ) -> Instruction:
        """Create an InitializeAccount instruction for the Token Program.

        Args:
            account: The account to initialize
            mint: The token mint
            owner: The account owner

        Returns:
            Instruction for initializing the account
        """
        accounts = [
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=SystemAddresses.RENT, is_signer=False, is_writable=False
            ),
        ]

        # InitializeAccount instruction discriminator (instruction 1 in Token Program)
        data = bytes([1])

        return Instruction(
            program_id=SystemAddresses.TOKEN_PROGRAM, data=data, accounts=accounts
        )

    def _create_close_account_instruction(
        self, account: Pubkey, destination: Pubkey, owner: Pubkey
    ) -> Instruction:
        """Create a CloseAccount instruction for the Token Program.

        Args:
            account: The account to close
            destination: Where to send the remaining lamports
            owner: The account owner (must sign)

        Returns:
            Instruction for closing the account
        """
        accounts = [
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=destination, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ]

        # CloseAccount instruction discriminator (instruction 9 in Token Program)
        data = bytes([9])

        return Instruction(
            program_id=SystemAddresses.TOKEN_PROGRAM, data=data, accounts=accounts
        )

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
        """Get the recommended compute unit limit for LetsBonk buy operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for buy operations
        """
        if config_override is not None:
            return config_override
        # Buy operations: ATA creation + WSOL creation/init/close + buy instruction
        return 150_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for LetsBonk sell operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for sell operations
        """
        if config_override is not None:
            return config_override
        # Sell operations: WSOL creation/init/close + sell instruction
        return 150_000

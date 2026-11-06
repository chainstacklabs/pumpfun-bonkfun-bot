"""Pump.fun InstructionBuilder: buy and sell instructions, IDL discriminators."""

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from core.pubkeys import is_sol_paired
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

# Account order for buy_v2 (27 accounts) and sell_v2 (26 accounts). Every account
# is mandatory and the order is identical for every coin type. See BUY.md and
# SELL.md under docs/instructions in the pump-fun public docs repository.
_BUY_V2_ACCOUNTS: list[tuple[str, bool]] = [
    ("global", False),
    ("base_mint", False),
    ("quote_mint", False),
    ("base_token_program", False),
    ("quote_token_program", False),
    ("associated_token_program", False),
    ("fee_recipient", True),
    ("associated_quote_fee_recipient", True),
    ("buyback_fee_recipient", True),
    ("associated_quote_buyback_fee_recipient", True),
    ("bonding_curve", True),
    ("associated_base_bonding_curve", True),
    ("associated_quote_bonding_curve", True),
    ("user", True),
    ("associated_base_user", True),
    ("associated_quote_user", True),
    ("creator_vault", True),
    ("associated_creator_vault", True),
    ("sharing_config", False),
    ("global_volume_accumulator", False),
    ("user_volume_accumulator", True),
    ("associated_user_volume_accumulator", True),
    ("fee_config", False),
    ("fee_program", False),
    ("system_program", False),
    ("event_authority", False),
    ("program", False),
]

# sell_v2 is buy_v2 without global_volume_accumulator.
_SELL_V2_ACCOUNTS: list[tuple[str, bool]] = [
    entry for entry in _BUY_V2_ACCOUNTS if entry[0] != "global_volume_accumulator"
]


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
        self._buy_v2_discriminator = discriminators["buy_v2"]
        self._sell_v2_discriminator = discriminators["sell_v2"]

        logger.info(
            "Pump.Fun instruction builder initialized with injected IDL parser "
            "(instruction set: v2)"
        )

    @property
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        return Platform.PUMP_FUN

    @staticmethod
    def _build_account_metas(
        layout: list[tuple[str, bool]],
        accounts_info: dict[str, Pubkey],
        signer: Pubkey,
    ) -> list[AccountMeta]:
        """Turn a v2 account layout into ordered AccountMetas.

        Args:
            layout: Ordered (account name, is_writable) pairs
            accounts_info: Resolved account addresses keyed by IDL name
            signer: The account that signs the transaction

        Returns:
            Ordered list of AccountMeta

        Raises:
            KeyError: If the address provider did not supply a required account
        """
        metas = []
        for name, is_writable in layout:
            pubkey = accounts_info[name]
            if pubkey is None:
                raise KeyError(f"Missing required account for v2 instruction: {name}")
            metas.append(
                AccountMeta(
                    pubkey=pubkey,
                    is_signer=pubkey == signer,
                    is_writable=is_writable,
                )
            )
        return metas

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for pump.fun: buy_v2 plus the ATAs it needs.

        Args:
            user: User's wallet address
            amount_in: Maximum quote amount to spend (raw quote units)
            minimum_amount_out: Exact tokens to buy (raw token units); the
                slippage cap is amount_in
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        return await self.build_buy_v2_instruction(
            token_info, user, amount_in, minimum_amount_out, address_provider
        )

    async def build_buy_v2_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build a buy_v2 instruction plus the ATAs it needs.

        Args:
            user: User's wallet address
            amount_in: Maximum quote amount to spend (raw quote units:
                lamports for SOL-paired coins, 1e-6 USDC for USDC-paired)
            minimum_amount_out: Exact base tokens to buy (raw token units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        instructions = []

        accounts_info = address_provider.get_buy_v2_instruction_accounts(
            token_info, user
        )

        # Base-token ATA for the buyer. buy_v2 does not create this for us.
        instructions.append(
            create_idempotent_associated_token_account(
                user,
                user,
                accounts_info["base_mint"],
                accounts_info["base_token_program"],
            )
        )

        # Quote ATA for the buyer. For SOL-paired coins the program transfers
        # native SOL and only seed-checks this account, so creating it would
        # burn ~0.002 SOL of rent for nothing. Non-SOL quotes need a real,
        # funded token account.
        if not is_sol_paired(accounts_info["quote_mint"]):
            instructions.append(
                create_idempotent_associated_token_account(
                    user,
                    user,
                    accounts_info["quote_mint"],
                    accounts_info["quote_token_program"],
                )
            )

        # buy_v2 args: amount (base tokens out), max_sol_cost (max quote in).
        # Volume tracking is unconditional; there is no track_volume flag.
        instruction_data = (
            self._buy_v2_discriminator
            + struct.pack("<Q", minimum_amount_out)
            + struct.pack("<Q", amount_in)
        )

        instructions.append(
            Instruction(
                program_id=accounts_info["program"],
                data=instruction_data,
                accounts=self._build_account_metas(
                    _BUY_V2_ACCOUNTS, accounts_info, user
                ),
            )
        )

        return instructions

    async def build_sell_v2_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build a sell_v2 instruction plus the ATAs it needs.

        Args:
            user: User's wallet address
            amount_in: Base tokens to sell (raw token units)
            minimum_amount_out: Minimum quote amount to receive (raw quote units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        instructions = []

        accounts_info = address_provider.get_sell_v2_instruction_accounts(
            token_info, user
        )

        # Proceeds of a non-SOL sale land in the seller's quote ATA, which must
        # exist. SOL-paired sales pay out in native SOL.
        if not is_sol_paired(accounts_info["quote_mint"]):
            instructions.append(
                create_idempotent_associated_token_account(
                    user,
                    user,
                    accounts_info["quote_mint"],
                    accounts_info["quote_token_program"],
                )
            )

        # sell_v2 args: amount (base tokens in), min_sol_output (min quote out).
        instruction_data = (
            self._sell_v2_discriminator
            + struct.pack("<Q", amount_in)
            + struct.pack("<Q", minimum_amount_out)
        )

        instructions.append(
            Instruction(
                program_id=accounts_info["program"],
                data=instruction_data,
                accounts=self._build_account_metas(
                    _SELL_V2_ACCOUNTS, accounts_info, user
                ),
            )
        )

        return instructions

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for pump.fun: sell_v2 plus the ATA it needs.

        Args:
            user: User's wallet address
            amount_in: Amount of tokens to sell (raw token units)
            minimum_amount_out: Minimum quote amount expected (raw quote units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        return await self.build_sell_v2_instruction(
            token_info, user, amount_in, minimum_amount_out, address_provider
        )

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for buy operation (for priority fee calculation).

        Args:
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_buy_v2_instruction_accounts(
            token_info, user
        )
        return self._writable_accounts(_BUY_V2_ACCOUNTS, accounts_info)

    @staticmethod
    def _writable_accounts(
        layout: list[tuple[str, bool]], accounts_info: dict[str, Pubkey]
    ) -> list[Pubkey]:
        """Collect the writable accounts from a v2 layout.

        getRecentPrioritizationFees is only meaningful for accounts that are
        write-locked, so program ids and sysvars are dropped.

        Args:
            layout: Ordered (account name, is_writable) pairs
            accounts_info: Resolved account addresses keyed by IDL name
        """
        return [
            accounts_info[name]
            for name, is_writable in layout
            if is_writable and accounts_info.get(name) is not None
        ]

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for sell operation (for priority fee calculation).

        Args:
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_sell_v2_instruction_accounts(
            token_info, user
        )
        return self._writable_accounts(_SELL_V2_ACCOUNTS, accounts_info)

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for pump.fun buy operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for buy operations
        """
        if config_override is not None:
            return config_override
        # buy_v2 touches 27 accounts; 180k leaves headroom for a non-SOL quote's
        # extra ATA init.
        # Re-measure with `tools/simulate_v2_trades.py` after a program upgrade;
        # the exact figure varies with account state.
        return 180_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for pump.fun sell operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for sell operations
        """
        if config_override is not None:
            return config_override
        # sell_v2 touches 26 accounts; 120k leaves headroom. Re-measure with
        # `tools/simulate_v2_trades.py` after a program upgrade; the exact figure
        # varies with account state.
        return 120_000

"""
Pump.Fun implementation of InstructionBuilder interface.

This module builds pump.fun-specific buy and sell instructions
by implementing the InstructionBuilder interface with IDL-based discriminators.
"""

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from core.pubkeys import TOKEN_DECIMALS, is_sol_paired
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

# Account order for buy_v2 (27 accounts) and sell_v2 (26 accounts).
# Every account is mandatory and the order is identical for every coin type —
# SOL-paired or USDC-paired, mayhem or not, cashback or not. This is the whole
# point of the v2 interface. See BUY.md and SELL.md under docs/instructions in
# the pump-fun public docs repository.
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

    def __init__(self, idl_parser: IDLParser, *, use_legacy_instructions: bool = False):
        """Initialize pump.fun instruction builder with injected IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for pump.fun platform
            use_legacy_instructions: Build the pre-v2 buy/sell instructions
                instead of buy_v2/sell_v2. Legacy instructions cannot trade
                coins paired with anything other than SOL.
        """
        self._idl_parser = idl_parser
        self._use_legacy_instructions = use_legacy_instructions

        # Get discriminators from injected IDL parser
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._buy_discriminator = discriminators["buy"]
        self._sell_discriminator = discriminators["sell"]
        self._buy_v2_discriminator = discriminators["buy_v2"]
        self._sell_v2_discriminator = discriminators["sell_v2"]

        logger.info(
            "Pump.Fun instruction builder initialized with injected IDL parser "
            f"(instruction set: {'legacy' if use_legacy_instructions else 'v2'})"
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
        """Build buy instruction(s) for pump.fun.

        Dispatches to buy_v2 unless the builder was constructed with
        ``use_legacy_instructions=True``.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Maximum quote amount to spend (raw quote units)
            minimum_amount_out: Minimum tokens expected (raw token units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        if not self._use_legacy_instructions:
            return await self.build_buy_v2_instruction(
                token_info, user, amount_in, minimum_amount_out, address_provider
            )
        return await self.build_buy_legacy_instruction(
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
            token_info: Token information
            user: User's wallet address
            amount_in: Maximum quote amount to spend (raw quote units:
                lamports for SOL-paired coins, 1e-6 USDC for USDC-paired)
            minimum_amount_out: Base tokens to buy (raw token units)
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
        # Unlike legacy buy there is no track_volume OptionBool — volume
        # tracking is unconditional now that user_volume_accumulator is
        # mandatory.
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
            token_info: Token information
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

    async def build_buy_legacy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build the pre-v2 18-account buy instruction.

        Only works for SOL-paired coins.

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
            # Remaining account: bonding_curve_v2 (readonly, required for all coins)
            AccountMeta(
                pubkey=accounts_info["bonding_curve_v2"],
                is_signer=False,
                is_writable=False,
            ),
            # 18th account: breaking-upgrade fee recipient (mutable) — required from 2026-04-28
            AccountMeta(
                pubkey=accounts_info["breaking_fee_recipient"],
                is_signer=False,
                is_writable=True,
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

        Dispatches to sell_v2 unless the builder was constructed with
        ``use_legacy_instructions=True``.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of tokens to sell (raw token units)
            minimum_amount_out: Minimum quote amount expected (raw quote units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        if not self._use_legacy_instructions:
            return await self.build_sell_v2_instruction(
                token_info, user, amount_in, minimum_amount_out, address_provider
            )
        return await self.build_sell_legacy_instruction(
            token_info, user, amount_in, minimum_amount_out, address_provider
        )

    async def build_sell_legacy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build the pre-v2 16/17-account sell instruction.

        Only works for SOL-paired coins.

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

        # Remaining accounts (after fee_program) for cashback + bonding_curve_v2
        if token_info.is_cashback_coin:
            # Cashback sell: user_volume_accumulator (mutable) + bonding_curve_v2 (readonly)
            sell_accounts.append(
                AccountMeta(
                    pubkey=accounts_info["user_volume_accumulator"],
                    is_signer=False,
                    is_writable=True,
                )
            )
        # bonding_curve_v2 is required for ALL coins (cashback and non-cashback)
        sell_accounts.append(
            AccountMeta(
                pubkey=accounts_info["bonding_curve_v2"],
                is_signer=False,
                is_writable=False,
            )
        )
        # 16/17th account: breaking-upgrade fee recipient (mutable) — required from 2026-04-28
        sell_accounts.append(
            AccountMeta(
                pubkey=accounts_info["breaking_fee_recipient"],
                is_signer=False,
                is_writable=True,
            )
        )

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
        if not self._use_legacy_instructions:
            accounts_info = address_provider.get_buy_v2_instruction_accounts(
                token_info, user
            )
            return self._writable_accounts(_BUY_V2_ACCOUNTS, accounts_info)

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
            accounts_info["bonding_curve_v2"],
            accounts_info["breaking_fee_recipient"],
        ]

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

        Returns:
            List of writable account addresses
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
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        if not self._use_legacy_instructions:
            accounts_info = address_provider.get_sell_v2_instruction_accounts(
                token_info, user
            )
            return self._writable_accounts(_SELL_V2_ACCOUNTS, accounts_info)

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
            accounts_info["bonding_curve_v2"],
            accounts_info["breaking_fee_recipient"],
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
        if self._use_legacy_instructions:
            # Buy operations: ATA creation + buy instruction
            return 100_000
        # buy_v2 touches 27 accounts, so it costs more than the legacy
        # 18-account buy. Mainnet simulation of a SOL-paired Token-2022 buy
        # (including base ATA creation) consumed ~125k CU; a non-SOL quote adds
        # another ATA init on top. Re-measure with
        # learning-examples/simulate_v2_trades.py after any program upgrade.
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
        if self._use_legacy_instructions:
            # Sell operations: typically just sell instruction (ATA exists)
            return 60_000
        # sell_v2 touches 26 accounts. Measured at ~85k CU by simulating a
        # buy+sell in one transaction on mainnet (the combined tx consumed
        # ~211k against ~126k for the buy alone).
        return 120_000

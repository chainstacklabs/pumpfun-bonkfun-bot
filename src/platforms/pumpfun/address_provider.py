"""
Pump.Fun implementation of AddressProvider interface.

This module provides all pump.fun-specific addresses and PDA derivations
by implementing the AddressProvider interface.
"""

from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from interfaces.core import AddressProvider, Platform, TokenInfo


@dataclass
class PumpFunAddresses:
    """Pump.fun program addresses."""

    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    )
    GLOBAL: Final[Pubkey] = Pubkey.from_string(
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    )
    EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
    )
    FEE: Final[Pubkey] = Pubkey.from_string(
        "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
    )
    # Mayhem mode fee recipient (hardcoded to avoid RPC calls)
    # To check if this address is up-to-date, fetch Global account data at offset 483
    # from the pump.fun Global account: 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
    MAYHEM_FEE: Final[Pubkey] = Pubkey.from_string(
        "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"
    )
    LIQUIDITY_MIGRATOR: Final[Pubkey] = Pubkey.from_string(
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
    )
    FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    )

    @staticmethod
    def find_global_volume_accumulator() -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for the global volume accumulator.

        Returns:
            Pubkey of the derived global volume accumulator account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for a user's volume accumulator.

        Args:
            user: Pubkey of the user account

        Returns:
            Pubkey of the derived user volume accumulator account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_fee_config() -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for the fee config.

        Returns:
            Pubkey of the derived fee config account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"fee_config", bytes(PumpFunAddresses.PROGRAM)],
            PumpFunAddresses.FEE_PROGRAM,
        )
        return derived_address


class PumpFunAddressProvider(AddressProvider):
    """Pump.Fun implementation of AddressProvider interface."""

    @property
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        return Platform.PUMP_FUN

    @property
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform."""
        return PumpFunAddresses.PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for pump.fun.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        # Get system addresses from the single source of truth
        system_addresses = SystemAddresses.get_all_system_addresses()

        # Add pump.fun specific addresses
        pumpfun_addresses = {
            # Pump.fun specific addresses
            "program": PumpFunAddresses.PROGRAM,
            "global": PumpFunAddresses.GLOBAL,
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "fee": PumpFunAddresses.FEE,
            "liquidity_migrator": PumpFunAddresses.LIQUIDITY_MIGRATOR,
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
        }

        # Combine system and platform-specific addresses
        return {**system_addresses, **pumpfun_addresses}

    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the bonding curve address for a token.

        For pump.fun, this is the bonding curve PDA derived from the mint.

        Args:
            base_mint: Token mint address
            quote_mint: Not used for pump.fun (SOL is always the quote)

        Returns:
            Bonding curve address
        """
        bonding_curve, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(base_mint)], PumpFunAddresses.PROGRAM
        )
        return bonding_curve

    def derive_user_token_account(
        self, user: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive user's associated token account address.

        Args:
            user: User's wallet address
            mint: Token mint address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            User's associated token account address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
        return get_associated_token_address(user, mint, token_program_id)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get pump.fun-specific additional accounts needed for trading.

        Args:
            token_info: Token information

        Returns:
            Dictionary of additional account addresses
        """
        accounts = {}

        # Add bonding curve if available
        if token_info.bonding_curve:
            accounts["bonding_curve"] = token_info.bonding_curve

        # Add associated bonding curve if available
        if token_info.associated_bonding_curve:
            accounts["associated_bonding_curve"] = token_info.associated_bonding_curve

        # Add creator vault if available
        if token_info.creator_vault:
            accounts["creator_vault"] = token_info.creator_vault

        # Derive associated bonding curve if not provided
        if not token_info.associated_bonding_curve and token_info.bonding_curve:
            accounts["associated_bonding_curve"] = self.derive_associated_bonding_curve(
                token_info.mint, token_info.bonding_curve, token_info.token_program_id
            )

        # Derive creator vault if not provided but creator is available
        if not token_info.creator_vault and token_info.creator:
            accounts["creator_vault"] = self.derive_creator_vault(token_info.creator)

        return accounts

    def derive_associated_bonding_curve(
        self, mint: Pubkey, bonding_curve: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive the associated bonding curve (ATA of bonding curve for the token).

        Args:
            mint: Token mint address
            bonding_curve: Bonding curve address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            Associated bonding curve address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

        derived_address, _ = Pubkey.find_program_address(
            [
                bytes(bonding_curve),
                bytes(token_program_id),
                bytes(mint),
            ],
            SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        )
        return derived_address

    def derive_creator_vault(self, creator: Pubkey) -> Pubkey:
        """Derive the creator vault address.

        Args:
            creator: Creator address

        Returns:
            Creator vault address
        """
        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)], PumpFunAddresses.PROGRAM
        )
        return creator_vault

    def derive_global_volume_accumulator(self) -> Pubkey:
        """Derive the global volume accumulator PDA.

        Returns:
            Global volume accumulator address
        """
        return PumpFunAddresses.find_global_volume_accumulator()

    def derive_user_volume_accumulator(self, user: Pubkey) -> Pubkey:
        """Derive the user volume accumulator PDA.

        Args:
            user: User address

        Returns:
            User volume accumulator address
        """
        return PumpFunAddresses.find_user_volume_accumulator(user)

    def derive_fee_config(self) -> Pubkey:
        """Derive the fee config PDA.

        Returns:
            Fee config address
        """
        return PumpFunAddresses.find_fee_config()

    def get_fee_recipient(self, token_info: TokenInfo) -> Pubkey:
        """Get the correct fee recipient based on mayhem mode.

        Args:
            token_info: Token information with is_mayhem_mode flag

        Returns:
            Fee recipient address (mayhem or standard)
        """
        if token_info.is_mayhem_mode:
            return PumpFunAddresses.MAYHEM_FEE
        return PumpFunAddresses.FEE

    def get_buy_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a buy instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for buy instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_PROGRAM
        )

        # Determine fee recipient based on mayhem mode
        fee_recipient = self.get_fee_recipient(token_info)

        return {
            "global": PumpFunAddresses.GLOBAL,
            "fee": fee_recipient,
            "mint": token_info.mint,
            "bonding_curve": additional_accounts.get(
                "bonding_curve", token_info.bonding_curve
            ),
            "associated_bonding_curve": additional_accounts.get(
                "associated_bonding_curve", token_info.associated_bonding_curve
            ),
            "user_token_account": self.derive_user_token_account(
                user, token_info.mint, token_program_id
            ),
            "user": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "token_program": token_program_id,
            "creator_vault": additional_accounts.get(
                "creator_vault", token_info.creator_vault
            ),
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "program": PumpFunAddresses.PROGRAM,
            "global_volume_accumulator": self.derive_global_volume_accumulator(),
            "user_volume_accumulator": self.derive_user_volume_accumulator(user),
            "fee_config": self.derive_fee_config(),
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
        }

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a sell instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for sell instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_PROGRAM
        )

        # Determine fee recipient based on mayhem mode
        fee_recipient = self.get_fee_recipient(token_info)

        return {
            "global": PumpFunAddresses.GLOBAL,
            "fee": fee_recipient,
            "mint": token_info.mint,
            "bonding_curve": additional_accounts.get(
                "bonding_curve", token_info.bonding_curve
            ),
            "associated_bonding_curve": additional_accounts.get(
                "associated_bonding_curve", token_info.associated_bonding_curve
            ),
            "user_token_account": self.derive_user_token_account(
                user, token_info.mint, token_program_id
            ),
            "user": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "creator_vault": additional_accounts.get(
                "creator_vault", token_info.creator_vault
            ),
            "token_program": token_program_id,
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "program": PumpFunAddresses.PROGRAM,
            "fee_config": self.derive_fee_config(),
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
        }

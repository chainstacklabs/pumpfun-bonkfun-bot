"""
LetsBonk implementation of AddressProvider interface.

This module provides all LetsBonk (Raydium LaunchLab) specific addresses and PDA derivations
by implementing the AddressProvider interface.
"""

from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from interfaces.core import AddressProvider, Platform, TokenInfo


@dataclass
class LetsBonkAddresses:
    """LetsBonk (Raydium LaunchLab) program addresses."""

    # Raydium LaunchLab program addresses
    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
    )
    # NOTE: GLOBAL_CONFIG is NOT constant across all pools!
    # Each pool can be initialized with different global_config values. Different global_configs
    # may define different program-wide settings, versions, or operational parameters.
    # The correct global_config for each pool is extracted during pool initialization
    # and stored in TokenInfo.global_config. This value below is a default, used as a fallback.
    GLOBAL_CONFIG: Final[Pubkey] = Pubkey.from_string(
        "6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"
    )
    # NOTE: PLATFORM_CONFIG is NOT constant across all pools!
    # Each pool is initialized with a specific platform_config that defines its fee structure,
    # launch restrictions, and other settings. Different pools may use different platform_configs
    # (e.g., standard launches vs partner launches). The correct platform_config for each pool
    # is extracted during pool initialization and stored in TokenInfo.platform_config.
    # This value below is the default/most common platform_config, used as a fallback.
    PLATFORM_CONFIG: Final[Pubkey] = Pubkey.from_string(
        "5thqcDwKp5QQ8US4XRMoseGeGbmLKMmoKZmS6zHrQAsA"
    )


class LetsBonkAddressProvider(AddressProvider):
    """LetsBonk (Raydium LaunchLab) implementation of AddressProvider interface."""

    @property
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        return Platform.LETS_BONK

    @property
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform."""
        return LetsBonkAddresses.PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for LetsBonk.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        # Get system addresses from the single source of truth
        system_addresses = SystemAddresses.get_all_system_addresses()

        # Add LetsBonk specific addresses
        letsbonk_addresses = {
            # Raydium LaunchLab specific addresses
            "program": LetsBonkAddresses.PROGRAM,
            "global_config": LetsBonkAddresses.GLOBAL_CONFIG,
            "platform_config": LetsBonkAddresses.PLATFORM_CONFIG,
        }

        # Combine system and platform-specific addresses
        return {**system_addresses, **letsbonk_addresses}

    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the pool state address for a token pair.

        For LetsBonk, this derives the pool state PDA using base_mint and WSOL.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)

        Returns:
            Pool state address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        pool_state, _ = Pubkey.find_program_address(
            [b"pool", bytes(base_mint), bytes(quote_mint)], LetsBonkAddresses.PROGRAM
        )
        return pool_state

    def derive_base_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the base vault address for a token pair.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)

        Returns:
            Base vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        # First derive the pool state address
        pool_state = self.derive_pool_address(base_mint, quote_mint)

        # Then derive the base vault using pool_vault seed
        base_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(base_mint)],
            LetsBonkAddresses.PROGRAM,
        )
        return base_vault

    def derive_quote_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the quote vault address for a token pair.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)

        Returns:
            Quote vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        # First derive the pool state address
        pool_state = self.derive_pool_address(base_mint, quote_mint)

        # Then derive the quote vault using pool_vault seed
        quote_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(quote_mint)],
            LetsBonkAddresses.PROGRAM,
        )
        return quote_vault

    def derive_user_token_account(self, user: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive user's associated token account address.

        Args:
            user: User's wallet address
            mint: Token mint address

        Returns:
            User's associated token account address
        """
        return get_associated_token_address(user, mint)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get LetsBonk-specific additional accounts needed for trading.

        Args:
            token_info: Token information

        Returns:
            Dictionary of additional account addresses
        """
        accounts = {}

        # Add pool state - derive if not present or use existing
        if token_info.pool_state:
            accounts["pool_state"] = token_info.pool_state
        else:
            accounts["pool_state"] = self.derive_pool_address(token_info.mint)

        # Add vault addresses - derive if not present or use existing
        if token_info.base_vault:
            accounts["base_vault"] = token_info.base_vault
        else:
            accounts["base_vault"] = self.derive_base_vault(token_info.mint)

        if token_info.quote_vault:
            accounts["quote_vault"] = token_info.quote_vault
        else:
            accounts["quote_vault"] = self.derive_quote_vault(token_info.mint)

        # Derive authority PDA
        accounts["authority"] = self.derive_authority_pda()

        # Derive event authority PDA
        accounts["event_authority"] = self.derive_event_authority_pda()

        return accounts

    def derive_authority_pda(self) -> Pubkey:
        """Derive the authority PDA for Raydium LaunchLab.

        This PDA acts as the authority for pool vault operations.

        Returns:
            Authority PDA address
        """
        AUTH_SEED = b"vault_auth_seed"
        authority_pda, _ = Pubkey.find_program_address(
            [AUTH_SEED], LetsBonkAddresses.PROGRAM
        )
        return authority_pda

    def derive_event_authority_pda(self) -> Pubkey:
        """Derive the event authority PDA for Raydium LaunchLab.

        This PDA is used for emitting program events during swaps.

        Returns:
            Event authority PDA address
        """
        EVENT_AUTHORITY_SEED = b"__event_authority"
        event_authority_pda, _ = Pubkey.find_program_address(
            [EVENT_AUTHORITY_SEED], LetsBonkAddresses.PROGRAM
        )
        return event_authority_pda

    def derive_creator_fee_vault(
        self, creator: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the creator fee vault PDA.

        This vault accumulates creator fees from trades.

        Args:
            creator: The pool creator's pubkey
            quote_mint: The quote token mint (defaults to WSOL)

        Returns:
            Creator fee vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        creator_fee_vault, _ = Pubkey.find_program_address(
            [bytes(creator), bytes(quote_mint)], LetsBonkAddresses.PROGRAM
        )
        return creator_fee_vault

    def derive_platform_fee_vault(
        self, platform_config: Pubkey | None = None, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the platform fee vault PDA.

        This vault accumulates platform fees from trades.

        Args:
            platform_config: The platform config account (defaults to LetsBonk config)
            quote_mint: The quote token mint (defaults to WSOL)

        Returns:
            Platform fee vault address
        """
        if platform_config is None:
            platform_config = LetsBonkAddresses.PLATFORM_CONFIG

        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        platform_fee_vault, _ = Pubkey.find_program_address(
            [bytes(platform_config), bytes(quote_mint)], LetsBonkAddresses.PROGRAM
        )
        return platform_fee_vault

    def create_wsol_account_with_seed(self, payer: Pubkey, seed: str) -> Pubkey:
        """Create a WSOL account address using createAccountWithSeed pattern.

        Args:
            payer: The account that will pay for and own the new account
            seed: String seed for deterministic account generation

        Returns:
            New WSOL account address
        """
        return Pubkey.create_with_seed(payer, seed, SystemAddresses.TOKEN_PROGRAM)

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

        # Use global_config from TokenInfo if available, otherwise use default
        global_config = (
            token_info.global_config
            if token_info.global_config
            else LetsBonkAddresses.GLOBAL_CONFIG
        )

        # Use platform_config from TokenInfo if available, otherwise use default
        platform_config = (
            token_info.platform_config
            if token_info.platform_config
            else LetsBonkAddresses.PLATFORM_CONFIG
        )

        accounts = {
            "payer": user,
            "authority": additional_accounts["authority"],
            "global_config": global_config,
            "platform_config": platform_config,
            "pool_state": additional_accounts["pool_state"],
            "user_base_token": self.derive_user_token_account(user, token_info.mint),
            "base_vault": additional_accounts["base_vault"],
            "quote_vault": additional_accounts["quote_vault"],
            "base_token_mint": token_info.mint,
            "quote_token_mint": SystemAddresses.SOL_MINT,
            "base_token_program": SystemAddresses.TOKEN_PROGRAM,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "event_authority": additional_accounts["event_authority"],
            "program": LetsBonkAddresses.PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "platform_fee_vault": self.derive_platform_fee_vault(platform_config),
        }

        # Add creator fee vault if creator is known
        if token_info.creator:
            accounts["creator_fee_vault"] = self.derive_creator_fee_vault(
                token_info.creator
            )

        return accounts

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

        # Use global_config from TokenInfo if available, otherwise use default
        global_config = (
            token_info.global_config
            if token_info.global_config
            else LetsBonkAddresses.GLOBAL_CONFIG
        )

        # Use platform_config from TokenInfo if available, otherwise use default
        platform_config = (
            token_info.platform_config
            if token_info.platform_config
            else LetsBonkAddresses.PLATFORM_CONFIG
        )

        accounts = {
            "payer": user,
            "authority": additional_accounts["authority"],
            "global_config": global_config,
            "platform_config": platform_config,
            "pool_state": additional_accounts["pool_state"],
            "user_base_token": self.derive_user_token_account(user, token_info.mint),
            "base_vault": additional_accounts["base_vault"],
            "quote_vault": additional_accounts["quote_vault"],
            "base_token_mint": token_info.mint,
            "quote_token_mint": SystemAddresses.SOL_MINT,
            "base_token_program": SystemAddresses.TOKEN_PROGRAM,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "event_authority": additional_accounts["event_authority"],
            "program": LetsBonkAddresses.PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "platform_fee_vault": self.derive_platform_fee_vault(platform_config),
        }

        # Add creator fee vault if creator is known
        if token_info.creator:
            accounts["creator_fee_vault"] = self.derive_creator_fee_vault(
                token_info.creator
            )

        return accounts

    def get_wsol_account_creation_accounts(
        self, user: Pubkey, wsol_account: Pubkey
    ) -> dict[str, Pubkey]:
        """Get accounts needed for WSOL account creation and initialization.

        Args:
            user: User's wallet address
            wsol_account: WSOL account to be created

        Returns:
            Dictionary of account addresses for WSOL operations
        """
        return {
            "payer": user,
            "wsol_account": wsol_account,
            "wsol_mint": SystemAddresses.SOL_MINT,
            "owner": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "token_program": SystemAddresses.TOKEN_PROGRAM,
            "rent": SystemAddresses.RENT,
        }

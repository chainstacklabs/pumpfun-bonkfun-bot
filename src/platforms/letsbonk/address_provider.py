"""
LetsBonk implementation of AddressProvider interface.

This module provides all LetsBonk (Raydium LaunchLab) specific addresses and PDA derivations
by implementing the AddressProvider interface.
"""

from dataclasses import dataclass
from typing import Final, cast

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
    # These are known LaunchLab configuration accounts, exposed for discovery
    # only. Pool metadata is authoritative and trading paths must never fall
    # back to either value.
    GLOBAL_CONFIG: Final[Pubkey] = Pubkey.from_string(
        "6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"
    )
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
        """Derive the pool state address for an explicit token pair."""
        if quote_mint is None:
            raise ValueError(
                "LetsBonk pool derivation requires the pool's quote_mint; "
                "refusing to assume wrapped SOL"
            )

        pool_state, _ = Pubkey.find_program_address(
            [b"pool", bytes(base_mint), bytes(quote_mint)], LetsBonkAddresses.PROGRAM
        )
        return pool_state

    def derive_base_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the base vault for an explicit token pair."""
        pool_state = self.derive_pool_address(base_mint, quote_mint)
        base_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(base_mint)],
            LetsBonkAddresses.PROGRAM,
        )
        return base_vault

    def derive_quote_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the quote vault for an explicit token pair."""
        if quote_mint is None:
            raise ValueError(
                "LetsBonk quote vault derivation requires the pool's quote_mint; "
                "refusing to assume wrapped SOL"
            )
        pool_state = self.derive_pool_address(base_mint, quote_mint)
        quote_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(quote_mint)],
            LetsBonkAddresses.PROGRAM,
        )
        return quote_vault

    def derive_user_token_account(
        self, user: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive a user's ATA under the mint's authoritative token program."""
        if token_program_id is None:
            raise ValueError(
                f"Token program is required to derive the ATA for mint {mint}"
            )
        return get_associated_token_address(user, mint, token_program_id)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get authoritative LetsBonk pool accounts needed for trading."""
        required = {
            "pool_state": token_info.pool_state,
            "base_vault": token_info.base_vault,
            "quote_vault": token_info.quote_vault,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "LetsBonk pool metadata is missing "
                f"{', '.join(missing)}; refusing to derive execution accounts"
            )

        return {
            "pool_state": cast("Pubkey", required["pool_state"]),
            "base_vault": cast("Pubkey", required["base_vault"]),
            "quote_vault": cast("Pubkey", required["quote_vault"]),
            "authority": self.derive_authority_pda(),
            "event_authority": self.derive_event_authority_pda(),
        }

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
        """Derive the creator fee vault for an explicit quote mint."""
        if quote_mint is None:
            raise ValueError(
                "Creator fee vault derivation requires the pool's quote_mint"
            )

        creator_fee_vault, _ = Pubkey.find_program_address(
            [bytes(creator), bytes(quote_mint)], LetsBonkAddresses.PROGRAM
        )
        return creator_fee_vault

    def derive_platform_fee_vault(
        self, platform_config: Pubkey | None = None, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the platform fee vault from authoritative pool metadata."""
        if platform_config is None:
            raise ValueError(
                "Platform fee vault derivation requires the pool's platform_config"
            )
        if quote_mint is None:
            raise ValueError(
                "Platform fee vault derivation requires the pool's quote_mint"
            )

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
        """Get fail-closed accounts for a LaunchLab buy instruction."""
        return self._get_trade_instruction_accounts(token_info, user)

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get fail-closed accounts for a LaunchLab sell instruction."""
        return self._get_trade_instruction_accounts(token_info, user)

    def _get_trade_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Build a trade account bundle without guessing pool metadata."""
        metadata = {
            "global_config": token_info.global_config,
            "platform_config": token_info.platform_config,
            "quote_token_mint": token_info.quote_mint,
            "base_token_program": token_info.token_program_id,
            "quote_token_program": token_info.quote_token_program_id,
            "creator": token_info.creator,
        }
        missing = [name for name, value in metadata.items() if value is None]
        if missing:
            raise ValueError(
                "LetsBonk trade metadata is missing "
                f"{', '.join(missing)}; refusing to build guessed accounts"
            )

        known_token_programs = {
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        }
        if metadata["base_token_program"] not in known_token_programs:
            raise ValueError(
                f"Unsupported base token program {metadata['base_token_program']}"
            )
        if metadata["quote_token_program"] not in known_token_programs:
            raise ValueError(
                f"Unsupported quote token program {metadata['quote_token_program']}"
            )

        additional_accounts = self.get_additional_accounts(token_info)
        quote_mint = cast("Pubkey", metadata["quote_token_mint"])
        expected_pool = self.derive_pool_address(token_info.mint, quote_mint)
        if additional_accounts["pool_state"] != expected_pool:
            raise ValueError(
                "LetsBonk pool_state does not match the base/quote mint pair"
            )
        if additional_accounts["base_vault"] != self.derive_base_vault(
            token_info.mint, quote_mint
        ):
            raise ValueError("LetsBonk base_vault does not match pool metadata")
        if additional_accounts["quote_vault"] != self.derive_quote_vault(
            token_info.mint, quote_mint
        ):
            raise ValueError("LetsBonk quote_vault does not match pool metadata")

        platform_config = cast("Pubkey", metadata["platform_config"])
        creator = cast("Pubkey", metadata["creator"])
        return {
            "payer": user,
            "authority": additional_accounts["authority"],
            "global_config": cast("Pubkey", metadata["global_config"]),
            "platform_config": platform_config,
            "pool_state": additional_accounts["pool_state"],
            "user_base_token": self.derive_user_token_account(
                user,
                token_info.mint,
                cast("Pubkey", metadata["base_token_program"]),
            ),
            "base_vault": additional_accounts["base_vault"],
            "quote_vault": additional_accounts["quote_vault"],
            "base_token_mint": token_info.mint,
            "quote_token_mint": quote_mint,
            "base_token_program": cast("Pubkey", metadata["base_token_program"]),
            "quote_token_program": cast("Pubkey", metadata["quote_token_program"]),
            "event_authority": additional_accounts["event_authority"],
            "program": LetsBonkAddresses.PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "platform_fee_vault": self.derive_platform_fee_vault(
                platform_config, quote_mint
            ),
            "creator_fee_vault": self.derive_creator_fee_vault(creator, quote_mint),
        }

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

"""Raydium LaunchLab addresses and PDA derivations, shared by the platforms on it."""

from typing import ClassVar, Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import (
    SystemAddresses,
    cached_quote_token_program,
    normalize_quote_mint,
)
from interfaces.core import AddressProvider, TokenInfo

LAUNCHLAB_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
)


def _pda(seeds: list[bytes]) -> Pubkey:
    return Pubkey.find_program_address(seeds, LAUNCHLAB_PROGRAM)[0]


class LaunchLabAddressProvider(AddressProvider):
    """Addresses for one launchpad built on Raydium LaunchLab.

    LaunchLab is one program serving many launchpads; a launchpad is the set of
    platform configs its pools are created under. Subclasses name theirs.

    Every pool address and both fee vaults are keyed by the pool's quote mint,
    so each derivation takes it from `TokenInfo.quote_mint`. Wrapped SOL is only
    the default when a caller has not resolved the quote yet.
    """

    PLATFORM_CONFIGS: ClassVar[frozenset[Pubkey]] = frozenset()

    @property
    def program_id(self) -> Pubkey:
        """Get the LaunchLab program ID."""
        return LAUNCHLAB_PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get the system addresses plus the LaunchLab program."""
        return {
            **SystemAddresses.get_all_system_addresses(),
            "program": LAUNCHLAB_PROGRAM,
        }

    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the pool state PDA for a base/quote pair."""
        quote = normalize_quote_mint(quote_mint)
        return _pda([b"pool", bytes(base_mint), bytes(quote)])

    def derive_pool_vault(self, pool_state: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive the pool's vault for one side of the pair."""
        return _pda([b"pool_vault", bytes(pool_state), bytes(mint)])

    def derive_user_token_account(
        self, user: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive a user's ATA. LaunchLab coins default to Token-2022."""
        return get_associated_token_address(
            user, mint, token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
        )

    def derive_authority_pda(self) -> Pubkey:
        """Derive the PDA that signs for every pool vault."""
        return _pda([b"vault_auth_seed"])

    def derive_event_authority_pda(self) -> Pubkey:
        """Derive the PDA the program emits its CPI events through."""
        return _pda([b"__event_authority"])

    def derive_creator_fee_vault(self, creator: Pubkey, quote_mint: Pubkey) -> Pubkey:
        """Derive the vault the creator's share of curve fees accrues to."""
        return _pda([bytes(creator), bytes(quote_mint)])

    def derive_platform_fee_vault(
        self, platform_config: Pubkey, quote_mint: Pubkey
    ) -> Pubkey:
        """Derive the vault the platform's share of curve fees accrues to.

        One per platform config and quote mint, shared by every pool on both.
        """
        return _pda([bytes(platform_config), bytes(quote_mint)])

    def resolve_quote(self, token_info: TokenInfo) -> tuple[Pubkey, Pubkey]:
        """Return the coin's (quote mint, quote token program).

        The program falls back to the warm cache, which knows wrapped SOL and
        every configured quote mint; an unresolved mint reads as SPL Token.
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        program = token_info.quote_token_program_id or cached_quote_token_program(
            quote_mint
        )
        return quote_mint, program

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get the pool, its vaults and the program PDAs for a coin."""
        quote_mint, _ = self.resolve_quote(token_info)
        pool_state = token_info.pool_state or self.derive_pool_address(
            token_info.mint, quote_mint
        )
        return {
            "pool_state": pool_state,
            "base_vault": token_info.base_vault
            or self.derive_pool_vault(pool_state, token_info.mint),
            "quote_vault": token_info.quote_vault
            or self.derive_pool_vault(pool_state, quote_mint),
            "authority": self.derive_authority_pda(),
            "event_authority": self.derive_event_authority_pda(),
        }

    def get_swap_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get every account buy_exact_in and sell_exact_in take, by IDL name.

        The two share one layout. `global_config` and `platform_config` vary per
        pool and must come from the creation transaction or the pool state.

        Raises:
            ValueError: If the coin's creator, global config or platform config
                is unknown — the fee vault and config accounts cannot be guessed
        """
        if not (
            token_info.creator
            and token_info.global_config
            and token_info.platform_config
        ):
            raise ValueError(
                f"LaunchLab coin {token_info.mint} is missing its creator, global "
                f"config or platform config; read the pool state first"
            )
        quote_mint, quote_token_program = self.resolve_quote(token_info)
        base_token_program = (
            token_info.token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
        )
        accounts = self.get_additional_accounts(token_info)
        return {
            **accounts,
            "payer": user,
            "global_config": token_info.global_config,
            "platform_config": token_info.platform_config,
            "user_base_token": self.derive_user_token_account(
                user, token_info.mint, base_token_program
            ),
            "base_token_mint": token_info.mint,
            "quote_token_mint": quote_mint,
            "base_token_program": base_token_program,
            "quote_token_program": quote_token_program,
            "program": LAUNCHLAB_PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "platform_fee_vault": self.derive_platform_fee_vault(
                token_info.platform_config, quote_mint
            ),
            "creator_fee_vault": self.derive_creator_fee_vault(
                token_info.creator, quote_mint
            ),
        }

    def get_buy_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get every account a buy needs."""
        return self.get_swap_accounts(token_info, user)

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get every account a sell needs."""
        return self.get_swap_accounts(token_info, user)

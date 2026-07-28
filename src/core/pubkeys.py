"""
System addresses and constants for Solana blockchain operations.
This module contains only system-level addresses that are shared across all platforms.
Platform-specific addresses are handled by their respective AddressProvider implementations.
"""

from typing import Final

from solders.pubkey import Pubkey

# Constants
LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6

# Token account constants
TOKEN_ACCOUNT_SIZE: Final[int] = 165  # Size of a token account in bytes
TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE: Final[int] = (
    2_039_280  # Rent-exempt minimum for token accounts
)

# Core system programs
SYSTEM_PROGRAM: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
TOKEN_2022_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
ASSOCIATED_TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# System accounts
RENT: Final[Pubkey] = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Native SOL token
SOL_MINT: Final[Pubkey] = Pubkey.from_string(
    "So11111111111111111111111111111111111111112"
)

# Quote mints supported by pump.fun's v2 trade instructions.
# `bonding_curve.quote_mint` is Pubkey::default() (all zeros) for SOL-paired
# coins; the v2 instructions still expect wrapped SOL to be passed explicitly.
# Doc: pump-public-docs README, "New Bonding Curve Trade Instructions".
DEFAULT_PUBKEY: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
WSOL_MINT: Final[Pubkey] = SOL_MINT
USDC_MINT: Final[Pubkey] = Pubkey.from_string(
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)

# Decimals per quote mint. SOL uses 9, USDC uses 6.
QUOTE_DECIMALS: Final[dict[Pubkey, int]] = {
    WSOL_MINT: 9,
    USDC_MINT: 6,
}

# Token program that owns each quote mint. Both current quote mints are
# legacy SPL Token, but the v2 instructions take quote_token_program
# separately from base_token_program, so keep them decoupled.
QUOTE_TOKEN_PROGRAMS: Final[dict[Pubkey, Pubkey]] = {
    WSOL_MINT: TOKEN_PROGRAM,
    USDC_MINT: TOKEN_PROGRAM,
}


def quote_decimals(quote_mint: Pubkey) -> int:
    """Get the decimal count for a quote mint, defaulting to SOL's 9.

    Args:
        quote_mint: Quote mint address

    Returns:
        Number of decimals used by the quote mint
    """
    return QUOTE_DECIMALS.get(quote_mint, 9)


def quote_units_per_token(quote_mint: Pubkey) -> int:
    """Get the raw-units-per-whole-unit factor for a quote mint.

    Args:
        quote_mint: Quote mint address

    Returns:
        10 ** decimals for the quote mint (1e9 for SOL, 1e6 for USDC)
    """
    return 10 ** quote_decimals(quote_mint)


def quote_token_program(quote_mint: Pubkey) -> Pubkey:
    """Get the token program owning a quote mint, defaulting to SPL Token.

    Args:
        quote_mint: Quote mint address

    Returns:
        Token program id for the quote mint
    """
    return QUOTE_TOKEN_PROGRAMS.get(quote_mint, TOKEN_PROGRAM)


def normalize_quote_mint(quote_mint: Pubkey | None) -> Pubkey:
    """Resolve a bonding curve's quote_mint into a concrete mint address.

    SOL-paired coins carry Pubkey::default() on-chain but must be traded with
    wrapped SOL passed as the quote mint.

    Args:
        quote_mint: Raw quote_mint from the bonding curve, or None

    Returns:
        Wrapped SOL for SOL-paired coins, otherwise the quote mint unchanged
    """
    if quote_mint is None or quote_mint == DEFAULT_PUBKEY:
        return WSOL_MINT
    return quote_mint


# Friendly aliases accepted in bot YAML so configs don't need raw mints.
QUOTE_MINT_ALIASES: Final[dict[str, Pubkey]] = {
    "sol": WSOL_MINT,
    "wsol": WSOL_MINT,
    "usdc": USDC_MINT,
}


def resolve_quote_mint(value: str | Pubkey) -> Pubkey:
    """Resolve a config value into a quote mint address.

    Accepts the aliases "sol"/"wsol"/"usdc" or a raw base58 mint address.

    Args:
        value: Alias or mint address from configuration

    Returns:
        Resolved quote mint

    Raises:
        ValueError: If the value is neither a known alias nor a valid address
    """
    if isinstance(value, Pubkey):
        return normalize_quote_mint(value)

    alias = QUOTE_MINT_ALIASES.get(str(value).strip().lower())
    if alias is not None:
        return alias

    try:
        return normalize_quote_mint(Pubkey.from_string(str(value)))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Unknown quote mint {value!r}. Use one of "
            f"{sorted(QUOTE_MINT_ALIASES)} or a base58 mint address."
        ) from exc


def resolve_quote_amounts(
    amounts: dict[str, float] | None,
) -> dict[Pubkey, float]:
    """Resolve a config map of quote mint -> spend amount.

    Args:
        amounts: Mapping of alias/mint address to amount in whole quote units

    Returns:
        Mapping keyed by resolved Pubkey (empty if amounts is None)

    Raises:
        ValueError: If a key is not a valid quote mint or an amount is not positive
    """
    if not amounts:
        return {}

    resolved: dict[Pubkey, float] = {}
    for key, amount in amounts.items():
        mint = resolve_quote_mint(key)
        if not isinstance(amount, int | float) or amount <= 0:
            raise ValueError(
                f"quote_amounts[{key!r}] must be a positive number, got {amount!r}"
            )
        resolved[mint] = float(amount)
    return resolved


def is_sol_paired(quote_mint: Pubkey | None) -> bool:
    """Check whether a coin is SOL-paired (native SOL transfers).

    Args:
        quote_mint: Raw or normalized quote mint

    Returns:
        True if the coin trades against native/wrapped SOL
    """
    return normalize_quote_mint(quote_mint) == WSOL_MINT


class SystemAddresses:
    """System-level Solana addresses shared across all platforms."""

    # Reference the module-level constants
    SYSTEM_PROGRAM = SYSTEM_PROGRAM
    TOKEN_PROGRAM = TOKEN_PROGRAM
    TOKEN_2022_PROGRAM = TOKEN_2022_PROGRAM
    ASSOCIATED_TOKEN_PROGRAM = ASSOCIATED_TOKEN_PROGRAM
    RENT = RENT
    SOL_MINT = SOL_MINT
    WSOL_MINT = WSOL_MINT
    USDC_MINT = USDC_MINT
    DEFAULT_PUBKEY = DEFAULT_PUBKEY

    @classmethod
    def get_all_system_addresses(cls) -> dict[str, Pubkey]:
        """Get all system addresses as a dictionary.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        return {
            "system_program": cls.SYSTEM_PROGRAM,
            "token_program": cls.TOKEN_PROGRAM,
            "token_2022_program": cls.TOKEN_2022_PROGRAM,
            "associated_token_program": cls.ASSOCIATED_TOKEN_PROGRAM,
            "rent": cls.RENT,
            "sol_mint": cls.SOL_MINT,
        }

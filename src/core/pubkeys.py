"""
System addresses and constants for Solana blockchain operations.
This module contains only system-level addresses that are shared across all platforms.
Platform-specific addresses are handled by their respective AddressProvider implementations.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Final

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

# Token program that owns each quote mint. WSOL and USDC are legacy SPL
# Token, but the v2 instructions take quote_token_program separately from
# base_token_program, so keep them decoupled. A pump.fun upgrade verified
# 2026-09-15 lets create_v2 pair a coin with a Token-2022 quote mint (error
# 6064 now accepts "SPL Token or Token-2022"), so this two-entry map is not
# exhaustive of every tradeable quote mint -- see
# resolve_quote_token_program below for mints outside it.
QUOTE_TOKEN_PROGRAMS: Final[dict[Pubkey, Pubkey]] = {
    WSOL_MINT: TOKEN_PROGRAM,
    USDC_MINT: TOKEN_PROGRAM,
}


# Byte offset of the `decimals` field in a Mint account's raw data. SPL Token
# and Token-2022 mints share the same base layout -- COption<Pubkey>
# mint_authority (4-byte tag + 32-byte pubkey = 36 bytes), u64 supply
# (8 bytes), then u8 decimals at byte 44. Token-2022 extensions are appended
# *after* this base 82-byte struct, never rearranging it. Verified 2026-09-15
# by reading byte 44 off-chain for WSOL (9), USDC (6) and a live Token-2022
# quote mint (6, in a 690-byte account carrying extensions) -- all three
# matched their known decimals.
_MINT_DECIMALS_OFFSET: Final[int] = 44


def _parse_mint_decimals(data: bytes) -> int:
    """Read the `decimals` field out of a raw Mint account's byte layout.

    Args:
        data: Raw account data, e.g. `.data` on the solders `Account` from
            `SolanaClient.get_account_info`

    Returns:
        The mint's decimal count

    Raises:
        ValueError: If the data is too short to be a Mint account
    """
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(
            f"Account data is only {len(data)} bytes, too short to be a "
            f"Mint account (decimals lives at byte {_MINT_DECIMALS_OFFSET})"
        )
    return data[_MINT_DECIMALS_OFFSET]


# Process-lifetime cache of quote mint -> decimals. Pre-seeded from
# QUOTE_DECIMALS so WSOL/USDC never cost an RPC call. Every other entry is
# added by resolve_quote_token_program, which reads decimals off the same
# mint-account fetch it already makes to resolve the token program -- so
# warming this cache costs zero RPC calls beyond that one read per mint.
_QUOTE_DECIMALS_CACHE: dict[Pubkey, int] = dict(QUOTE_DECIMALS)


def quote_decimals(quote_mint: Pubkey) -> int:
    """Get a quote mint's decimal count from the warm cache -- zero RPC.

    Mirrors `cached_quote_token_program`'s shape: pre-seeded with WSOL (9)
    and USDC (6), and every other configured quote mint is resolved (and
    thus cached) once at startup by `resolve_quote_token_program`, so an
    uncached mint here is one the bot will refuse to start trading rather
    than one this silently mis-scales -- see that function's docstring.

    Args:
        quote_mint: Quote mint address

    Returns:
        Number of decimals used by the quote mint
    """
    return _QUOTE_DECIMALS_CACHE.get(quote_mint, 9)


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


# Process-lifetime cache of quote mint -> owning token program. Pre-seeded
# from QUOTE_TOKEN_PROGRAMS so WSOL/USDC never cost an RPC call. Every other
# entry is added by resolve_quote_token_program, normally once per mint at
# bot startup, so the hot path (cached_quote_token_program) never has to make
# a chain read to learn a configured quote mint's token program.
_QUOTE_TOKEN_PROGRAM_CACHE: dict[Pubkey, Pubkey] = dict(QUOTE_TOKEN_PROGRAMS)


async def resolve_quote_token_program(
    quote_mint: Pubkey,
    get_account_info: Callable[[Pubkey], Awaitable[Any]],
) -> Pubkey:
    """Resolve and cache the token program *and* decimals for a quote mint.

    Returns instantly, with no RPC call, for a mint already in
    QUOTE_TOKEN_PROGRAMS or resolved by an earlier call in this process.
    Otherwise fetches the mint account once -- a mint account's owner *is*
    the token program that created it, and its raw data carries `decimals`
    at a fixed byte offset shared by SPL Token and Token-2022 (see
    `_parse_mint_decimals`) -- and caches both results for the rest of the
    process. One fetch resolves both facts, so this never costs more than
    one RPC call per quote mint even though it warms two caches.

    This does one RPC call in the worst case, so callers on a zero-RPC hot
    path (e.g. `extreme_fast_mode`) must not call it directly; read
    `cached_quote_token_program` / `quote_decimals` instead once this has
    warmed the caches.

    Args:
        quote_mint: Quote mint address to resolve
        get_account_info: Async getter returning an account object with
            `.owner` (Pubkey) and `.data` (bytes) attributes, e.g.
            `SolanaClient.get_account_info`

    Returns:
        Token program id that owns the quote mint

    Raises:
        ValueError: If the mint's owner is neither SPL Token nor Token-2022,
            or its account data is too short to carry a decimals field.
            Left for the caller to leave uncaught at startup -- trading a
            quote mint the bot cannot correctly size or derive accounts for
            is worse than refusing to start.
    """
    cached = _QUOTE_TOKEN_PROGRAM_CACHE.get(quote_mint)
    if cached is not None:
        return cached

    account = await get_account_info(quote_mint)
    owner = account.owner
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError(
            f"Quote mint {quote_mint} is owned by {owner}, expected "
            f"{TOKEN_PROGRAM} (SPL Token) or {TOKEN_2022_PROGRAM} (Token-2022)"
        )
    decimals = _parse_mint_decimals(bytes(account.data))
    _QUOTE_TOKEN_PROGRAM_CACHE[quote_mint] = owner
    _QUOTE_DECIMALS_CACHE[quote_mint] = decimals
    return owner


def cached_quote_token_program(quote_mint: Pubkey) -> Pubkey:
    """Read a quote mint's token program from the warm cache -- zero RPC.

    Returns whatever `resolve_quote_token_program` cached for this mint.
    A mint that was never resolved falls back to `quote_token_program`'s
    SPL Token default. That is safe for the bot's own hot path:
    `_resolve_quote_config` only ever trades a quote mint the operator
    configured an amount for, and every configured mint is resolved (and
    thus cached) once at startup, so an uncached mint here is one the bot
    will skip before it ever reaches a buy.

    Args:
        quote_mint: Quote mint address

    Returns:
        Cached token program id, or the SPL Token default if unresolved
    """
    return _QUOTE_TOKEN_PROGRAM_CACHE.get(quote_mint, quote_token_program(quote_mint))


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

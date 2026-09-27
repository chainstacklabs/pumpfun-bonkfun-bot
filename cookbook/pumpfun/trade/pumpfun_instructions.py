"""Self-contained pump.fun v2 trade helpers for the cookbook scripts.

`buy_v2` / `sell_v2` take 27 and 26 mandatory accounts in a fixed order,
identical for every coin. Imports nothing from `src/`.

Docs: BUY.md, SELL.md and COIN_CREATION.md under docs/instructions in
github.com/pump-fun/pump-public-docs
"""

import secrets
import struct
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from construct import Flag, Int64ul, Struct
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

# Programs and well-known accounts
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Quote mints. bonding_curve.quote_mint is Pubkey::default() for SOL-paired
# coins, but the v2 instructions expect wrapped SOL to be passed explicitly.
DEFAULT_PUBKEY = Pubkey.from_string("11111111111111111111111111111111")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
QUOTE_DECIMALS = {WSOL_MINT: 9, USDC_MINT: 6}

# Token program that owns each quote mint below. WSOL and USDC are legacy SPL
# Token, but create_v2 can pair a coin with a Token-2022 quote mint (error 6064
# accepts "SPL Token or Token-2022"), so this map is not exhaustive -- see
# resolve_quote_token_program for mints outside it.
QUOTE_TOKEN_PROGRAMS = {WSOL_MINT: TOKEN_PROGRAM, USDC_MINT: TOKEN_PROGRAM}

TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000

# Account discriminator for the BondingCurve account.
BONDING_CURVE_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)

_SHORT_CURVE_MSG = "Bonding curve data ends before {name} at byte {offset}"

# create_v2 mints under this authority, and the five mayhem accounts below are
# mandatory on every create_v2 whatever is_mayhem_mode says -- the IDL marks none
# of them optional and omitting them fails with AnchorError 3005
# (AccountNotEnoughKeys) on sol_vault. The argument, not the account list, is what
# makes a coin a mayhem coin.
PUMP_MINT_AUTHORITY = Pubkey.from_string("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM")
MAYHEM_PROGRAM = Pubkey.from_string("MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e")
MAYHEM_GLOBAL_PARAMS = Pubkey.from_string(
    "13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ"
)
MAYHEM_SOL_VAULT = Pubkey.from_string("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s")

# Instruction discriminators (first 8 bytes of sha256("global:<name>")).
CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])
EXTEND_ACCOUNT_DISCRIMINATOR = bytes([234, 102, 194, 203, 150, 72, 62, 229])
BUY_V2_DISCRIMINATOR = bytes([184, 23, 238, 97, 103, 197, 211, 61])
SELL_V2_DISCRIMINATOR = bytes([93, 246, 130, 60, 231, 233, 64, 178])
# buy_exact_quote_in_v2 takes the same 27 accounts as buy_v2, in the same
# order — only the discriminator and the two arguments differ.
BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR = bytes([194, 171, 28, 70, 104, 77, 91, 47])
# buy_exact_sol_in is SOL-only and pre-dates the v2 account list. The IDL
# under-reports it at 16 accounts; on chain it needs 18 (see the builder).
BUY_EXACT_SOL_IN_DISCRIMINATOR = bytes([56, 252, 116, 8, 158, 223, 205, 95])
# Both payouts are permissionless: the IDL marks no account a signer, because
# the funds can only move to the wallet they already belong to.
COLLECT_CREATOR_FEE_V2_DISCRIMINATOR = bytes([207, 17, 138, 242, 4, 34, 19, 56])
CLAIM_CASHBACK_V2_DISCRIMINATOR = bytes([122, 243, 204, 65, 94, 116, 29, 55])

# Fee recipients: 8 normal (non-mayhem coins), 8 reserved (mayhem coins),
# 8 buyback (every coin). See FEE_RECIPIENTS.md in the pump-fun public docs.
NORMAL_FEE_RECIPIENTS = [
    Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"),
    Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"),
    Pubkey.from_string("7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX"),
    Pubkey.from_string("9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz"),
    Pubkey.from_string("AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY"),
    Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"),
    Pubkey.from_string("FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz"),
    Pubkey.from_string("G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP"),
]
RESERVED_FEE_RECIPIENTS = [
    Pubkey.from_string("GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"),
    Pubkey.from_string("4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6"),
    Pubkey.from_string("8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR"),
    Pubkey.from_string("4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH"),
    Pubkey.from_string("8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6"),
    Pubkey.from_string("Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk"),
    Pubkey.from_string("463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq"),
    Pubkey.from_string("6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA"),
]
BUYBACK_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]


def normalize_quote_mint(quote_mint: Pubkey | None) -> Pubkey:
    """Map a curve's raw quote_mint onto the mint to pass to the instruction.

    Args:
        quote_mint: Raw value read from the bonding curve, or None

    Returns:
        Wrapped SOL for SOL-paired coins, otherwise the mint unchanged
    """
    if quote_mint is None or quote_mint == DEFAULT_PUBKEY:
        return WSOL_MINT
    return quote_mint


def is_sol_paired(quote_mint: Pubkey | None) -> bool:
    """Whether a coin settles in native SOL.

    Args:
        quote_mint: Raw or normalized quote mint

    Returns:
        True if the coin is SOL-paired
    """
    return normalize_quote_mint(quote_mint) == WSOL_MINT


# Byte offset of `decimals` in a Mint account. SPL Token and Token-2022 share the
# same base layout -- COption<Pubkey> mint_authority (4-byte tag + 32-byte
# pubkey), u64 supply, then u8 decimals at byte 44. Token-2022 extensions are
# appended after the 82-byte base struct, never rearranging it. Mirrors
# core/pubkeys.py, duplicated because this module imports nothing from src/.
_MINT_DECIMALS_OFFSET = 44


def _parse_mint_decimals(data: bytes) -> int:
    """Read the `decimals` field out of a raw Mint account's byte layout.

    Args:
        data: Raw account data, e.g. `.data` on an `AsyncClient.get_account_info`
            response's `.value`

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


# Process-lifetime cache of quote mint -> decimals, pre-seeded so WSOL/USDC never
# cost an RPC call. Warmed by `resolve_quote_token_program` off the mint-account
# fetch it already makes, so it costs no extra RPC calls.
_QUOTE_DECIMALS_CACHE = dict(QUOTE_DECIMALS)


def quote_units(quote_mint: Pubkey) -> int:
    """Raw units per whole unit of a quote mint, from the warm cache.

    Pre-seeded with WSOL (9) and USDC (6). Every other quote mint must be
    resolved by `resolve_quote_token_program` first, which caches its decimals
    off the same account read it already makes.

    Raises rather than guessing: `QuoteControl` admits mints at 6, 8 and 9
    decimals, so a default of 9 overstates a slippage cap by 10x or 1000x, in the
    same direction as the price error it causes, so the two compound instead of
    cancelling.

    Returns:
        10 ** decimals for the quote mint (1e9 for SOL, 1e6 for USDC)

    Raises:
        ValueError: If the mint's decimals have not been resolved yet
    """
    decimals = _QUOTE_DECIMALS_CACHE.get(quote_mint)
    if decimals is None:
        raise ValueError(
            f"Decimals for quote mint {quote_mint} are unknown. Call "
            f"resolve_quote_token_program() for it before pricing or sizing a "
            f"trade -- guessing here misprices the trade by a power of ten."
        )
    return 10**decimals


def quote_token_program_for(quote_mint: Pubkey) -> Pubkey:
    """Token program owning a quote mint, defaulting to SPL Token.

    Returns:
        Token program id for quote_mint if known, else SPL Token
    """
    return QUOTE_TOKEN_PROGRAMS.get(quote_mint, TOKEN_PROGRAM)


# Process-lifetime cache of quote mint -> owning token program, pre-seeded so
# WSOL/USDC never cost an RPC call.
_QUOTE_TOKEN_PROGRAM_CACHE = dict(QUOTE_TOKEN_PROGRAMS)


async def resolve_quote_token_program(
    quote_mint: Pubkey,
    get_account_info: Callable[[Pubkey], Awaitable[Any]],
) -> Pubkey:
    """Resolve and cache the token program *and* decimals for a quote mint.

    Returns with no RPC call for a mint already known or resolved earlier in this
    process. Otherwise fetches the mint account once and caches both facts: the
    account's owner *is* the token program, and its data carries `decimals` at a
    fixed offset (see `_parse_mint_decimals`). `build_v2_accounts` and its callers
    are synchronous, so call this first and pass the result as
    `quote_token_program_id` for a mint outside QUOTE_TOKEN_PROGRAMS.

    Args:
        quote_mint: Quote mint address to resolve
        get_account_info: Async getter returning an account object with
            `.owner` (Pubkey) and `.data` (bytes) attributes, e.g.
            `AsyncClient.get_account_info` (unwrap `.value` from the RPC
            response first)

    Returns:
        Token program id that owns the quote mint

    Raises:
        ValueError: If the mint's owner is neither SPL Token nor Token-2022,
            or its account data is too short to carry a decimals field
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


def find_bonding_curve(mint: Pubkey) -> Pubkey:
    """Derive the bonding curve PDA.

    Args:
        mint: Base token mint
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)[0]


def find_creator_vault(creator: Pubkey) -> Pubkey:
    """Derive the creator vault PDA.

    Args:
        creator: Coin creator
    """
    return Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)], PUMP_PROGRAM
    )[0]


def find_global_volume_accumulator() -> Pubkey:
    """Derive the global volume accumulator PDA."""
    return Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_PROGRAM)[0]


def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    """Derive a user's volume accumulator PDA.

    Args:
        user: User wallet
    """
    return Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM
    )[0]


def find_fee_config() -> Pubkey:
    """Derive the fee config PDA (under the pump fees program)."""
    return Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM
    )[0]


def find_sharing_config(base_mint: Pubkey) -> Pubkey:
    """Derive the creator-fee sharing config PDA (under the pump fees program).

    Mandatory on buy_v2/sell_v2.

    Args:
        base_mint: Base token mint
    """
    return Pubkey.find_program_address(
        [b"sharing-config", bytes(base_mint)], PUMP_FEE_PROGRAM
    )[0]


def find_mayhem_state(mint: Pubkey) -> Pubkey:
    """Derive a coin's mayhem state PDA (under the mayhem program).

    The seed is hyphenated, unlike the pump program's volume-accumulator seeds.

    Args:
        mint: Base token mint
    """
    return Pubkey.find_program_address([b"mayhem-state", bytes(mint)], MAYHEM_PROGRAM)[
        0
    ]


def find_holder_rewards(mint: Pubkey) -> Pubkey:
    """Derive the address that replaces the creator on a holder-reward coin.

    A coin created with `is_holder_reward` does not carry its creator in
    `BondingCurve.creator`; the program substitutes this PDA so the creator fee
    accrues to holders. Trades must derive `creator_vault` from it, not from the
    wallet that signed the create, or the vault seeds do not match (2006).

    Args:
        mint: Base token mint
    """
    return Pubkey.find_program_address([b"holder-rewards", bytes(mint)], PUMP_PROGRAM)[
        0
    ]


def find_quote_control() -> Pubkey:
    """Derive the QuoteControl PDA, the registry of permitted quote mints.

    `Global.whitelisted_quote_mints` is the older, much shorter list; this
    account is the one a create is actually checked against.
    """
    return Pubkey.find_program_address([b"quote-control"], PUMP_PROGRAM)[0]


def curve_creator(mint: Pubkey, creator: Pubkey, *, is_holder_reward: bool) -> Pubkey:
    """The creator a coin's bonding curve will actually carry.

    Args:
        mint: Base token mint
        creator: Creator passed to create_v2
        is_holder_reward: Whether the coin was created as a holder-reward coin
    """
    return find_holder_rewards(mint) if is_holder_reward else creator


def find_associated_token_account(
    owner: Pubkey, mint: Pubkey, token_program: Pubkey
) -> Pubkey:
    """Derive an associated token account address.

    Args:
        owner: ATA owner (may be a PDA)
        mint: Token mint
        token_program: Token program owning the mint
    """
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]


def pick_fee_recipient(*, is_mayhem_mode: bool) -> Pubkey:
    """Pick a fee_recipient from the set the program expects for this coin.

    Args:
        is_mayhem_mode: Whether the coin is in mayhem mode
    """
    pool = RESERVED_FEE_RECIPIENTS if is_mayhem_mode else NORMAL_FEE_RECIPIENTS
    return secrets.choice(pool)


def pick_buyback_fee_recipient() -> Pubkey:
    """Pick a buyback fee recipient, required on every v2 trade."""
    return secrets.choice(BUYBACK_FEE_RECIPIENTS)


class BondingCurveState:
    """Parsed pump.fun BondingCurve account.

    The account is 125 bytes as created, and `extend_account` can grow it to 151,
    256 or any other length the program allows. The struct below covers the
    leading fields, which sit at the same offsets regardless of total length. The
    SOL-named reserve fields were renamed to quote fields when non-SOL quote
    assets landed; the old names are kept as aliases.

    The fields are declared below rather than left to the struct parse, so an
    editor and a type checker can both see what a curve holds.
    """

    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int
    complete: bool
    virtual_sol_reserves: int
    real_sol_reserves: int
    #: None when the account is too short to carry the field.
    creator: Pubkey | None
    is_mayhem_mode: bool
    is_cashback_coin: bool
    quote_mint: Pubkey | None
    is_sol_paired: bool
    creator_fee_bps: int | None
    can_edit_creator_fee: bool | None
    is_holder_reward: bool | None

    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_quote_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_quote_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )
    # Byte offsets from the start of the account, discriminator included.
    _CREATOR_OFFSET = 49
    _MAYHEM_OFFSET = 81
    _CASHBACK_OFFSET = 82
    _QUOTE_MINT_OFFSET = 83
    # The three fields create_v2 added after quote_mint, filling the 36 reserved
    # bytes the old 151-byte layout carried. A legacy `create` curve ends before
    # them, so they read as None rather than as a value the coin never had.
    _CREATOR_FEE_BPS_OFFSET = 115
    _CAN_EDIT_CREATOR_FEE_OFFSET = 123
    _IS_HOLDER_REWARD_OFFSET = 124

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve account data.

        Args:
            data: Raw account data including the 8-byte discriminator

        Raises:
            ValueError: If the discriminator is wrong or the data is truncated
        """
        if len(data) < 8:
            raise ValueError("Data too short to contain discriminator")
        if data[:8] != BONDING_CURVE_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        parsed = self._STRUCT.parse(data[8:])
        self.virtual_token_reserves = parsed.virtual_token_reserves
        self.virtual_quote_reserves = parsed.virtual_quote_reserves
        self.real_token_reserves = parsed.real_token_reserves
        self.real_quote_reserves = parsed.real_quote_reserves
        self.token_total_supply = parsed.token_total_supply
        self.complete = parsed.complete

        # Aliases for the pre-rename field names.
        self.virtual_sol_reserves = self.virtual_quote_reserves
        self.real_sol_reserves = self.real_quote_reserves

        self.creator = self._read_pubkey(data, self._CREATOR_OFFSET)
        self.is_mayhem_mode = self._read_flag(
            data, self._MAYHEM_OFFSET, "is_mayhem_mode"
        )
        self.is_cashback_coin = self._read_flag(
            data, self._CASHBACK_OFFSET, "is_cashback_coin"
        )
        raw_quote_mint = self._read_pubkey(data, self._QUOTE_MINT_OFFSET)
        self.quote_mint = normalize_quote_mint(raw_quote_mint)
        self.is_sol_paired = is_sol_paired(raw_quote_mint)

        self.creator_fee_bps = self._read_u64(data, self._CREATOR_FEE_BPS_OFFSET)
        self.can_edit_creator_fee = self._read_optional_flag(
            data, self._CAN_EDIT_CREATOR_FEE_OFFSET
        )
        self.is_holder_reward = self._read_optional_flag(
            data, self._IS_HOLDER_REWARD_OFFSET
        )

    @staticmethod
    def _read_pubkey(data: bytes, offset: int) -> Pubkey | None:
        """Read a 32-byte pubkey if the data extends that far.

        Args:
            data: Raw account data
            offset: Byte offset

        Returns:
            Pubkey, or None if the field is absent
        """
        if len(data) < offset + 32:
            return None
        return Pubkey.from_bytes(data[offset : offset + 32])

    @staticmethod
    def _read_u64(data: bytes, offset: int) -> int | None:
        """Read a little-endian u64 if the data extends that far.

        Args:
            data: Raw account data
            offset: Byte offset

        Returns:
            Value, or None if the field is absent
        """
        if len(data) < offset + 8:
            return None
        return struct.unpack_from("<Q", data, offset)[0]

    @staticmethod
    def _read_optional_flag(data: bytes, offset: int) -> bool | None:
        """Read a single-byte bool that a shorter curve legitimately lacks.

        Unlike `_read_flag`, absence is an answer here: a legacy `create` curve
        predates these fields, and None says the coin cannot have the property
        rather than that it does not.

        Args:
            data: Raw account data
            offset: Byte offset

        Returns:
            Flag value, or None if the field is absent
        """
        if len(data) <= offset:
            return None
        return bool(data[offset])

    @staticmethod
    def _read_flag(data: bytes, offset: int, name: str) -> bool:
        """Read a single-byte bool, refusing to guess when the field is absent.

        Args:
            data: Raw account data
            offset: Byte offset
            name: Field name, used in the error

        Returns:
            Flag value

        Raises:
            ValueError: If the data ends before the flag. is_mayhem_mode picks
                the fee recipient for a buy, and the wrong one reverts with
                NotAuthorized (6000), so defaulting it only moves the failure
                to somewhere harder to read.
        """
        if len(data) <= offset:
            raise ValueError(_SHORT_CURVE_MSG.format(name=name, offset=offset))
        return bool(data[offset])

    def price_per_token(self) -> float:
        """Current price in whole quote units per whole token.

        Returns:
            Price, or 0.0 if reserves are empty
        """
        if not self.virtual_token_reserves or not self.virtual_quote_reserves:
            return 0.0
        return (self.virtual_quote_reserves / quote_units(self.quote_mint)) / (
            self.virtual_token_reserves / 10**TOKEN_DECIMALS
        )


# Global's fields in declaration order, walked rather than indexed: an offset
# table silently reads the wrong field the day a field is inserted above it,
# and every value below is one set_params call away from changing.
_GLOBAL_LAYOUT: tuple[tuple[str, str], ...] = (
    ("initialized", "bool"),
    ("authority", "pubkey"),
    ("fee_recipient", "pubkey"),
    ("initial_virtual_token_reserves", "u64"),
    ("initial_virtual_sol_reserves", "u64"),
    ("initial_real_token_reserves", "u64"),
    ("token_total_supply", "u64"),
    ("fee_basis_points", "u64"),
    ("withdraw_authority", "pubkey"),
    ("enable_migrate", "bool"),
    ("pool_migration_fee", "u64"),
    ("creator_fee_basis_points", "u64"),
    ("fee_recipients", "pubkey[7]"),
    ("set_creator_authority", "pubkey"),
    ("admin_set_creator_authority", "pubkey"),
    ("create_v2_enabled", "bool"),
    ("whitelist_pda", "pubkey"),
    ("reserved_fee_recipient", "pubkey"),
    ("mayhem_mode_enabled", "bool"),
    ("reserved_fee_recipients", "pubkey[7]"),
    ("is_cashback_enabled", "bool"),
    ("buyback_fee_recipients", "pubkey[8]"),
    ("buyback_basis_points", "u64"),
    ("initial_virtual_quote_reserves", "u64"),
    ("whitelisted_quote_mints", "pubkey[1]"),
    ("creator_fee_configurable", "bool"),
    ("max_configurable_creator_fee_bps", "u64"),
    ("holder_reward_claim_authority", "pubkey"),
    ("is_holder_reward_enabled", "bool"),
)

_GLOBAL_SIZES = {"bool": 1, "u64": 8, "pubkey": 32}


def decode_global(data: bytes) -> dict[str, Any]:
    """Decode the pump.fun Global account.

    Stops at the last field the buffer holds rather than raising, so a Global
    that has grown a new trailing field still yields every field before it.

    Args:
        data: Raw account data, discriminator included

    Returns:
        Field name to value; pubkey arrays come back as lists
    """
    fields: dict[str, Any] = {}
    offset = 8
    for name, kind in _GLOBAL_LAYOUT:
        base, _, count = kind.partition("[")
        repeat = int(count.rstrip("]")) if count else 1
        width = _GLOBAL_SIZES[base] * repeat
        if offset + width > len(data):
            break
        chunk = data[offset : offset + width]
        if base == "bool":
            fields[name] = bool(chunk[0])
        elif base == "u64":
            fields[name] = struct.unpack("<Q", chunk)[0]
        elif count:
            fields[name] = [
                Pubkey.from_bytes(chunk[i : i + 32]) for i in range(0, width, 32)
            ]
        else:
            fields[name] = Pubkey.from_bytes(chunk)
        offset += width
    return fields


async def fetch_global(
    get_account_info: Callable[[Pubkey], Awaitable[Any]],
) -> dict[str, Any]:
    """Read and decode Global.

    Args:
        get_account_info: Async getter returning an account object with a
            `.data` attribute, as `resolve_quote_token_program` takes
    """
    return decode_global(bytes((await get_account_info(PUMP_GLOBAL)).data))


# QuoteControl: admin (32) then 64 reserved bytes, then a vec of
# (mint, initial_virtual_quote_reserves).
_QUOTE_CONTROL_MINTS_OFFSET = 8 + 32 + 64
_QUOTE_CONTROL_ENTRY_SIZE = 32 + 8


def decode_quote_control(data: bytes) -> dict[Pubkey, int]:
    """Decode QuoteControl into quote mint -> opening virtual quote reserves.

    Each admitted mint carries its own opening reserve, which is the coin's
    starting price in that asset. It is not a scaled version of the SOL one.

    Args:
        data: Raw account data, discriminator included

    Returns:
        Mint to initial_virtual_quote_reserves, in the mint's raw units

    Raises:
        ValueError: If the account is too short to hold the vec length
    """
    if len(data) < _QUOTE_CONTROL_MINTS_OFFSET + 4:
        msg = f"QuoteControl account is only {len(data)} bytes"
        raise ValueError(msg)
    count = struct.unpack_from("<I", data, _QUOTE_CONTROL_MINTS_OFFSET)[0]
    start = _QUOTE_CONTROL_MINTS_OFFSET + 4
    entries = {}
    for index in range(count):
        at = start + index * _QUOTE_CONTROL_ENTRY_SIZE
        if at + _QUOTE_CONTROL_ENTRY_SIZE > len(data):
            break
        entries[Pubkey.from_bytes(data[at : at + 32])] = struct.unpack_from(
            "<Q", data, at + 32
        )[0]
    return entries


async def fetch_quote_control(
    get_account_info: Callable[[Pubkey], Awaitable[Any]],
) -> dict[Pubkey, int]:
    """Read and decode the QuoteControl registry.

    Args:
        get_account_info: Async getter returning an account object with `.data`
    """
    account = await get_account_info(find_quote_control())
    return decode_quote_control(bytes(account.data))


def opening_quote_reserves(
    registry: dict[Pubkey, int], quote_mint: Pubkey, global_state: dict[str, Any]
) -> int:
    """Opening virtual quote reserves for a coin priced in `quote_mint`.

    Three sources, because there are three kinds of admitted mint and they do
    not carry the same figure:

    - wrapped SOL takes `Global.initial_virtual_sol_reserves`;
    - a `QuoteControl` entry carries its own reserve, which is the coin's
      opening price in that asset and is not a scaled version of the SOL one;
    - a mint in the older `Global.whitelisted_quote_mints` that QuoteControl
      does not list takes `Global.initial_virtual_quote_reserves`. USDC is one,
      so treating QuoteControl as the whole registry refuses a mint the program
      accepts.

    Raises rather than substituting a default for an unlisted mint: across the
    admitted mints the figure spans several orders of magnitude, so a stand-in
    misprices the opening buy instead of approximating it.

    Args:
        registry: Decoded QuoteControl, from `decode_quote_control`
        quote_mint: Asset the coin is priced in
        global_state: Decoded Global

    Returns:
        Opening virtual quote reserves, in the quote mint's raw units

    Raises:
        ValueError: If neither registry admits the mint
    """
    if is_sol_paired(quote_mint) or quote_mint == WSOL_MINT:
        return global_state["initial_virtual_sol_reserves"]
    reserves = registry.get(quote_mint)
    if reserves is not None:
        return reserves
    if quote_mint in (global_state.get("whitelisted_quote_mints") or []):
        return global_state["initial_virtual_quote_reserves"]
    msg = (
        f"Neither QuoteControl nor Global admits {quote_mint}, so a coin "
        f"cannot be priced in it. QuoteControl lists {len(registry)} mints; "
        f"pumpfun_read_quote_mints.py prints them."
    )
    raise ValueError(msg)


def check_quote_mint_tradable(
    quote_mint: Pubkey, parsed_info: Mapping[str, Any]
) -> float | None:
    """Refuse a quote mint that cannot currently settle a trade.

    Tokenised equities among the admitted mints carry Token-2022 extensions
    their issuer controls. `pausableConfig` stops every transfer of the mint
    while it is set, which fails every trade on every coin priced in it, so a
    paused mint is refused rather than discovered at buy time.
    `scaledUiAmountConfig` means the displayed amount and the raw amount differ
    by a multiplier the issuer moves; trade amounts here are raw, and the
    multiplier is returned so a caller can say so.

    Args:
        quote_mint: Asset the coin is priced in
        parsed_info: The `info` mapping from a jsonParsed mint account

    Returns:
        The scaled-UI multiplier, or None when the mint is not scaled

    Raises:
        ValueError: If the mint is paused
    """
    extensions = {
        entry.get("extension"): entry.get("state", {})
        for entry in (parsed_info.get("extensions") or [])
        if isinstance(entry, dict)
    }
    if (extensions.get("pausableConfig") or {}).get("paused"):
        msg = (
            f"Quote mint {quote_mint} is paused by its issuer. Every trade on "
            f"every coin priced in it fails until that is lifted."
        )
        raise ValueError(msg)
    multiplier = (extensions.get("scaledUiAmountConfig") or {}).get("multiplier")
    return float(multiplier) if multiplier not in (None, "") else None


def check_creator_fee_quote(
    registry: dict[Pubkey, int], quote_mint: Pubkey, creator_fee_bps: int | None
) -> None:
    """Refuse a creator fee the program will accept and then not apply.

    A creator fee is only honoured on a coin priced in a mint `QuoteControl`
    admits. Anywhere else -- wrapped SOL, or a mint carried only by the older
    `Global.whitelisted_quote_mints` such as USDC -- the program takes the
    argument, emits `CreateEvent.creator_fee_bps = 0` and stores 0 on the curve,
    with no error. The token program is not what decides it: the fee lands on
    SPL Token and Token-2022 quote mints alike, provided QuoteControl lists them.

    Args:
        registry: Decoded QuoteControl, from `decode_quote_control`
        quote_mint: Asset the coin is priced in
        creator_fee_bps: Requested fee, or None if the arg is being omitted

    Raises:
        ValueError: If a fee is requested against a mint QuoteControl does not
            admit
    """
    if not creator_fee_bps:
        return
    if quote_mint in registry:
        return
    msg = (
        f"A creator fee is only applied to a coin priced in a mint QuoteControl "
        f"admits, and {quote_mint} is not one -- the program would accept "
        f"--creator-fee-bps and store 0. pumpfun_read_quote_mints.py prints the "
        f"mints that do carry a fee."
    )
    raise ValueError(msg)


def check_mayhem_quote_pairing(quote_mint: Pubkey, *, is_mayhem_mode: bool) -> None:
    """Refuse a mayhem coin priced in anything but SOL.

    The program rejects the pairing with `MayhemModeQuoteMintNotAllowed` (6071).
    Checked before sending, because the create otherwise lands as a reverted
    transaction that has already cost fees.

    Args:
        quote_mint: Asset the coin would be priced in
        is_mayhem_mode: Whether the coin opts into mayhem mode

    Raises:
        ValueError: If a mayhem coin is paired with a non-SOL quote asset
    """
    if not is_mayhem_mode:
        return
    if is_sol_paired(quote_mint) or quote_mint == WSOL_MINT:
        return
    msg = (
        f"Mayhem mode cannot be combined with a non-SOL quote asset: the "
        f"program rejects it with MayhemModeQuoteMintNotAllowed (6071). Drop "
        f"--mayhem, or drop --quote-mint {quote_mint}."
    )
    raise ValueError(msg)


def encode_create_v2_trailing_args(
    *,
    is_cashback_enabled: bool | None = None,
    creator_fee_bps: int | None = None,
    is_holder_reward: bool | None = None,
) -> bytes:
    """Encode create_v2's three trailing args.

    They are positional with no presence tag, so setting a later one forces the
    earlier ones onto the wire, while a shorter form cannot have set a later one.
    Four wire lengths follow `is_mayhem_mode`: 0, 1, 9 and 10 bytes. Neither
    OptionBool nor OptionU64 is a discriminated Option -- each is a single-field
    struct serializing as its bare inner value.

    `None` means omit, mirroring the decoder, which reports an absent arg as
    unset rather than as its default. That distinction is the caller's to make
    and cannot be inferred: coins on chain send `is_cashback_enabled=False`, and
    `creator_fee_bps=0`, explicitly, so collapsing a default to an omission
    would make those wire forms unreachable. Pass 0 or False to send the value,
    leave it None to leave it off.

    An earlier arg left None while a later one is set is filled with the value
    the program would have defaulted it to.

    Args:
        is_cashback_enabled: create_v2 rejects True with 6082
            (CashbackDeprecated); settable anyway so the encoder matches the
            instruction rather than the subset currently accepted
        creator_fee_bps: Bounded by Global's max_configurable_creator_fee_bps
        is_holder_reward: Sets the creator fee aside for holders

    Returns:
        0, 1, 9 or 10 bytes to append after is_mayhem_mode
    """
    send_holder = is_holder_reward is not None
    send_fee = send_holder or creator_fee_bps is not None
    send_cashback = send_fee or is_cashback_enabled is not None
    if not send_cashback:
        return b""

    data = struct.pack("<?", bool(is_cashback_enabled))
    if send_fee:
        data += struct.pack("<Q", creator_fee_bps or 0)
    if send_holder:
        data += struct.pack("<?", bool(is_holder_reward))
    return data


def build_create_v2_instruction(  # noqa: PLR0913
    *,
    mint: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    is_mayhem_mode: bool = False,
    creator_fee_bps: int | None = None,
    is_holder_reward: bool | None = None,
    quote_mint: Pubkey = WSOL_MINT,
    quote_token_program: Pubkey | None = None,
) -> Instruction:
    """Build create_v2, which mints the coin under Token-2022.

    Args:
        mint: The new coin's mint, a keypair you generate; it signs too
        user: Wallet paying for the accounts
        creator: Creator written into the curve, which need not be `user`. On a
            holder-reward coin the program substitutes its own address anyway --
            see `curve_creator`
        name: Coin name
        symbol: Coin ticker
        uri: Metadata URI
        is_mayhem_mode: Whether the coin opts into mayhem mode
        creator_fee_bps: Creator fee, bounded by Global's
            max_configurable_creator_fee_bps and only settable while
            creator_fee_configurable. None omits the arg entirely
        is_holder_reward: Set the creator fee aside for holders instead. None
            omits the arg, which is itself proof the coin is not one
        quote_mint: Asset the coin is priced in. Wrapped SOL gives an ordinary
            SOL-paired coin. **A creator fee only takes effect on a coin priced
            in something other than SOL** -- pump.fun accepts creator_fee_bps on
            a SOL-paired coin and stores zero
        quote_token_program: Token program owning quote_mint. Resolve it from
            chain with `resolve_quote_token_program`; omit only for a mint in
            QUOTE_TOKEN_PROGRAMS

    Returns:
        The create_v2 instruction, 20 accounts
    """
    bonding_curve = find_bonding_curve(mint)
    accounts = [
        AccountMeta(pubkey=mint, is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_MINT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=find_associated_token_account(
                bonding_curve, mint, TOKEN_2022_PROGRAM
            ),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_2022_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=MAYHEM_PROGRAM, is_signer=False, is_writable=True),
        AccountMeta(pubkey=MAYHEM_GLOBAL_PARAMS, is_signer=False, is_writable=False),
        AccountMeta(pubkey=MAYHEM_SOL_VAULT, is_signer=False, is_writable=True),
        AccountMeta(pubkey=find_mayhem_state(mint), is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=find_associated_token_account(
                MAYHEM_SOL_VAULT, mint, TOKEN_2022_PROGRAM
            ),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]

    # Accounts 17-20 are remaining accounts: all four or none. They are sent for
    # SOL-paired coins too, carrying wrapped SOL, so a 20-account create_v2 is
    # not by itself evidence of a non-SOL quote asset -- read quote_mint off the
    # curve for that. Omitting them is what silently drops a creator fee.
    quote_program = quote_token_program or quote_token_program_for(quote_mint)
    accounts += [
        AccountMeta(pubkey=quote_mint, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=find_associated_token_account(
                bonding_curve, quote_mint, quote_program
            ),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=quote_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=find_quote_control(), is_signer=False, is_writable=False),
    ]

    def borsh_string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded

    data = (
        CREATE_V2_DISCRIMINATOR
        + borsh_string(name)
        + borsh_string(symbol)
        + borsh_string(uri)
        + bytes(creator)
        + struct.pack("<?", is_mayhem_mode)
        + encode_create_v2_trailing_args(
            creator_fee_bps=creator_fee_bps, is_holder_reward=is_holder_reward
        )
    )
    return Instruction(PUMP_PROGRAM, data, accounts)


def build_extend_account_instruction(
    bonding_curve: Pubkey, user: Pubkey
) -> Instruction:
    """Build extend_account, which takes no arguments.

    Growing the curve past the 125 bytes create_v2 allocates is what makes a coin
    visible on pump.fun's frontend. The coin trades without it.

    Args:
        bonding_curve: The coin's bonding curve
        user: Wallet paying for the extra space
    """
    return Instruction(
        PUMP_PROGRAM,
        EXTEND_ACCOUNT_DISCRIMINATOR,
        [
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
        ],
    )


def build_v2_accounts(
    *,
    base_mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    quote_mint: Pubkey,
    base_token_program: Pubkey,
    is_mayhem_mode: bool,
    include_global_volume_accumulator: bool,
    quote_token_program_id: Pubkey | None = None,
) -> list[AccountMeta]:
    """Build the ordered account list shared by buy_v2 and sell_v2.

    Args:
        base_mint: Coin being traded
        creator: Coin creator, from bonding_curve.creator
        user: Signer / trader
        quote_mint: Normalized quote mint (wrapped SOL for SOL-paired coins)
        base_token_program: Token program owning base_mint
        is_mayhem_mode: Selects which fee recipient set to draw from
        include_global_volume_accumulator: True for buy_v2, False for sell_v2
        quote_token_program_id: Token program owning quote_mint. Omit to use
            `quote_token_program`'s SPL Token default; pass the result of
            `resolve_quote_token_program` for a mint outside that default,
            e.g. a Token-2022-paired coin.

    Returns:
        Ordered AccountMeta list (27 entries for buy_v2, 26 for sell_v2)
    """
    quote_program = quote_token_program_id or quote_token_program_for(quote_mint)
    bonding_curve = find_bonding_curve(base_mint)
    creator_vault = find_creator_vault(creator)
    user_volume_accumulator = find_user_volume_accumulator(user)
    fee_recipient = pick_fee_recipient(is_mayhem_mode=is_mayhem_mode)
    buyback_fee_recipient = pick_buyback_fee_recipient()

    def ata(owner: Pubkey, mint: Pubkey, program: Pubkey) -> Pubkey:
        return find_associated_token_account(owner, mint, program)

    accounts = [
        (PUMP_GLOBAL, False),
        (base_mint, False),
        (quote_mint, False),
        (base_token_program, False),
        (quote_program, False),
        (ASSOCIATED_TOKEN_PROGRAM, False),
        (fee_recipient, True),
        (ata(fee_recipient, quote_mint, quote_program), True),
        (buyback_fee_recipient, True),
        (ata(buyback_fee_recipient, quote_mint, quote_program), True),
        (bonding_curve, True),
        (ata(bonding_curve, base_mint, base_token_program), True),
        (ata(bonding_curve, quote_mint, quote_program), True),
        (user, True),
        (ata(user, base_mint, base_token_program), True),
        (ata(user, quote_mint, quote_program), True),
        (creator_vault, True),
        (ata(creator_vault, quote_mint, quote_program), True),
        (find_sharing_config(base_mint), False),
    ]
    if include_global_volume_accumulator:
        accounts.append((find_global_volume_accumulator(), False))
    accounts += [
        (user_volume_accumulator, True),
        (ata(user_volume_accumulator, quote_mint, quote_program), True),
        (find_fee_config(), False),
        (PUMP_FEE_PROGRAM, False),
        (SYSTEM_PROGRAM, False),
        (PUMP_EVENT_AUTHORITY, False),
        (PUMP_PROGRAM, False),
    ]

    return [
        AccountMeta(pubkey=pubkey, is_signer=pubkey == user, is_writable=writable)
        for pubkey, writable in accounts
    ]


def build_buy_v2_instruction(
    *,
    base_mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    token_amount_raw: int,
    max_quote_cost_raw: int,
    quote_mint: Pubkey = WSOL_MINT,
    base_token_program: Pubkey = TOKEN_2022_PROGRAM,
    is_mayhem_mode: bool = False,
    quote_token_program_id: Pubkey | None = None,
) -> Instruction:
    """Build a buy_v2 instruction.

    Args:
        base_mint: Coin to buy
        creator: Coin creator, from bonding_curve.creator
        user: Buyer / signer
        token_amount_raw: Base tokens to buy, in raw units
        max_quote_cost_raw: Spend cap in the quote mint's raw units
        quote_mint: Normalized quote mint
        base_token_program: Token program owning base_mint
        is_mayhem_mode: Whether the coin is in mayhem mode
        quote_token_program_id: Token program owning quote_mint; see
            `build_v2_accounts`

    Returns:
        The buy_v2 instruction
    """
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=BUY_V2_DISCRIMINATOR
        + struct.pack("<Q", token_amount_raw)
        + struct.pack("<Q", max_quote_cost_raw),
        accounts=build_v2_accounts(
            base_mint=base_mint,
            creator=creator,
            user=user,
            quote_mint=quote_mint,
            base_token_program=base_token_program,
            is_mayhem_mode=is_mayhem_mode,
            include_global_volume_accumulator=True,
            quote_token_program_id=quote_token_program_id,
        ),
    )


def build_buy_exact_quote_in_v2_instruction(
    *,
    base_mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    spendable_quote_in_raw: int,
    min_tokens_out_raw: int,
    quote_mint: Pubkey = WSOL_MINT,
    base_token_program: Pubkey = TOKEN_2022_PROGRAM,
    is_mayhem_mode: bool = False,
    quote_token_program_id: Pubkey | None = None,
) -> Instruction:
    """Build a buy_exact_quote_in_v2 instruction.

    The mirror of `buy_v2`: that fixes how many tokens you receive and caps the
    spend, this fixes the spend and floors what you receive — the natural form
    when the quote asset is a budget you hold.

    Fees come out of `spendable_quote_in_raw`, so the whole amount leaves the
    wallet and the tokens arrive against what is left.

    Args:
        base_mint: Coin to buy
        creator: Coin creator, from bonding_curve.creator
        user: Buyer / signer
        spendable_quote_in_raw: Exact amount to spend, in the quote mint's raw
            units, fees included
        min_tokens_out_raw: Floor on base tokens received, in raw units. This is
            the slippage protection; the caller owns it.
        quote_mint: Normalized quote mint
        base_token_program: Token program owning base_mint
        is_mayhem_mode: Whether the coin is in mayhem mode
        quote_token_program_id: Token program owning quote_mint; see
            `build_v2_accounts`

    Returns:
        The buy_exact_quote_in_v2 instruction
    """
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR
        + struct.pack("<Q", spendable_quote_in_raw)
        + struct.pack("<Q", min_tokens_out_raw),
        accounts=build_v2_accounts(
            base_mint=base_mint,
            creator=creator,
            user=user,
            quote_mint=quote_mint,
            base_token_program=base_token_program,
            is_mayhem_mode=is_mayhem_mode,
            include_global_volume_accumulator=True,
            quote_token_program_id=quote_token_program_id,
        ),
    )


def build_buy_exact_sol_in_instruction(
    *,
    mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    spendable_sol_in_lamports: int,
    min_tokens_out_raw: int,
    base_token_program: Pubkey = TOKEN_2022_PROGRAM,
    is_mayhem_mode: bool = False,
    track_volume: bool = True,
) -> Instruction:
    """Build a buy_exact_sol_in instruction.

    SOL only. This one pre-dates non-SOL quote assets and has no quote accounts
    at all, so it cannot trade a coin paired with USDC, another coin or a
    tokenized equity — use `build_buy_exact_quote_in_v2_instruction` for those.

    **The IDL lists 16 accounts and the program requires 18.** The two it omits
    are the `bonding-curve-v2` PDA and a buyback fee recipient, both writable,
    both appended after `fee_program`. Sending the IDL's 16 fails with
    AnchorError 6062 (BuybackFeeRecipientMissing), which names the missing
    account but not where it goes.

    Args:
        mint: Coin to buy
        creator: Coin creator, from bonding_curve.creator
        user: Buyer / signer
        spendable_sol_in_lamports: Exact lamports to spend, fees included
        min_tokens_out_raw: Floor on base tokens received, in raw units
        base_token_program: Token program owning the mint
        is_mayhem_mode: Whether the coin is in mayhem mode, which selects the
            reserved fee recipient set instead of the normal one
        track_volume: Whether to credit the user's volume accumulator
    """
    bonding_curve = find_bonding_curve(mint)
    accounts = [
        AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=pick_fee_recipient(is_mayhem_mode=is_mayhem_mode),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=find_associated_token_account(
                bonding_curve, mint, base_token_program
            ),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(
            pubkey=find_associated_token_account(user, mint, base_token_program),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_token_program, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=find_creator_vault(creator), is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=find_global_volume_accumulator(), is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=find_user_volume_accumulator(user), is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=find_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        # The two the IDL omits.
        AccountMeta(
            pubkey=Pubkey.find_program_address(
                [b"bonding-curve-v2", bytes(mint)], PUMP_PROGRAM
            )[0],
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(
            pubkey=pick_buyback_fee_recipient(), is_signer=False, is_writable=True
        ),
    ]
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=BUY_EXACT_SOL_IN_DISCRIMINATOR
        + struct.pack("<Q", spendable_sol_in_lamports)
        + struct.pack("<Q", min_tokens_out_raw)
        # OptionBool is a single-field Anchor struct with no presence tag, so
        # it serializes as its bare inner bool.
        + struct.pack("<?", track_volume),
        accounts=accounts,
    )


def build_collect_creator_fee_v2_instruction(
    *,
    creator: Pubkey,
    quote_mint: Pubkey = WSOL_MINT,
    quote_token_program_id: Pubkey | None = None,
) -> Instruction:
    """Build a collect_creator_fee_v2 instruction.

    Sweeps whatever has accrued in the creator's vault for one quote asset into
    the creator's own token account. Takes no arguments and no signer — the
    money can only go to the wallet it already belongs to, so anyone may run it.

    A creator vault is per (creator, quote asset), so a creator whose coins are
    priced in several assets collects each one separately.

    Args:
        creator: The coin creator whose vault to sweep
        quote_mint: Quote asset to collect, normalized
        quote_token_program_id: Token program owning quote_mint; resolved from
            the cache when omitted

    Returns:
        The collect_creator_fee_v2 instruction
    """
    program = quote_token_program_id or quote_token_program_for(quote_mint)
    vault = find_creator_vault(creator)
    accounts = [
        AccountMeta(pubkey=creator, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=find_associated_token_account(creator, quote_mint, program),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=vault, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=find_associated_token_account(vault, quote_mint, program),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=quote_mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=program, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=COLLECT_CREATOR_FEE_V2_DISCRIMINATOR,
        accounts=accounts,
    )


def build_claim_cashback_v2_instruction(
    *,
    user: Pubkey,
    quote_mint: Pubkey = WSOL_MINT,
    quote_token_program_id: Pubkey | None = None,
) -> Instruction:
    """Build a claim_cashback_v2 instruction.

    Pays out the cashback accrued on the user's volume accumulator. `create_v2`
    refuses to mint new cashback coins (error 6082, CashbackDeprecated), but
    older coins keep accruing and stay claimable, so this path is still live.

    Args:
        user: Wallet whose cashback to pay out
        quote_mint: Quote asset the cashback accrued in, normalized
        quote_token_program_id: Token program owning quote_mint; resolved from
            the cache when omitted

    Returns:
        The claim_cashback_v2 instruction
    """
    program = quote_token_program_id or quote_token_program_for(quote_mint)
    accumulator = find_user_volume_accumulator(user)
    accounts = [
        AccountMeta(pubkey=user, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accumulator, is_signer=False, is_writable=True),
        AccountMeta(pubkey=quote_mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=program, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=find_associated_token_account(accumulator, quote_mint, program),
            is_signer=False,
            is_writable=True,
        ),
        # Any token account of quote_mint owned by the user works here; the
        # associated one is the obvious choice.
        AccountMeta(
            pubkey=find_associated_token_account(user, quote_mint, program),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=CLAIM_CASHBACK_V2_DISCRIMINATOR,
        accounts=accounts,
    )


def build_sell_v2_instruction(
    *,
    base_mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    token_amount_raw: int,
    min_quote_output_raw: int,
    quote_mint: Pubkey = WSOL_MINT,
    base_token_program: Pubkey = TOKEN_2022_PROGRAM,
    is_mayhem_mode: bool = False,
    quote_token_program_id: Pubkey | None = None,
) -> Instruction:
    """Build a sell_v2 instruction.

    Args:
        base_mint: Coin to sell
        creator: Coin creator, from bonding_curve.creator
        user: Seller / signer
        token_amount_raw: Base tokens to sell, in raw units
        min_quote_output_raw: Minimum acceptable payout in raw quote units
        quote_mint: Normalized quote mint
        base_token_program: Token program owning base_mint
        is_mayhem_mode: Whether the coin is in mayhem mode
        quote_token_program_id: Token program owning quote_mint; see
            `build_v2_accounts`

    Returns:
        The sell_v2 instruction
    """
    return Instruction(
        program_id=PUMP_PROGRAM,
        data=SELL_V2_DISCRIMINATOR
        + struct.pack("<Q", token_amount_raw)
        + struct.pack("<Q", min_quote_output_raw),
        accounts=build_v2_accounts(
            base_mint=base_mint,
            creator=creator,
            user=user,
            quote_mint=quote_mint,
            base_token_program=base_token_program,
            is_mayhem_mode=is_mayhem_mode,
            include_global_volume_accumulator=False,
            quote_token_program_id=quote_token_program_id,
        ),
    )

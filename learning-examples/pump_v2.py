"""Self-contained pump.fun v2 trade helpers for the learning examples.

The bonding-curve program's `buy_v2` / `sell_v2` instructions take 27 and 26
mandatory accounts in a fixed order, identical for every coin — SOL-paired or
USDC-paired, mayhem or not, cashback or not. Encoding that once here keeps the
example scripts from each carrying their own copy of the list, which is how they
drift out of sync with the program.

Deliberately standalone: it imports nothing from `src/`, so the examples stay
readable on their own. `learning-examples/verify_v2_account_layout.py` checks the
layout below against `idl/pump_fun_idl.json`.

Docs: BUY.md, SELL.md and COIN_CREATION.md under docs/instructions in
github.com/pump-fun/pump-public-docs
"""

import secrets
import struct

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

TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000

# Account discriminator for the BondingCurve account.
BONDING_CURVE_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)

# Instruction discriminators (first 8 bytes of sha256("global:<name>")).
BUY_V2_DISCRIMINATOR = bytes([184, 23, 238, 97, 103, 197, 211, 61])
SELL_V2_DISCRIMINATOR = bytes([93, 246, 130, 60, 231, 233, 64, 178])

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


def quote_units(quote_mint: Pubkey) -> int:
    """Raw units per whole unit of a quote mint.

    Args:
        quote_mint: Quote mint address

    Returns:
        1e9 for SOL, 1e6 for USDC
    """
    return 10 ** QUOTE_DECIMALS.get(quote_mint, 9)


def quote_token_program(quote_mint: Pubkey) -> Pubkey:
    """Token program owning a quote mint.

    Args:
        quote_mint: Quote mint address

    Returns:
        Token program id (both current quote mints are legacy SPL Token)
    """
    return TOKEN_PROGRAM


def find_bonding_curve(mint: Pubkey) -> Pubkey:
    """Derive the bonding curve PDA.

    Args:
        mint: Base token mint

    Returns:
        Bonding curve address
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)[0]


def find_creator_vault(creator: Pubkey) -> Pubkey:
    """Derive the creator vault PDA.

    Args:
        creator: Coin creator

    Returns:
        Creator vault address
    """
    return Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)], PUMP_PROGRAM
    )[0]


def find_global_volume_accumulator() -> Pubkey:
    """Derive the global volume accumulator PDA.

    Returns:
        Global volume accumulator address
    """
    return Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_PROGRAM)[0]


def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    """Derive a user's volume accumulator PDA.

    Args:
        user: User wallet

    Returns:
        User volume accumulator address
    """
    return Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM
    )[0]


def find_fee_config() -> Pubkey:
    """Derive the fee config PDA (under the pump fees program).

    Returns:
        Fee config address
    """
    return Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM
    )[0]


def find_sharing_config(base_mint: Pubkey) -> Pubkey:
    """Derive the creator-fee sharing config PDA (under the pump fees program).

    Mandatory on buy_v2/sell_v2.

    Args:
        base_mint: Base token mint

    Returns:
        Sharing config address
    """
    return Pubkey.find_program_address(
        [b"sharing-config", bytes(base_mint)], PUMP_FEE_PROGRAM
    )[0]


def find_associated_token_account(
    owner: Pubkey, mint: Pubkey, token_program: Pubkey
) -> Pubkey:
    """Derive an associated token account address.

    Args:
        owner: ATA owner (may be a PDA)
        mint: Token mint
        token_program: Token program owning the mint

    Returns:
        Associated token account address
    """
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]


def pick_fee_recipient(*, is_mayhem_mode: bool) -> Pubkey:
    """Pick a fee_recipient from the set the program expects for this coin.

    Args:
        is_mayhem_mode: Whether the coin is in mayhem mode

    Returns:
        A fee recipient address
    """
    pool = RESERVED_FEE_RECIPIENTS if is_mayhem_mode else NORMAL_FEE_RECIPIENTS
    return secrets.choice(pool)


def pick_buyback_fee_recipient() -> Pubkey:
    """Pick a buyback fee recipient, required on every v2 trade.

    Returns:
        A buyback fee recipient address
    """
    return secrets.choice(BUYBACK_FEE_RECIPIENTS)


class BondingCurveState:
    """Parsed pump.fun BondingCurve account.

    The account is 151 bytes: the 115-byte documented struct followed by
    reserved padding. The SOL-named reserve fields were renamed to quote fields
    when non-SOL quote assets landed; the old names are kept as aliases.
    """

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

        self.__dict__.update(self._STRUCT.parse(data[8:]))

        # Aliases for the pre-rename field names.
        self.virtual_sol_reserves = self.virtual_quote_reserves
        self.real_sol_reserves = self.real_quote_reserves

        self.creator = self._read_pubkey(data, self._CREATOR_OFFSET)
        self.is_mayhem_mode = self._read_flag(data, self._MAYHEM_OFFSET)
        self.is_cashback_coin = self._read_flag(data, self._CASHBACK_OFFSET)
        raw_quote_mint = self._read_pubkey(data, self._QUOTE_MINT_OFFSET)
        self.quote_mint = normalize_quote_mint(raw_quote_mint)
        self.is_sol_paired = is_sol_paired(raw_quote_mint)

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
    def _read_flag(data: bytes, offset: int) -> bool:
        """Read a single-byte bool if the data extends that far.

        Args:
            data: Raw account data
            offset: Byte offset

        Returns:
            Flag value, or False if the field is absent
        """
        return bool(data[offset]) if len(data) > offset else False

    def price_per_token(self) -> float:
        """Current price in whole quote units per whole token.

        Returns:
            Price, or 0.0 if reserves are empty
        """
        if not self.virtual_token_reserves or not self.virtual_quote_reserves:
            return 0.0
        return (
            self.virtual_quote_reserves / 10 ** QUOTE_DECIMALS.get(self.quote_mint, 9)
        ) / (self.virtual_token_reserves / 10**TOKEN_DECIMALS)


def build_v2_accounts(
    *,
    base_mint: Pubkey,
    creator: Pubkey,
    user: Pubkey,
    quote_mint: Pubkey,
    base_token_program: Pubkey,
    is_mayhem_mode: bool,
    include_global_volume_accumulator: bool,
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

    Returns:
        Ordered AccountMeta list (27 entries for buy_v2, 26 for sell_v2)
    """
    quote_program = quote_token_program(quote_mint)
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
        ),
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
        ),
    )

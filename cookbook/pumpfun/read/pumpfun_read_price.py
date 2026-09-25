"""Read one pump.fun bonding curve and print the token price in its quote asset.

Usage:
    uv run cookbook/pumpfun/read/pumpfun_read_price.py <BONDING_CURVE_ADDRESS>

pump.fun coins are not all SOL-paired. The curve carries a `quote_mint`, and the
quote-side reserves are denominated in that mint's raw units — 1e9 for SOL, 1e6 for
USDC. Dividing by a hardcoded 1e9 prints a USDC-paired coin's price 1000x too low.
`quote_mint` is `Pubkey::default()` (all zeros) on SOL-paired coins.
"""

import argparse
import asyncio
import os
import struct
from typing import Final

from construct import Bytes, Flag, Int64ul, Struct
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

TOKEN_DECIMALS: Final[int] = 6

WSOL_MINT: Final[Pubkey] = Pubkey.from_string(
    "So11111111111111111111111111111111111111112"
)
USDC_MINT: Final[Pubkey] = Pubkey.from_string(
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)
# All-zero quote_mint means the coin is SOL-paired.
DEFAULT_QUOTE_MINT: Final[Pubkey] = Pubkey.from_bytes(bytes(32))
QUOTE_DECIMALS: Final[dict[Pubkey, int]] = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS: Final[dict[Pubkey, str]] = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}

# Here and later all the discriminators are precalculated. See cookbook/solana/anchor_calculate_discriminator.py
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

# Data lengths, excluding the 8-byte discriminator: V2 stops after `creator`, V4
# runs through `quote_mint`. Live accounts are 125 bytes as created; extend_account
# can grow one to 151, 256, or any other length the program allows — the tail
# past V4 is unread here regardless of total length.
_V2_LENGTH: Final[int] = 73
_V4_LENGTH: Final[int] = 107

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


class BondingCurveState:
    """Parse bonding curve account data - supports all versions.

    The fields are declared below rather than left to the struct parse, so an
    editor and a type checker can both see what a curve holds. The quote-side
    reserves are in the quote mint's raw units — 1e9 for SOL, 1e6 for USDC — so
    they are only lamports on a SOL-paired coin. `virtual_sol_reserves` and
    `real_sol_reserves` are aliases kept from before the rename.
    """

    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int
    complete: bool
    virtual_sol_reserves: int
    real_sol_reserves: int
    #: None on a layout predating the creator field.
    creator: Pubkey | None
    is_mayhem_mode: bool
    is_cashback_coin: bool
    #: All zeros on a SOL-paired coin.
    quote_mint: Pubkey

    _STRUCT_V1 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_quote_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_quote_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    _STRUCT_V2 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_quote_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_quote_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
    )

    _STRUCT_V3 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_quote_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_quote_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,
    )

    # Current layout, covering the account as created (125 bytes) and, at the same
    # offsets, as it reads once extend_account has grown it to 151, 256, or any
    # other length. Trailing fields appended past this struct are not needed for
    # a price read and are left unread.
    _STRUCT_V4 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_quote_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_quote_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,
        "is_cashback_coin" / Flag,
        "quote_mint" / Bytes(32),
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data - auto-detects version.

        Args:
            data: Raw account data including the 8-byte discriminator
        """
        body = data[8:]
        data_length = len(body)

        if data_length < _V2_LENGTH:  # V1: without creator and mayhem mode
            parsed = self._STRUCT_V1.parse(body)
        elif data_length == _V2_LENGTH:  # V2: with creator, without mayhem mode
            parsed = self._STRUCT_V2.parse(body)
        elif data_length < _V4_LENGTH:  # V3: with creator and mayhem mode
            parsed = self._STRUCT_V3.parse(body)
        else:  # V4: adds is_cashback_coin and quote_mint
            parsed = self._STRUCT_V4.parse(body)

        self.virtual_token_reserves = parsed.virtual_token_reserves
        self.virtual_quote_reserves = parsed.virtual_quote_reserves
        self.real_token_reserves = parsed.real_token_reserves
        self.real_quote_reserves = parsed.real_quote_reserves
        self.token_total_supply = parsed.token_total_supply
        self.complete = parsed.complete

        # The trailing fields arrived one layout at a time; an older account
        # stops short of them and reads as the behaviour they replaced.
        creator = parsed.get("creator")
        self.creator = Pubkey.from_bytes(creator) if creator else None
        self.is_mayhem_mode = parsed.get("is_mayhem_mode", False)
        self.is_cashback_coin = parsed.get("is_cashback_coin", False)
        quote_mint = parsed.get("quote_mint")
        self.quote_mint = (
            Pubkey.from_bytes(quote_mint) if quote_mint else DEFAULT_QUOTE_MINT
        )

        # The SOL-named fields were renamed when non-SOL quote assets landed. Keep the
        # old names working for anything that still reads them.
        self.virtual_sol_reserves = self.virtual_quote_reserves
        self.real_sol_reserves = self.real_quote_reserves


def normalize_quote_mint(quote_mint: Pubkey) -> Pubkey:
    """Resolve an all-zero quote mint to wrapped SOL.

    Args:
        quote_mint: The curve's raw quote_mint field

    Returns:
        The effective quote mint
    """
    return WSOL_MINT if quote_mint == DEFAULT_QUOTE_MINT else quote_mint


# The `decimals` byte sits at this offset in both SPL Token and Token-2022
# mints; Token-2022 extensions are appended after the base struct and never
# move it.
_MINT_DECIMALS_OFFSET = 44


async def read_quote_decimals(conn: AsyncClient, quote_mint: Pubkey) -> int:
    """Read a quote mint's decimals from chain.

    pump.fun's quote assets are not just SOL and USDC. Its `QuoteControl`
    registry admits mints at 6, 8 and 9 decimals — tokenized equities are 8
    (xStocks) or 6 (Backpack Securities) — so assuming 9 misreports the price
    by a factor of ten or a thousand. One account read settles it.

    Args:
        conn: Connected RPC client
        quote_mint: The effective quote mint

    Returns:
        The mint's decimal count

    Raises:
        ValueError: If the mint account is missing or too short to be a mint
    """
    if quote_mint in QUOTE_DECIMALS:
        return QUOTE_DECIMALS[quote_mint]

    response = await conn.get_account_info(quote_mint, encoding="base64")
    if response.value is None:
        raise ValueError(f"Quote mint {quote_mint} does not exist on chain")
    data = bytes(response.value.data)
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(
            f"Account {quote_mint} is only {len(data)} bytes, too short to be a mint"
        )
    return data[_MINT_DECIMALS_OFFSET]


async def get_bonding_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    """Fetch and parse a bonding curve account.

    Args:
        conn: Connected RPC client
        curve_address: The bonding curve PDA

    Returns:
        The parsed curve state

    Raises:
        ValueError: If the account is missing or is not a bonding curve
    """
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return BondingCurveState(data)


def calculate_bonding_curve_price(
    curve_state: BondingCurveState, quote_decimals: int
) -> float:
    """Price one token in the curve's quote asset.

    Args:
        curve_state: The parsed curve state
        quote_decimals: Decimals of the curve's quote mint, read from chain

    Returns:
        Price per token, denominated in the quote mint

    Raises:
        ValueError: If either virtual reserve is non-positive
    """
    if (
        curve_state.virtual_token_reserves <= 0
        or curve_state.virtual_quote_reserves <= 0
    ):
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_quote_reserves / 10**quote_decimals) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def show_price(curve_address: Pubkey) -> None:
    """Print the price of the coin behind one bonding curve.

    Args:
        curve_address: The bonding curve to read
    """
    try:
        async with AsyncClient(RPC_ENDPOINT) as conn:
            state = await get_bonding_curve_state(conn, curve_address)
            quote_mint = normalize_quote_mint(state.quote_mint)
            quote_decimals = await read_quote_decimals(conn, quote_mint)
            price = calculate_bonding_curve_price(state, quote_decimals)

            symbol = QUOTE_SYMBOLS.get(quote_mint, str(quote_mint))

            print("Token price:")
            print(f"  {price:.10f} {symbol}")
            print(f"\nquote mint:  {quote_mint} ({quote_decimals} decimals)")
            print(f"mayhem:      {state.is_mayhem_mode}")
            print(f"cashback:    {state.is_cashback_coin}")
            print(f"complete:    {state.complete}")
    except ValueError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


def main() -> None:
    """Parse the command line and print the price."""
    parser = argparse.ArgumentParser(description="Print one coin's price")
    parser.add_argument("curve", help="The coin's bonding curve address")
    args = parser.parse_args()

    asyncio.run(show_price(Pubkey.from_string(args.curve)))


if __name__ == "__main__":
    main()

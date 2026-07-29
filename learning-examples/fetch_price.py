"""Read one pump.fun bonding curve and print the token price in its quote asset.

Usage:
    uv run learning-examples/fetch_price.py <BONDING_CURVE_ADDRESS>

pump.fun coins are not all SOL-paired. The curve carries a `quote_mint`, and the
quote-side reserves are denominated in that mint's raw units — 1e9 for SOL, 1e6 for
USDC. Dividing by a hardcoded 1e9 prints a USDC-paired coin's price 1000x too low.
`quote_mint` is `Pubkey::default()` (all zeros) on SOL-paired coins.
"""

import asyncio
import os
import struct
import sys
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

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

# Data lengths, excluding the 8-byte discriminator: V2 stops after `creator`, V4
# runs through `quote_mint`. Live accounts are 151 bytes, so the tail is padding.
_V2_LENGTH: Final[int] = 73
_V4_LENGTH: Final[int] = 107

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


class BondingCurveState:
    """Parse bonding curve account data - supports all versions."""

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

    # Current layout. The account is 151 bytes: this struct is 107 bytes after the
    # 8-byte discriminator, and the remaining 36 are reserved padding.
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

        self.creator = None
        self.is_mayhem_mode = False
        self.is_cashback_coin = False
        self.quote_mint = DEFAULT_QUOTE_MINT

        if data_length < _V2_LENGTH:  # V1: without creator and mayhem mode
            parsed = self._STRUCT_V1.parse(body)
        elif data_length == _V2_LENGTH:  # V2: with creator, without mayhem mode
            parsed = self._STRUCT_V2.parse(body)
        elif data_length < _V4_LENGTH:  # V3: with creator and mayhem mode
            parsed = self._STRUCT_V3.parse(body)
        else:  # V4: adds is_cashback_coin and quote_mint
            parsed = self._STRUCT_V4.parse(body)

        self.__dict__.update(parsed)
        if isinstance(self.creator, bytes):
            self.creator = Pubkey.from_bytes(self.creator)
        if isinstance(self.quote_mint, bytes):
            self.quote_mint = Pubkey.from_bytes(self.quote_mint)

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


def quote_units(quote_mint: Pubkey) -> int:
    """Return the raw units per whole token of the quote mint.

    Args:
        quote_mint: The effective quote mint

    Returns:
        1e9 for SOL, 1e6 for USDC, defaulting to 1e9 for an unknown mint
    """
    return 10 ** QUOTE_DECIMALS.get(quote_mint, 9)


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


def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    """Price one token in the curve's quote asset.

    Args:
        curve_state: The parsed curve state

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

    quote_mint = normalize_quote_mint(curve_state.quote_mint)
    return (curve_state.virtual_quote_reserves / quote_units(quote_mint)) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def main() -> None:
    """Print the price of the coin behind the bonding curve given on the CLI."""
    if len(sys.argv) < 2:
        print("Usage: uv run learning-examples/fetch_price.py <BONDING_CURVE_ADDRESS>")
        return

    try:
        async with AsyncClient(RPC_ENDPOINT) as conn:
            curve_address = Pubkey.from_string(sys.argv[1])
            state = await get_bonding_curve_state(conn, curve_address)
            price = calculate_bonding_curve_price(state)

            quote_mint = normalize_quote_mint(state.quote_mint)
            symbol = QUOTE_SYMBOLS.get(quote_mint, str(quote_mint))

            print("Token price:")
            print(f"  {price:.10f} {symbol}")
            print(f"\nquote mint:  {quote_mint}")
            print(f"mayhem:      {state.is_mayhem_mode}")
            print(f"cashback:    {state.is_cashback_coin}")
            print(f"complete:    {state.complete}")
    except ValueError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    asyncio.run(main())

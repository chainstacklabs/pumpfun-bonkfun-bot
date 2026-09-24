"""Decode a bonding curve account's raw bytes into its fields and a price.

Usage:
    uv run cookbook/pumpfun/decode/pumpfun_decode_curve_getaccountinfo.py [curve.json]

Falls back to the fixture beside this file. `getAccountInfo` returns base64 bytes
and nothing else — the layout is yours to know. This walks it field by field:
reserves, the completion flag, the creator, the mayhem and cashback flags, and
the quote mint.

Two things the layout does not make obvious:

- **`quote_mint` is all zeros on SOL-paired coins**, not wrapped SOL. The quote
  reserves are always in that mint's own raw units — 1e9 for SOL, 1e6 for USDC —
  so a price computed against a hardcoded 1e9 is 1000x off for a USDC pair. Any
  other quote mint is read from its mint account, which needs
  `SOLANA_NODE_RPC_ENDPOINT` in `.env`.
- **The account grows.** `create_v2` allocates 125 bytes and `extend_account` can
  push it to 151, 256 or more. Every field below sits at a fixed offset from the
  start, so decode any length at or above 125 the same way and never filter on
  the total.
"""

import argparse
import asyncio
import base64
import json
import os
import struct
import sys

from construct import Bytes, Flag, Int64ul, Struct
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

TOKEN_DECIMALS = 6
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)

# Quote assets. A curve's `quote_mint` is all zeros when the coin is SOL-paired, and
# the quote-side reserves are always raw units of that mint: 1e9 for SOL, 1e6 for USDC.
DEFAULT_QUOTE_MINT = Pubkey.from_bytes(bytes(32))
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
QUOTE_DECIMALS = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}

# Same offset in SPL Token and Token-2022: extensions are appended after it.
_MINT_DECIMALS_OFFSET = 44


class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),  # Added new creator field - 32 bytes for Pubkey
        "is_mayhem_mode" / Flag,  # Added mayhem mode flag - 1 byte
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data - supports all versions."""
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        # Required fields (always present)
        offset = 8
        self.virtual_token_reserves = int.from_bytes(
            data[offset : offset + 8], "little"
        )
        offset += 8
        self.virtual_sol_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.real_token_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.real_sol_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.token_total_supply = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.complete = bool(data[offset])
        offset += 1

        # Optional fields (may not be present in older versions)
        if len(data) >= offset + 32:
            self.creator = Pubkey.from_bytes(data[offset : offset + 32])
            offset += 32

            if len(data) > offset:
                self.is_mayhem_mode = bool(data[offset])
                offset += 1
            else:
                self.is_mayhem_mode = None

            if len(data) > offset:
                self.is_cashback_coin = bool(data[offset])
                offset += 1
            else:
                self.is_cashback_coin = None

            if len(data) >= offset + 32:
                self.quote_mint = Pubkey.from_bytes(data[offset : offset + 32])
            else:
                self.quote_mint = DEFAULT_QUOTE_MINT

        else:
            self.creator = None
            self.is_mayhem_mode = None
            self.is_cashback_coin = None
            self.quote_mint = DEFAULT_QUOTE_MINT

    @property
    def effective_quote_mint(self) -> Pubkey:
        """The quote mint to price against, resolving all-zeros to wrapped SOL."""
        return WSOL_MINT if self.quote_mint == DEFAULT_QUOTE_MINT else self.quote_mint

    @property
    def quote_symbol(self) -> str:
        """Display symbol of the quote asset."""
        mint = self.effective_quote_mint
        return QUOTE_SYMBOLS.get(mint, str(mint))


async def read_quote_decimals(quote_mint: Pubkey) -> int:
    """Read a quote mint's decimals from chain.

    `QuoteControl` admits mints from 4 to 12 decimals, so a default of 9 is
    wrong for most of them and misprices the coin silently. The curve names its
    quote mint but not its decimals, so the one field it cannot give is fetched.

    Raises:
        ValueError: If no endpoint is set, or the mint is missing or too short
    """
    if quote_mint in QUOTE_DECIMALS:
        return QUOTE_DECIMALS[quote_mint]

    if not RPC_ENDPOINT:
        raise ValueError(
            f"This coin is quoted in {quote_mint}, whose decimals only the mint "
            f"account gives. Set SOLANA_NODE_RPC_ENDPOINT in .env."
        )

    async with AsyncClient(RPC_ENDPOINT) as conn:
        response = await conn.get_account_info(quote_mint, encoding="base64")
    if response.value is None:
        raise ValueError(f"Quote mint {quote_mint} does not exist on chain")
    data = bytes(response.value.data)
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(
            f"Account {quote_mint} is only {len(data)} bytes, too short to be a mint"
        )
    return data[_MINT_DECIMALS_OFFSET]


def calculate_bonding_curve_price(
    curve_state: BondingCurveState, quote_unit: int
) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / quote_unit) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


def decode_bonding_curve_data(raw_data: str) -> BondingCurveState:
    decoded_data = base64.b64decode(raw_data)
    if decoded_data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")
    return BondingCurveState(decoded_data)


DEFAULT_FIXTURE = "cookbook/pumpfun/decode/raw_bonding_curve_from_getaccountinfo.json"


async def decode_and_print(account_path: str) -> None:
    """Decode one saved curve account and print its fields and price."""
    with open(account_path) as file:
        json_data = json.load(file)

    # Extract the base64 encoded data
    encoded_data = json_data["result"]["value"]["data"][0]

    bonding_curve_state = decode_bonding_curve_data(encoded_data)

    # Calculate and print the token price
    quote_unit = 10 ** await read_quote_decimals(
        bonding_curve_state.effective_quote_mint
    )
    token_price = calculate_bonding_curve_price(bonding_curve_state, quote_unit)
    symbol = bonding_curve_state.quote_symbol

    print("Bonding Curve State:")
    print(f"  Virtual Token Reserves: {bonding_curve_state.virtual_token_reserves}")
    print(
        f"  Virtual Quote Reserves: {bonding_curve_state.virtual_sol_reserves} raw {symbol}"
    )
    print(f"  Real Token Reserves: {bonding_curve_state.real_token_reserves}")
    print(
        f"  Real Quote Reserves: {bonding_curve_state.real_sol_reserves} raw {symbol}"
    )
    print(f"  Token Total Supply: {bonding_curve_state.token_total_supply}")
    print(f"  Complete: {bonding_curve_state.complete}")
    print(f"  Mayhem Mode: {bonding_curve_state.is_mayhem_mode}")
    print(f"  Cashback Coin: {bonding_curve_state.is_cashback_coin}")
    print(f"  Quote Mint: {bonding_curve_state.effective_quote_mint}")
    print(f"\nToken Price: {token_price:.10f} {symbol}")


def main() -> None:
    """Parse the command line and decode the curve account."""
    parser = argparse.ArgumentParser(
        description="Decode a bonding curve account's raw bytes"
    )
    parser.add_argument(
        "account",
        nargs="?",
        default=DEFAULT_FIXTURE,
        help=f"Saved getAccountInfo response (default {DEFAULT_FIXTURE})",
    )
    args = parser.parse_args()

    try:
        asyncio.run(decode_and_print(args.account))
    except ValueError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()

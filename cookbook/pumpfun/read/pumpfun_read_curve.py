"""Module for checking the status of a token's bonding curve on the Solana network using
the Pump.fun program. It allows querying the bonding curve state and completion status.

Usage:
    uv run cookbook/pumpfun/read/pumpfun_read_curve.py <MINT>
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

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

# Constants
PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

# Shortest account that still reaches quote_mint: 8 discriminator + 41 base fields
# + 32 creator + 1 mayhem + 1 cashback + 32 quote_mint.
_V5_MIN_LENGTH: Final[int] = 115

# Quote assets. `quote_mint` is all zeros on SOL-paired coins; the quote-side reserves
# are always in the quote mint's raw units (1e9 for SOL, 1e6 for USDC).
DEFAULT_QUOTE_MINT: Final[Pubkey] = Pubkey.from_bytes(bytes(32))
WSOL_MINT: Final[Pubkey] = Pubkey.from_string(
    "So11111111111111111111111111111111111111112"
)
USDC_MINT: Final[Pubkey] = Pubkey.from_string(
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)
QUOTE_DECIMALS: Final[dict[Pubkey, int]] = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS: Final[dict[Pubkey, str]] = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}

# Same offset in SPL Token and Token-2022: extensions are appended after it.
_MINT_DECIMALS_OFFSET: Final[int] = 44


def effective_quote_mint(quote_mint: Pubkey) -> Pubkey:
    """Resolve a curve's raw quote_mint field to the mint it prices against.

    The field is all zeros on SOL-paired coins, not wrapped SOL.
    """
    return WSOL_MINT if quote_mint == DEFAULT_QUOTE_MINT else quote_mint


async def read_quote_decimals(conn: AsyncClient, quote_mint: Pubkey) -> int:
    """Read a quote mint's decimals from chain.

    `QuoteControl` admits mints from 4 to 12 decimals, so a default of 9 is
    wrong for most of them and misscales every quote-side figure silently.

    Raises:
        ValueError: If the mint is missing or too short to be a mint
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


class BondingCurveState:
    """Represents the state of a bonding curve account.

    The fields are declared below rather than left to the struct parse, so an
    editor and a type checker can both see what a curve holds. The quote-side
    reserves are in the quote mint's raw units — 1e9 for SOL, 1e6 for USDC — so
    they are only lamports on a SOL-paired coin.
    """

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool
    #: All zeros on a SOL-paired coin; `effective_quote_mint` resolves it.
    quote_mint: Pubkey

    # V2: Struct with creator field (81 bytes total: 8 discriminator + 73 data)
    _STRUCT_V2 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),  # Added new creator field - 32 bytes for Pubkey
    )

    # V3: Struct with creator + mayhem mode (82 bytes total: 8 discriminator + 74 data)
    _STRUCT_V3 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,  # Added mayhem mode flag - 1 byte
    )

    # V4: V3 + is_cashback_coin (83 bytes total: 8 discriminator + 75 data) — added in the late-Feb 2026 cashback upgrade
    _STRUCT_V4 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,
        "is_cashback_coin" / Flag,
    )

    # V5: V4 + quote_mint. Accounts are 125 bytes as created and extend_account
    # can grow one to any length the program allows; this struct covers the
    # leading fields, which sit at the same offsets regardless. Quote-side
    # reserves are in the quote mint's raw units, so a non-SOL coin must not be
    # scaled by 1e9.
    _STRUCT_V5 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,
        "is_cashback_coin" / Flag,
        "quote_mint" / Bytes(32),
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data.

        Args:
            data: Raw account data including the 8-byte discriminator

        Raises:
            ValueError: If the discriminator is wrong
        """
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        total_length = len(data)
        if total_length == 81:  # V2: Creator only
            parsed = self._STRUCT_V2.parse(data[8:])
        elif total_length == 82:  # V3: Creator + mayhem
            parsed = self._STRUCT_V3.parse(data[8:])
        elif total_length < _V5_MIN_LENGTH:  # V4: Creator + mayhem + cashback
            parsed = self._STRUCT_V4.parse(data[8:])
        else:  # V5: + quote_mint
            parsed = self._STRUCT_V5.parse(data[8:])

        self.virtual_token_reserves = parsed.virtual_token_reserves
        self.virtual_sol_reserves = parsed.virtual_sol_reserves
        self.real_token_reserves = parsed.real_token_reserves
        self.real_sol_reserves = parsed.real_sol_reserves
        self.token_total_supply = parsed.token_total_supply
        self.complete = parsed.complete
        self.creator = Pubkey.from_bytes(parsed.creator)
        # The trailing fields arrived one layout at a time; an older account
        # stops short of them and reads as the behaviour they replaced.
        self.is_mayhem_mode = parsed.get("is_mayhem_mode", False)
        self.is_cashback_coin = parsed.get("is_cashback_coin", False)
        quote_mint = parsed.get("quote_mint")
        self.quote_mint = (
            Pubkey.from_bytes(quote_mint) if quote_mint else DEFAULT_QUOTE_MINT
        )


def get_bonding_curve_address(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    """Derives the associated bonding curve address for a given mint.

    Args:
        mint: The token mint address
        program_id: The program ID for the bonding curve

    Returns:
        Tuple of (bonding curve address, bump seed)
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], program_id)


async def get_bonding_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    """Fetches and validates the state of a bonding curve account.

    Args:
        conn: AsyncClient connection to Solana RPC
        curve_address: Address of the bonding curve account

    Returns:
        BondingCurveState object containing parsed account data

    Raises:
        ValueError: If account data is invalid or missing
    """
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return BondingCurveState(data)


async def check_token_status(mint_address: str) -> None:
    """Checks and prints the status of a token and its bonding curve.

    Args:
        mint_address: The token mint address as a string
    """
    try:
        mint = Pubkey.from_string(mint_address)
        bonding_curve_address, bump = get_bonding_curve_address(mint, PUMP_PROGRAM_ID)

        print("\nToken status:")
        print("-" * 50)
        print(f"Token mint:              {mint}")
        print(f"Bonding curve:           {bonding_curve_address}")
        if bump is not None:
            print(f"Bump seed:               {bump}")
        print("-" * 50)

        # Check completion status
        async with AsyncClient(RPC_ENDPOINT) as client:
            try:
                curve_state = await get_bonding_curve_state(
                    client, bonding_curve_address
                )

                quote_mint = effective_quote_mint(curve_state.quote_mint)
                quote_symbol = QUOTE_SYMBOLS.get(quote_mint, str(quote_mint))
                quote_unit = 10 ** await read_quote_decimals(client, quote_mint)

                print("\nBonding curve status:")
                print("-" * 50)
                print(f"Creator:             {curve_state.creator}")
                print(f"Quote asset:         {quote_symbol} ({quote_mint})")
                print(
                    f"Mayhem Mode:         {'✅ Enabled' if curve_state.is_mayhem_mode else '❌ Disabled'}"
                )
                print(
                    f"Cashback Coin:       {'✅ Enabled' if curve_state.is_cashback_coin else '❌ Disabled'}"
                )
                print(
                    f"Completed:           {'✅ Migrated' if curve_state.complete else '❌ Bonding curve'}"
                )

                print("\nBonding curve reserves:")
                print(f"Virtual Token:       {curve_state.virtual_token_reserves:,}")
                print(
                    f"Virtual quote:       {curve_state.virtual_sol_reserves:,} raw "
                    f"({curve_state.virtual_sol_reserves / quote_unit:,.6f} {quote_symbol})"
                )
                print(f"Real Token:          {curve_state.real_token_reserves:,}")
                print(
                    f"Real quote:          {curve_state.real_sol_reserves:,} raw "
                    f"({curve_state.real_sol_reserves / quote_unit:,.6f} {quote_symbol})"
                )
                print(f"Total Supply:        {curve_state.token_total_supply:,}")

                if curve_state.complete:
                    print(
                        "\nNote: This bonding curve has completed and liquidity has been migrated to PumpSwap."
                    )
                print("-" * 50)

            except ValueError as e:
                print(f"\nError accessing bonding curve: {e}")

    except ValueError as e:
        print(f"\nError: Invalid address format - {e}")
    except Exception as e:
        print(f"\nUnexpected error: {e}")


def main() -> None:
    """Main entry point for the token status checker."""
    parser = argparse.ArgumentParser(description="Check token bonding curve status")
    parser.add_argument("mint_address", help="The token mint address")
    args = parser.parse_args()

    asyncio.run(check_token_status(args.mint_address))


if __name__ == "__main__":
    main()

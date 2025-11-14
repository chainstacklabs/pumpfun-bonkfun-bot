"""
Module for checking the status of a token's bonding curve on the Solana network using
the Pump.fun program. It allows querying the bonding curve state and completion status.
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

# Change to token you want to query
TOKEN_MINT = "..."

# Constants
PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)


class BondingCurveState:
    """
    Represents the state of a bonding curve account.

    Attributes:
        virtual_token_reserves: Virtual token reserves in the curve
        virtual_sol_reserves: Virtual SOL reserves in the curve
        real_token_reserves: Real token reserves in the curve
        real_sol_reserves: Real SOL reserves in the curve
        token_total_supply: Total token supply in the curve
        complete: Whether the curve has completed and liquidity migrated
        is_mayhem_mode: Whether the curve is in mayhem mode
    """

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

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data."""
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        total_length = len(data)

        if total_length == 81:  # V2: Creator only
            parsed = self._STRUCT_V2.parse(data[8:])
            self.__dict__.update(parsed)
            # Convert raw bytes to Pubkey for creator field
            self.creator = Pubkey.from_bytes(self.creator)
            self.is_mayhem_mode = False

        elif total_length >= 82:  # V3: Creator + mayhem mode
            parsed = self._STRUCT_V3.parse(data[8:])
            self.__dict__.update(parsed)
            # Convert raw bytes to Pubkey for creator field
            self.creator = Pubkey.from_bytes(self.creator)

        else:
            raise ValueError(f"Unexpected bonding curve size: {total_length} bytes")


def get_bonding_curve_address(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    """
    Derives the associated bonding curve address for a given mint.

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
    """
    Fetches and validates the state of a bonding curve account.

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
    """
    Checks and prints the status of a token and its bonding curve.

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

                print("\nBonding curve status:")
                print("-" * 50)
                print(f"Creator:             {curve_state.creator}")
                print(
                    f"Mayhem Mode:         {'✅ Enabled' if curve_state.is_mayhem_mode else '❌ Disabled'}"
                )
                print(
                    f"Completed:           {'✅ Migrated' if curve_state.complete else '❌ Bonding curve'}"
                )

                print("\nBonding curve reserves:")
                print(f"Virtual Token:       {curve_state.virtual_token_reserves:,}")
                print(
                    f"Virtual SOL:         {curve_state.virtual_sol_reserves:,} lamports"
                )
                print(f"Real Token:          {curve_state.real_token_reserves:,}")
                print(
                    f"Real SOL:            {curve_state.real_sol_reserves:,} lamports"
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
    parser.add_argument(
        "mint_address", nargs="?", help="The token mint address", default=TOKEN_MINT
    )
    args = parser.parse_args()

    asyncio.run(check_token_status(args.mint_address))


if __name__ == "__main__":
    main()

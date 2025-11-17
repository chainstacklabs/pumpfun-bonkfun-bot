import asyncio
import os
import struct
from typing import Final

from construct import Bytes, Flag, Int64ul, Struct
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6
CURVE_ADDRESS: Final[str] = "..."  # Replace with actual bonding curve address

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


class BondingCurveState:
    """Parse bonding curve account data - supports all versions."""

    _STRUCT_V1 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    _STRUCT_V2 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
    )

    _STRUCT_V3 = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
        "is_mayhem_mode" / Flag,
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data - auto-detects version."""
        data_length = len(data) - 8

        if data_length < 73:  # V1: without creator and mayhem mode
            parsed = self._STRUCT_V1.parse(data[8:])
            self.__dict__.update(parsed)
            self.creator = None
            self.is_mayhem_mode = False
        elif data_length == 73:  # V2: with creator, without mayhem mode
            parsed = self._STRUCT_V2.parse(data[8:])
            self.__dict__.update(parsed)
            if isinstance(self.creator, bytes):
                self.creator = Pubkey.from_bytes(self.creator)
            self.is_mayhem_mode = False
        else:  # V3: with creator and mayhem mode
            parsed = self._STRUCT_V3.parse(data[8:])
            self.__dict__.update(parsed)
            if isinstance(self.creator, bytes):
                self.creator = Pubkey.from_bytes(self.creator)


async def get_bonding_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return BondingCurveState(data)


def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def main() -> None:
    try:
        async with AsyncClient(RPC_ENDPOINT) as conn:
            curve_address = Pubkey.from_string(CURVE_ADDRESS)
            bonding_curve_state = await get_bonding_curve_state(conn, curve_address)
            token_price_sol = calculate_bonding_curve_price(bonding_curve_state)

            print("Token price:")
            print(f"  {token_price_sol:.10f} SOL")
    except ValueError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    asyncio.run(main())

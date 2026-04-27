"""
Module for querying and analyzing soon-to-gradute tokens in the Pump.fun program.
It includes functionality to fetch bonding curves based on token reserves and
find associated SPL token accounts.

Note: getProgramAccounts may be slow as it is a pretty heavy method for RPC.
"""

import asyncio
import os
import struct
from typing import Final

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts, TokenAccountOpts
from solders.pubkey import Pubkey

load_dotenv()

# Constants
RPC_ENDPOINT: Final[str] = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
TOKEN_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)

# The 8-byte discriminator for bonding curve accounts in Pump.fun
BONDING_CURVE_DISCRIMINATOR_BYTES: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")


async def get_bonding_curves_by_reserves(client: AsyncClient | None = None) -> list:
    """
    Fetch bonding curve accounts with real token reserves below a threshold.

    Args:
        client: Optional AsyncClient instance. If None, a new one will be created.

    Returns:
        List of bonding curve accounts matching the criteria
    """
    # Define the reserve threshold (100 trillion in token base units)
    threshold: int = 100_000_000_000_000
    threshold_bytes: bytes = threshold.to_bytes(8, "little")
    msb_prefix: bytes = threshold_bytes[6:]  # Most significant bytes for pre-filtering

    should_close_client: bool = client is None
    try:
        if should_close_client:
            client = AsyncClient(RPC_ENDPOINT, commitment="processed", timeout=180)
            await client.is_connected()

        # Define on-chain filters for getProgramAccounts
        filters = [
            MemcmpOpts(
                offset=0, bytes=BONDING_CURVE_DISCRIMINATOR_BYTES
            ),  # Match bonding curve accounts
            MemcmpOpts(
                offset=30, bytes=msb_prefix
            ),  # Pre-filter by real token reserves MSB
            MemcmpOpts(offset=48, bytes=b"\x00"),  # Ensure complete flag is False
        ]

        # Query accounts matching filters
        response = await client.get_program_accounts(
            PUMP_PROGRAM_ID, encoding="base64", filters=filters
        )

        result = []
        for acc in response.value:
            raw = acc.account.data

            # Extract real_token_reserves (u64 = 8 bytes, little-endian)
            offset: int = 24  # real_token_reserves field offset
            real_token_reserves: int = struct.unpack("<Q", raw[offset : offset + 8])[0]

            # Post-filter: ensure value is below the threshold
            if real_token_reserves < threshold:
                print(f"Pubkey: {acc.pubkey}")
                print(f"Real token reserves: {real_token_reserves / 10**6} tokens")
                print("=" * 50)
                result.append(acc)

        return result
    finally:
        if should_close_client and client:
            await client.close()


async def find_associated_bonding_curve(
    bonding_curve_address: str, client: AsyncClient | None = None
) -> dict | None:
    """
    Find the SPL token account owned by a bonding curve.

    Args:
        bonding_curve_address: The bonding curve public key (as a string)
        client: Optional AsyncClient instance. If None, a new one will be created.

    Returns:
        The associated SPL token account data or None if not found
    """
    should_close_client: bool = client is None
    try:
        if should_close_client:
            client = AsyncClient(RPC_ENDPOINT)
            await client.is_connected()

        response = await client.get_token_accounts_by_owner(
            Pubkey.from_string(bonding_curve_address),
            TokenAccountOpts(program_id=TOKEN_PROGRAM_ID),
        )

        if response.value and len(response.value) > 0:
            return response.value[0].account
        else:
            print(f"No token accounts found for {bonding_curve_address}")
            return None
    except Exception as e:
        print(f"Error finding associated token account: {e}")
        return None
    finally:
        if should_close_client and client:
            await client.close()


def get_mint_address(data: bytes) -> str:
    """
    Extract the mint address from SPL token account data.

    Args:
        data: The token account data as bytes

    Returns:
        The mint address as a base58-encoded string
    """
    return str(Pubkey(data[:32]))


async def main() -> None:
    """Main entry point for querying and processing bonding curves."""
    async with AsyncClient(RPC_ENDPOINT, commitment="processed", timeout=240) as client:
        await client.is_connected()

        bonding_curves = await get_bonding_curves_by_reserves(client)
        print(f"Total matches: {len(bonding_curves)}")
        print("=" * 50)

        for bonding_curve in bonding_curves:
            # Find the SPL token account owned by the bonding curve
            associated_token_account = await find_associated_bonding_curve(
                str(bonding_curve.pubkey), client
            )

            if associated_token_account:
                mint_address = get_mint_address(associated_token_account.data)
                print(f"Bonding curve: {bonding_curve.pubkey}")
                print(f"Mint address: {mint_address}")
                print("=" * 50)

            # For demonstration, only process the first curve
            break


if __name__ == "__main__":
    asyncio.run(main())

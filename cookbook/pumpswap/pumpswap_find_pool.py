"""This module provides functionality to:
1. Find market addresses by base mint
2. Fetch and parse market data (including pool addresses) from Pump AMM program accounts

Usage:
    uv run cookbook/pumpswap/pumpswap_find_pool.py <MINT>
"""

import argparse
import asyncio
import os
import struct

import base58
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import MemcmpOpts
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")



async def get_market_address_by_base_mint(
    base_mint_address: Pubkey, amm_program_id: Pubkey
):
    async with AsyncClient(RPC_ENDPOINT, timeout=120) as client:
        # Define the offset for base_mint field
        offset = 43

        # Create the filter to match the base_mint. MemcmpOpts takes the bytes
        # base58-encoded, which is what a Pubkey's str already is.
        filters = [MemcmpOpts(offset=offset, bytes=str(base_mint_address))]

        # Retrieve the accounts that match the filter
        response = await client.get_program_accounts(
            amm_program_id,  # AMM program ID
            encoding="base64",
            filters=filters,
        )

        pool_addresses = [account.pubkey for account in response.value]
        if not pool_addresses:
            # No pool means the coin has not graduated off its bonding curve
            # yet, which is the common case rather than an error.
            return None
        return pool_addresses[0]


async def get_market_data(market_address: Pubkey):
    async with AsyncClient(RPC_ENDPOINT, timeout=120) as client:
        response = await client.get_account_info(market_address, encoding="base64")
        data = response.value.data
        parsed_data = {}

        offset = 8
        # Fields end at 261; live pool accounts are 301 bytes with trailing padding.
        # virtual_quote_reserves is an i128, not a u64 — reading only 8 bytes happens
        # to work while the high half is zero, and silently breaks when it isn't.
        fields = [
            ("pool_bump", "u8"),
            ("index", "u16"),
            ("creator", "pubkey"),
            ("base_mint", "pubkey"),
            ("quote_mint", "pubkey"),
            ("lp_mint", "pubkey"),
            ("pool_base_token_account", "pubkey"),
            ("pool_quote_token_account", "pubkey"),
            ("lp_supply", "u64"),
            ("coin_creator", "pubkey"),
            ("is_mayhem_mode", "bool"),
            ("is_cashback_coin", "bool"),
            ("virtual_quote_reserves", "i128"),
        ]

        for field_name, field_type in fields:
            if field_type == "pubkey":
                value = data[offset : offset + 32]
                parsed_data[field_name] = base58.b58encode(value).decode("utf-8")
                offset += 32
            elif field_type in {"u64", "i64"}:
                value = (
                    struct.unpack("<Q", data[offset : offset + 8])[0]
                    if field_type == "u64"
                    else struct.unpack("<q", data[offset : offset + 8])[0]
                )
                parsed_data[field_name] = value
                offset += 8
            elif field_type == "i128":
                if len(data) < offset + 16:
                    parsed_data[field_name] = 0
                    continue
                parsed_data[field_name] = int.from_bytes(
                    data[offset : offset + 16], "little", signed=True
                )
                offset += 16
            elif field_type == "u16":
                value = struct.unpack("<H", data[offset : offset + 2])[0]
                parsed_data[field_name] = value
                offset += 2
            elif field_type == "u8":
                value = data[offset]
                parsed_data[field_name] = value
                offset += 1
            elif field_type == "bool":
                parsed_data[field_name] = (
                    bool(data[offset]) if len(data) > offset else False
                )
                offset += 1

        return parsed_data


async def show_pool(token_mint: Pubkey) -> None:
    """Find and print one coin's PumpSwap pool.

    Args:
        token_mint: The coin whose pool to look up
    """
    market_address = await get_market_address_by_base_mint(
        token_mint, PUMP_AMM_PROGRAM_ID
    )
    if market_address is None:
        print(
            f"No PumpSwap pool for {token_mint}.\n"
            "It has not graduated off its bonding curve yet — trade it with\n"
            "cookbook/pumpfun/trade/pumpfun_buy_token_v2.py instead."
        )
        return
    print(market_address)

    market_data = await get_market_data(market_address)
    for key, value in market_data.items():
        print(f"  {key}: {value}")

    # Quote against effective reserves. Upstream's release note says
    # virtual_quote_reserves is 0 on all pools; that is out of date — pools carry
    # 17.5845 SOL of virtual reserves, so quoting off the raw vault balance
    # under-prices by anywhere from a few percent to over 20%.
    virtual = market_data.get("virtual_quote_reserves", 0)
    if virtual:
        print(
            f"\nNote: this pool carries {virtual / 1e9:.9f} SOL of virtual quote "
            "reserves. Add them to pool_quote_token_account.amount before quoting."
        )


def main() -> None:
    """Parse the command line and print the pool."""
    parser = argparse.ArgumentParser(description="Find a coin's PumpSwap pool")
    parser.add_argument("mint", help="The coin's mint address")
    args = parser.parse_args()

    asyncio.run(show_pool(Pubkey.from_string(args.mint)))


if __name__ == "__main__":
    main()

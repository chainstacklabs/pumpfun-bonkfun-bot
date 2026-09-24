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

# coin_creator sits after discriminator(8) + pool_bump(1) + index(2) + six
# pubkeys(192) + lp_supply(8). It is set only on a pool the coin graduated into
# and left at the default on every other pool, so it is what tells the canonical
# pool from a copy anyone can open against the same mint.
POOL_COIN_CREATOR_OFFSET = 211
DEFAULT_COIN_CREATOR = Pubkey.default()


def read_pool_coin_creator(data: bytes) -> Pubkey:
    """Read a pool account's coin_creator.

    Args:
        data: Raw pool account data, discriminator included

    Returns:
        The pubkey, or the default one if the account is too short to hold it
    """
    end = POOL_COIN_CREATOR_OFFSET + 32
    if len(data) < end:
        return DEFAULT_COIN_CREATOR
    return Pubkey.from_bytes(data[POOL_COIN_CREATOR_OFFSET:end])


async def get_pools_by_base_mint(
    base_mint_address: Pubkey, amm_program_id: Pubkey
) -> tuple[list[Pubkey], list[Pubkey]]:
    """Find every pool holding this base mint, split by whether it is canonical.

    Anyone can open a pool against any mint, and getProgramAccounts returns them
    in no defined order, so the first match is an arbitrary pool rather than the
    coin's own.

    Args:
        base_mint_address: The coin's mint
        amm_program_id: PUMP AMM program address

    Returns:
        Canonical pools and the rest, each in the order the RPC returned them
    """
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

    canonical, others = [], []
    for account in response.value:
        target = (
            canonical
            if read_pool_coin_creator(bytes(account.account.data))
            != DEFAULT_COIN_CREATOR
            else others
        )
        target.append(account.pubkey)
    return canonical, others


# Same offset in SPL Token and Token-2022: extensions are appended after it.
_MINT_DECIMALS_OFFSET = 44


async def read_mint_decimals(mint: Pubkey) -> int:
    """Read a mint's decimals from chain.

    pump-amm pools do not all quote in SOL, so only the pool's own quote mint
    says what power of ten its reserves are in.

    Raises:
        ValueError: If the mint is missing or too short to be a mint
    """
    async with AsyncClient(RPC_ENDPOINT, timeout=120) as client:
        response = await client.get_account_info(mint, encoding="base64")
    if response.value is None:
        raise ValueError(f"Mint {mint} does not exist on chain")
    data = bytes(response.value.data)
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(f"Account {mint} is too short to be a mint")
    return data[_MINT_DECIMALS_OFFSET]


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
    canonical, others = await get_pools_by_base_mint(token_mint, PUMP_AMM_PROGRAM_ID)
    if not canonical and not others:
        print(
            f"No PumpSwap pool for {token_mint}.\n"
            "It has not graduated off its bonding curve yet — trade it with\n"
            "cookbook/pumpfun/trade/pumpfun_buy_token_v2.py instead."
        )
        return
    if not canonical:
        print(
            f"No canonical pool for {token_mint}. The {len(others)} pool(s) below "
            "carry its base mint but none was created by graduation:"
        )
        for pool in others:
            print(f"  {pool}")
        return

    market_address = canonical[0]
    print(market_address)

    market_data = await get_market_data(market_address)
    for key, value in market_data.items():
        print(f"  {key}: {value}")

    if len(canonical) > 1:
        print(f"\nWARNING: {len(canonical)} canonical pools carry this base mint:")
        for pool in canonical[1:]:
            print(f"  {pool}")
    if others:
        print(
            f"\n{len(others)} further pool(s) carry this base mint and are not "
            "canonical. Trading one means passing --pool to the buy/sell scripts:"
        )
        for pool in others:
            print(f"  {pool}")

    # Quote against effective reserves. Upstream's release note says
    # virtual_quote_reserves is 0 on all pools; that is out of date — pools carry
    # 17.5845 SOL of virtual reserves, so quoting off the raw vault balance
    # under-prices by anywhere from a few percent to over 20%.
    virtual = market_data.get("virtual_quote_reserves", 0)
    if virtual:
        quote_mint = Pubkey.from_string(market_data["quote_mint"])
        quote_decimals = await read_mint_decimals(quote_mint)
        print(
            f"\nNote: this pool carries {virtual / 10**quote_decimals:.9f} "
            f"of virtual quote reserves ({quote_mint}, {quote_decimals} decimals). "
            "Add them to pool_quote_token_account.amount before quoting."
        )


def main() -> None:
    """Parse the command line and print the pool."""
    parser = argparse.ArgumentParser(description="Find a coin's PumpSwap pool")
    parser.add_argument("mint", help="The coin's mint address")
    args = parser.parse_args()

    asyncio.run(show_pool(Pubkey.from_string(args.mint)))


if __name__ == "__main__":
    main()

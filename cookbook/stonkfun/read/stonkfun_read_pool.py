"""Read a StonkFun coin's pool: price, quote asset, fees, transfer tax and progress.

Usage:
    uv run cookbook/stonkfun/read/stonkfun_read_pool.py <MINT>

StonkFun runs no program of its own. Every coin is a Raydium LaunchLab pool
created under one of StonkFun's two platform configs: *standard*, or *reward*,
where the coin is a Token-2022 mint with a 1% or 3% transfer fee that is paid
out to holders. The quote asset is whatever the launcher picked — SOL, a
tokenized stock, a stablecoin, another coin — so everything here is in the
pool's own quote units.

The price is `(virtual_quote + real_quote) / (virtual_base - real_base)`. The
virtual reserves are fixed at launch; the real ones carry every trade since.
Reading the virtual reserves alone gives the launch price forever.
"""

import argparse
import asyncio
import os
import struct

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import DataSliceOpts, MemcmpOpts
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

LAUNCHLAB_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
STONKFUN_PLATFORM_CONFIGS = {
    Pubkey.from_string("4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"): "standard",
    Pubkey.from_string("6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"): "reward",
}

POOL_STATE_SIZE = 429
# PoolState: 8 discriminator, epoch u64, five u8 flags, ten u64 amounts, a
# five-u64 vesting schedule, then the pubkeys.
POOL_PUBKEYS_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8
POOL_BASE_MINT_OFFSET = POOL_PUBKEYS_OFFSET + 2 * 32

POOL_STATUS = {0: "trading on the curve", 1: "waiting for migration", 2: "migrated"}

# Fee rates are parts per million of the quote side.
FEE_RATE_DENOMINATOR = 1_000_000
# GlobalConfig: discriminator, epoch, curve_type u8, index u16, migrate_fee u64.
GLOBAL_TRADE_FEE_RATE_OFFSET = 8 + 8 + 1 + 2 + 8
# PlatformConfig: discriminator, epoch, two wallets, three u64 scales.
PLATFORM_FEE_RATE_OFFSET = 8 + 8 + 32 + 32 + 3 * 8
# then name[64], web[256], img[256], cpswap_config.
PLATFORM_CREATOR_FEE_RATE_OFFSET = PLATFORM_FEE_RATE_OFFSET + 8 + 64 + 256 + 256 + 32

# Token-2022 mint extensions start after the 82-byte base mint, padding to
# 165 bytes and a one-byte account type.
TOKEN_2022_EXTENSIONS_OFFSET = 166
TRANSFER_FEE_CONFIG_EXTENSION = 1


def decode_pool(data: bytes) -> dict:
    """Decode the PoolState fields a trader needs."""
    u64s = struct.unpack_from("<10Q", data, 8 + 8 + 5)
    pubkeys = [
        Pubkey.from_bytes(
            data[POOL_PUBKEYS_OFFSET + 32 * i : POOL_PUBKEYS_OFFSET + 32 * (i + 1)]
        )
        for i in range(7)
    ]
    return {
        "status": data[17],
        "base_decimals": data[18],
        "quote_decimals": data[19],
        "supply": u64s[0],
        "total_base_sell": u64s[1],
        "virtual_base": u64s[2],
        "virtual_quote": u64s[3],
        "real_base": u64s[4],
        "real_quote": u64s[5],
        "total_quote_fund_raising": u64s[6],
        "global_config": pubkeys[0],
        "platform_config": pubkeys[1],
        "base_mint": pubkeys[2],
        "quote_mint": pubkeys[3],
        "base_vault": pubkeys[4],
        "quote_vault": pubkeys[5],
        "creator": pubkeys[6],
    }


def transfer_fee(mint_data: bytes, epoch: int) -> tuple[int, int]:
    """The transfer fee in force for `epoch`, as (basis points, max fee raw).

    (0, 0) when the mint has no TransferFeeConfig extension. A rate change is
    scheduled two epochs ahead, so the newer entry only applies from its epoch.
    """
    offset = TOKEN_2022_EXTENSIONS_OFFSET
    while offset + 4 <= len(mint_data):
        kind, length = struct.unpack_from("<HH", mint_data, offset)
        body = mint_data[offset + 4 : offset + 4 + length]
        if kind == TRANSFER_FEE_CONFIG_EXTENSION:
            # Two authorities and the withheld amount, then older and newer
            # fees, each (epoch u64, maximum_fee u64, basis_points u16).
            older = struct.unpack_from("<QQH", body, 72)
            newer = struct.unpack_from("<QQH", body, 90)
            active = newer if epoch >= newer[0] else older
            return active[2], active[1]
        if kind == 0 and length == 0:
            break
        offset += 4 + length
    return 0, 0


async def find_pool(client: AsyncClient, mint: Pubkey) -> Pubkey:
    """Find a coin's pool without knowing its quote asset.

    The pool address is derived from both mints, so a coin quoted in an asset
    you did not guess derives to an empty account. Filtering the program's
    accounts on the base mint finds it whatever the quote.

    Raises:
        ValueError: If no LaunchLab pool has this base mint
    """
    response = await client.get_program_accounts(
        LAUNCHLAB_PROGRAM,
        encoding="base64",
        data_slice=DataSliceOpts(offset=0, length=0),
        filters=[
            POOL_STATE_SIZE,
            MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=str(mint)),
        ],
    )
    if not response.value:
        raise ValueError(f"No LaunchLab pool found for {mint}")
    return response.value[0].pubkey


async def read_pool(mint: Pubkey) -> None:
    """Print one StonkFun coin's pool state."""
    async with AsyncClient(RPC_ENDPOINT, commitment=Confirmed) as client:
        pool_address = await find_pool(client, mint)
        pool = decode_pool((await client.get_account_info(pool_address)).value.data)

        mode = STONKFUN_PLATFORM_CONFIGS.get(pool["platform_config"])
        if mode is None:
            print(f"{mint} is a LaunchLab coin, but not a StonkFun one")
            print(f"(platform config {pool['platform_config']}).")
            return

        accounts = (
            await client.get_multiple_accounts(
                [pool["global_config"], pool["platform_config"], mint]
            )
        ).value
        global_data, platform_data, mint_data = (a.data for a in accounts)
        trade_fee_rate = struct.unpack_from(
            "<Q", global_data, GLOBAL_TRADE_FEE_RATE_OFFSET
        )[0]
        platform_fee_rate = struct.unpack_from(
            "<Q", platform_data, PLATFORM_FEE_RATE_OFFSET
        )[0]
        creator_fee_rate = struct.unpack_from(
            "<Q", platform_data, PLATFORM_CREATOR_FEE_RATE_OFFSET
        )[0]
        epoch = (await client.get_epoch_info()).value.epoch
        tax_bps, _ = transfer_fee(mint_data, epoch)

        base_unit = 10 ** pool["base_decimals"]
        quote_unit = 10 ** pool["quote_decimals"]
        quote_reserve = pool["virtual_quote"] + pool["real_quote"]
        base_reserve = pool["virtual_base"] - pool["real_base"]
        price = (quote_reserve / quote_unit) / (base_reserve / base_unit)
        launch_price = (pool["virtual_quote"] / quote_unit) / (
            pool["virtual_base"] / base_unit
        )
        total_fee_rate = trade_fee_rate + platform_fee_rate + creator_fee_rate

        print(f"Mint:            {mint}")
        print(f"Pool:            {pool_address}")
        print(f"Mode:            {mode}")
        print(f"Status:          {POOL_STATUS.get(pool['status'], pool['status'])}")
        print(f"Creator:         {pool['creator']}")
        print(
            f"Quote asset:     {pool['quote_mint']} ({pool['quote_decimals']} decimals)"
        )
        print(f"Price:           {price:.12f} quote per token")
        print(f"Launch price:    {launch_price:.12f} quote per token")
        print(
            f"Curve fee:       {total_fee_rate / FEE_RATE_DENOMINATOR:.2%} of the quote side "
            f"(Raydium {trade_fee_rate / FEE_RATE_DENOMINATOR:.2%}, "
            f"platform {platform_fee_rate / FEE_RATE_DENOMINATOR:.2%}, "
            f"creator {creator_fee_rate / FEE_RATE_DENOMINATOR:.2%})"
        )
        print(f"Transfer tax:    {tax_bps / 100:.2f}% on every transfer of the coin")
        print(
            f"Raised:          {pool['real_quote'] / quote_unit:.6f} of "
            f"{pool['total_quote_fund_raising'] / quote_unit:.6f} quote "
            f"({pool['real_quote'] / pool['total_quote_fund_raising']:.2%} to graduation)"
        )
        print(
            f"Sold:            {pool['real_base'] / base_unit:,.0f} of "
            f"{pool['total_base_sell'] / base_unit:,.0f} tokens on the curve"
        )


def main() -> None:
    """Parse the command line and read the pool."""
    parser = argparse.ArgumentParser(description="Read a StonkFun coin's pool state")
    parser.add_argument("mint", help="The coin's mint address")
    args = parser.parse_args()
    asyncio.run(read_pool(Pubkey.from_string(args.mint)))


if __name__ == "__main__":
    main()

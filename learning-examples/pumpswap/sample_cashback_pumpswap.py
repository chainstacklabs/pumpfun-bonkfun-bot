"""Post-cutover sampler: find a successful cashback PumpSwap buy/sell on mainnet.

Goal: identify the seed/position of the +1 account that the program requires for
cashback pools (27-account buy / 26-account sell vs 26 / 24 non-cashback).

Strategy:
1. Pull recent signatures for pAMM program.
2. For each tx, fetch full tx, find the pAMM buy/sell ix.
3. Resolve the pool account from the ix; fetch its data; check byte 244
   (is_cashback_coin) — only proceed if it's 1.
4. Print the full account list with counts so we can diff against the known
   26/24 non-cashback layout in manual_buy/sell_pumpswap.py.

Usage:
    uv run learning-examples/pumpswap/sample_cashback_pumpswap.py [LIMIT]

Env: SOLANA_NODE_RPC_ENDPOINT (defaults to public mainnet)
"""

import asyncio
import os
import sys

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")

# Pool layout: byte 244 = is_cashback_coin (per CLAUDE.md / PR #167 notes).
POOL_IS_CASHBACK_OFFSET = 244

RPC = os.environ.get(
    "SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
)


async def is_cashback_pool(client: AsyncClient, pool: Pubkey) -> bool | None:
    resp = await client.get_account_info(pool, encoding="base64")
    if resp.value is None:
        return None
    data = resp.value.data
    if len(data) <= POOL_IS_CASHBACK_OFFSET:
        return False
    return data[POOL_IS_CASHBACK_OFFSET] == 1


def classify_ix(ix_data: bytes) -> str | None:
    if ix_data.startswith(BUY_DISCRIMINATOR):
        return "buy"
    if ix_data.startswith(SELL_DISCRIMINATOR):
        return "sell"
    return None


async def inspect_tx(client: AsyncClient, sig: Signature) -> dict | None:
    """Return diagnostic dict if this tx contains a cashback buy/sell."""
    resp = await client.get_transaction(
        sig, encoding="base64", max_supported_transaction_version=0
    )
    if resp.value is None or resp.value.transaction.meta is None:
        return None
    if resp.value.transaction.meta.err is not None:
        return None  # only successful txs

    tx = resp.value.transaction.transaction
    msg = tx.message
    account_keys = list(msg.account_keys)
    # include loaded addresses from ALTs
    loaded = resp.value.transaction.meta.loaded_addresses
    if loaded is not None:
        account_keys.extend(loaded.writable)
        account_keys.extend(loaded.readonly)

    for ix in msg.instructions:
        program_id = account_keys[ix.program_id_index]
        if program_id != PUMP_AMM_PROGRAM_ID:
            continue
        kind = classify_ix(bytes(ix.data))
        if kind is None:
            continue

        # PumpSwap convention: account index 0 of the ix is the pool.
        if not ix.accounts:
            continue
        pool = account_keys[ix.accounts[0]]
        cashback = await is_cashback_pool(client, pool)
        if not cashback:
            continue

        return {
            "sig": str(sig),
            "kind": kind,
            "pool": str(pool),
            "n_accounts": len(ix.accounts),
            "accounts": [str(account_keys[i]) for i in ix.accounts],
        }
    return None


async def main(limit: int = 200) -> None:
    async with AsyncClient(RPC) as client:
        print(f"Sampling up to {limit} recent pAMM signatures from {RPC}")
        sigs_resp = await client.get_signatures_for_address(
            PUMP_AMM_PROGRAM_ID, limit=limit
        )
        sigs = [s.signature for s in sigs_resp.value if s.err is None]
        print(f"  got {len(sigs)} successful signatures")

        for sig in sigs:
            try:
                hit = await inspect_tx(client, sig)
            except (ValueError, RuntimeError) as e:
                print(f"  [skip] {sig}: {e}")
                continue
            if hit is None:
                continue
            print()
            print(f"=== CASHBACK {hit['kind'].upper()} ===")
            print(f"  sig:   {hit['sig']}")
            print(f"  pool:  {hit['pool']}")
            print(f"  count: {hit['n_accounts']} accounts")
            for i, a in enumerate(hit["accounts"]):
                print(f"    [{i:2d}] {a}")
            return

        print("No cashback PumpSwap buy/sell found in window.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    asyncio.run(main(n))

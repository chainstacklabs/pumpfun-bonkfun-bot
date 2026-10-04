"""Listen for new StonkFun coins over blockSubscribe.

Usage:
    uv run cookbook/stonkfun/listen/stonkfun_listen_tokens_blocksubscribe.py

One subscription per StonkFun platform config — `mentionsAccountOrProgram`
takes a single address — so only blocks touching StonkFun pools arrive, not
every LaunchLab launchpad. Each transaction is decoded from its own accounts
and arguments, including inner instructions and accounts behind an address
lookup table. Slower than Geyser: a block arrives whole, at `confirmed`.

WebSocket API Reference:
https://solana.com/docs/rpc/websocket/blocksubscribe
"""

import asyncio
import base64
import json
import os
import struct

import base58
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# Block frames run far past websockets' 1 MiB default, which closes the
# connection with 1009 instead of delivering them.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024

LAUNCHLAB_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
STONKFUN_PLATFORM_CONFIGS = {
    Pubkey.from_string("4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"): "standard",
    Pubkey.from_string("6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"): "reward",
}

# Anchor discriminator: the first 8 bytes of the instruction name's hash.
INITIALIZE_WITH_TOKEN_2022 = bytes([37, 190, 126, 222, 44, 154, 171, 17])

# Slots remembered to drop a block both subscriptions delivered.
SEEN_SLOTS_LIMIT = 1000

# Account positions in initialize_with_token_2022, from the IDL.
PAYER, CREATOR, PLATFORM_CONFIG = 0, 1, 3
POOL_STATE, BASE_MINT, QUOTE_MINT = 5, 6, 7


def decode_launch(data: bytes, accounts: list[int], keys: list[Pubkey]) -> dict | None:
    """Decode an initialize_with_token_2022 instruction, or None if it is not one."""
    if not data.startswith(INITIALIZE_WITH_TOKEN_2022) or len(accounts) <= QUOTE_MINT:
        return None
    if max(accounts) >= len(keys):
        return None
    mode = STONKFUN_PLATFORM_CONFIGS.get(keys[accounts[PLATFORM_CONFIG]])
    if mode is None:
        return None

    offset = 8 + 1  # discriminator, decimals
    strings = []
    for _ in range(3):  # name, symbol, uri
        (length,) = struct.unpack_from("<I", data, offset)
        strings.append(data[offset + 4 : offset + 4 + length].decode(errors="replace"))
        offset += 4 + length
    curve_type = data[offset]
    offset += 1 + (25 if curve_type == 0 else 17)  # the curve's fields
    offset += 24 + 1  # vesting, migrated-pool creator fee side
    transfer_fee_bps = (
        struct.unpack_from("<H", data, offset + 1)[0] if data[offset] == 1 else 0
    )

    return {
        "mode": mode,
        "name": strings[0],
        "symbol": strings[1],
        "mint": keys[accounts[BASE_MINT]],
        "pool": keys[accounts[POOL_STATE]],
        "quote_mint": keys[accounts[QUOTE_MINT]],
        "creator": keys[accounts[CREATOR]],
        "transfer_fee_bps": transfer_fee_bps,
    }


def instructions_and_keys(
    tx: dict,
) -> tuple[list[tuple[int, list[int], bytes]], list[Pubkey]]:
    """Every instruction in a base64 block transaction, and the keys they index.

    Lookup-table accounts follow the static keys, writable then read-only. Inner
    instructions come from the meta, with base58 data.
    """
    message = VersionedTransaction.from_bytes(
        base64.b64decode(tx["transaction"][0])
    ).message
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    keys = list(message.account_keys) + [
        Pubkey.from_string(key)
        for key in loaded.get("writable", []) + loaded.get("readonly", [])
    ]
    instructions = [
        (ix.program_id_index, list(ix.accounts), bytes(ix.data))
        for ix in message.instructions
    ]
    for group in meta.get("innerInstructions") or []:
        instructions += [
            (ix["programIdIndex"], ix["accounts"], base58.b58decode(ix["data"]))
            for ix in group["instructions"]
        ]
    return instructions, keys


def handle_transaction(tx: dict) -> None:
    """Print any StonkFun launch in one block transaction."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return
    # Cheap gate before decoding the envelope: a launch logs its instruction.
    if not any(
        "InitializeWithToken2022" in line for line in meta.get("logMessages") or []
    ):
        return
    instructions, keys = instructions_and_keys(tx)
    for program_index, accounts, data in instructions:
        if program_index >= len(keys) or keys[program_index] != LAUNCHLAB_PROGRAM:
            continue
        launch = decode_launch(data, accounts, keys)
        if launch:
            print("\n" + "=" * 80)
            print(f"NEW STONKFUN COIN ({launch['mode']})")
            print("=" * 80)
            print(f"Name:           {launch['name']} ({launch['symbol']})")
            print(f"Mint:           {launch['mint']}")
            print(f"Pool:           {launch['pool']}")
            print(f"Quote asset:    {launch['quote_mint']}")
            print(f"Creator:        {launch['creator']}")
            print(f"Transfer tax:   {launch['transfer_fee_bps'] / 100:.2f}%")
            print("=" * 80)


async def listen() -> None:
    """Subscribe to blocks touching either platform config and decode launches."""
    async with websockets.connect(
        WSS_ENDPOINT, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
    ) as websocket:
        for request_id, config in enumerate(STONKFUN_PLATFORM_CONFIGS, start=1):
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "blockSubscribe",
                        "params": [
                            {"mentionsAccountOrProgram": str(config)},
                            {
                                "commitment": "confirmed",
                                "encoding": "base64",
                                "showRewards": False,
                                "transactionDetails": "full",
                                # Whole-frame setting: a lower value nulls the
                                # entire block, not just its v1 transactions.
                                "maxSupportedTransactionVersion": 1,
                            },
                        ],
                    }
                )
            )
        print("Subscribed to blocks touching StonkFun's platform configs")

        # Both subscriptions can deliver the same block when it touches both
        # configs; remember recent slots so each prints once.
        seen_slots: set[int] = set()
        while True:
            try:
                message = json.loads(await websocket.recv())
            except websockets.ConnectionClosed:
                print("WebSocket connection closed.")
                return
            if message.get("method") != "blockNotification":
                continue
            value = message["params"]["result"]["value"]
            block = value.get("block")
            # `block` is null for a skipped or unavailable slot.
            if not block or value["slot"] in seen_slots:
                continue
            seen_slots.add(value["slot"])
            if len(seen_slots) > SEEN_SLOTS_LIMIT:
                seen_slots.clear()
            for tx in block.get("transactions") or []:
                try:
                    handle_transaction(tx)
                except Exception as e:  # noqa: BLE001
                    print(f"Could not decode a transaction: {e}")


async def main() -> None:
    """Reconnect for as long as the script runs."""
    while True:
        try:
            await listen()
        except (websockets.WebSocketException, OSError) as e:
            print(f"Connection error: {e!s}")
        print("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

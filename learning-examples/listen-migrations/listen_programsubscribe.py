"""
Monitors Solana for new Pump AMM markets via WebSocket.
Fetches existing markets to filter out already existing ones, parses market account data (e.g., mints, token accounts, creator),
and excludes user-created markets. May also detect non-migration-based markets (if they created by a program).

Note: this method consumes HUGE AMOUNT OF MESSAGES from a WebSocket.
"""

import asyncio
import base64
import json
import os
import struct

import aiohttp
import base58
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")

MARKET_ACCOUNT_LENGTH = 8 + 1 + 2 + 32 * 6 + 8 + 32 + 1  # discriminator + pool_bump + index + 6 pubkeys + lp_supply + coin_creator + is_mayhem_mode = 244 bytes
MARKET_DISCRIMINATOR = base58.b58encode(b"\xf1\x9am\x04\x11\xb1m\xbc").decode()
QUOTE_MINT_SOL = base58.b58encode(
    bytes(Pubkey.from_string("So11111111111111111111111111111111111111112"))
).decode()


async def fetch_existing_market_pubkeys():
    headers = {"Content-Type": "application/json"}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            str(PUMP_AMM_PROGRAM_ID),
            {
                "encoding": "base64",
                "commitment": "processed",
                "filters": [
                    {"dataSize": MARKET_ACCOUNT_LENGTH},
                    {"memcmp": {"offset": 0, "bytes": MARKET_DISCRIMINATOR}},
                    {"memcmp": {"offset": 75, "bytes": QUOTE_MINT_SOL}},
                ],
            },
        ],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(RPC_ENDPOINT, headers=headers, json=body) as resp:
            res = await resp.json()
            return {account["pubkey"] for account in res.get("result", [])}


def parse_market_account_data(data):
    """Parse Pool account data according to pump_swap_idl.json structure.

    Total 11 fields including the new is_mayhem_mode field added with mayhem update.
    """
    parsed_data = {}
    offset = 8  # Discriminator

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
    ]

    try:
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
            elif field_type == "u16":
                value = struct.unpack("<H", data[offset : offset + 2])[0]
                parsed_data[field_name] = value
                offset += 2
            elif field_type == "u8":
                value = data[offset]
                parsed_data[field_name] = value
                offset += 1
            elif field_type == "bool":
                value = bool(data[offset])
                parsed_data[field_name] = value
                offset += 1
    except Exception as e:
        print(f"[ERROR] Failed to parse market data: {e}")

    return parsed_data


async def listen_new_markets():
    known_pubkeys = await fetch_existing_market_pubkeys()
    print(f"[INFO] Loaded {len(known_pubkeys)} existing markets")

    while True:
        try:
            print("[INFO] Connecting to WebSocket...")
            async with websockets.connect(WSS_ENDPOINT) as ws:
                sub_msg = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "programSubscribe",
                        "params": [
                            str(PUMP_AMM_PROGRAM_ID),
                            {
                                "commitment": "processed",
                                "encoding": "base64",
                                "filters": [
                                    {"dataSize": MARKET_ACCOUNT_LENGTH},
                                    {
                                        "memcmp": {
                                            "offset": 0,
                                            "bytes": MARKET_DISCRIMINATOR,
                                        }
                                    },
                                    {"memcmp": {"offset": 75, "bytes": QUOTE_MINT_SOL}},
                                ],
                            },
                        ],
                    }
                )
                await ws.send(sub_msg)
                print(f"[INFO] Subscribed to: {PUMP_AMM_PROGRAM_ID}")

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    if "method" in data and data["method"] == "programNotification":
                        message_value = data["params"]["result"]["value"]
                        pubkey = message_value["pubkey"]
                        raw_account_data = message_value["account"].get("data", [None])[
                            0
                        ]
                        slot = data["params"]["result"]["context"]["slot"]

                        if pubkey in known_pubkeys:
                            # print("[INFO] Skipping already existed market...")
                            continue

                        if not raw_account_data:
                            print("[ERROR] Account data is empty")
                            continue

                        try:
                            account_data = base64.b64decode(raw_account_data)
                            parsed = parse_market_account_data(account_data)

                            if Pubkey.from_string(
                                parsed.get("creator", "")
                            ).is_on_curve():
                                print("[INFO] Skipping user-created market...")
                                continue  # skip user-created pool

                            print("\n[INFO] New market account detected:")
                            print(f"  pubkey: {pubkey}")
                            print(f"  slot: {slot}")
                            for k, v in parsed.items():
                                print(f"  {k}: {v}")

                            known_pubkeys.add(pubkey)
                        except Exception as e:
                            print(f"[ERROR] Failed to decode account: {e}")

        except Exception as e:
            print(f"[ERROR] Connection error: {e}")
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(listen_new_markets())

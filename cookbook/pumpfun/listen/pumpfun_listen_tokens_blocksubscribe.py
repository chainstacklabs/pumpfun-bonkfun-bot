"""Listens to Solana blocks for Pump.fun coin creations via WebSocket.
Reads each transaction's `meta.logMessages` for the CreateEvent the program
emits, which carries the mint, bonding curve and creator as literal pubkeys.

Usage:
    uv run cookbook/pumpfun/listen/pumpfun_listen_tokens_blocksubscribe.py

Performance: Usually slower than other listeners due to block-level processing.

This script uses blockSubscribe which receives entire blocks containing transactions
that mention the Pump.fun program. Detection routes on the logs rather than the
transaction envelope: a `create` reached by CPI is in the logs and absent from the
envelope's top-level instructions. To take an envelope apart, see
cookbook/pumpfun/decode/pumpfun_decode_transaction_blocksubscribe.py.

WebSocket API Reference:
https://solana.com/docs/rpc/websocket/blocksubscribe

Address Lookup Tables (ALT) Support:
https://solana.com/docs/advanced/lookup-tables
"""

import asyncio
import base64
import json
import os
import struct

import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# Solana's blockSubscribe (and a busy logsSubscribe) sends frames well past
# websockets' 1 MiB default, which kills the connection with a 1009 close
# instead of delivering the message. Same value the bot's own listeners use.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)


def print_token_info(token_data, signature=None):
    """Print token information in a consistent, user-friendly format.

    Args:
        token_data: Dictionary containing token fields
        signature: Optional transaction signature
    """
    print("\n" + "=" * 80)
    print("🎯 NEW TOKEN DETECTED")
    print("=" * 80)
    print(f"Name:             {token_data.get('name', 'N/A')}")
    print(f"Symbol:           {token_data.get('symbol', 'N/A')}")
    print(f"Mint:             {token_data.get('mint', 'N/A')}")

    if "bondingCurve" in token_data:
        print(f"Bonding Curve:    {token_data['bondingCurve']}")
    if "associatedBondingCurve" in token_data:
        print(f"Associated BC:    {token_data['associatedBondingCurve']}")
    if "user" in token_data:
        print(f"User:             {token_data['user']}")
    if "creator" in token_data:
        print(f"Creator:          {token_data['creator']}")

    print(f"Token Standard:   {token_data.get('token_standard', 'N/A')}")
    print(f"Mayhem Mode:      {token_data.get('is_mayhem_mode', 'N/A')}")

    if "uri" in token_data:
        print(f"URI:              {token_data['uri']}")
    if signature:
        print(f"Signature:        {signature}")

    print("=" * 80 + "\n")


# First 8 bytes of sha256("event:CreateEvent"). Anchor emits the event as a
# base64 "Program data:" log line, which the RPC has already decoded for us.
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])

# The two log lines the pump.fun program writes when it creates a coin.
CREATE_INSTRUCTION_LOGS = (
    "Program log: Instruction: Create",
    "Program log: Instruction: CreateV2",
)

# The CreateEvent layout, in order. Trailing fields were appended by later
# upgrades, so a shorter payload stops early rather than failing.
_CREATE_EVENT_FIELDS = [
    ("name", "string"),
    ("symbol", "string"),
    ("uri", "string"),
    ("mint", "publicKey"),
    ("bondingCurve", "publicKey"),
    ("user", "publicKey"),
    ("creator", "publicKey"),
    ("timestamp", "i64"),
    ("virtual_token_reserves", "u64"),
    ("virtual_sol_reserves", "u64"),
    ("real_token_reserves", "u64"),
    ("token_total_supply", "u64"),
    ("token_program", "publicKey"),
    ("is_mayhem_mode", "bool"),
    ("is_cashback_enabled", "bool"),
]


def parse_create_event(data):
    """Decode a CreateEvent payload into a dict.

    This is the version-agnostic path. The event carries the mint, the curve and
    the creator directly, so nothing here has to deserialize the transaction
    envelope or resolve account indices against a lookup table.

    Args:
        data: Raw event bytes, discriminator included

    Returns:
        The decoded fields, or None if the payload is not a CreateEvent
    """
    if len(data) < 8 or data[:8] != CREATE_EVENT_DISCRIMINATOR:
        return None

    offset = 8
    parsed = {}
    for name, kind in _CREATE_EVENT_FIELDS:
        try:
            if kind == "string":
                length = struct.unpack_from("<I", data, offset)[0]
                offset += 4
                parsed[name] = data[offset : offset + length].decode("utf-8", "replace")
                offset += length
            elif kind == "publicKey":
                parsed[name] = str(Pubkey.from_bytes(data[offset : offset + 32]))
                offset += 32
            elif kind == "u64":
                parsed[name] = struct.unpack_from("<Q", data, offset)[0]
                offset += 8
            elif kind == "i64":
                parsed[name] = struct.unpack_from("<q", data, offset)[0]
                offset += 8
            elif kind == "bool":
                parsed[name] = bool(data[offset])
                offset += 1
        except (struct.error, IndexError):
            # A field the running program does not emit yet. Everything before
            # it is still valid.
            break
    return parsed


def find_create_event(logs):
    """Pull the CreateEvent out of a transaction's log messages.

    Args:
        logs: `meta.logMessages` for one transaction

    Returns:
        The decoded event, or None if the transaction created no coin
    """
    # Matched whole: Anchor writes `Instruction: <Name>` for every program, so
    # a substring test also accepts CreateTokenAccount, CreatePool and the rest
    # from whichever programs share the transaction.
    if not any(log in CREATE_INSTRUCTION_LOGS for log in logs):
        return None
    for log in logs:
        if "Program data:" not in log:
            continue
        try:
            decoded = base64.b64decode(log.split(": ", 1)[1])
        except (ValueError, IndexError):
            continue
        event = parse_create_event(decoded)
        if not event or "bondingCurve" not in event:
            continue

        # The event names the token program. The associated bonding curve is an
        # ordinary ATA of the curve under that program, so it is derived rather
        # than read out of the instruction's account list.
        token_program = Pubkey.from_string(
            event.get("token_program") or str(TOKEN_2022_PROGRAM)
        )
        event["token_standard"] = (
            "token2022" if token_program == TOKEN_2022_PROGRAM else "legacy"
        )
        event["associatedBondingCurve"] = str(
            Pubkey.find_program_address(
                [
                    bytes(Pubkey.from_string(event["bondingCurve"])),
                    bytes(token_program),
                    bytes(Pubkey.from_string(event["mint"])),
                ],
                ASSOCIATED_TOKEN_PROGRAM,
            )[0]
        )
        return event
    return None


def handle_transaction(tx):
    """Detect and print a coin creation in one transaction from a block.

    Detection routes on `meta.logMessages`. The RPC has already decoded the
    envelope by the time it emits the logs, and they read the same for every
    transaction version, so the logs are the one route: a `create` reached by
    CPI appears in them and is absent from the envelope's top-level
    instructions. Opening the envelope is a separate exercise, done by
    `cookbook/pumpfun/decode/pumpfun_decode_transaction_blocksubscribe.py`.

    Args:
        tx: One entry from a blockSubscribe notification's `transactions`
    """
    meta = tx.get("meta") or {}
    # The runtime keeps the logs a transaction emitted before it failed, so a
    # create that succeeded inside a transaction a later instruction reverted
    # still leaves a CreateEvent here. The mint does not exist, and a snipe on
    # it fails with InvalidMint, so `err` has to be read to tell the two apart.
    if meta.get("err") is not None:
        return

    logs = meta.get("logMessages")
    if not logs:
        return
    event = find_create_event(logs)
    if not event:
        return

    version = tx.get("version", "legacy")
    label = "legacy" if version == "legacy" else f"v{version}"
    print(f"\n🔍 Found CreateEvent in a {label} transaction")
    print_token_info(event)

    loaded_addresses = meta.get("loadedAddresses")
    if loaded_addresses:
        writable_count = len(loaded_addresses.get("writable", []))
        readonly_count = len(loaded_addresses.get("readonly", []))
        if writable_count or readonly_count:
            print(
                f"ℹ️  [ALT] Used Address Lookup Table: {writable_count} writable, "
                f"{readonly_count} readonly\n"
            )


async def listen_and_decode_create():
    """Main listener function that subscribes to Solana blocks and decodes Pump.fun token creations.

    This function:
    1. Subscribes to blocks mentioning the Pump.fun program
    2. Reads each transaction's `meta.logMessages` for a CreateEvent
    3. Reports address lookup table use alongside each creation
    """
    async with websockets.connect(
        WSS_ENDPOINT, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
    ) as websocket:
        subscription_message = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "blockSubscribe",
                "params": [
                    {"mentionsAccountOrProgram": str(PUMP_PROGRAM_ID)},
                    {
                        "commitment": "confirmed",
                        "encoding": "base64",
                        "showRewards": False,
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            }
        )
        await websocket.send(subscription_message)
        print(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM_ID}")

        while True:
            try:
                response = await websocket.recv()
                data = json.loads(response)

                if "method" in data and data["method"] == "blockNotification":
                    if "params" in data and "result" in data["params"]:
                        block_data = data["params"]["result"]
                        if "value" in block_data and "block" in block_data["value"]:
                            block = block_data["value"]["block"]
                            # `block` is null for a skipped or unavailable slot:
                            # the key is present, the value is not. Without this
                            # check the membership test below raises TypeError.
                            if block and "transactions" in block:
                                for tx in block["transactions"]:
                                    if not isinstance(tx, dict):
                                        continue
                                    handle_transaction(tx)
                elif "result" in data:
                    print("Subscription confirmed")
                else:
                    print(
                        f"Received unexpected message type: {data.get('method', 'Unknown')}"
                    )
            except websockets.ConnectionClosed:
                # Leave the recv loop so main() can reconnect. Swallowing this here
                # would make the next recv() raise immediately, spinning the loop.
                print("WebSocket connection closed.")
                break
            except Exception as e:
                print(f"An error occurred: {e!s}")
                print(f"Error details: {type(e).__name__}")
                import traceback

                traceback.print_exc()


async def main() -> None:
    """Reconnect for as long as the script runs."""
    while True:
        try:
            await listen_and_decode_create()
        except (websockets.WebSocketException, OSError) as e:
            print(f"Connection error: {e!s}")
        print("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

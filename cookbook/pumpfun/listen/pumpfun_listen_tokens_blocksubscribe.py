"""Listens to Solana blocks for Pump.fun 'create' instructions via WebSocket.
Decodes transaction data to extract mint, bonding curve, and user details.

Usage:
    uv run cookbook/pumpfun/listen/pumpfun_listen_tokens_blocksubscribe.py

Performance: Usually slower than other listeners due to block-level processing.

This script uses blockSubscribe which receives entire blocks containing transactions
that mention the Pump.fun program. It then decodes the instruction data from each
transaction to extract token creation details.

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

import base58
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# Solana's blockSubscribe (and a busy logsSubscribe) sends frames well past
# websockets' 1 MiB default, which kills the connection with a 1009 close
# instead of delivering the message. Same value the bot's own listeners use.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: cookbook/solana/anchor_calculate_discriminator.py
CREATE_DISCRIMINATOR = 8576854823835016728
CREATE_V2_DISCRIMINATOR = struct.unpack(
    "<Q", bytes([214, 144, 76, 236, 95, 139, 49, 180])
)[0]


def print_token_info(token_data, signature=None):
    """
    Print token information in a consistent, user-friendly format.

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
    print(f"Mayhem Mode:      {token_data.get('is_mayhem_mode', False)}")

    if "uri" in token_data:
        print(f"URI:              {token_data['uri']}")
    if signature:
        print(f"Signature:        {signature}")

    print("=" * 80 + "\n")


def get_account_keys(transaction, instruction, loaded_addresses=None):
    """
    Safely extract account keys for an instruction from a versioned transaction.
    Handles both static account keys and loaded addresses from lookup tables.

    Args:
        transaction: VersionedTransaction object
        instruction: Instruction object
        loaded_addresses: Dict with 'writable' and 'readonly' loaded addresses from tx meta

    Returns:
        List of account keys as strings, or None if unable to resolve
    """
    account_keys = []
    static_keys = transaction.message.account_keys

    # Combine all available account keys: static + loaded
    all_keys = list(static_keys)

    if loaded_addresses:
        # Add loaded writable addresses
        if "writable" in loaded_addresses:
            for addr in loaded_addresses["writable"]:
                all_keys.append(Pubkey.from_string(addr))

        # Add loaded readonly addresses
        if "readonly" in loaded_addresses:
            for addr in loaded_addresses["readonly"]:
                all_keys.append(Pubkey.from_string(addr))

    # Now resolve account indices
    for index in instruction.accounts:
        try:
            if index < len(all_keys):
                account_keys.append(str(all_keys[index]))
            else:
                print(
                    f"Warning: Account index {index} out of range (max: {len(all_keys) - 1})"
                )
                return None
        except (IndexError, Exception) as e:
            print(f"Error resolving account at index {index}: {e}")
            return None

    return account_keys


# First 8 bytes of sha256("event:CreateEvent"). Anchor emits the event as a
# base64 "Program data:" log line, which the RPC has already decoded for us.
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])

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
    if not any("Program log: Instruction: Create" in log for log in logs):
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


def load_idl(file_path):
    with open(file_path) as f:
        return json.load(f)


def decode_create_instruction(ix_data, ix_def, accounts):
    """
    Decode legacy Create instruction (Metaplex tokens).

    The Create instruction creates tokens using the Metaplex Token Metadata standard.
    Instruction data contains: name, symbol, uri, and additional creator pubkey.
    Account references are extracted from the accounts array.

    Args:
        ix_data: Raw instruction data bytes
        ix_def: Instruction definition from IDL
        accounts: List of account pubkeys involved in the instruction

    Returns:
        Dictionary containing decoded token information
    """
    args = {}
    offset = 8  # Skip 8-byte discriminator

    # Parse instruction arguments according to IDL definition
    for arg in ix_def["args"]:
        if arg["type"] == "string":
            # String format: 4-byte length prefix + UTF-8 encoded string
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif arg["type"] == "pubkey":
            # Pubkey is 32 bytes, encoded as base58
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")

        args[arg["name"]] = value

    # Extract account addresses from the accounts array
    # Account layout for Create instruction:
    # 0: mint, 1: metadata, 2: bondingCurve, 3: associatedBondingCurve,
    # 4: tokenProgram, 5: systemProgram, 6: rent, 7: user
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])
    args["token_standard"] = "legacy"
    args["is_mayhem_mode"] = False

    return args


def decode_create_v2_instruction(ix_data, ix_def, accounts):
    """
    Decode CreateV2 instruction (Token2022 tokens).

    The CreateV2 instruction creates tokens using the Token-2022 standard, which supports
    additional features like transfer fees, interest-bearing tokens, and more.
    This instruction includes an optional is_mayhem_mode flag.

    Token-2022 Reference:
    https://spl.solana.com/token-2022

    Args:
        ix_data: Raw instruction data bytes
        ix_def: Instruction definition from IDL
        accounts: List of account pubkeys involved in the instruction

    Returns:
        Dictionary containing decoded token information
    """
    args = {}
    offset = 8  # Skip 8-byte discriminator

    # Parse instruction arguments according to IDL definition.
    # CreateV2 args: name, symbol, uri, creator (pubkey), is_mayhem_mode (bool),
    # is_cashback_enabled (OptionBool), creator_fee_bps (OptionU64),
    # is_holder_reward (OptionBool). The last two arrived with the 2026-09-15
    # program upgrade.
    #
    # OptionBool and OptionU64 are single-field Anchor structs with no presence
    # tag: each serializes as its bare inner value, 1 and 8 bytes. They are also
    # positional rather than independently optional, and the trailing ones are
    # legally absent from the wire — three lengths are live on chain (no
    # trailing args, is_cashback_enabled only, is_cashback_enabled plus
    # creator_fee_bps). An absent one is reported as None, meaning unset, rather
    # than as a fabricated default. Same rule as utils/idl_parser.py (issue
    # #184); reading a fixed number of trailing bytes raises IndexError instead.
    for arg in ix_def["args"]:
        t = arg["type"]
        if t == "string":
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif t == "pubkey":
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        elif t == "bool":
            value = bool(ix_data[offset]) if offset < len(ix_data) else False
            offset += 1
        elif isinstance(t, dict) and "defined" in t:
            defined_name = (
                t["defined"]["name"] if isinstance(t["defined"], dict) else t["defined"]
            )
            if defined_name == "OptionBool":
                if offset >= len(ix_data):
                    value = None
                else:
                    value = bool(ix_data[offset])
                    offset += 1
            elif defined_name == "OptionU64":
                if offset + 8 > len(ix_data):
                    value = None
                else:
                    value = struct.unpack_from("<Q", ix_data, offset)[0]
                    offset += 8
            else:
                raise ValueError(f"Unsupported defined type: {defined_name}")
        else:
            raise ValueError(f"Unsupported type: {t}")

        args[arg["name"]] = value

    # Extract account addresses from the accounts array
    # Account layout for CreateV2 instruction:
    # 0: mint, 1: metadata, 2: bondingCurve, 3: associatedBondingCurve,
    # 4: tokenProgram (Token2022), 5: user, 6: systemProgram, 7: rent
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[5])
    args["token_standard"] = "token2022"

    return args


def decode_from_envelope(tx, idl):
    """Decode a coin creation straight from the transaction bytes.

    The fallback for a block that arrives without `logMessages`. It is a
    fallback and not the main path because the envelope is the one part of a
    transaction whose format changes underneath you: solders 0.26 raises
    `ValueError: io error: unexpected end of file` on a v1 transaction, and
    solders only learned to read one in 0.29, which needs solana-py 0.40.

    Args:
        tx: One entry from a blockSubscribe notification's `transactions`
        idl: The parsed pump.fun IDL

    Returns:
        True if a creation was found and printed
    """
    try:
        transaction = VersionedTransaction.from_bytes(
            base64.b64decode(tx["transaction"][0])
        )
    except (ValueError, KeyError, IndexError):
        return False

    meta = tx.get("meta") or {}
    loaded_addresses = meta.get("loadedAddresses")
    for ix in transaction.message.instructions:
        program = transaction.message.account_keys[ix.program_id_index]
        if str(program) != str(PUMP_PROGRAM_ID):
            continue
        ix_data = bytes(ix.data)
        if len(ix_data) < 8:
            continue
        discriminator = struct.unpack("<Q", ix_data[:8])[0]
        if discriminator not in (CREATE_DISCRIMINATOR, CREATE_V2_DISCRIMINATOR):
            continue

        is_v2 = discriminator == CREATE_V2_DISCRIMINATOR
        wanted = "create_v2" if is_v2 else "create"
        ix_def = next(
            (instr for instr in idl["instructions"] if instr["name"] == wanted),
            next(instr for instr in idl["instructions"] if instr["name"] == "create"),
        )
        account_keys = get_account_keys(transaction, ix, loaded_addresses)
        if account_keys is None:
            print("⚠️  Skipping transaction due to unresolved accounts")
            continue

        decode = decode_create_v2_instruction if is_v2 else decode_create_instruction
        print("\n🔍 Found a creation by decoding the envelope (no logs in this block)")
        print_token_info(decode(ix_data, ix_def, account_keys))
        return True
    return False


def handle_transaction(tx, idl):
    """Detect and print a coin creation in one transaction from a block.

    Detection routes on `meta.logMessages`, which the RPC has already decoded
    and which reads the same whatever version the transaction is. The envelope
    is only opened afterwards, to report address lookup table use, and only when
    the installed solders can read it — transaction v1 (live since 2026-09-15)
    is not deserializable by solders 0.26, and gating detection on that decode
    is what made this example blind to every v1 block.

    Args:
        tx: One entry from a blockSubscribe notification's `transactions`
        idl: The parsed pump.fun IDL, kept for the envelope path
    """
    meta = tx.get("meta") or {}
    logs = meta.get("logMessages")
    version = tx.get("version", "legacy")

    if logs:
        event = find_create_event(logs)
        if not event:
            return
        label = "legacy" if version == "legacy" else f"v{version}"
        print(f"\n🔍 Found CreateEvent in a {label} transaction")
        print_token_info(event)
    # Some providers return blocks without logMessages. The envelope is then
    # the only route, and it only works for a version solders can read.
    elif not decode_from_envelope(tx, idl):
        return

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
    """
    Main listener function that subscribes to Solana blocks and decodes Pump.fun token creations.

    This function:
    1. Loads the Pump.fun IDL for instruction parsing
    2. Subscribes to blocks mentioning the Pump.fun program
    3. Decodes transactions to extract Create/CreateV2 instructions
    4. Handles Address Lookup Tables (ALTs) for account resolution
    """
    idl = load_idl("idl/pump_fun_idl.json")

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
                                    handle_transaction(tx, idl)
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

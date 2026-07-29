"""
Listens for new Pump.fun token creations via Solana WebSocket.
Monitors logs for 'Create' instructions, decodes and prints token details (name, symbol, mint, etc.).

Performance: Usually faster than blockSubscribe, but slower than Geyser.

This script uses logsSubscribe which receives program logs containing event data.
Event logs include all token fields directly, making parsing simpler and faster than
decoding full transactions. It also derives each coin's associated bonding curve,
which is the token account the curve holds its supply in.

WebSocket API Reference:
https://solana.com/docs/rpc/websocket/logssubscribe

Program Logs and Events:
https://solana.com/docs/programs/debugging#logging

Program Derived Addresses:
https://solana.com/docs/core/pda
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

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# Solana's blockSubscribe (and a busy logsSubscribe) sends frames well past
# websockets' 1 MiB default, which kills the connection with a 1009 close
# instead of delivering the message. Same value the bot's own listeners use.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Coins created with `create_v2` are Token2022; legacy `create` coins are SPL Token.
# The associated bonding curve is an ATA, and an ATA's address depends on which token
# program owns the mint — deriving a Token2022 coin's ATA against the legacy program
# yields a valid-looking address that does not exist on chain.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Event discriminator for CreateEvent (8-byte identifier)
# This is emitted by both Create and CreateV2 instructions
# Calculated using the first 8 bytes of sha256("event:CreateEvent")
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])


def print_token_info(
    token_data, signature=None, associated_bonding_curve: str | None = None
):
    """
    Print token information in a consistent, user-friendly format.

    Args:
        token_data: Dictionary containing token fields
        signature: Optional transaction signature
        associated_bonding_curve: Optional derived associated bonding curve address
    """
    print("\n" + "=" * 80)
    print("🎯 NEW TOKEN DETECTED")
    print("=" * 80)
    print(f"Name:             {token_data.get('name', 'N/A')}")
    print(f"Symbol:           {token_data.get('symbol', 'N/A')}")
    print(f"Mint:             {token_data.get('mint', 'N/A')}")

    if "bondingCurve" in token_data:
        print(f"Bonding Curve:    {token_data['bondingCurve']}")
    if associated_bonding_curve:
        print(f"Associated BC:    {associated_bonding_curve}")
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



def find_associated_bonding_curve(
    mint: Pubkey, bonding_curve: Pubkey, token_standard: str
) -> Pubkey:
    """
    Derive the associated token account the bonding curve holds its supply in.

    ATA derivation: find_program_address(
        [bonding_curve, token_program_id, mint], associated_token_program_id
    )

    Args:
        mint: The token mint pubkey
        bonding_curve: The bonding curve pubkey
        token_standard: "token2022" for create_v2 coins, anything else for legacy

    Returns:
        The derived associated bonding curve address
    """
    token_program = (
        TOKEN_2022_PROGRAM_ID
        if token_standard == "token2022"  # noqa: S105 - a token standard, not a secret
        else TOKEN_PROGRAM_ID
    )
    derived_address, _ = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return derived_address


def parse_create_instruction(data):
    """
    Parse CreateEvent data from legacy Create instruction (Metaplex tokens).

    Event logs contain all fields directly embedded in the event data, unlike
    instruction data which requires account lookup. Event format:
    - 8 bytes: event discriminator
    - Variable: name (4-byte length + UTF-8 string)
    - Variable: symbol (4-byte length + UTF-8 string)
    - Variable: uri (4-byte length + UTF-8 string)
    - 32 bytes: mint pubkey
    - 32 bytes: bondingCurve pubkey
    - 32 bytes: user pubkey
    - 32 bytes: creator pubkey

    Args:
        data: Raw event data bytes from program logs

    Returns:
        Dictionary containing decoded token information, or None if parsing fails
    """
    return _parse_create_event(data, token_standard_hint="legacy")


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


def _parse_create_event(data, token_standard_hint):
    """Parse a CreateEvent payload (same on-chain layout for both create and create_v2)."""
    if len(data) < 8:
        print(f"⚠️  Data too short for Create event: {len(data)} bytes")
        return None
    offset = 8
    parsed_data = {}
    try:
        for field_name, field_type in _CREATE_EVENT_FIELDS:
            if field_type == "string":
                if offset + 4 > len(data):
                    raise ValueError(f"Not enough data for {field_name} length at offset {offset}")
                length = struct.unpack("<I", data[offset : offset + 4])[0]
                offset += 4
                if offset + length > len(data):
                    raise ValueError(f"Not enough data for {field_name} value (length={length}) at offset {offset}")
                value = data[offset : offset + length].decode("utf-8")
                offset += length
            elif field_type == "publicKey":
                if offset + 32 > len(data):
                    raise ValueError(f"Not enough data for {field_name} at offset {offset}")
                value = base58.b58encode(data[offset : offset + 32]).decode("utf-8")
                offset += 32
            elif field_type == "u64":
                value = struct.unpack("<Q", data[offset : offset + 8])[0]
                offset += 8
            elif field_type == "i64":
                value = struct.unpack("<q", data[offset : offset + 8])[0]
                offset += 8
            elif field_type == "bool":
                value = bool(data[offset]) if offset < len(data) else False
                offset += 1
            parsed_data[field_name] = value

        parsed_data["token_standard"] = token_standard_hint
        return parsed_data
    except Exception as e:
        print(f"❌ Parse Create event error: {e}")
        print(f"   Data length: {len(data)} bytes, offset: {offset}")
        print(f"   Data hex: {data.hex()[:200]}...")
        return None


def parse_create_v2_instruction(data):
    """Parse CreateEvent emitted by CreateV2 instruction (Token2022 tokens)."""
    return _parse_create_event(data, token_standard_hint="token2022")


async def listen_for_new_tokens():
    while True:
        try:
            async with websockets.connect(
                WSS_ENDPOINT, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
            ) as websocket:
                subscription_message = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(PUMP_PROGRAM_ID)]},
                            {"commitment": "processed"},
                        ],
                    }
                )
                await websocket.send(subscription_message)
                print(
                    f"Listening for new token creations from program: {PUMP_PROGRAM_ID}"
                )

                # Wait for subscription confirmation
                response = await websocket.recv()
                print(f"Subscription response: {response}")

                while True:
                    try:
                        response = await websocket.recv()
                        data = json.loads(response)

                        if "method" in data and data["method"] == "logsNotification":
                            log_data = data["params"]["result"]["value"]
                            logs = log_data.get("logs", [])

                            # Detect both Create and CreateV2 instructions
                            is_create = any(
                                "Program log: Instruction: Create" in log
                                for log in logs
                            )
                            is_create_v2 = any(
                                "Program log: Instruction: CreateV2" in log
                                for log in logs
                            )

                            if is_create or is_create_v2:
                                for log in logs:
                                    if "Program data:" in log:
                                        try:
                                            encoded_data = log.split(": ")[1]
                                            decoded_data = base64.b64decode(
                                                encoded_data
                                            )

                                            # Check if this is a CreateEvent by validating discriminator
                                            if len(decoded_data) < 8:
                                                continue

                                            event_discriminator = decoded_data[:8]
                                            if event_discriminator != CREATE_EVENT_DISCRIMINATOR:
                                                # Skip non-CreateEvent logs (e.g., TradeEvent, ExtendAccountEvent)
                                                continue

                                            print(f"\n🔍 Found CreateEvent, length: {len(decoded_data)} bytes")
                                            print(f"   Signature: {log_data.get('signature')}")

                                            # Both create and create_v2 emit the same CreateEvent
                                            # The difference is in the optional is_mayhem_mode field
                                            if is_create_v2:
                                                print("📝 Instruction: CreateV2 (Token2022)")
                                                parsed_data = parse_create_v2_instruction(
                                                    decoded_data
                                                )
                                            else:
                                                print("📝 Instruction: Create (Legacy/Metaplex)")
                                                parsed_data = parse_create_instruction(
                                                    decoded_data
                                                )

                                            if parsed_data and "name" in parsed_data:
                                                associated_curve = find_associated_bonding_curve(
                                                    Pubkey.from_string(parsed_data["mint"]),
                                                    Pubkey.from_string(parsed_data["bondingCurve"]),
                                                    parsed_data.get("token_standard", ""),
                                                )
                                                # Print token information in consistent format
                                                print_token_info(
                                                    parsed_data,
                                                    signature=log_data.get("signature"),
                                                    associated_bonding_curve=str(associated_curve),
                                                )
                                            else:
                                                print("⚠️  Parsing failed for CreateEvent")
                                        except Exception as e:
                                            print(f"❌ Error processing log: {e!s}")

                    except Exception as e:
                        print(f"An error occurred while processing message: {e}")
                        break

        except Exception as e:
            print(f"Connection error: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(listen_for_new_tokens())

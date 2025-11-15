"""
Listens for new Pump.fun token creations via Solana WebSocket.
Monitors logs for 'Create' instructions, decodes and prints token details (name, symbol, mint, etc.).
Additionally, calculates an associated bonding curve address for each token.

It is usually faster than blockSubscribe, but slower than Geyser.
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
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Event discriminators from IDL
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])


def find_associated_bonding_curve(mint: Pubkey, bonding_curve: Pubkey) -> Pubkey:
    """
    Find the associated bonding curve for a given mint and bonding curve.
    This uses the standard ATA derivation.
    """
    derived_address, _ = Pubkey.find_program_address(
        [
            bytes(bonding_curve),
            bytes(TOKEN_PROGRAM_ID),
            bytes(mint),
        ],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return derived_address


def parse_create_instruction(data):
    """Parse legacy Create instruction (Metaplex tokens)."""
    if len(data) < 8:
        print(f"⚠️  Data too short for Create instruction: {len(data)} bytes")
        return None
    offset = 8
    parsed_data = {}

    # Parse fields based on CreateEvent structure
    fields = [
        ("name", "string"),
        ("symbol", "string"),
        ("uri", "string"),
        ("mint", "publicKey"),
        ("bondingCurve", "publicKey"),
        ("user", "publicKey"),
        ("creator", "publicKey"),
    ]

    try:
        for field_name, field_type in fields:
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

            parsed_data[field_name] = value

        parsed_data["token_standard"] = "legacy"
        parsed_data["is_mayhem_mode"] = False
        return parsed_data
    except Exception as e:
        print(f"❌ Parse Create error: {e}")
        print(f"   Data length: {len(data)} bytes, offset: {offset}")
        return None


def parse_create_v2_instruction(data):
    """Parse CreateV2 instruction (Token2022 tokens)."""
    if len(data) < 8:
        print(f"⚠️  Data too short for CreateV2 instruction: {len(data)} bytes")
        return None
    offset = 8
    parsed_data = {}

    # Parse fields based on CreateV2Event structure
    fields = [
        ("name", "string"),
        ("symbol", "string"),
        ("uri", "string"),
        ("mint", "publicKey"),
        ("bondingCurve", "publicKey"),
        ("user", "publicKey"),
        ("creator", "publicKey"),
    ]

    try:
        for field_name, field_type in fields:
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

            parsed_data[field_name] = value

        # Parse is_mayhem_mode (OptionBool at the end)
        if offset < len(data):
            is_mayhem_mode = bool(data[offset])
            parsed_data["is_mayhem_mode"] = is_mayhem_mode
        else:
            parsed_data["is_mayhem_mode"] = False

        parsed_data["token_standard"] = "token2022"
        return parsed_data
    except Exception as e:
        print(f"❌ Parse CreateV2 error: {e}")
        print(f"   Data length: {len(data)} bytes, offset: {offset}")
        return None


async def listen_for_new_tokens():
    while True:
        try:
            async with websockets.connect(WSS_ENDPOINT) as websocket:
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
                                                parsed_data = (
                                                    parse_create_v2_instruction(
                                                        decoded_data
                                                    )
                                                )
                                            else:
                                                print("📝 Instruction: Create (Legacy/Metaplex)")
                                                parsed_data = parse_create_instruction(
                                                    decoded_data
                                                )

                                            if parsed_data and "name" in parsed_data:
                                                for key, value in parsed_data.items():
                                                    print(f"{key}: {value}")

                                                # Calculate associated bonding curve
                                                mint = Pubkey.from_string(
                                                    parsed_data["mint"]
                                                )
                                                bonding_curve = Pubkey.from_string(
                                                    parsed_data["bondingCurve"]
                                                )
                                                associated_curve = (
                                                    find_associated_bonding_curve(
                                                        mint, bonding_curve
                                                    )
                                                )
                                                print(
                                                    f"Associated Bonding Curve: {associated_curve}"
                                                )
                                                print("\n")
                                            else:
                                                print(f"⚠️  Parsing failed for CreateEvent")
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

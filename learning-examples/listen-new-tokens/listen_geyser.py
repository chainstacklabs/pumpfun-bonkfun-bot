"""
Monitors Solana for new Pump.fun token creations using Geyser gRPC.
Decodes 'create' instructions to extract and display token details (name, symbol, mint, bonding curve).

Performance: Proven to be the fastest listener method available.

This script uses Yellowstone Dragon's Mouth Geyser gRPC interface, which provides
real-time streaming of Solana blockchain data with lower latency than WebSocket methods.
Requires a Geyser API token for access.

Geyser gRPC Reference:
https://docs.triton.one/rpc-pool/grpc-subscriptions

Authentication: Supports both Basic and X-Token authentication methods.
Configure via GEYSER_ENDPOINT, GEYSER_API_TOKEN, and AUTH_TYPE variables.
"""

import asyncio
import os
import struct

import base58
import grpc
from dotenv import load_dotenv
from generated import geyser_pb2, geyser_pb2_grpc
from solders.pubkey import Pubkey

load_dotenv()


GEYSER_ENDPOINT = os.getenv("GEYSER_ENDPOINT")
GEYSER_API_TOKEN = os.getenv("GEYSER_API_TOKEN")
# Authentication type: "x-token" or "basic"
AUTH_TYPE = "x-token"

PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: learning-examples/calculate_discriminator.py
PUMP_CREATE_PREFIX = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_PREFIX = bytes([214, 144, 76, 236, 95, 139, 49, 180])


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

    if "bonding_curve" in token_data:
        print(f"Bonding Curve:    {token_data['bonding_curve']}")
    if "associated_bonding_curve" in token_data:
        print(f"Associated BC:    {token_data['associated_bonding_curve']}")
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



async def create_geyser_connection():
    """Establish a secure connection to the Geyser endpoint using the configured auth type."""
    if AUTH_TYPE == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback((("x-token", GEYSER_API_TOKEN),), None)
        )
    else:  # Default to basic auth
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback(
                (("authorization", f"Basic {GEYSER_API_TOKEN}"),), None
            )
        )

    creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
    channel = grpc.aio.secure_channel(GEYSER_ENDPOINT, creds)
    return geyser_pb2_grpc.GeyserStub(channel)


def create_subscription_request():
    """Create a subscription request for Pump.fun transactions."""
    request = geyser_pb2.SubscribeRequest()
    request.transactions["pump_filter"].account_include.append(str(PUMP_PROGRAM_ID))
    request.transactions["pump_filter"].failed = False
    request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return request


def decode_create_instruction(ix_data: bytes, keys, accounts) -> dict:
    """Decode a legacy create instruction (Metaplex) from transaction data."""
    # Skip past the 8-byte discriminator prefix
    offset = 8

    # Extract account keys in base58 format
    def get_account_key(index):
        if index >= len(accounts):
            return "N/A"
        account_index = accounts[index]
        return base58.b58encode(keys[account_index]).decode()

    # Read string fields (prefixed with length)
    def read_string():
        nonlocal offset
        # Get string length (4-byte uint)
        length = struct.unpack_from("<I", ix_data, offset)[0]
        offset += 4
        # Extract and decode the string
        value = ix_data[offset : offset + length].decode()
        offset += length
        return value

    def read_pubkey():
        nonlocal offset
        value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
        offset += 32
        return value

    name = read_string()
    symbol = read_string()
    uri = read_string()
    creator = read_pubkey()

    token_info = {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "creator": creator,
        "mint": get_account_key(0),
        "metadata": get_account_key(1),
        "bonding_curve": get_account_key(2),
        "associated_bonding_curve": get_account_key(3),
        "token_program": get_account_key(4),
        "system_program": get_account_key(5),
        "rent": get_account_key(6),
        "user": get_account_key(7),
        "token_standard": "legacy",
        "is_mayhem_mode": False,
    }

    return token_info


def decode_create_v2_instruction(ix_data: bytes, keys, accounts) -> dict:
    """Decode a CreateV2 instruction (Token2022) from transaction data."""
    # Skip past the 8-byte discriminator prefix
    offset = 8

    # Extract account keys in base58 format
    def get_account_key(index):
        if index >= len(accounts):
            return "N/A"
        account_index = accounts[index]
        return base58.b58encode(keys[account_index]).decode()

    # Read string fields (prefixed with length)
    def read_string():
        nonlocal offset
        # Get string length (4-byte uint)
        length = struct.unpack_from("<I", ix_data, offset)[0]
        offset += 4
        # Extract and decode the string
        value = ix_data[offset : offset + length].decode()
        offset += length
        return value

    def read_pubkey():
        nonlocal offset
        value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
        offset += 32
        return value

    name = read_string()
    symbol = read_string()
    uri = read_string()
    creator = read_pubkey()

    # Parse is_mayhem_mode (OptionBool at the end)
    is_mayhem_mode = False
    if offset < len(ix_data):
        is_mayhem_mode = bool(ix_data[offset])

    token_info = {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "creator": creator,
        "mint": get_account_key(0),
        "bonding_curve": get_account_key(2),
        "associated_bonding_curve": get_account_key(3),
        "user": get_account_key(5),
        "token_standard": "token2022",
        "is_mayhem_mode": is_mayhem_mode,
    }

    return token_info




async def monitor_pump():
    """Monitor Solana blockchain for new Pump.fun token creations."""
    print(f"Starting Pump.fun token monitor using {AUTH_TYPE.upper()} authentication")
    stub = await create_geyser_connection()
    request = create_subscription_request()

    async for update in stub.Subscribe(iter([request])):
        # Skip non-transaction updates
        if not update.HasField("transaction"):
            continue

        tx = update.transaction.transaction.transaction
        msg = getattr(tx, "message", None)
        if msg is None:
            continue

        # Check each instruction in the transaction
        for ix in msg.instructions:
            # Check for both Create and CreateV2 instructions
            is_create = ix.data.startswith(PUMP_CREATE_PREFIX)
            is_create_v2 = ix.data.startswith(PUMP_CREATE_V2_PREFIX)

            if not (is_create or is_create_v2):
                continue

            # Decode based on instruction type
            if is_create_v2:
                info = decode_create_v2_instruction(
                    ix.data, msg.account_keys, ix.accounts
                )
            else:
                info = decode_create_instruction(ix.data, msg.account_keys, ix.accounts)

            # Extract transaction signature
            signature = base58.b58encode(
                bytes(update.transaction.transaction.signature)
            ).decode()

            # Print token information in consistent format
            print_token_info(info, signature=signature)


if __name__ == "__main__":
    asyncio.run(monitor_pump())

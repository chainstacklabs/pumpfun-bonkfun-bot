"""Listen for new pump.fun coins over Geyser gRPC.

Usage:
    uv run cookbook/pumpfun/listen/pumpfun_listen_tokens_geyser.py

Decodes `create` instructions for the token details, and reports which
transaction format each coin was created in, with its inline budget for v1. Uses
the Yellowstone Dragon's Mouth interface, the lowest-latency of the executed
streams; needs a Geyser API token.

Geyser gRPC Reference:
https://docs.triton.one/rpc-pool/grpc-subscriptions

Authentication: Supports both Basic and X-Token authentication methods.
Configure via GEYSER_ENDPOINT, GEYSER_API_TOKEN and GEYSER_AUTH_TYPE.
"""

import asyncio
import os
import struct
import sys
from pathlib import Path

import base58
import grpc
from dotenv import load_dotenv
from solders.pubkey import Pubkey

# The geyser stubs are generated once, into src/geyser/generated. Reuse them rather
# than keeping a second copy here that drifts out of sync with proto/.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.geyser.generated import geyser_pb2, geyser_pb2_grpc

load_dotenv()


GEYSER_ENDPOINT = os.getenv("GEYSER_ENDPOINT")
GEYSER_API_TOKEN = os.getenv("GEYSER_API_TOKEN")
AUTH_TYPE = os.getenv("GEYSER_AUTH_TYPE", "x-token").lower()

BAD_AUTH_TYPE_MSG = "GEYSER_AUTH_TYPE must be 'x-token' or 'basic'"

PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: cookbook/solana/anchor_calculate_discriminator.py
PUMP_CREATE_PREFIX = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_PREFIX = bytes([214, 144, 76, 236, 95, 139, 49, 180])


def print_token_info(token_data, signature=None, envelope: dict | None = None):
    """Print token information in a consistent, user-friendly format.

    Args:
        token_data: Dictionary containing token fields
        signature: Optional transaction signature
        envelope: Optional envelope summary from `describe_envelope`
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
    print(f"Mayhem Mode:      {token_data.get('is_mayhem_mode', 'N/A')}")

    if "uri" in token_data:
        print(f"URI:              {token_data['uri']}")
    if signature:
        print(f"Signature:        {signature}")
    if envelope:
        print(f"Tx version:       {envelope['version']}")
        if envelope["config"]:
            print(f"Tx config:        {envelope['config']}")
        if envelope["cost_units"] is not None:
            print(f"Cost units:       {envelope['cost_units']}")

    print("=" * 80 + "\n")


async def create_geyser_connection():
    """Establish a secure connection to the Geyser endpoint using the configured auth type.

    Raises:
        ValueError: If GEYSER_AUTH_TYPE names a scheme the endpoint does not take
    """
    if AUTH_TYPE == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback((("x-token", GEYSER_API_TOKEN),), None)
        )
    elif AUTH_TYPE == "basic":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback(
                (("authorization", f"Basic {GEYSER_API_TOKEN}"),), None
            )
        )
    else:
        raise ValueError(BAD_AUTH_TYPE_MSG)

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


def resolve_account_keys(
    tx: geyser_pb2.SubscribeUpdateTransactionInfo,
) -> list[bytes]:
    """Build the full account-key table for one transaction.

    A v0 transaction indexes accounts past the end of `message.account_keys` when it
    uses an address lookup table; geyser reports those in the transaction meta, in
    this exact order: static keys, then writable loaded, then read-only loaded.
    Coins minted through a router (most of them) arrive this way, so a listener that
    only reads `message.account_keys` mislabels or drops them.

    Args:
        tx: A geyser `SubscribeUpdateTransactionInfo`

    Returns:
        Account keys as raw 32-byte values, indexable by an instruction's account list
    """
    keys = list(tx.transaction.message.account_keys)
    meta = getattr(tx, "meta", None)
    if meta is not None:
        keys.extend(meta.loaded_writable_addresses)
        keys.extend(meta.loaded_readonly_addresses)
    return keys


def describe_envelope(tx: geyser_pb2.SubscribeUpdateTransactionInfo) -> dict:
    """Summarise which transaction format a coin was created in, and its budget.

    Transaction v1 (SIMD-0385) carries its compute budget inline on the message
    as `config` instead of as separate ComputeBudget instructions. Geyser sets
    that field only for v1, so its presence is how you tell a v1 transaction
    from a legacy or v0 one: the `versioned` flag is true for both v0 and v1 and
    cannot separate them.

    Args:
        tx: A geyser `SubscribeUpdateTransactionInfo`

    Returns:
        Dict with the detected `version`, a readable `config` string (empty for
        legacy/v0) and `cost_units` when the validator reported it
    """
    msg = tx.transaction.message
    is_v1 = msg.HasField("config")

    config = ""
    if is_v1:
        cfg = msg.config
        parts = []
        # Every field is optional, and an unset one means zero rather than a
        # default: a v1 transaction that omits the compute unit limit budgets
        # 0 CU and dies at account loading.
        if cfg.HasField("priority_fee"):
            # v1 states a total in lamports, not micro-lamports per CU as v0 does.
            parts.append(f"priority_fee={cfg.priority_fee} lamports")
        if cfg.HasField("compute_unit_limit"):
            parts.append(f"cu_limit={cfg.compute_unit_limit}")
        if cfg.HasField("loaded_accounts_data_size_limit"):
            parts.append(f"data_size={cfg.loaded_accounts_data_size_limit}")
        if cfg.HasField("heap_size"):
            parts.append(f"heap={cfg.heap_size}")
        config = ", ".join(parts) or "all fields unset"

    meta = getattr(tx, "meta", None)
    cost_units = None
    if meta is not None and meta.HasField("cost_units"):
        cost_units = meta.cost_units

    return {
        "version": "v1" if is_v1 else ("v0" if msg.versioned else "legacy"),
        "config": config,
        "cost_units": cost_units,
    }


def decode_create_instruction(ix_data: bytes, keys, accounts) -> dict:
    """Decode a legacy create instruction (Metaplex) from transaction data."""
    # Skip past the 8-byte discriminator prefix
    offset = 8

    # Extract account keys in base58 format
    def get_account_key(index):
        if index >= len(accounts):
            return "N/A"
        account_index = accounts[index]
        # A v0 transaction can index accounts that live in an address lookup table,
        # which geyser reports under meta.loaded_*_addresses rather than
        # message.account_keys. Without this guard those coins raise IndexError and
        # kill the listener.
        if account_index >= len(keys):
            return "N/A"
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
        # A v0 transaction can index accounts that live in an address lookup table,
        # which geyser reports under meta.loaded_*_addresses rather than
        # message.account_keys. Without this guard those coins raise IndexError and
        # kill the listener.
        if account_index >= len(keys):
            return "N/A"
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
        "bonding_curve": get_account_key(2),
        "associated_bonding_curve": get_account_key(3),
        "user": get_account_key(5),
        "token_standard": "token2022",
    }

    # CreateV2 trailing args: is_mayhem_mode (bool, 1B), is_cashback_enabled
    # (OptionBool, 1B). Either may be truncated off the wire; a missing one is
    # left out of the dict rather than reported as False.
    if offset < len(ix_data):
        token_info["is_mayhem_mode"] = bool(ix_data[offset])
        offset += 1
    if offset < len(ix_data):
        token_info["is_cashback_enabled"] = bool(ix_data[offset])

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

        keys = resolve_account_keys(update.transaction.transaction)

        # Check each instruction in the transaction
        for ix in msg.instructions:
            # Check for both Create and CreateV2 instructions
            is_create = ix.data.startswith(PUMP_CREATE_PREFIX)
            is_create_v2 = ix.data.startswith(PUMP_CREATE_V2_PREFIX)

            if not (is_create or is_create_v2):
                continue

            # Decode based on instruction type
            if is_create_v2:
                info = decode_create_v2_instruction(ix.data, keys, ix.accounts)
            else:
                info = decode_create_instruction(ix.data, keys, ix.accounts)

            # Extract transaction signature
            signature = base58.b58encode(
                bytes(update.transaction.transaction.signature)
            ).decode()

            # Print token information in consistent format
            print_token_info(
                info,
                signature=signature,
                envelope=describe_envelope(update.transaction.transaction),
            )


if __name__ == "__main__":
    asyncio.run(monitor_pump())

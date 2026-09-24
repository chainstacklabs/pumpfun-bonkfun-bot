"""Monitor Solana for new pump.fun coins on the pre-execution deshred stream.

Decodes `create` instructions the moment entries are formed from shreds, before
the transaction runs.

Usage:
    uv run cookbook/pumpfun/listen/pumpfun_listen_tokens_deshred.py

Companion to `pumpfun_listen_tokens_geyser.py`, which reads the ordinary executed
stream. The two are different RPCs on the same endpoint, and the trade-off
between them is the point of this example.

`SubscribeDeshred` delivers a transaction as entries form from shreds, BEFORE any
execution. Raced against `Subscribe` on the pump.fun program it carries the same
signatures with no orphans either way, and on creates it usually arrives first;
`tools/compare_deshred_latency.py` reproduces that comparison.

What you give up:

  - **A coin created through a router is invisible.** The create reaches the
    program as a CPI, and inner instructions are produced *by* execution, so a
    pre-execution stream never carries them — undetectable, not dropped, and no
    decoding recovers them. The executed listeners walk `meta.innerInstructions`
    and do not have this blind spot.
  - No TransactionStatusMeta, so no `meta.log_messages` and no CreateEvent. This
    script decodes the create instruction instead, the fallback route elsewhere
    in this repo. The creator it prints is `args.creator`, which is user-supplied
    and may differ from the canonical `BondingCurve.creator`; read the curve if
    you need the real one.
  - No success or failure, by construction. The executed stream filters with
    `failed = False`; here you see every shredded transaction, landed or not.

So this is a latency demo, not a better listener: a few milliseconds earlier on
most coins, in exchange for never seeing a few percent of them.

Address lookup tables are already resolved, reported as loaded_writable_addresses
then loaded_readonly_addresses (that order), so instruction account indices
resolve as they do on the executed stream.

Geyser gRPC reference:
https://docs.triton.one/project-yellowstone/dragons-mouth-grpc-subscriptions

Authentication: Basic or X-Token, via GEYSER_ENDPOINT, GEYSER_API_TOKEN and
GEYSER_AUTH_TYPE.
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
# Run cookbook/solana/anchor_calculate_discriminator.py to recompute them.
PUMP_CREATE_PREFIX = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_PREFIX = bytes([214, 144, 76, 236, 95, 139, 49, 180])


def print_token_info(token_data: dict, signature: str, slot: int) -> None:
    """Print token information in a consistent, user-friendly format.

    Args:
        token_data: Dictionary containing token fields
        signature: Transaction signature
        slot: Slot the entry was shredded in
    """
    print("\n" + "=" * 80)
    print("🎯 NEW TOKEN DETECTED (pre-execution)")
    print("=" * 80)
    print(f"Name:             {token_data.get('name', 'N/A')}")
    print(f"Symbol:           {token_data.get('symbol', 'N/A')}")
    print(f"Mint:             {token_data.get('mint', 'N/A')}")
    print(f"Bonding Curve:    {token_data.get('bonding_curve', 'N/A')}")
    print(f"Associated BC:    {token_data.get('associated_bonding_curve', 'N/A')}")
    print(f"User:             {token_data.get('user', 'N/A')}")
    # From the instruction args, not the curve: may differ from BondingCurve.creator.
    print(f"Creator (args):   {token_data.get('creator', 'N/A')}")
    print(f"Token Standard:   {token_data.get('token_standard', 'N/A')}")
    print(f"Mayhem Mode:      {token_data.get('is_mayhem_mode', 'N/A')}")
    print(f"URI:              {token_data.get('uri', 'N/A')}")
    print(f"Tx version:       {token_data.get('tx_version', 'N/A')}")
    print(f"Slot:             {slot}")
    print(f"Signature:        {signature}")
    print("Outcome:          unknown - nothing has executed yet")
    print("=" * 80 + "\n")


async def create_geyser_connection() -> geyser_pb2_grpc.GeyserStub:
    """Establish a secure connection to the Geyser endpoint using the configured auth type.

    Returns:
        A Geyser stub bound to an authenticated channel

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


def create_subscription_request() -> geyser_pb2.SubscribeDeshredRequest:
    """Create a deshred subscription request for Pump.fun transactions.

    This is its own request type, not a SubscribeRequest: the deshred stream has
    no commitment level, because commitment describes execution and nothing has
    executed. There is also no `failed` filter for the same reason.

    Returns:
        The request to open the stream with
    """
    request = geyser_pb2.SubscribeDeshredRequest()
    request.deshred_transactions["pump_filter"].account_include.append(
        str(PUMP_PROGRAM_ID)
    )
    request.deshred_transactions["pump_filter"].vote = False
    return request


def resolve_account_keys(
    tx: geyser_pb2.SubscribeUpdateDeshredTransactionInfo,
) -> list[bytes]:
    """Build the full account-key table for one deshred transaction.

    A v0 transaction indexes accounts past the end of `message.account_keys` when
    it uses an address lookup table. The deshred stream resolves those for you and
    reports them on the update itself rather than under a meta, in this exact
    order: static keys, then writable loaded, then read-only loaded.

    Args:
        tx: A geyser `SubscribeUpdateDeshredTransactionInfo`

    Returns:
        Account keys as raw 32-byte values, indexable by an instruction's account list
    """
    keys = list(tx.transaction.message.account_keys)
    keys.extend(tx.loaded_writable_addresses)
    keys.extend(tx.loaded_readonly_addresses)
    return keys


def decode_create_instruction(
    ix_data: bytes, keys: list[bytes], accounts: bytes, *, is_v2: bool
) -> dict:
    """Decode a create or create_v2 instruction from its raw data.

    Args:
        ix_data: Instruction data, starting with the 8-byte discriminator
        keys: Full account-key table from `resolve_account_keys`
        accounts: The instruction's account indices
        is_v2: True for `create_v2` (Token2022), False for legacy `create`

    Returns:
        Dictionary of decoded token fields
    """
    offset = 8  # Skip past the 8-byte discriminator prefix

    def get_account_key(index: int) -> str:
        if index >= len(accounts):
            return "N/A"
        account_index = accounts[index]
        if account_index >= len(keys):
            return "N/A"
        return base58.b58encode(keys[account_index]).decode()

    def read_string() -> str:
        nonlocal offset
        length = struct.unpack_from("<I", ix_data, offset)[0]
        offset += 4
        value = ix_data[offset : offset + length].decode()
        offset += length
        return value

    def read_pubkey() -> str:
        nonlocal offset
        value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
        offset += 32
        return value

    name = read_string()
    symbol = read_string()
    uri = read_string()
    creator = read_pubkey()

    if not is_v2:
        return {
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "creator": creator,
            "mint": get_account_key(0),
            "bonding_curve": get_account_key(2),
            "associated_bonding_curve": get_account_key(3),
            "user": get_account_key(7),
            "token_standard": "legacy",
        }

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

    # create_v2 trailing args are positional and may be truncated on the wire.
    # A missing one is left out of the dict: the caller prints "N/A" for it,
    # rather than a False the instruction never carried.
    if offset < len(ix_data):
        token_info["is_mayhem_mode"] = bool(ix_data[offset])
        offset += 1

    return token_info


def describe_version(message: geyser_pb2.Message) -> str:
    """Name the transaction format a message is in.

    `config` is set only for transaction v1 (SIMD-0385), so its presence is what
    separates v1 from v0 -- the `versioned` flag is true for both.

    Args:
        message: The transaction's message

    Returns:
        "v1", "v0" or "legacy"
    """
    if message.HasField("config"):
        return "v1"
    return "v0" if message.versioned else "legacy"


async def monitor_pump() -> None:
    """Monitor the deshred stream for new Pump.fun token creations."""
    print(f"Starting Pump.fun deshred monitor using {AUTH_TYPE.upper()} authentication")
    print("Reading pre-execution transactions: outcomes are not yet known.\n")
    stub = await create_geyser_connection()
    request = create_subscription_request()

    async for update in stub.SubscribeDeshred(iter([request])):
        if not update.HasField("deshred_transaction"):
            continue

        tx = update.deshred_transaction.transaction
        msg = getattr(tx.transaction, "message", None)
        if msg is None:
            continue

        keys = resolve_account_keys(tx)

        for ix in msg.instructions:
            is_create = ix.data.startswith(PUMP_CREATE_PREFIX)
            is_create_v2 = ix.data.startswith(PUMP_CREATE_V2_PREFIX)
            if not (is_create or is_create_v2):
                continue

            info = decode_create_instruction(
                ix.data, keys, ix.accounts, is_v2=is_create_v2
            )
            info["tx_version"] = describe_version(msg)
            signature = base58.b58encode(bytes(tx.signature)).decode()
            print_token_info(info, signature, update.deshred_transaction.slot)


if __name__ == "__main__":
    asyncio.run(monitor_pump())

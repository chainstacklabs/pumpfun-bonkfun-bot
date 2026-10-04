"""Listen for new StonkFun coins over Geyser gRPC.

Usage:
    uv run cookbook/stonkfun/listen/stonkfun_listen_tokens_geyser.py

StonkFun has no program of its own: a launch is a Raydium LaunchLab
`initialize_with_token_2022` that names one of StonkFun's two platform configs.
Subscribing to those two accounts instead of to the whole LaunchLab program
keeps every other launchpad on LaunchLab out of the stream. Trades touch the
configs too, so most of what arrives is trades, and the decoder skips them.

Everything printed comes from the instruction itself — no extra RPC call:
the accounts give the mint, pool, quote asset and creator, and the arguments
give the name, the curve and the transfer fee.

Geyser gRPC Reference:
https://docs.triton.one/rpc-pool/grpc-subscriptions

Authentication: Supports both Basic and X-Token authentication methods.
Configure via GEYSER_ENDPOINT, GEYSER_API_TOKEN and GEYSER_AUTH_TYPE.
"""

import asyncio
import os
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

LAUNCHLAB_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
STONKFUN_PLATFORM_CONFIGS = {
    Pubkey.from_string("4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"): "standard",
    Pubkey.from_string("6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"): "reward",
}

# Anchor discriminator: the first 8 bytes of the instruction name's hash.
INITIALIZE_WITH_TOKEN_2022 = bytes([37, 190, 126, 222, 44, 154, 171, 17])

# Account positions in initialize_with_token_2022, from the IDL. The payer and
# the creator are separate slots: the pool's creator, and the fee vault keyed
# to it, come from index 1, not from whoever paid.
PAYER, CREATOR, GLOBAL_CONFIG, PLATFORM_CONFIG = 0, 1, 2, 3
POOL_STATE, BASE_MINT, QUOTE_MINT = 5, 6, 7
BASE_VAULT, QUOTE_VAULT, QUOTE_TOKEN_PROGRAM = 8, 9, 11

CURVE_TYPES = {0: "constant product", 1: "fixed price", 2: "linear"}


def resolve_account_keys(tx: geyser_pb2.SubscribeUpdateTransactionInfo) -> list[bytes]:
    """All account keys an instruction can index: static, then lookup-table ones.

    Launches sent as v0 transactions can put accounts behind an address lookup
    table; geyser reports those in the meta, writable first, then read-only.
    """
    keys = list(tx.transaction.message.account_keys)
    meta = getattr(tx, "meta", None)
    if meta is not None:
        keys.extend(meta.loaded_writable_addresses)
        keys.extend(meta.loaded_readonly_addresses)
    return keys


def decode_launch(data: bytes, accounts: bytes, keys: list[bytes]) -> dict | None:
    """Decode an initialize_with_token_2022 instruction, or None if it is not one."""
    if (
        not data.startswith(INITIALIZE_WITH_TOKEN_2022)
        or len(accounts) <= QUOTE_TOKEN_PROGRAM
    ):
        return None
    if max(accounts) >= len(keys):
        return None

    def account(position: int) -> Pubkey:
        return Pubkey.from_bytes(keys[accounts[position]])

    mode = STONKFUN_PLATFORM_CONFIGS.get(account(PLATFORM_CONFIG))
    if mode is None:
        return None

    offset = 8

    def read(fmt: str) -> Any:
        nonlocal offset
        values = struct.unpack_from(fmt, data, offset)
        offset += struct.calcsize(fmt)
        return values if len(values) > 1 else values[0]

    def read_string() -> str:
        nonlocal offset
        length = read("<I")
        value = data[offset : offset + length].decode(errors="replace")
        offset += length
        return value

    decimals = read("<B")
    name, symbol, uri = read_string(), read_string(), read_string()
    curve_type = read("<B")
    if curve_type == 0:
        supply, total_base_sell, raise_target, _migrate = read("<QQQB")
    else:
        supply, raise_target, _migrate = read("<QQB")
        total_base_sell = None
    read("<QQQ")  # vesting: locked amount, cliff, unlock period
    read("<B")  # which side the migrated pool's creator fee is taken on
    transfer_fee_bps = read("<H") if read("<B") == 1 else 0

    return {
        "mode": mode,
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "mint": account(BASE_MINT),
        "pool": account(POOL_STATE),
        "quote_mint": account(QUOTE_MINT),
        "quote_token_program": account(QUOTE_TOKEN_PROGRAM),
        "creator": account(CREATOR),
        "payer": account(PAYER),
        "global_config": account(GLOBAL_CONFIG),
        "decimals": decimals,
        "curve": CURVE_TYPES.get(curve_type, str(curve_type)),
        "supply": supply,
        "total_base_sell": total_base_sell,
        "raise_target": raise_target,
        "transfer_fee_bps": transfer_fee_bps,
    }


def instructions_of(tx: geyser_pb2.SubscribeUpdateTransactionInfo) -> Iterator:
    """Top-level instructions, then the inner ones a router may have issued."""
    yield from tx.transaction.message.instructions
    meta = getattr(tx, "meta", None)
    if meta is not None:
        for group in meta.inner_instructions:
            yield from group.instructions


def print_launch(launch: dict, signature: str) -> None:
    """Print one launch."""
    unit = 10 ** launch["decimals"]
    print("\n" + "=" * 80)
    print(f"NEW STONKFUN COIN ({launch['mode']})")
    print("=" * 80)
    print(f"Name:           {launch['name']} ({launch['symbol']})")
    print(f"Mint:           {launch['mint']}")
    print(f"Pool:           {launch['pool']}")
    print(f"Quote asset:    {launch['quote_mint']}")
    print(f"Creator:        {launch['creator']}")
    if launch["payer"] != launch["creator"]:
        print(f"Paid by:        {launch['payer']}")
    print(f"Curve:          {launch['curve']}, {launch['supply'] / unit:,.0f} supply")
    if launch["total_base_sell"] is not None:
        print(f"Sold on curve:  {launch['total_base_sell'] / unit:,.0f}")
    print(f"Raise target:   {launch['raise_target']} raw quote units")
    print(f"Transfer tax:   {launch['transfer_fee_bps'] / 100:.2f}%")
    print(f"URI:            {launch['uri']}")
    print(f"Signature:      {signature}")
    print("=" * 80)


async def create_geyser_connection() -> geyser_pb2_grpc.GeyserStub:
    """Open a Geyser stub using the configured auth type.

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


def create_subscription_request() -> geyser_pb2.SubscribeRequest:
    """Subscribe to successful transactions touching either StonkFun platform config."""
    request = geyser_pb2.SubscribeRequest()
    stonkfun = request.transactions["stonkfun"]
    stonkfun.account_include.extend(str(config) for config in STONKFUN_PLATFORM_CONFIGS)
    stonkfun.failed = False
    request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return request


async def monitor_stonkfun() -> None:
    """Print every StonkFun launch as it happens."""
    print(f"Watching StonkFun launches using {AUTH_TYPE.upper()} authentication")
    stub = await create_geyser_connection()

    async for update in stub.Subscribe(iter([create_subscription_request()])):
        if not update.HasField("transaction"):
            continue
        tx = update.transaction.transaction
        keys = resolve_account_keys(tx)
        for ix in instructions_of(tx):
            if ix.program_id_index >= len(keys):
                continue
            if Pubkey.from_bytes(keys[ix.program_id_index]) != LAUNCHLAB_PROGRAM:
                continue
            launch = decode_launch(bytes(ix.data), bytes(ix.accounts), keys)
            if launch:
                print_launch(launch, base58.b58encode(bytes(tx.signature)).decode())


if __name__ == "__main__":
    asyncio.run(monitor_stonkfun())

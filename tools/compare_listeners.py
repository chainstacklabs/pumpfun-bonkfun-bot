"""Race the pump.fun token-detection methods against each other.

Compares four listeners in real time and reports which detects each coin first,
with per-method latency, message counts and coverage:

1. `blockSubscribe` — whole blocks mentioning the program; slowest.
   https://solana.com/docs/rpc/websocket/blocksubscribe
2. `logsSubscribe` — program logs; the event data carries every field.
   https://solana.com/docs/rpc/websocket/logssubscribe
3. Geyser gRPC — Yellowstone Dragon's Mouth streaming, post-execution.
   https://docs.triton.one/rpc-pool/grpc-subscriptions
4. Geyser `SubscribeDeshred` — the same endpoint pre-execution; earliest, and
   the only lane that cannot see a coin created through a router.

This races on *coverage*: which lane saw a given mint, and how much later than
the winner. A lane that never reports a mint is the finding, not a rounding
error — `shreds` misses router creates. Every lane is restricted to pump.fun
coins.
`tools/compare_deshred_latency.py` answers the other question, pairing deshred
against executed per signature to measure the lead itself.

Read-only. No funds are moved and nothing is submitted.

Configuration: set provider endpoints in `.env`, or edit the providers dict at
the bottom of this file. Every lane starts automatically for a provider that
has the endpoint it needs: `wss` gives blocks and logs, `geyser` gives both the
executed and the deshred stream.

Usage:
    uv run tools/compare_listeners.py
    uv run tools/compare_listeners.py --duration 600
"""

import argparse
import asyncio
import base64
import json
import multiprocessing as mp
import os
import struct
import sys
import time
from pathlib import Path
from queue import Empty

import base58
import grpc
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey

# Reach the shared geyser stubs in src/geyser/generated (imported lazily below).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(override=True)

# Solana's blockSubscribe (and a busy logsSubscribe) sends frames well past
# websockets' 1 MiB default, which kills the connection with a 1009 close
# instead of delivering the message. Same value the bot's own listeners use.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024

# ============ CONSTANTS ============

# Pump.fun program ID
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: cookbook/solana/anchor_calculate_discriminator.py
PUMP_CREATE_PREFIX = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_PREFIX = bytes([214, 144, 76, 236, 95, 139, 49, 180])

# Event discriminator for CreateEvent (8-byte identifier)
# This is emitted by both Create and CreateV2 instructions
# Calculated using the first 8 bytes of sha256("event:CreateEvent")
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])

# PDA ["mint-authority"], a constant. Account 1 of both create and create_v2 and
# absent from every trade instruction, so a deshred subscription filtered on it
# yields creations only. Filtering on the program id instead delivers every
# pump.fun transaction pre-execution, which is enough volume to make a Python
# consumer lag until the server ends the stream.
PUMP_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"

# A lane detecting less than this share of the best lane's coins is called out
# in the summary. Lanes normally land within a few percent of each other, so a
# lane at a third of the best is reporting a fault, not a slower feed.
STARVED_LANE_SHARE = 0.6

# Seconds to keep draining the queue after the sampling deadline, so a lane's
# final frame count is not lost to the shutdown.
LANE_SHUTDOWN_GRACE = 10

# The two log lines the pump.fun program writes when it creates a coin.
CREATE_LOG = "Program log: Instruction: Create"
CREATE_V2_LOG = "Program log: Instruction: CreateV2"

# Sampling window in seconds, overridable with --duration. Short runs see few
# coins, and a lane's coverage gap only shows up over enough of them.
DEFAULT_DURATION = 60

GEYSER_AUTH_TYPE = os.getenv("GEYSER_AUTH_TYPE", "x-token").lower()

BAD_AUTH_TYPE_MSG = "GEYSER_AUTH_TYPE must be 'x-token' or 'basic'"


class DetectionTracker:
    """Tracks and analyzes detection times for both methods across providers"""

    def __init__(self):
        self.tokens = {}  # {mint: {provider: timestamp}}
        self.messages = {}  # {provider: count}
        self.start_time = time.time()

    def add_token(self, mint, name, symbol, provider, timestamp):
        """Record a token detection event"""
        if mint not in self.tokens:
            self.tokens[mint] = {"name": name, "symbol": symbol, "detections": {}}
        self.tokens[mint]["detections"][provider] = timestamp
        print(
            f"[TOKEN] mint={mint} name={name} symbol={symbol} provider={provider} time={timestamp:.3f}"
        )

    def add_messages(self, lane, count):
        """Add to a lane's frame count.

        Keyed per lane, not per provider: a provider's lanes hold separate
        subscriptions, and a lane falling behind is only visible against the
        others' counts.
        """
        self.messages[lane] = self.messages.get(lane, 0) + count

    def print_summary(self):
        """Print detailed summary statistics of the comparison test"""
        test_duration = time.time() - self.start_time

        total_messages = sum(self.messages.values())

        print("\n=== Test Summary ===")
        print(f"Test duration: {test_duration:.2f} seconds")
        print(f"Messages received: {total_messages}")

        # Count unique tokens detected by each provider
        provider_tokens = {}
        all_providers = set()
        for mint, token_data in self.tokens.items():
            providers = token_data["detections"].keys()
            all_providers.update(providers)
            for provider in providers:
                if provider not in provider_tokens:
                    provider_tokens[provider] = 0
                provider_tokens[provider] += 1

        print(f"Tokens detected: {len(self.tokens)}")
        for provider, count in sorted(provider_tokens.items()):
            print(f"  - {provider}: {count}")

        print("\n=== Lane Throughput And Coverage ===")
        print("Lane                   | Frames   | Frames/s | Coins | vs best")
        print("-" * 64)
        best = max(provider_tokens.values(), default=0)
        for lane in sorted(set(self.messages) | set(provider_tokens)):
            frames = self.messages.get(lane, 0)
            coins = provider_tokens.get(lane, 0)
            share = (coins / best * 100) if best else 0.0
            rate = frames / test_duration if test_duration else 0.0
            print(
                f"{lane:<22} | {frames:<8} | {rate:>8.1f} | {coins:>5} | {share:>5.0f}%"
            )

        starved = sorted(
            lane
            for lane, coins in provider_tokens.items()
            if best and coins / best < STARVED_LANE_SHARE
        )
        if starved:
            print(f"\n[WARN] Far below the best lane: {', '.join(starved)}")
            print(
                "       Read that lane's Frames figure before calling this a coverage"
            )
            print("       gap: a lane starved of frames also detects fewer coins.")
        print()

        print("=== Token Detection Provider Performance ===")
        self._print_provider_performance()

        # Print token details
        print("\n=== Detected Tokens ===")
        print(
            "Mint                                         | Name             | Symbol | First Provider  | Detected By"
        )
        print("-" * 100)

        for mint, token_data in sorted(
            self.tokens.items(), key=lambda x: min(x[1]["detections"].values())
        ):
            name = token_data["name"][:15]  # Truncate long names
            symbol = token_data["symbol"][:6]  # Truncate long symbols

            first_provider = min(token_data["detections"].items(), key=lambda x: x[1])[
                0
            ]

            # Get list of providers that detected this token
            providers = ", ".join(sorted(token_data["detections"].keys()))

            print(
                f"{mint} | {name:<16} | {symbol:<6} | {first_provider:<14} | {providers}"
            )

    def _print_provider_performance(self):
        """Print performance metrics for providers"""
        # Count how many times each provider was first
        first_count = {}
        total_tokens = len(self.tokens)

        for mint, token_data in self.tokens.items():
            detections = token_data["detections"]
            if not detections:
                continue

            # Find the fastest provider for this token
            fastest_provider = min(detections.items(), key=lambda x: x[1])[0]
            if fastest_provider not in first_count:
                first_count[fastest_provider] = 0
            first_count[fastest_provider] += 1

        if not first_count:
            print("No tokens detected")
            return

        # Print rankings
        print("Provider                | First Detections | Percentage")
        print("-" * 60)

        for provider, count in sorted(
            first_count.items(), key=lambda x: x[1], reverse=True
        ):
            percentage = (count / total_tokens) * 100 if total_tokens > 0 else 0
            print(f"{provider:<22} | {count:<16} | {percentage:.1f}%")

        # Calculate average latency between providers
        self._print_provider_latency_matrix()

    def _print_provider_latency_matrix(self):
        """Print a matrix of average latency between providers"""
        # Get unique providers
        all_providers = set()
        for token_data in self.tokens.values():
            all_providers.update(token_data["detections"].keys())

        if len(all_providers) <= 1:
            return

        providers_list = sorted(all_providers)

        # Calculate column width based on longest provider name
        max_provider_len = max(len(provider) for provider in providers_list)
        col_width = max(max_provider_len, 8)  # Minimum 8 for latency values

        print("\nAverage Latency Matrix (ms):")

        # Print header
        header = f"{'':>{col_width}} |"
        for provider in providers_list:
            header += f" {provider:>{col_width}} |"
        print(header)
        print("-" * len(header))

        # Calculate and print latency matrix
        for provider1 in providers_list:
            row = f"{provider1:>{col_width}} |"
            for provider2 in providers_list:
                if provider1 == provider2:
                    row += f" {'—':>{col_width}} |"
                    continue

                # Calculate average latency
                latencies = []
                for token_data in self.tokens.values():
                    detections = token_data["detections"]
                    if provider1 in detections and provider2 in detections:
                        latency_ms = (
                            detections[provider2] - detections[provider1]
                        ) * 1000
                        latencies.append(latency_ms)

                if latencies:
                    avg_latency = sum(latencies) / len(latencies)
                    row += f" {avg_latency:>+{col_width}.1f} |"
                else:
                    row += f" {'?':>{col_width}} |"
            print(row)


# ============ TOKEN DETECTION METHODS ============


async def fetch_existing_token_mints():
    """Fetch existing token mints to avoid duplicate detections"""
    # You could implement this by querying a known database or API
    # For simplicity, we'll return an empty set
    return set()


def decode_create_instruction(ix_data, account_keys):
    """Decode legacy Create instruction (Metaplex tokens) from instruction data."""
    if len(ix_data) < 8:
        return None

    offset = 8  # Skip discriminator
    parsed_data = {}

    try:
        # Read string fields from instruction data
        def read_string():
            nonlocal offset
            length = struct.unpack("<I", ix_data[offset : offset + 4])[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
            return value

        def read_pubkey():
            nonlocal offset
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
            return value

        # Parse instruction arguments
        parsed_data["name"] = read_string()
        parsed_data["symbol"] = read_string()
        parsed_data["uri"] = read_string()
        parsed_data["creator"] = read_pubkey()

        # Extract accounts from account_keys array
        if len(account_keys) >= 8:
            parsed_data["mint"] = account_keys[0]
            parsed_data["bondingCurve"] = account_keys[2]
            parsed_data["user"] = account_keys[7]
        elif len(account_keys) > 0:
            parsed_data["mint"] = account_keys[0]

        parsed_data["token_standard"] = "legacy"
        parsed_data["is_mayhem_mode"] = False
        return parsed_data
    except Exception as e:
        print(f"[ERROR] Failed to decode create instruction: {e}")
        return None


def decode_create_v2_instruction(ix_data, account_keys):
    """Decode CreateV2 instruction (Token2022 tokens) from instruction data."""
    if len(ix_data) < 8:
        return None

    offset = 8  # Skip discriminator
    parsed_data = {}

    try:
        # Read string fields from instruction data
        def read_string():
            nonlocal offset
            length = struct.unpack("<I", ix_data[offset : offset + 4])[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
            return value

        def read_pubkey():
            nonlocal offset
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
            return value

        # Parse instruction arguments
        parsed_data["name"] = read_string()
        parsed_data["symbol"] = read_string()
        parsed_data["uri"] = read_string()
        parsed_data["creator"] = read_pubkey()

        # CreateV2 trailing args: is_mayhem_mode (bool, 1B), is_cashback_enabled (OptionBool, 1B)
        if offset < len(ix_data):
            parsed_data["is_mayhem_mode"] = bool(ix_data[offset])
            offset += 1
        else:
            parsed_data["is_mayhem_mode"] = False
        if offset < len(ix_data):
            parsed_data["is_cashback_enabled"] = bool(ix_data[offset])
        else:
            parsed_data["is_cashback_enabled"] = False

        # Extract accounts from account_keys array
        if len(account_keys) >= 6:
            parsed_data["mint"] = account_keys[0]
            parsed_data["bondingCurve"] = account_keys[2]
            parsed_data["user"] = account_keys[5]
        elif len(account_keys) > 0:
            parsed_data["mint"] = account_keys[0]

        parsed_data["token_standard"] = "token2022"
        return parsed_data
    except Exception as e:
        print(f"[ERROR] Failed to decode create v2 instruction: {e}")
        return None


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
    """Parse a CreateEvent payload (same on-chain layout for create and create_v2)."""
    if len(data) < 8:
        return None
    offset = 8
    parsed_data = {}
    try:
        for field_name, field_type in _CREATE_EVENT_FIELDS:
            if field_type == "string":
                if offset + 4 > len(data):
                    raise ValueError(
                        f"Not enough data for {field_name} length at offset {offset}"
                    )
                length = struct.unpack("<I", data[offset : offset + 4])[0]
                offset += 4
                if offset + length > len(data):
                    raise ValueError(
                        f"Not enough data for {field_name} value (length={length}) at offset {offset}"
                    )
                value = data[offset : offset + length].decode("utf-8")
                offset += length
            elif field_type == "publicKey":
                if offset + 32 > len(data):
                    raise ValueError(
                        f"Not enough data for {field_name} at offset {offset}"
                    )
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
        print(f"[ERROR] Failed to parse create event: {e}")
        return None


def parse_create_event(data):
    """Parse CreateEvent emitted by legacy Create instruction."""
    return _parse_create_event(data, token_standard_hint="legacy")


def parse_create_v2_event(data):
    """Parse CreateEvent emitted by CreateV2 instruction (Token2022 tokens)."""
    return _parse_create_event(data, token_standard_hint="token2022")


def is_transaction_successful(logs):
    """Check if a transaction was successful based on log messages"""
    for log in logs:
        if "AnchorError thrown" in log or "Error" in log:
            return False
    return True


# ============ WEBSOCKET LISTENERS ============


async def listen_block_subscription(wss_url, provider_name, tracker, known_tokens=None):
    """Listen for new tokens via block subscription"""
    if known_tokens is None:
        known_tokens = set()

    lane = f"{provider_name}_block"

    while True:
        try:
            print(f"[INFO] Connecting block listener to {provider_name}...")
            async with websockets.connect(
                wss_url, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
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
                await websocket.recv()
                print(f"[INFO] Block listener active for {provider_name}")

                while True:
                    try:
                        response = await websocket.recv()
                        data = json.loads(response)
                        tracker.increment_messages(lane)

                        if data.get("method") != "blockNotification":
                            continue

                        block_data = data["params"]["result"]
                        if (
                            "value" not in block_data
                            or "block" not in block_data["value"]
                        ):
                            continue

                        # `block` is null for a skipped or unavailable slot,
                        # which would make the membership test raise TypeError
                        # and cost this lane the whole notification.
                        block = block_data["value"]["block"]
                        if not block or "transactions" not in block:
                            continue

                        for tx in block["transactions"]:
                            if not isinstance(tx, dict) or "transaction" not in tx:
                                continue

                            # Route on meta.logMessages, which the RPC has
                            # already decoded and which reads the same for every
                            # transaction version. Deserializing the envelope
                            # here makes this lane miss whatever version solders
                            # cannot read, and report the gap as a speed
                            # difference against logs and geyser.
                            meta = tx.get("meta") or {}
                            logs = meta.get("logMessages") or []
                            if not (CREATE_LOG in logs or CREATE_V2_LOG in logs):
                                continue

                            decoded = None
                            for log in logs:
                                if "Program data:" not in log:
                                    continue
                                try:
                                    payload = base64.b64decode(log.split(": ", 1)[1])
                                except (ValueError, IndexError):
                                    continue
                                if payload[:8] != CREATE_EVENT_DISCRIMINATOR:
                                    continue
                                decoded = parse_create_event(payload)
                                break

                            if not decoded:
                                continue

                            mint = decoded.get("mint")
                            if not mint or mint in known_tokens:
                                continue

                            is_v2 = CREATE_V2_LOG in logs
                            kind = (
                                "CreateV2 (Token2022)" if is_v2 else "Create (Legacy)"
                            )
                            print(f"[{provider_name}_block] Detected: {kind}")

                            try:
                                tracker.add_token(
                                    mint,
                                    decoded["name"],
                                    decoded["symbol"],
                                    lane,
                                    time.time(),
                                )
                                known_tokens.add(mint)
                            except Exception as e:
                                print(
                                    f"[ERROR] Failed to process block instruction: {e}"
                                )

                    except websockets.ConnectionClosed:
                        # Break out so the outer loop reconnects. Without this the
                        # broad handler below swallows the disconnect and recv()
                        # raises again immediately, spinning at millions of
                        # iterations per minute.
                        print(
                            f"[WARN] Block listener for {provider_name}: connection closed"
                        )
                        break
                    except Exception as e:
                        print(f"[ERROR] Block listener for {provider_name}: {e}")

        except Exception as e:
            print(
                f"[ERROR] Connection error in block listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def listen_logs_subscription(wss_url, provider_name, tracker, known_tokens=None):
    """Listen for new tokens via logs subscription"""
    if known_tokens is None:
        known_tokens = set()

    lane = f"{provider_name}_logs"

    while True:
        try:
            print(f"[INFO] Connecting logs listener to {provider_name}...")
            async with websockets.connect(
                wss_url, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
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
                await websocket.recv()
                print(f"[INFO] Logs listener active for {provider_name}")

                while True:
                    try:
                        response = await websocket.recv()
                        data = json.loads(response)
                        tracker.increment_messages(lane)

                        if data.get("method") != "logsNotification":
                            continue

                        log_data = data["params"]["result"]["value"]
                        logs = log_data.get("logs", [])

                        # Detect both Create and CreateV2 instructions. The
                        # lines are matched whole: Anchor writes
                        # `Instruction: <Name>` for every program, so a
                        # substring test also accepts CreateTokenAccount,
                        # CreatePool and the rest from whichever programs share
                        # the transaction.
                        is_create = CREATE_LOG in logs
                        is_create_v2 = CREATE_V2_LOG in logs

                        if not (is_create or is_create_v2):
                            continue

                        for log in logs:
                            if "Program data:" in log:
                                try:
                                    encoded_data = log.split(": ")[1]
                                    data_bytes = base64.b64decode(encoded_data)

                                    # Check if this is a CreateEvent by validating discriminator
                                    if len(data_bytes) < 8:
                                        continue

                                    event_discriminator = data_bytes[:8]
                                    if (
                                        event_discriminator
                                        != CREATE_EVENT_DISCRIMINATOR
                                    ):
                                        # Skip non-CreateEvent logs (e.g., TradeEvent, ExtendAccountEvent)
                                        continue

                                    # Parse based on instruction type
                                    if is_create_v2:
                                        print(
                                            f"[{provider_name}_logs] Detected: CreateV2 instruction (Token2022)"
                                        )
                                        parsed = parse_create_v2_event(data_bytes)
                                    else:
                                        print(
                                            f"[{provider_name}_logs] Detected: Create instruction (Legacy/Metaplex)"
                                        )
                                        parsed = parse_create_event(data_bytes)

                                    if not parsed:
                                        continue

                                    mint = parsed.get("mint")
                                    if not mint:
                                        continue
                                    if mint in known_tokens:
                                        continue

                                    ts = time.time()
                                    tracker.add_token(
                                        mint,
                                        parsed.get("name", "Unknown"),
                                        parsed.get("symbol", "UNK"),
                                        lane,
                                        ts,
                                    )
                                    known_tokens.add(mint)
                                    break
                                except Exception as e:
                                    print(f"[ERROR] Failed to decode Program data: {e}")

                    except Exception as e:
                        print(f"[ERROR] Logs listener for {provider_name}: {e}")
                        break

        except Exception as e:
            print(f"[ERROR] Connection error in logs listener for {provider_name}: {e}")
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


def build_geyser_credentials(api_token):
    """Build channel credentials for the configured Geyser auth type.

    Args:
        api_token: The token or basic-auth blob from the environment

    Returns:
        Composite channel credentials

    Raises:
        ValueError: If GEYSER_AUTH_TYPE is neither "x-token" nor "basic"
    """
    if GEYSER_AUTH_TYPE == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _context, callback: callback((("x-token", api_token),), None)
        )
    elif GEYSER_AUTH_TYPE == "basic":
        auth = grpc.metadata_call_credentials(
            lambda _context, callback: callback(
                (("authorization", f"Basic {api_token}"),), None
            )
        )
    else:
        raise ValueError(BAD_AUTH_TYPE_MSG)

    return grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)


async def listen_geyser_grpc(
    endpoint, api_token, provider_name, tracker, known_tokens=None
):
    """Listen for new tokens via Geyser gRPC API"""
    try:
        # Generated once into src/geyser/generated; see docs/listeners-and-geyser.md.
        from src.geyser.generated import geyser_pb2, geyser_pb2_grpc
    except ImportError:
        print(
            "[ERROR] Could not import geyser_pb2 or geyser_pb2_grpc. "
            "Regenerate them into src/geyser/generated from src/geyser/proto"
        )
        return

    if known_tokens is None:
        known_tokens = set()

    lane = f"{provider_name}_geyser"

    # Built once, outside the retry loop: the reconnect handler below catches
    # every exception and sleeps, so a bad auth type raised in there would
    # reconnect forever instead of reporting a misconfiguration.
    creds = build_geyser_credentials(api_token)

    while True:
        try:
            print(f"[INFO] Connecting Geyser gRPC listener to {provider_name}...")

            channel = grpc.aio.secure_channel(endpoint, creds)
            stub = geyser_pb2_grpc.GeyserStub(channel)

            request = geyser_pb2.SubscribeRequest()
            request.transactions["pump_filter"].account_include.append(
                str(PUMP_PROGRAM_ID)
            )
            request.transactions["pump_filter"].failed = False
            request.commitment = geyser_pb2.CommitmentLevel.PROCESSED

            print(f"[INFO] Geyser gRPC listener active for {provider_name}")

            async for update in stub.Subscribe(iter([request])):
                tracker.increment_messages(lane)

                # Skip non-transaction updates
                if not update.HasField("transaction"):
                    continue

                tx = update.transaction.transaction.transaction
                msg = getattr(tx, "message", None)
                if msg is None:
                    continue

                for ix in msg.instructions:
                    # Check for both Create and CreateV2 instructions
                    is_create = ix.data.startswith(PUMP_CREATE_PREFIX)
                    is_create_v2 = ix.data.startswith(PUMP_CREATE_V2_PREFIX)

                    if not (is_create or is_create_v2):
                        continue

                    # Convert account keys to string format
                    account_keys = []
                    for account_idx in ix.accounts:
                        if account_idx < len(msg.account_keys):
                            account_keys.append(
                                base58.b58encode(
                                    bytes(msg.account_keys[account_idx])
                                ).decode()
                            )

                    if len(account_keys) == 0:
                        continue

                    mint = account_keys[0]
                    if mint in known_tokens:
                        continue

                    # Decode based on instruction type
                    if is_create_v2:
                        print(
                            f"[{provider_name}_geyser] Detected: CreateV2 instruction (Token2022)"
                        )
                        decoded = decode_create_v2_instruction(ix.data, account_keys)
                    else:
                        print(
                            f"[{provider_name}_geyser] Detected: Create instruction (Legacy/Metaplex)"
                        )
                        decoded = decode_create_instruction(ix.data, account_keys)

                    if not decoded:
                        continue

                    ts = time.time()
                    tracker.add_token(
                        mint,
                        decoded["name"],
                        decoded["symbol"],
                        lane,
                        ts,
                    )
                    known_tokens.add(mint)

        except Exception as e:
            print(
                f"[ERROR] Connection error in Geyser gRPC listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def listen_deshred_grpc(
    endpoint, api_token, provider_name, tracker, known_tokens=None
):
    """Listen for new tokens via the Geyser deshred stream, before execution.

    This lane sees a transaction as entries form from shreds, so it carries no
    TransactionStatusMeta: there are no logs and no CreateEvent, and the create
    instruction is the only route to a mint. Only top-level instructions are
    walked, because inner instructions are produced by execution and do not
    exist yet -- a coin created through a router is therefore absent from this
    lane's coverage while every other lane reports it.
    """
    try:
        # Generated once into src/geyser/generated; see docs/listeners-and-geyser.md.
        from src.geyser.generated import geyser_pb2, geyser_pb2_grpc
    except ImportError:
        print(
            "[ERROR] Could not import geyser_pb2 or geyser_pb2_grpc. "
            "Regenerate them into src/geyser/generated from src/geyser/proto"
        )
        return

    if known_tokens is None:
        known_tokens = set()

    lane = f"{provider_name}_shreds"

    creds = build_geyser_credentials(api_token)

    while True:
        try:
            print(f"[INFO] Connecting deshred listener to {provider_name}...")

            channel = grpc.aio.secure_channel(endpoint, creds)
            stub = geyser_pb2_grpc.GeyserStub(channel)

            # Its own request type, not a SubscribeRequest: the deshred stream
            # carries no commitment level and no `failed` filter, because both
            # describe execution and nothing has executed.
            request = geyser_pb2.SubscribeDeshredRequest()
            deshred_filter = request.deshred_transactions["pump_filter"]
            deshred_filter.account_include.append(PUMP_MINT_AUTHORITY)
            deshred_filter.vote = False

            print(f"[INFO] Deshred listener active for {provider_name}")

            async for update in stub.SubscribeDeshred(iter([request])):
                tracker.increment_messages(lane)

                if not update.HasField("deshred_transaction"):
                    continue

                transaction = update.deshred_transaction.transaction
                msg = getattr(transaction.transaction, "message", None)
                if msg is None:
                    continue

                # A v0 transaction indexes accounts past the end of
                # message.account_keys when it uses a lookup table. The deshred
                # stream resolves those onto the update itself rather than
                # under a meta, in this order.
                resolved_keys = list(msg.account_keys)
                resolved_keys.extend(transaction.loaded_writable_addresses)
                resolved_keys.extend(transaction.loaded_readonly_addresses)

                for ix in msg.instructions:
                    is_create = ix.data.startswith(PUMP_CREATE_PREFIX)
                    is_create_v2 = ix.data.startswith(PUMP_CREATE_V2_PREFIX)

                    if not (is_create or is_create_v2):
                        continue

                    account_keys = [
                        base58.b58encode(bytes(resolved_keys[account_idx])).decode()
                        for account_idx in ix.accounts
                        if account_idx < len(resolved_keys)
                    ]

                    if len(account_keys) == 0:
                        continue

                    mint = account_keys[0]
                    if mint in known_tokens:
                        continue

                    if is_create_v2:
                        print(
                            f"[{provider_name}_shreds] Detected: CreateV2 instruction (Token2022)"
                        )
                        decoded = decode_create_v2_instruction(ix.data, account_keys)
                    else:
                        print(
                            f"[{provider_name}_shreds] Detected: Create instruction (Legacy/Metaplex)"
                        )
                        decoded = decode_create_instruction(ix.data, account_keys)

                    if not decoded:
                        continue

                    ts = time.time()
                    tracker.add_token(
                        mint,
                        decoded["name"],
                        decoded["symbol"],
                        lane,
                        ts,
                    )
                    known_tokens.add(mint)

        except Exception as e:
            print(
                f"[ERROR] Connection error in deshred listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


# ============ MAIN TEST RUNNER ============

# Each lane is built in the child process, so the parent only ships a kind and
# a dict of plain strings across the process boundary.
LANE_COROUTINES = {
    "block": lambda a, t, k: listen_block_subscription(a["wss"], a["provider"], t, k),
    "logs": lambda a, t, k: listen_logs_subscription(a["wss"], a["provider"], t, k),
    "geyser": lambda a, t, k: listen_geyser_grpc(
        a["endpoint"], a["token"], a["provider"], t, k
    ),
    "shreds": lambda a, t, k: listen_deshred_grpc(
        a["endpoint"], a["token"], a["provider"], t, k
    ),
}


class QueueTracker:
    """Stands in for DetectionTracker inside a lane process.

    Frame counts are batched rather than sent one by one: a busy lane takes tens
    of thousands of frames in a run, and a queue put per frame would cost more
    than the lane's own decode.
    """

    FLUSH_SECONDS = 1.0

    def __init__(self, queue):
        self.queue = queue
        self.lane = None
        self.pending = 0
        self.last_flush = time.time()

    def add_token(self, mint, name, symbol, lane, timestamp):
        self.queue.put(("token", mint, name, symbol, lane, timestamp))

    def increment_messages(self, lane):
        self.lane = lane
        self.pending += 1
        now = time.time()
        if now - self.last_flush >= self.FLUSH_SECONDS:
            self.flush()
            self.last_flush = now

    def flush(self):
        if self.pending and self.lane:
            self.queue.put(("messages", self.lane, self.pending))
            self.pending = 0


def run_lane_process(kind, lane_args, queue, duration, known_tokens):
    """Run one lane for `duration` seconds and report through `queue`."""
    tracker = QueueTracker(queue)

    async def main():
        task = asyncio.create_task(
            LANE_COROUTINES[kind](lane_args, tracker, set(known_tokens))
        )
        await asyncio.sleep(duration)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        tracker.flush()


def build_lane_specs(providers):
    """Return [(kind, args)] for every lane a provider has the endpoints for."""
    lanes = []
    for provider_name, urls in providers.items():
        if urls.get("wss"):
            wss = {"wss": urls["wss"], "provider": provider_name}
            lanes.append(("block", wss))
            lanes.append(("logs", dict(wss)))

        endpoint, api_token = urls.get("geyser") or (None, None)
        if endpoint and api_token:
            grpc_args = {
                "endpoint": endpoint,
                "token": api_token,
                "provider": provider_name,
            }
            lanes.append(("geyser", grpc_args))
            lanes.append(("shreds", dict(grpc_args)))
    return lanes


def run_comparison_test(providers, test_duration=DEFAULT_DURATION):
    """Race every lane, one process each, and report which saw what.

    The lanes get a process apiece because they starve each other inside one
    event loop: the gRPC lanes saturate it, the logsSubscribe websocket falls
    behind draining its socket, and the RPC drops that subscription's
    notifications. The logs lane then reports a fraction of the coins it would
    otherwise decode, which reads as a hole in the listener rather than an
    artifact of this harness.

    Args:
        providers: {provider_name: {'wss': url, 'geyser': (endpoint, token)}}
        test_duration: How long to sample for, in seconds

    Returns:
        The DetectionTracker holding every lane's detections
    """
    tracker = DetectionTracker()
    known_tokens = asyncio.run(fetch_existing_token_mints())
    print(f"[INFO] Loaded {len(known_tokens)} existing tokens")

    # spawn, not fork: grpc.aio and a forked event loop do not survive together.
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    seed = sorted(known_tokens)

    processes = []
    for kind, lane_args in build_lane_specs(providers):
        print(f"[INFO] Starting {kind} listener for {lane_args['provider']}")
        process = ctx.Process(
            target=run_lane_process,
            args=(kind, lane_args, queue, test_duration, seed),
            daemon=True,
        )
        process.start()
        processes.append(process)

    print(f"[INFO] Test running for {test_duration} seconds...")
    deadline = time.time() + test_duration
    # Children exit on their own at the deadline; the grace window is for the
    # counts they flush on the way out.
    hard_stop = deadline + LANE_SHUTDOWN_GRACE

    while True:
        try:
            event = queue.get(timeout=0.5)
        except Empty:
            past_deadline = time.time() > deadline
            if past_deadline and not any(p.is_alive() for p in processes):
                break
            if time.time() > hard_stop:
                break
            continue

        if event[0] == "token":
            _, mint, name, symbol, lane, timestamp = event
            tracker.add_token(mint, name, symbol, lane, timestamp)
        elif event[0] == "messages":
            _, lane, count = event
            tracker.add_messages(lane, count)

    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=5)

    tracker.print_summary()
    return tracker


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Race the pump.fun token-detection methods against each other"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help=f"seconds to sample (default: {DEFAULT_DURATION})",
    )
    args = parser.parse_args()

    # Read providers from environment variables
    providers = {
        "provider_1": {
            "wss": os.environ.get("SOLANA_NODE_WSS_ENDPOINT"),
            "geyser": (
                os.environ.get("GEYSER_ENDPOINT"),
                os.environ.get("GEYSER_API_TOKEN"),
            ),
        },
        # Add more providers to .env as needed
    }

    # Filter out any providers with missing endpoints
    providers = {
        name: urls
        for name, urls in providers.items()
        if (urls.get("wss"))
        or ("geyser" in urls and urls["geyser"][0] and urls["geyser"][1])
    }

    print(
        f"[INFO] Starting Pump.fun token detector comparison test for {args.duration} seconds"
    )
    print(f"[INFO] Providers: {', '.join(providers.keys())}")

    run_comparison_test(providers, test_duration=args.duration)

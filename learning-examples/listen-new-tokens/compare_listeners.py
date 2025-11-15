"""
Performance Comparison Tool for Pump.fun Token Detection Methods

This script compares four methods of detecting new Pump.fun tokens in real-time:

1. Block Subscription (blockSubscribe)
   - Method: WebSocket subscription to blocks mentioning Pump.fun program
   - Speed: Slowest (processes entire blocks)
   - Reference: https://solana.com/docs/rpc/websocket/blocksubscribe

2. Logs Subscription (logsSubscribe)
   - Method: WebSocket subscription to program logs
   - Speed: Fast (event data includes all fields)
   - Reference: https://solana.com/docs/rpc/websocket/logssubscribe

3. Geyser gRPC
   - Method: Yellowstone Dragon's Mouth gRPC streaming
   - Speed: Fastest (optimized streaming protocol)
   - Reference: https://docs.triton.one/rpc-pool/grpc-subscriptions

4. PumpPortal WebSocket
   - Method: Third-party aggregated WebSocket feed
   - Speed: Fast (pre-processed data)
   - Note: Requires trust in third-party provider

The script tracks which method detects each token first and provides detailed
performance statistics including:
- First detection counts per method
- Average latency between methods
- Message counts per provider
- Token detection coverage

Configuration: Set provider endpoints in .env or modify the providers dict at the bottom.
"""

import asyncio
import base64
import json
import os
import struct
import time

import base58
import grpc
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv(override=True)

# ============ CONSTANTS ============

# Pump.fun program ID
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: learning-examples/calculate_discriminator.py
PUMP_CREATE_PREFIX = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_PREFIX = bytes([214, 144, 76, 236, 95, 139, 49, 180])

# Event discriminator for CreateEvent (8-byte identifier)
# This is emitted by both Create and CreateV2 instructions
# Calculated using the first 8 bytes of sha256("event:CreateEvent")
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])

# PumpPortal WebSocket endpoint (third-party service)
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# Test duration in seconds
TEST_DURATION = 30

# Geyser authentication type: "x-token" or "basic"
GEYSER_AUTH_TYPE = "x-token"


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

    def increment_messages(self, provider):
        """Count WebSocket/gRPC messages received by listener"""
        if provider not in self.messages:
            self.messages[provider] = 0
        self.messages[provider] += 1

    def print_summary(self):
        """Print detailed summary statistics of the comparison test"""
        test_duration = time.time() - self.start_time

        # Count total messages
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

        print("\n=== Provider Message Counts ===")
        print("Provider                | Messages")
        print("-" * 40)
        for provider in sorted(self.messages.keys()):
            message_count = self.messages.get(provider, 0)
            print(f"{provider:<22} | {message_count:<8}")
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

            # Find first provider
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
    """
    Fetch existing token mints to avoid duplicate detections
    """
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

        # Parse is_mayhem_mode (OptionBool at the end)
        if offset < len(ix_data):
            parsed_data["is_mayhem_mode"] = bool(ix_data[offset])
        else:
            parsed_data["is_mayhem_mode"] = False

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


def parse_create_event(data):
    """Parse legacy Create event from logs (event data includes all fields)."""
    if len(data) < 8:
        return None

    offset = 8  # Skip discriminator
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
        print(f"[ERROR] Failed to parse create event: {e}")
        return None


def parse_create_v2_event(data):
    """Parse CreateV2 event from logs (event data includes all fields)."""
    if len(data) < 8:
        return None

    offset = 8  # Skip discriminator
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
        print(f"[ERROR] Failed to parse create v2 event: {e}")
        return None


def is_transaction_successful(logs):
    """Check if a transaction was successful based on log messages"""
    for log in logs:
        if "AnchorError thrown" in log or "Error" in log:
            return False
    return True


# ============ WEBSOCKET LISTENERS ============


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
                print(f"Warning: Account index {index} out of range (max: {len(all_keys)-1})")
                return None
        except (IndexError, Exception) as e:
            print(f"Error resolving account at index {index}: {e}")
            return None

    return account_keys


async def listen_block_subscription(wss_url, provider_name, tracker, known_tokens=None):
    """
    Listen for new tokens via block subscription
    """
    if known_tokens is None:
        known_tokens = set()

    while True:
        try:
            print(f"[INFO] Connecting block listener to {provider_name}...")
            async with websockets.connect(wss_url) as websocket:
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
                                "maxSupportedTransactionVersion": 0,
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
                        tracker.increment_messages(provider_name)

                        if data.get("method") != "blockNotification":
                            continue

                        block_data = data["params"]["result"]
                        if (
                            "value" not in block_data
                            or "block" not in block_data["value"]
                        ):
                            continue

                        block = block_data["value"]["block"]
                        if "transactions" not in block:
                            continue

                        for tx in block["transactions"]:
                            if not isinstance(tx, dict) or "transaction" not in tx:
                                continue

                            tx_data_b64 = tx["transaction"][0]
                            tx_data = base64.b64decode(tx_data_b64)

                            try:
                                transaction = VersionedTransaction.from_bytes(tx_data)

                                # Extract loaded addresses from transaction metadata
                                loaded_addresses = None
                                if "meta" in tx and tx["meta"] and "loadedAddresses" in tx["meta"]:
                                    loaded_addresses = tx["meta"]["loadedAddresses"]

                                for ix in transaction.message.instructions:
                                    if (
                                        transaction.message.account_keys[
                                            ix.program_id_index
                                        ]
                                        == PUMP_PROGRAM_ID
                                    ):
                                        data_bytes = bytes(ix.data)

                                        # Check for both Create and CreateV2 instructions
                                        is_create = data_bytes.startswith(PUMP_CREATE_PREFIX)
                                        is_create_v2 = data_bytes.startswith(PUMP_CREATE_V2_PREFIX)

                                        if not (is_create or is_create_v2):
                                            continue

                                        # Get account keys with ALT support
                                        account_keys = get_account_keys(
                                            transaction, ix, loaded_addresses
                                        )
                                        if account_keys is None:
                                            print("Skipping transaction due to unresolved accounts")
                                            continue

                                        # Decode based on instruction type
                                        if is_create_v2:
                                            print(f"[{provider_name}_block] Detected: CreateV2 instruction (Token2022)")
                                            decoded = decode_create_v2_instruction(data_bytes, account_keys)
                                        else:
                                            print(f"[{provider_name}_block] Detected: Create instruction (Legacy/Metaplex)")
                                            decoded = decode_create_instruction(data_bytes, account_keys)

                                        if not decoded:
                                            continue

                                        mint = decoded.get("mint")
                                        if not mint:
                                            continue

                                        if mint in known_tokens:
                                            continue

                                        try:
                                            ts = time.time()
                                            tracker.add_token(
                                                mint,
                                                decoded["name"],
                                                decoded["symbol"],
                                                f"{provider_name}_block",
                                                ts,
                                            )
                                            known_tokens.add(mint)
                                        except Exception as e:
                                            print(
                                                f"[ERROR] Failed to process block instruction: {e}"
                                            )
                            except Exception as e:
                                print(f"[ERROR] Failed to process transaction: {e}")

                    except Exception as e:
                        print(f"[ERROR] Block listener for {provider_name}: {e}")

        except Exception as e:
            print(
                f"[ERROR] Connection error in block listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def listen_logs_subscription(wss_url, provider_name, tracker, known_tokens=None):
    """
    Listen for new tokens via logs subscription
    """
    if known_tokens is None:
        known_tokens = set()

    while True:
        try:
            print(f"[INFO] Connecting logs listener to {provider_name}...")
            async with websockets.connect(wss_url) as websocket:
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
                        tracker.increment_messages(provider_name)

                        if data.get("method") != "logsNotification":
                            continue

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
                                    if event_discriminator != CREATE_EVENT_DISCRIMINATOR:
                                        # Skip non-CreateEvent logs (e.g., TradeEvent, ExtendAccountEvent)
                                        continue

                                    # Parse based on instruction type
                                    if is_create_v2:
                                        print(f"[{provider_name}_logs] Detected: CreateV2 instruction (Token2022)")
                                        parsed = parse_create_v2_event(data_bytes)
                                    else:
                                        print(f"[{provider_name}_logs] Detected: Create instruction (Legacy/Metaplex)")
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
                                        f"{provider_name}_logs",
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


async def listen_geyser_grpc(
    endpoint, api_token, provider_name, tracker, known_tokens=None
):
    """
    Listen for new tokens via Geyser gRPC API
    """
    try:
        # Import the generated protobuf modules
        from generated import geyser_pb2, geyser_pb2_grpc
    except ImportError:
        print(
            "[ERROR] Could not import geyser_pb2 or geyser_pb2_grpc. Make sure to generate from .proto files"
        )
        return

    if known_tokens is None:
        known_tokens = set()

    while True:
        try:
            print(f"[INFO] Connecting Geyser gRPC listener to {provider_name}...")

            if GEYSER_AUTH_TYPE == "x-token":
                auth = grpc.metadata_call_credentials(
                    lambda _context, callback: callback((("x-token", api_token),), None)
                )
            else:
                auth = grpc.metadata_call_credentials(
                    lambda _context, callback: callback(
                        (("authorization", f"Basic {api_token}"),), None
                    )
                )

            creds = grpc.composite_channel_credentials(
                grpc.ssl_channel_credentials(), auth
            )
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
                tracker.increment_messages(provider_name)

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
                                base58.b58encode(bytes(msg.account_keys[account_idx])).decode()
                            )

                    if len(account_keys) == 0:
                        continue

                    mint = account_keys[0]
                    if mint in known_tokens:
                        continue

                    # Decode based on instruction type
                    if is_create_v2:
                        print(f"[{provider_name}_geyser] Detected: CreateV2 instruction (Token2022)")
                        decoded = decode_create_v2_instruction(ix.data, account_keys)
                    else:
                        print(f"[{provider_name}_geyser] Detected: Create instruction (Legacy/Metaplex)")
                        decoded = decode_create_instruction(ix.data, account_keys)

                    if not decoded:
                        continue

                    ts = time.time()
                    tracker.add_token(
                        mint,
                        decoded["name"],
                        decoded["symbol"],
                        f"{provider_name}_geyser",
                        ts,
                    )
                    known_tokens.add(mint)

        except Exception as e:
            print(
                f"[ERROR] Connection error in Geyser gRPC listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def listen_pumpportal(provider_name, tracker, known_tokens=None):
    """
    Listen for new tokens via PumpPortal WebSocket
    """
    if known_tokens is None:
        known_tokens = set()

    while True:
        try:
            print("[INFO] Connecting to PumpPortal WebSocket...")
            async with websockets.connect(PUMPPORTAL_WS_URL) as websocket:
                # Subscribe to new token events
                await websocket.send(
                    json.dumps({"method": "subscribeNewToken", "params": []})
                )
                print(f"[INFO] PumpPortal listener active for {provider_name}")

                while True:
                    try:
                        # Receive WebSocket message
                        message = await websocket.recv()
                        data = json.loads(message)
                        tracker.increment_messages(provider_name)

                        # Extract token information
                        token_info = None
                        if "method" in data and data["method"] == "newToken":
                            token_info = data.get("params", [{}])[0]
                        elif "signature" in data and "mint" in data:
                            token_info = data

                        if not token_info:
                            continue

                        # Get token details
                        mint = token_info.get("mint")
                        name = token_info.get("name", "Unknown")
                        symbol = token_info.get("symbol", "UNK")

                        if not mint:
                            continue

                        # Skip known tokens
                        if mint in known_tokens:
                            continue

                        # Record the token detection
                        ts = time.time()
                        tracker.add_token(
                            mint, name, symbol, f"{provider_name}_pumpportal", ts
                        )
                        known_tokens.add(mint)

                    except Exception as e:
                        print(f"[ERROR] PumpPortal listener for {provider_name}: {e}")
                        break

        except Exception as e:
            print(
                f"[ERROR] Connection error in PumpPortal listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


# ============ MAIN TEST RUNNER ============


async def run_comparison_test(providers, test_duration=600):
    """
    Run the comparison test with multiple WebSocket endpoints

    Args:
        providers: Dict of {provider_name: {'wss': wss_url, 'geyser': (endpoint, api_token)}}
        test_duration: How long to run the test in seconds (default: 10 minutes)
    """
    # Initialize our tracker and fetch existing tokens to avoid duplicates
    tracker = DetectionTracker()
    known_tokens = await fetch_existing_token_mints()
    print(f"[INFO] Loaded {len(known_tokens)} existing tokens")

    tasks = []

    # Start all listeners for each provider
    for provider_name, urls in providers.items():
        if urls.get("wss"):
            print(f"[INFO] Starting block listener for {provider_name}")
            task = asyncio.create_task(
                listen_block_subscription(
                    urls["wss"], provider_name, tracker, known_tokens.copy()
                )
            )
            tasks.append(task)

        if urls.get("wss"):
            print(f"[INFO] Starting logs listener for {provider_name}")
            task = asyncio.create_task(
                listen_logs_subscription(
                    urls["wss"], provider_name, tracker, known_tokens.copy()
                )
            )
            tasks.append(task)

        if urls.get("geyser"):
            endpoint, api_token = urls["geyser"]
            if endpoint and api_token:
                print(f"[INFO] Starting Geyser gRPC listener for {provider_name}")
                task = asyncio.create_task(
                    listen_geyser_grpc(
                        endpoint, api_token, provider_name, tracker, known_tokens.copy()
                    )
                )
                tasks.append(task)

    # Start PumpPortal listener (only once, not per provider)
    print("[INFO] Starting PumpPortal listener")
    task = asyncio.create_task(
        listen_pumpportal("pumpportal", tracker, known_tokens.copy())
    )
    tasks.append(task)

    print(f"[INFO] Test running for {test_duration} seconds...")
    await asyncio.sleep(test_duration)

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    tracker.print_summary()


if __name__ == "__main__":
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
        f"[INFO] Starting Pump.fun token detector comparison test for {TEST_DURATION} seconds"
    )
    print(f"[INFO] Providers: {', '.join(providers.keys())}")

    asyncio.run(run_comparison_test(providers, test_duration=TEST_DURATION))

"""
This script compares two methods of detecting migrations:

1. Migration Program Listener - Listens to migration wrapper program (39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg)
   which emits detailed migration events via logsSubscribe

2. Direct Pool Account Listener - Listens to pump_amm program (pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA)
   for new Pool account creations via programSubscribe

Note: The migration wrapper program emits a different event structure than
CompletePumpAmmMigrationEvent in pump_fun_idl.json.

The script tracks which method detects new migrations first and provides detailed performance
statistics including message counts, detection timing, provider latency comparison.

Configure multiple RPC endpoints in .env file to test provider performance.
"""

import asyncio
import base64
import json
import os
import struct
import time

import aiohttp
import base58
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
MIGRATION_PROGRAM_ID = Pubkey.from_string(
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
)
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
QUOTE_MINT_SOL = base58.b58encode(
    bytes(Pubkey.from_string("So11111111111111111111111111111111111111112"))
).decode()

MARKET_DISCRIMINATOR = base58.b58encode(b"\xf1\x9am\x04\x11\xb1m\xbc").decode()
MARKET_ACCOUNT_LENGTH = 8 + 1 + 2 + 32 * 6 + 8 + 32 + 1  # Pool account with is_mayhem_mode = 244 bytes


class DetectionTracker:
    """Tracks and analyzes detection times for both methods across providers"""

    def __init__(self):
        self.migrations = {}  # {base_mint: {provider: timestamp}}
        self.markets = {}  # {base_mint: {provider: timestamp}}
        self.migration_messages = {}  # {provider: count}
        self.market_messages = {}  # {provider: count}
        self.start_time = time.time()

    def add_migration(self, base_mint, provider, timestamp):
        """Record a migration detection event"""
        if base_mint not in self.migrations:
            self.migrations[base_mint] = {}
        self.migrations[base_mint][provider] = timestamp
        print(
            f"[MIGRATION] base_mint={base_mint} provider={provider} time={timestamp:.3f}"
        )

    def add_market(self, base_mint, provider, timestamp):
        """Record a market detection event"""
        if base_mint not in self.markets:
            self.markets[base_mint] = {}
        self.markets[base_mint][provider] = timestamp
        print(
            f"[MARKET] base_mint={base_mint} provider={provider} time={timestamp:.3f}"
        )

    def increment_migration_messages(self, provider):
        """Count WebSocket messages received by migration listener"""
        if provider not in self.migration_messages:
            self.migration_messages[provider] = 0
        self.migration_messages[provider] += 1

    def increment_market_messages(self, provider):
        """Count WebSocket messages received by market listener"""
        if provider not in self.market_messages:
            self.market_messages[provider] = 0
        self.market_messages[provider] += 1

    def print_summary(self):
        """Print detailed summary statistics of the comparison test"""
        test_duration = time.time() - self.start_time

        # Count total messages
        total_migration_messages = sum(self.migration_messages.values())
        total_market_messages = sum(self.market_messages.values())

        print("\n=== Test Summary ===")
        print(f"Test duration: {test_duration:.2f} seconds")
        print(
            f"WebSocket messages received: {total_migration_messages + total_market_messages}"
        )
        print(f"  - Migration events: {total_migration_messages}")
        print(f"  - Market events: {total_market_messages}")

        # Count unique tokens detected by each method
        unique_migrations = set(self.migrations.keys())
        unique_markets = set(self.markets.keys())
        common_tokens = unique_migrations & unique_markets

        print(f"Tokens detected: {len(unique_migrations | unique_markets)}")
        print(f"  - Migration events: {len(unique_migrations)}")
        print(f"  - Market events: {len(unique_markets)}")
        print(f"  - Detected in both: {len(common_tokens)}\n")

        print("=== Provider Message Counts ===")
        print(
            "Provider                | Migration Messages | Market Messages | Total Messages"
        )
        print("-" * 80)
        all_providers = set(self.migration_messages.keys()) | set(
            self.market_messages.keys()
        )
        for provider in sorted(all_providers):
            migration_count = self.migration_messages.get(provider, 0)
            market_count = self.market_messages.get(provider, 0)
            total = migration_count + market_count
            print(
                f"{provider:<22} | {migration_count:<18} | {market_count:<14} | {total}"
            )
        print()

        print("=== Migration Event Provider Performance ===")
        self._print_provider_performance(self.migrations)

        print("\n=== Market Event Provider Performance ===")
        self._print_provider_performance(self.markets)

        # Compare detection methods for tokens detected by both
        if common_tokens:
            print("\n=== Detection Timing Comparison: Migration vs Market ===")
            print(
                "Base Mint                                     | First Detection Method | First Provider | Time Delta (ms)"
            )
            print("-" * 100)

            migration_first = 0
            market_first = 0
            total_delta_ms = 0

            for base_mint in sorted(common_tokens):
                # Find earliest time for each method
                migration_time = (
                    min(self.migrations[base_mint].values())
                    if base_mint in self.migrations
                    else float("inf")
                )
                market_time = (
                    min(self.markets[base_mint].values())
                    if base_mint in self.markets
                    else float("inf")
                )

                # Find provider with earliest time for each method
                migration_provider = None
                if base_mint in self.migrations:
                    migration_provider = min(
                        self.migrations[base_mint].items(), key=lambda x: x[1]
                    )[0]

                market_provider = None
                if base_mint in self.markets:
                    market_provider = min(
                        self.markets[base_mint].items(), key=lambda x: x[1]
                    )[0]

                delta_ms = abs(migration_time - market_time) * 1000
                total_delta_ms += delta_ms

                if migration_time < market_time:
                    first_method = "Migration"
                    first_provider = migration_provider
                    migration_first += 1
                else:
                    first_method = "Market"
                    first_provider = market_provider
                    market_first += 1

                print(
                    f"{base_mint} | {first_method:<21} | {first_provider:<14} | {delta_ms:8.1f}"
                )

            # Print statistics summary
            if common_tokens:
                avg_delta_ms = total_delta_ms / len(common_tokens)
                print("\nSummary statistics:")
                print(
                    f"  - Migration detected first: {migration_first}/{len(common_tokens)} ({migration_first / len(common_tokens) * 100:.1f}%)"
                )
                print(
                    f"  - Market detected first: {market_first}/{len(common_tokens)} ({market_first / len(common_tokens) * 100:.1f}%)"
                )
                print(f"  - Average timing difference: {avg_delta_ms:.1f} ms")

    def _print_provider_performance(self, events_dict):
        """Print performance metrics for providers using a specific detection method"""
        # Count how many times each provider was first
        first_count = {}
        total_events = 0

        for base_mint, providers in events_dict.items():
            total_events += 1
            if not providers:
                continue

            # Find the fastest provider for this event
            fastest_provider = min(providers.items(), key=lambda x: x[1])[0]
            if fastest_provider not in first_count:
                first_count[fastest_provider] = 0
            first_count[fastest_provider] += 1

        if not first_count:
            print("No events detected")
            return

        # Print rankings
        print("Provider                | First Detections | Percentage")
        print("-" * 60)

        for provider, count in sorted(
            first_count.items(), key=lambda x: x[1], reverse=True
        ):
            percentage = (count / total_events) * 100 if total_events > 0 else 0
            print(f"{provider:<22} | {count:<16} | {percentage:.1f}%")

        # Calculate average latency between providers
        self._print_provider_latency_matrix(events_dict)

    def _print_provider_latency_matrix(self, events_dict):
        """Print a matrix of average latency between providers"""
        # Get unique providers
        all_providers = set()
        for providers_data in events_dict.values():
            all_providers.update(providers_data.keys())

        if len(all_providers) <= 1:
            return

        providers_list = sorted(all_providers)

        print("\nAverage Latency Matrix (ms):")
        # Print header
        header = "           |"
        for provider in providers_list:
            header += f" {provider[:8]:>8} |"
        print(header)
        print("-" * len(header))

        # Calculate and print latency matrix
        for provider1 in providers_list:
            row = f"{provider1[:8]:>8} |"
            for provider2 in providers_list:
                if provider1 == provider2:
                    row += "      — |"
                    continue

                # Calculate average latency
                latencies = []
                for base_mint, providers_data in events_dict.items():
                    if provider1 in providers_data and provider2 in providers_data:
                        latency_ms = (
                            providers_data[provider2] - providers_data[provider1]
                        ) * 1000
                        latencies.append(latency_ms)

                if latencies:
                    avg_latency = sum(latencies) / len(latencies)
                    row += f" {avg_latency:>+7.1f} |"
                else:
                    row += "      ? |"
            print(row)


# ============ MARKET DETECTION METHODS ============


async def fetch_existing_market_pubkeys():
    """
    Fetch existing AMM market accounts from the blockchain

    Used to filter out already existing markets when detecting new ones
    """
    headers = {"Content-Type": "application/json"}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            str(PUMP_AMM_PROGRAM_ID),
            {
                "encoding": "base64",
                "commitment": "processed",
                "filters": [
                    {"dataSize": MARKET_ACCOUNT_LENGTH},
                    {"memcmp": {"offset": 0, "bytes": MARKET_DISCRIMINATOR}},
                    {"memcmp": {"offset": 75, "bytes": QUOTE_MINT_SOL}},
                ],
            },
        ],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(RPC_ENDPOINT, headers=headers, json=body) as resp:
            res = await resp.json()
            return {account["pubkey"] for account in res.get("result", [])}


def parse_market_account_data(data):
    """
    Parse binary Pool account data according to pump_swap_idl.json structure

    Total 11 fields including is_mayhem_mode field added with mayhem update
    """
    parsed_data = {}
    offset = 8  # Skip discriminator

    fields = [
        ("pool_bump", "u8"),
        ("index", "u16"),
        ("creator", "pubkey"),
        ("base_mint", "pubkey"),
        ("quote_mint", "pubkey"),
        ("lp_mint", "pubkey"),
        ("pool_base_token_account", "pubkey"),
        ("pool_quote_token_account", "pubkey"),
        ("lp_supply", "u64"),
        ("coin_creator", "pubkey"),
        ("is_mayhem_mode", "bool"),
    ]

    try:
        for field_name, field_type in fields:
            if field_type == "pubkey":
                value = data[offset : offset + 32]
                parsed_data[field_name] = base58.b58encode(value).decode("utf-8")
                offset += 32
            elif field_type in {"u64", "i64"}:
                value = (
                    struct.unpack("<Q", data[offset : offset + 8])[0]
                    if field_type == "u64"
                    else struct.unpack("<q", data[offset : offset + 8])[0]
                )
                parsed_data[field_name] = value
                offset += 8
            elif field_type == "u16":
                value = struct.unpack("<H", data[offset : offset + 2])[0]
                parsed_data[field_name] = value
                offset += 2
            elif field_type == "u8":
                value = data[offset]
                parsed_data[field_name] = value
                offset += 1
            elif field_type == "bool":
                value = bool(data[offset])
                parsed_data[field_name] = value
                offset += 1
    except Exception as e:
        print(f"[ERROR] Failed to parse market data: {e}")

    return parsed_data


def parse_migrate_instruction(data):
    """
    Parse migration event from the migration wrapper program

    Note: This parses the event emitted by the migration wrapper program
    (39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg), which has a different
    structure than CompletePumpAmmMigrationEvent in pump_fun_idl.json.
    """
    if len(data) < 8:
        print(f"[ERROR] Data length too short: {len(data)} bytes")
        return None

    offset = 8  # Skip discriminator
    parsed_data = {}

    fields = [
        ("timestamp", "i64"),
        ("index", "u16"),
        ("creator", "pubkey"),
        ("baseMint", "pubkey"),
        ("quoteMint", "pubkey"),
        ("baseMintDecimals", "u8"),
        ("quoteMintDecimals", "u8"),
        ("baseAmountIn", "u64"),
        ("quoteAmountIn", "u64"),
        ("poolBaseAmount", "u64"),
        ("poolQuoteAmount", "u64"),
        ("minimumLiquidity", "u64"),
        ("initialLiquidity", "u64"),
        ("lpTokenAmountOut", "u64"),
        ("poolBump", "u8"),
        ("pool", "pubkey"),
        ("lpMint", "pubkey"),
        ("userBaseTokenAccount", "pubkey"),
        ("userQuoteTokenAccount", "pubkey"),
    ]

    try:
        for field_name, field_type in fields:
            if field_type == "pubkey":
                value = data[offset : offset + 32]
                parsed_data[field_name] = base58.b58encode(value).decode("utf-8")
                offset += 32
            elif field_type in {"u64", "i64"}:
                value = (
                    struct.unpack("<Q", data[offset : offset + 8])[0]
                    if field_type == "u64"
                    else struct.unpack("<q", data[offset : offset + 8])[0]
                )
                parsed_data[field_name] = value
                offset += 8
            elif field_type == "u16":
                value = struct.unpack("<H", data[offset : offset + 2])[0]
                parsed_data[field_name] = value
                offset += 2
            elif field_type == "u8":
                value = data[offset]
                parsed_data[field_name] = value
                offset += 1

        return parsed_data

    except Exception as e:
        print(f"[ERROR] Failed to parse migration data at offset {offset}: {e}")
        return None


def is_transaction_successful(logs):
    """Check if a transaction was successful based on log messages"""
    for log in logs:
        if "AnchorError thrown" in log or "Error" in log:
            return False
    return True


# ============ WEBSOCKET LISTENERS ============


async def listen_for_migrations(wss_url, provider_name, tracker, known_events=None):
    """
    Listen for migration instructions via WebSocket

    Args:
        wss_url: WebSocket URL to connect to
        provider_name: Name of the RPC provider
        tracker: DetectionTracker instance to record events
        known_events: Set of already known (provider, base_mint) tuples to prevent duplicates
    """
    if known_events is None:
        known_events = set()

    while True:
        try:
            print(f"[INFO] Connecting migration listener to {provider_name}...")
            async with websockets.connect(wss_url) as websocket:
                # Subscribe to logs mentioning the migration program
                subscription_message = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(MIGRATION_PROGRAM_ID)]},
                            {"commitment": "processed"},
                        ],
                    }
                )
                await websocket.send(subscription_message)
                await websocket.recv()  # Get subscription confirmation
                print(f"[INFO] Migration listener active for {provider_name}")

                while True:
                    try:
                        # Receive WebSocket message
                        response = await websocket.recv()
                        data = json.loads(response)
                        tracker.increment_migration_messages(provider_name)

                        # Check if it's a notification and not something else
                        if data.get("method") != "logsNotification":
                            continue

                        # Get transaction logs
                        log_data = data["params"]["result"]["value"]
                        logs = log_data.get("logs", [])

                        # Skip failed transactions
                        if not is_transaction_successful(logs):
                            continue

                        # Skip if not a Migrate instruction
                        if not any("Instruction: Migrate" in log for log in logs):
                            continue

                        # Skip already migrated curves
                        if any("already migrated" in log for log in logs):
                            continue

                        # Search for Program data in logs
                        for log in logs:
                            if log.startswith("Program data:"):
                                try:
                                    # Decode and parse the instruction data
                                    data = base64.b64decode(log.split(": ")[1])
                                    parsed = parse_migrate_instruction(data)

                                    if parsed and "baseMint" in parsed:
                                        base_mint = parsed["baseMint"]
                                        # Only track the timestamp for the first time we see this event
                                        # from this provider, but still count messages
                                        if (
                                            provider_name,
                                            base_mint,
                                        ) not in known_events:
                                            ts = time.time()
                                            tracker.add_migration(
                                                base_mint, provider_name, ts
                                            )
                                            known_events.add((provider_name, base_mint))
                                    break
                                except Exception as e:
                                    print(f"[ERROR] Failed to decode Program data: {e}")

                    except Exception as e:
                        print(f"[ERROR] Migration listener for {provider_name}: {e}")

        except Exception as e:
            print(
                f"[ERROR] Connection error in migration listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def listen_for_markets(wss_url, provider_name, tracker, known_markets):
    """
    Listen for new market accounts via WebSocket

    Args:
        wss_url: WebSocket URL to connect to
        provider_name: Name of the RPC provider
        tracker: DetectionTracker instance to record events
        known_markets: Set of already known market pubkeys to prevent duplicates
    """
    while True:
        try:
            print(f"[INFO] Connecting market listener to {provider_name}...")
            async with websockets.connect(wss_url) as websocket:
                # Subscribe to program account changes
                sub_msg = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "programSubscribe",
                        "params": [
                            str(PUMP_AMM_PROGRAM_ID),
                            {
                                "commitment": "processed",
                                "encoding": "base64",
                                "filters": [
                                    {"dataSize": MARKET_ACCOUNT_LENGTH},
                                    {
                                        "memcmp": {
                                            "offset": 0,
                                            "bytes": MARKET_DISCRIMINATOR,
                                        }
                                    },
                                    {"memcmp": {"offset": 75, "bytes": QUOTE_MINT_SOL}},
                                ],
                            },
                        ],
                    }
                )
                await websocket.send(sub_msg)
                await websocket.recv()  # Get subscription confirmation
                print(f"[INFO] Market listener active for {provider_name}")

                # Track events already seen by this provider
                provider_known = set()

                while True:
                    try:
                        # Receive WebSocket message
                        msg = await websocket.recv()
                        data = json.loads(msg)
                        tracker.increment_market_messages(provider_name)

                        # Check if it's a notification and not something else
                        if data.get("method") != "programNotification":
                            continue

                        # Extract account information
                        message_value = data["params"]["result"]["value"]
                        pubkey = message_value["pubkey"]
                        raw_account_data = message_value["account"].get("data", [None])[
                            0
                        ]

                        # Skip if we've already processed this market (either globally or for this provider)
                        if pubkey in known_markets or pubkey in provider_known:
                            continue
                        provider_known.add(pubkey)

                        # Skip if there's no data
                        if not raw_account_data:
                            print("[ERROR] Account data is empty")
                            continue

                        try:
                            # Decode and parse the account data
                            account_data = base64.b64decode(raw_account_data)
                            parsed = parse_market_account_data(account_data)

                            # Skip user-created markets (they are on-curve)
                            if (
                                parsed.get("creator")
                                and Pubkey.from_string(
                                    parsed.get("creator")
                                ).is_on_curve()
                            ):
                                continue  # skip user-created pool

                            # Record the market detection
                            base_mint = parsed.get("base_mint")
                            if base_mint:
                                ts = time.time()
                                tracker.add_market(base_mint, provider_name, ts)

                                # Add to the shared known markets to avoid duplicate processing
                                known_markets.add(pubkey)

                        except Exception as e:
                            print(f"[ERROR] Failed to decode account: {e}")

                    except Exception as e:
                        print(f"[ERROR] Market listener for {provider_name}: {e}")

        except Exception as e:
            print(
                f"[ERROR] Connection error in market listener for {provider_name}: {e}"
            )
            print("[INFO] Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


# ============ MAIN TEST RUNNER ============


async def run_comparison_test(
    migration_wss_endpoints, market_wss_endpoints, test_duration=600
):
    """
    Run the comparison test with multiple WebSocket endpoints

    Args:
        migration_wss_endpoints: Dict of {provider_name: wss_url} for migration listeners
        market_wss_endpoints: Dict of {provider_name: wss_url} for market listeners
        test_duration: How long to run the test in seconds (default: 10 minutes)
    """
    # Initialize our tracker and fetch existing markets to avoid duplicates
    tracker = DetectionTracker()
    known_markets = await fetch_existing_market_pubkeys()
    print(f"[INFO] Loaded {len(known_markets)} existing markets")

    known_migration_events = set()
    tasks = []

    # Start migration listeners
    for provider_name, wss_url in migration_wss_endpoints.items():
        print(f"[INFO] Starting migration listener for {provider_name}")
        task = asyncio.create_task(
            listen_for_migrations(
                wss_url, provider_name, tracker, known_migration_events
            )
        )
        tasks.append(task)

    # Start market listeners
    for provider_name, wss_url in market_wss_endpoints.items():
        print(f"[INFO] Starting market listener for {provider_name}")
        task = asyncio.create_task(
            listen_for_markets(wss_url, provider_name, tracker, known_markets)
        )
        tasks.append(task)

    # Run for specified duration
    print(f"[INFO] Test running for {test_duration} seconds...")
    await asyncio.sleep(test_duration)

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    tracker.print_summary()


if __name__ == "__main__":
    # Read providers from environment variables
    # You can add more providers by adding additional environment variables
    migration_providers = {
        "chainstack": os.environ.get("SOLANA_NODE_WSS_ENDPOINT"),
        "provider_2": os.environ.get("SOLANA_NODE_WSS_ENDPOINT_2"),
        # Add more providers to .env as needed
    }

    market_providers = {
        "chainstack": os.environ.get("SOLANA_NODE_WSS_ENDPOINT"),
        "provider_2": os.environ.get("SOLANA_NODE_WSS_ENDPOINT_2"),
        # Add more providers to .env as needed
    }

    # Filter out any providers with missing endpoints
    migration_providers = {
        name: url for name, url in migration_providers.items() if url
    }
    market_providers = {name: url for name, url in market_providers.items() if url}

    # Get test duration from environment or use default (10 minutes)
    TEST_DURATION = int(os.environ.get("TEST_DURATION", 600))

    print(
        f"[INFO] Starting Solana detector comparison test for {TEST_DURATION} seconds"
    )
    print(f"[INFO] Migration providers: {', '.join(migration_providers.keys())}")
    print(f"[INFO] Market providers: {', '.join(market_providers.keys())}")

    # Run the test
    asyncio.run(
        run_comparison_test(
            migration_providers, market_providers, test_duration=TEST_DURATION
        )
    )

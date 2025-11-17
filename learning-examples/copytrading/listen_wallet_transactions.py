"""
Listens for pump.fun bonding curve buy/sell transactions involving a specific wallet.
Filters transactions to show only buy/sell operations on pump.fun bonding curves,
excluding pump AMM (migrated) transactions..
"""

import asyncio
import base64
import binascii
import json
import os
import struct
import sys
from datetime import datetime

import base58
import websockets
from dotenv import load_dotenv

load_dotenv()

# Configuration
WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
WALLET_TO_TRACK = "..."  # Change this to your target wallet

# Pump.fun program constants
PUMP_BONDING_CURVE_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

# TradeEvent discriminator and parsing constants
TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
EVENT_DISCRIMINATOR_SIZE = 8

# Display settings
MAX_LOGS_TO_SHOW = 5
RECONNECT_DELAY = 5
MIN_LOG_PARTS_FOR_PROGRAM = 2
PING_INTERVAL = 20

if not WSS_ENDPOINT:
    print("Error: SOLANA_NODE_WSS_ENDPOINT environment variable not set")
    sys.exit(1)


async def subscribe_to_wallet_logs(websocket):
    """Subscribe to logs mentioning our target wallet."""
    subscription_message = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [WALLET_TO_TRACK]}, {"commitment": "processed"}],
        }
    )

    await websocket.send(subscription_message)
    print(f"Subscribed to logs mentioning wallet: {WALLET_TO_TRACK}")

    # Wait for subscription confirmation
    response = await websocket.recv()
    response_data = json.loads(response)
    if "result" in response_data:
        print(f"Subscription confirmed with ID: {response_data['result']}")
    else:
        print(f"Unexpected subscription response: {response}")
    print("=" * 80)


async def keep_connection_alive(websocket):
    """Send ping messages to keep the WebSocket connection alive."""
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                pong_waiter = await websocket.ping()
                await asyncio.wait_for(pong_waiter, timeout=10)
            except TimeoutError:
                print("Ping timeout - server not responding")
                await websocket.close()
                return
    except asyncio.CancelledError:
        pass
    except (websockets.exceptions.WebSocketException, ConnectionError) as e:
        print(f"Ping error: {e}")


def is_pump_bonding_curve_buysell(logs):
    """Check if transaction is a pump.fun bonding curve buy/sell transaction."""
    # Must mention pump.fun bonding curve program
    if not any(PUMP_BONDING_CURVE_PROGRAM_ID in log for log in logs):
        return False

    # Must NOT mention pump AMM program (exclude migrated tokens)
    if any(PUMP_AMM_PROGRAM_ID in log for log in logs):
        return False

    # Must have TradeEvent data (more reliable than instruction logs)
    if parse_trade_event(logs) is None:
        return False

    return True


def parse_trade_event(logs):
    """Parse TradeEvent data from transaction logs."""
    for log in logs:
        if "Program data:" in log:
            try:
                encoded_data = log.split("Program data: ")[1].strip()
                decoded_data = base64.b64decode(encoded_data)

                if len(decoded_data) >= EVENT_DISCRIMINATOR_SIZE:
                    discriminator = decoded_data[:EVENT_DISCRIMINATOR_SIZE]
                    if discriminator == TRADE_EVENT_DISCRIMINATOR:
                        return decode_trade_event(
                            decoded_data[EVENT_DISCRIMINATOR_SIZE:]
                        )
            except (ValueError, binascii.Error):
                continue
    return None


def decode_trade_event(data):
    """Decode TradeEvent structure from raw bytes with progressive parsing.

    Supports both pre-mayhem and post-mayhem IDL versions by parsing fields
    progressively based on available bytes. This ensures backward compatibility
    with older transaction logs.

    Core fields (always present): mint, sol_amount, token_amount, is_buy, user,
    timestamp, virtual_sol_reserves, virtual_token_reserves

    Extended fields (added later): real_sol_reserves, real_token_reserves,
    fee_recipient, fee_basis_points, fee, creator, creator_fee_basis_points,
    creator_fee, track_volume, total_unclaimed_tokens, total_claimed_tokens,
    current_sol_volume, last_update_timestamp, ix_name
    """
    # Minimum size for core fields: 32+8+8+1+32+8+8+8 = 105 bytes
    if len(data) < 105:
        return None

    offset = 0

    # Parse core fields (always present in all versions)
    mint = data[offset : offset + 32]
    offset += 32

    sol_amount = struct.unpack("<Q", data[offset : offset + 8])[0]
    offset += 8

    token_amount = struct.unpack("<Q", data[offset : offset + 8])[0]
    offset += 8

    is_buy = bool(data[offset])
    offset += 1

    user = data[offset : offset + 32]
    offset += 32

    timestamp = struct.unpack("<q", data[offset : offset + 8])[0]
    offset += 8

    virtual_sol_reserves = struct.unpack("<Q", data[offset : offset + 8])[0]
    offset += 8

    virtual_token_reserves = struct.unpack("<Q", data[offset : offset + 8])[0]
    offset += 8

    # Parse extended fields if bytes remaining (added in later versions)
    # Real reserves (8+8 = 16 bytes)
    if len(data) >= offset + 16:
        real_sol_reserves = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        real_token_reserves = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
    else:
        real_sol_reserves = 0
        real_token_reserves = 0

    # Fee recipient and fee details (32+8+8 = 48 bytes)
    if len(data) >= offset + 48:
        fee_recipient = data[offset : offset + 32]
        offset += 32
        fee_basis_points = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        fee = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
    else:
        fee_recipient = b'\x00' * 32
        fee_basis_points = 0
        fee = 0

    # Creator and creator fee details (32+8+8 = 48 bytes)
    if len(data) >= offset + 48:
        creator = data[offset : offset + 32]
        offset += 32
        creator_fee_basis_points = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        creator_fee = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
    else:
        creator = b'\x00' * 32
        creator_fee_basis_points = 0
        creator_fee = 0

    # Volume tracking fields (1+8+8+8+8 = 33 bytes)
    if len(data) >= offset + 33:
        track_volume = bool(data[offset])
        offset += 1
        total_unclaimed_tokens = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        total_claimed_tokens = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        current_sol_volume = struct.unpack("<Q", data[offset : offset + 8])[0]
        offset += 8
        last_update_timestamp = struct.unpack("<q", data[offset : offset + 8])[0]
        offset += 8
    else:
        track_volume = False
        total_unclaimed_tokens = 0
        total_claimed_tokens = 0
        current_sol_volume = 0
        last_update_timestamp = 0

    # Parse string field (ix_name): 4 bytes for length + string data
    if len(data) >= offset + 4:
        string_length = struct.unpack("<I", data[offset : offset + 4])[0]
        offset += 4
        if len(data) >= offset + string_length:
            ix_name = data[offset : offset + string_length].decode("utf-8")
        else:
            ix_name = ""
    else:
        ix_name = ""

    return {
        "mint": base58.b58encode(mint).decode(),
        "sol_amount": sol_amount,
        "token_amount": token_amount,
        "is_buy": is_buy,
        "user": base58.b58encode(user).decode(),
        "timestamp": timestamp,
        "virtual_sol_reserves": virtual_sol_reserves,
        "virtual_token_reserves": virtual_token_reserves,
        "real_sol_reserves": real_sol_reserves,
        "real_token_reserves": real_token_reserves,
        "fee_recipient": base58.b58encode(fee_recipient).decode() if fee_recipient != b'\x00' * 32 else None,
        "fee_basis_points": fee_basis_points,
        "fee": fee,
        "creator": base58.b58encode(creator).decode() if creator != b'\x00' * 32 else None,
        "creator_fee_basis_points": creator_fee_basis_points,
        "creator_fee": creator_fee,
        "track_volume": track_volume,
        "total_unclaimed_tokens": total_unclaimed_tokens,
        "total_claimed_tokens": total_claimed_tokens,
        "current_sol_volume": current_sol_volume,
        "last_update_timestamp": last_update_timestamp,
        "ix_name": ix_name,
        "price_per_token": (sol_amount * 1_000_000) / token_amount
        if token_amount > 0
        else 0,
    }


def display_transaction_info(signature, logs):
    """Display formatted transaction information."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{timestamp}] Pump.fun Bonding Curve Transaction:")
    print(f"  Signature: {signature}")
    print(f"  Wallet: {WALLET_TO_TRACK}")

    # Parse trade event data
    trade_data = parse_trade_event(logs)
    if trade_data:
        # Core transaction info (always present)
        ix_name = trade_data.get('ix_name', '')
        trade_type = 'BUY' if trade_data['is_buy'] else 'SELL'
        print(f"  Type: {trade_type}{f' ({ix_name})' if ix_name else ''}")
        print(f"  Token: {trade_data['mint']}")
        print(f"  SOL Amount: {trade_data['sol_amount'] / 1_000_000_000:.6f} SOL")
        print(f"  Token Amount: {trade_data['token_amount']:,}")
        print(
            f"  Price per Token: {trade_data['price_per_token'] / 1_000_000_000:.9f} SOL"
        )
        print(f"  Trader: {trade_data['user']}")

        # Fee info (may not be present in older transactions)
        if trade_data['fee'] > 0 or trade_data['fee_basis_points'] > 0:
            print(f"  Fee: {trade_data['fee'] / 1_000_000_000:.6f} SOL ({trade_data['fee_basis_points']} bps)")

        if trade_data['creator_fee'] > 0 or trade_data['creator_fee_basis_points'] > 0:
            print(f"  Creator Fee: {trade_data['creator_fee'] / 1_000_000_000:.6f} SOL ({trade_data['creator_fee_basis_points']} bps)")

        if trade_data['creator']:
            print(f"  Creator: {trade_data['creator']}")

        if trade_data['fee_recipient']:
            print(f"  Fee Recipient: {trade_data['fee_recipient']}")

        # Reserve info
        print(f"  Virtual Reserves: {trade_data['virtual_sol_reserves'] / 1_000_000_000:.6f} SOL / {trade_data['virtual_token_reserves']:,} tokens")

        if trade_data['real_sol_reserves'] > 0 or trade_data['real_token_reserves'] > 0:
            print(f"  Real Reserves: {trade_data['real_sol_reserves'] / 1_000_000_000:.6f} SOL / {trade_data['real_token_reserves']:,} tokens")

    # Extract and display program info
    display_program_info(logs)

    # Show pump.fun related logs
    display_pump_logs(logs)

    print("  " + "-" * 76)


def display_program_info(logs):
    """Extract and display program information from logs."""
    programs_involved = set()
    instructions = []

    for log in logs:
        if "Program " in log and " invoke" in log:
            parts = log.split()
            if len(parts) >= MIN_LOG_PARTS_FOR_PROGRAM:
                program_id = parts[1]
                programs_involved.add(program_id)
        elif "Instruction:" in log:
            instruction = log.split("Instruction: ")[-1]
            instructions.append(instruction)

    if programs_involved:
        print(f"  Programs involved: {', '.join(programs_involved)}")
    if instructions:
        print(f"  Instructions: {', '.join(instructions)}")


def display_pump_logs(logs):
    """Display relevant pump.fun logs."""
    pump_logs = [
        log
        for log in logs
        if any(
            keyword in log
            for keyword in [
                "Program log: Instruction:",
                "Program data:",
                PUMP_BONDING_CURVE_PROGRAM_ID,
            ]
        )
    ]

    if pump_logs:
        print("  Pump.fun logs:")
        for log in pump_logs[:MAX_LOGS_TO_SHOW]:
            print(f"    {log}")
        if len(pump_logs) > MAX_LOGS_TO_SHOW:
            print(f"    ... and {len(pump_logs) - MAX_LOGS_TO_SHOW} more logs")


async def handle_transaction(log_data):
    """Handle a transaction log notification."""
    signature = log_data.get("signature", "unknown")
    logs = log_data.get("logs", [])

    # Check if this is a pump.fun bonding curve buy/sell transaction
    if not is_pump_bonding_curve_buysell(logs):
        return

    display_transaction_info(signature, logs)


async def process_websocket_message(websocket):
    """Process incoming WebSocket messages."""
    try:
        response = await asyncio.wait_for(websocket.recv(), timeout=30)
        data = json.loads(response)

        if "method" not in data or data["method"] != "logsNotification":
            return

        log_data = data["params"]["result"]["value"]
        await handle_transaction(log_data)

    except TimeoutError:
        print("No data received for 30 seconds")
    except websockets.exceptions.ConnectionClosed:
        print("WebSocket connection closed")
        raise
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"Error processing message: {e}")


async def listen_for_transactions():
    """Main function to listen for wallet transactions."""
    print(f"Starting to monitor wallet: {WALLET_TO_TRACK}")
    print(f"Connecting to: {WSS_ENDPOINT}")
    print("Looking for pump.fun bonding curve buy/sell transactions only...")
    print("=" * 80)

    while True:
        try:
            async with websockets.connect(WSS_ENDPOINT) as websocket:
                await subscribe_to_wallet_logs(websocket)
                ping_task = asyncio.create_task(keep_connection_alive(websocket))

                try:
                    while True:
                        await process_websocket_message(websocket)
                except websockets.exceptions.ConnectionClosed:
                    print("WebSocket connection closed. Reconnecting...")
                    ping_task.cancel()

        except (
            websockets.exceptions.WebSocketException,
            ConnectionError,
            OSError,
        ) as e:
            print(f"Connection error: {e}")
            print(f"Reconnecting in {RECONNECT_DELAY} seconds...")
            await asyncio.sleep(RECONNECT_DELAY)


def main():
    """Main function to run the wallet transaction listener."""
    try:
        asyncio.run(listen_for_transactions())
    except KeyboardInterrupt:
        print("\nStopping wallet transaction listener...")
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

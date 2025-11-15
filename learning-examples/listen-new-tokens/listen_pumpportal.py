"""
Listens for new Pump.fun token creations via PumpPortal WebSocket.

Performance: Fast, real-time data via third-party API.

This script uses PumpPortal's WebSocket API, a third-party service that aggregates
and provides real-time Pump.fun token creation events. This provides additional
market data like initial buy amounts and market cap that aren't available in
raw blockchain data.

PumpPortal API: https://pumpportal.fun/

Note: This is a third-party service and requires trust in the data provider.
For trustless monitoring, use the direct blockchain listeners (logs, block, geyser).
"""

import asyncio
import json
from datetime import datetime

import websockets

# PumpPortal WebSocket URL
WS_URL = "wss://pumpportal.fun/api/data"


def print_token_info(token_data):
    """
    Print token information in a consistent, user-friendly format.

    Args:
        token_data: Dictionary containing token fields from PumpPortal
    """
    print("\n" + "=" * 80)
    print("🎯 NEW TOKEN DETECTED (via PumpPortal)")
    print("=" * 80)
    print(f"Name:             {token_data.get('name', 'N/A')}")
    print(f"Symbol:           {token_data.get('symbol', 'N/A')}")
    print(f"Mint:             {token_data.get('mint', 'N/A')}")

    # PumpPortal-specific fields
    if "initialBuy" in token_data:
        initial_buy_sol = token_data['initialBuy']
        print(f"Initial Buy:      {initial_buy_sol:.6f} SOL")

    if "marketCapSol" in token_data:
        market_cap_sol = token_data['marketCapSol']
        print(f"Market Cap:       {market_cap_sol:.6f} SOL")

    if "bondingCurveKey" in token_data:
        print(f"Bonding Curve:    {token_data['bondingCurveKey']}")

    if "traderPublicKey" in token_data:
        print(f"Creator:          {token_data['traderPublicKey']}")

    # Virtual reserves
    if "vSolInBondingCurve" in token_data:
        v_sol = token_data['vSolInBondingCurve']
        print(f"Virtual SOL:      {v_sol:.6f} SOL")

    if "vTokensInBondingCurve" in token_data:
        v_tokens = token_data['vTokensInBondingCurve']
        print(f"Virtual Tokens:   {v_tokens:,.0f}")

    if "uri" in token_data:
        print(f"URI:              {token_data['uri']}")

    if "signature" in token_data:
        print(f"Signature:        {token_data['signature']}")

    print("=" * 80 + "\n")



async def listen_for_new_tokens():
    async with websockets.connect(WS_URL) as websocket:
        # Subscribe to new token events
        await websocket.send(json.dumps({"method": "subscribeNewToken", "params": []}))

        print("Listening for new token creations...")

        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)

                if "method" in data and data["method"] == "newToken":
                    token_info = data.get("params", [{}])[0]
                elif "signature" in data and "mint" in data:
                    token_info = data
                else:
                    continue

                # Print token information in consistent format
                print_token_info(token_info)
            except websockets.exceptions.ConnectionClosed:
                print("\nWebSocket connection closed. Reconnecting...")
                break
            except json.JSONDecodeError:
                print(f"\nReceived non-JSON message: {message}")
            except Exception as e:
                print(f"\nAn error occurred: {e}")


async def main():
    while True:
        try:
            await listen_for_new_tokens()
        except Exception as e:
            print(f"\nAn error occurred: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

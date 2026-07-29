import asyncio
import json
import os

import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# Solana's blockSubscribe (and a busy logsSubscribe) sends frames well past
# websockets' 1 MiB default, which kills the connection with a 1009 close
# instead of delivering the message. Same value the bot's own listeners use.
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
PUMP_MIGRATOR_ID = Pubkey.from_string("39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg")


def process_initialize2_transaction(data):
    """Process and decode an initialize2 transaction"""
    try:
        signature = data["transaction"]["signatures"][0]
        account_keys = data["transaction"]["message"]["accountKeys"]

        # Check raydium_amm_idl.json for the account keys
        # The token address is typically the 19th account (index 18)
        # The liquidity pool address is typically the 3rd account (index 2)
        if len(account_keys) > 18:
            token_address = account_keys[18]
            liquidity_address = account_keys[2]

            print(f"\nSignature: {signature}")
            print(f"Token Address: {token_address}")
            print(f"Liquidity Address: {liquidity_address}")
            print("=" * 50)
        else:
            print(f"\nError: Not enough account keys (found {len(account_keys)})")

    except Exception as e:
        print(f"\nError: {e!s}")


async def listen_for_events():
    while True:
        try:
            async with websockets.connect(
                WSS_ENDPOINT, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
            ) as websocket:
                subscription_message = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "blockSubscribe",
                        "params": [
                            {"mentionsAccountOrProgram": str(PUMP_MIGRATOR_ID)},
                            {
                                "commitment": "confirmed",
                                "encoding": "json",
                                "showRewards": False,
                                "transactionDetails": "full",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    }
                )

                await websocket.send(subscription_message)
                response = await websocket.recv()
                print(f"Subscription response: {response}")
                print("\nListening for Raydium pool initialization events...")

                while True:
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=30)
                        data = json.loads(response)

                        if "method" in data and data["method"] == "blockNotification":
                            if "params" in data and "result" in data["params"]:
                                block_data = data["params"]["result"]
                                if (
                                    "value" in block_data
                                    and "block" in block_data["value"]
                                ):
                                    block = block_data["value"]["block"]
                                    if "transactions" in block:
                                        for tx in block["transactions"]:
                                            logs = tx.get("meta", {}).get(
                                                "logMessages", []
                                            )

                                            # Check for initialize2 instruction
                                            for log in logs:
                                                if (
                                                    "Program log: initialize2: InitializeInstruction2"
                                                    in log
                                                ):
                                                    print(
                                                        "Found initialize2 instruction!"
                                                    )
                                                    process_initialize2_transaction(tx)
                                                    break

                    except TimeoutError:
                        print("\nChecking connection...")
                        print("Connection alive")
                        continue

        except Exception as e:
            print(f"\nConnection error: {e!s}")
            print("Retrying in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(listen_for_events())

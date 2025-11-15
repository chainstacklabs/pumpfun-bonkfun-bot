"""
Listens to Solana blocks for Pump.fun 'create' instructions via WebSocket.
Decodes transaction data to extract mint, bonding curve, and user details.

Performance: Usually slower than other listeners due to block-level processing.

This script uses blockSubscribe which receives entire blocks containing transactions
that mention the Pump.fun program. It then decodes the instruction data from each
transaction to extract token creation details.

WebSocket API Reference:
https://solana.com/docs/rpc/websocket/blocksubscribe

Address Lookup Tables (ALT) Support:
https://solana.com/docs/advanced/lookup-tables
"""

import asyncio
import base64
import json
import os
import struct

import base58
import websockets
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

WSS_ENDPOINT = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Instruction discriminators (8-byte identifiers for instruction types)
# Calculated using the first 8 bytes of sha256("global:create") for legacy Create
# and sha256("global:createV2") for Token2022 CreateV2
# See: learning-examples/calculate_discriminator.py
CREATE_DISCRIMINATOR = 8576854823835016728
CREATE_V2_DISCRIMINATOR = struct.unpack("<Q", bytes([214, 144, 76, 236, 95, 139, 49, 180]))[0]


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

    if "bondingCurve" in token_data:
        print(f"Bonding Curve:    {token_data['bondingCurve']}")
    if "associatedBondingCurve" in token_data:
        print(f"Associated BC:    {token_data['associatedBondingCurve']}")
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


def load_idl(file_path):
    with open(file_path) as f:
        return json.load(f)


def decode_create_instruction(ix_data, ix_def, accounts):
    """
    Decode legacy Create instruction (Metaplex tokens).

    The Create instruction creates tokens using the Metaplex Token Metadata standard.
    Instruction data contains: name, symbol, uri, and additional creator pubkey.
    Account references are extracted from the accounts array.

    Args:
        ix_data: Raw instruction data bytes
        ix_def: Instruction definition from IDL
        accounts: List of account pubkeys involved in the instruction

    Returns:
        Dictionary containing decoded token information
    """
    args = {}
    offset = 8  # Skip 8-byte discriminator

    # Parse instruction arguments according to IDL definition
    for arg in ix_def["args"]:
        if arg["type"] == "string":
            # String format: 4-byte length prefix + UTF-8 encoded string
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif arg["type"] == "pubkey":
            # Pubkey is 32 bytes, encoded as base58
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")

        args[arg["name"]] = value

    # Extract account addresses from the accounts array
    # Account layout for Create instruction:
    # 0: mint, 1: metadata, 2: bondingCurve, 3: associatedBondingCurve,
    # 4: tokenProgram, 5: systemProgram, 6: rent, 7: user
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])
    args["token_standard"] = "legacy"
    args["is_mayhem_mode"] = False

    return args


def decode_create_v2_instruction(ix_data, ix_def, accounts):
    """
    Decode CreateV2 instruction (Token2022 tokens).

    The CreateV2 instruction creates tokens using the Token-2022 standard, which supports
    additional features like transfer fees, interest-bearing tokens, and more.
    This instruction includes an optional is_mayhem_mode flag.

    Token-2022 Reference:
    https://spl.solana.com/token-2022

    Args:
        ix_data: Raw instruction data bytes
        ix_def: Instruction definition from IDL
        accounts: List of account pubkeys involved in the instruction

    Returns:
        Dictionary containing decoded token information
    """
    args = {}
    offset = 8  # Skip 8-byte discriminator

    # Parse instruction arguments according to IDL definition
    for arg in ix_def["args"]:
        if arg["type"] == "string":
            # String format: 4-byte length prefix + UTF-8 encoded string
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif arg["type"] == "pubkey":
            # Pubkey is 32 bytes, encoded as base58
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")

        args[arg["name"]] = value

    # Parse is_mayhem_mode (OptionBool at the end)
    # Format: 1 byte (0 = false/None, 1 = true)
    is_mayhem_mode = False
    if offset < len(ix_data):
        is_mayhem_mode = bool(ix_data[offset])

    # Extract account addresses from the accounts array
    # Account layout for CreateV2 instruction:
    # 0: mint, 1: metadata, 2: bondingCurve, 3: associatedBondingCurve,
    # 4: tokenProgram (Token2022), 5: user, 6: systemProgram, 7: rent
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[5])
    args["token_standard"] = "token2022"
    args["is_mayhem_mode"] = is_mayhem_mode

    return args


async def listen_and_decode_create():
    """
    Main listener function that subscribes to Solana blocks and decodes Pump.fun token creations.

    This function:
    1. Loads the Pump.fun IDL for instruction parsing
    2. Subscribes to blocks mentioning the Pump.fun program
    3. Decodes transactions to extract Create/CreateV2 instructions
    4. Handles Address Lookup Tables (ALTs) for account resolution
    """
    idl = load_idl("idl/pump_fun_idl.json")

    async with websockets.connect(WSS_ENDPOINT) as websocket:
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
        print(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM_ID}")

        while True:
            try:
                response = await websocket.recv()
                data = json.loads(response)

                if "method" in data and data["method"] == "blockNotification":
                    if "params" in data and "result" in data["params"]:
                        block_data = data["params"]["result"]
                        if "value" in block_data and "block" in block_data["value"]:
                            block = block_data["value"]["block"]
                            if "transactions" in block:
                                for tx in block["transactions"]:
                                    if isinstance(tx, dict) and "transaction" in tx:
                                        tx_data_decoded = base64.b64decode(
                                            tx["transaction"][0]
                                        )
                                        transaction = VersionedTransaction.from_bytes(
                                            tx_data_decoded
                                        )

                                        # Extract loaded addresses from transaction metadata
                                        loaded_addresses = None
                                        if "meta" in tx and tx["meta"] and "loadedAddresses" in tx["meta"]:
                                            loaded_addresses = tx["meta"]["loadedAddresses"]

                                        for ix in transaction.message.instructions:
                                            if str(
                                                transaction.message.account_keys[
                                                    ix.program_id_index
                                                ]
                                            ) == str(PUMP_PROGRAM_ID):
                                                ix_data = bytes(ix.data)
                                                discriminator = struct.unpack(
                                                    "<Q", ix_data[:8]
                                                )[0]

                                                if discriminator == CREATE_DISCRIMINATOR:
                                                    # Legacy Create instruction (Metaplex tokens)
                                                    create_ix = next(
                                                        instr
                                                        for instr in idl["instructions"]
                                                        if instr["name"] == "create"
                                                    )
                                                    account_keys = get_account_keys(
                                                        transaction, ix, loaded_addresses
                                                    )
                                                    if account_keys is None:
                                                        print("⚠️  Skipping transaction due to unresolved accounts")
                                                        continue

                                                    # Decode instruction data
                                                    decoded_args = decode_create_instruction(
                                                        ix_data,
                                                        create_ix,
                                                        account_keys,
                                                    )

                                                    # Print token information
                                                    print_token_info(decoded_args)

                                                    # Note if using Address Lookup Tables
                                                    if loaded_addresses:
                                                        writable_count = len(loaded_addresses.get("writable", []))
                                                        readonly_count = len(loaded_addresses.get("readonly", []))
                                                        if writable_count > 0 or readonly_count > 0:
                                                            print(f"ℹ️  [ALT] Used Address Lookup Table: {writable_count} writable, {readonly_count} readonly\n")

                                                elif discriminator == CREATE_V2_DISCRIMINATOR:
                                                    # CreateV2 instruction (Token2022 tokens)
                                                    create_v2_ix = next(
                                                        (instr for instr in idl["instructions"]
                                                         if instr["name"] == "createV2"),
                                                        next(instr for instr in idl["instructions"]
                                                             if instr["name"] == "create")
                                                    )
                                                    account_keys = get_account_keys(
                                                        transaction, ix, loaded_addresses
                                                    )
                                                    if account_keys is None:
                                                        print("⚠️  Skipping transaction due to unresolved accounts")
                                                        continue

                                                    # Decode instruction data
                                                    decoded_args = decode_create_v2_instruction(
                                                        ix_data,
                                                        create_v2_ix,
                                                        account_keys,
                                                    )

                                                    # Print token information
                                                    print_token_info(decoded_args)

                                                    # Note if using Address Lookup Tables
                                                    if loaded_addresses:
                                                        writable_count = len(loaded_addresses.get("writable", []))
                                                        readonly_count = len(loaded_addresses.get("readonly", []))
                                                        if writable_count > 0 or readonly_count > 0:
                                                            print(f"ℹ️  [ALT] Used Address Lookup Table: {writable_count} writable, {readonly_count} readonly\n")
                elif "result" in data:
                    print("Subscription confirmed")
                else:
                    print(
                        f"Received unexpected message type: {data.get('method', 'Unknown')}"
                    )
            except Exception as e:
                print(f"An error occurred: {e!s}")
                print(f"Error details: {type(e).__name__}")
                import traceback

                traceback.print_exc()

    print("WebSocket connection closed.")


if __name__ == "__main__":
    asyncio.run(listen_and_decode_create())

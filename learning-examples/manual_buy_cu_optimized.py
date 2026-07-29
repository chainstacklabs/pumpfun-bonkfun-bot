"""
Pump.fun Token Buy Script with Compute Unit Optimization

This script is identical to manual_buy.py but adds SetLoadedAccountsDataSizeLimit
instruction. By default, Solana transactions can load up to 64MB of account data
(costing 16k CU). By setting a lower limit (512KB), we reduce CU consumption and
improve transaction priority.

Key difference from manual_buy.py:
- Adds set_loaded_accounts_data_size_limit(512_000) before other instructions

NOTE: The CU savings from this optimization are NOT visible in transaction "consumed CU"
metrics, which only show execution CU. The 16k CU loaded accounts overhead is counted
separately for transaction priority/cost calculation. This makes the real impact hard
to measure directly, but it improves priority.

Reference: https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
"""

import asyncio
import base64
import hashlib
import json
import os
import struct

import base58
import pump_v2
import tx_status
import websockets
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
)

# Discriminators
EXPECTED_DISCRIMINATOR = pump_v2.BONDING_CURVE_DISCRIMINATOR
TOKEN_DECIMALS = 6

# Global constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# 8 breaking-upgrade fee recipients (pump.fun program upgrade 2026-04-28).
# One must be appended (mutable) AFTER bonding-curve-v2 on every buy/sell.
# Doc: github.com/pump-fun/pump-public-docs/blob/main/docs/BREAKING_FEE_RECIPIENT.md
BREAKING_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
RPC_WEBSOCKET = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# logsSubscribe frames exceed the websockets library's 1 MiB default, which
# closes the connection with 1009 ("message too big").
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024


BondingCurveState = pump_v2.BondingCurveState


async def get_pump_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return pump_v2.BondingCurveState(data)


def calculate_pump_curve_price(curve_state: pump_v2.BondingCurveState) -> float:
    """Price of one whole token in whole units of the curve's quote asset.

    Args:
        curve_state: Parsed curve state

    Returns:
        Price in the quote asset

    Raises:
        ValueError: If reserves are empty
    """
    price = curve_state.price_per_token()
    if price <= 0:
        raise ValueError("Invalid reserve state")
    return price


def _find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)],
        PUMP_PROGRAM,
    )
    return derived_address


def _find_global_volume_accumulator() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"],
        PUMP_PROGRAM,
    )
    return derived_address


def _find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMP_PROGRAM,
    )
    return derived_address


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


def _find_bonding_curve_v2(mint: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"bonding-curve-v2", bytes(mint)],
        PUMP_PROGRAM,
    )
    return derived_address


async def get_fee_recipient(
    client: AsyncClient, curve_state: BondingCurveState
) -> Pubkey:
    """Determine the correct fee recipient based on mayhem mode.

    Mayhem mode tokens use a different fee recipient (reserved_fee_recipient from Global account)
    instead of the standard fee recipient. This function checks the bonding curve state
    and returns the appropriate fee recipient.

    Args:
        client: Solana RPC client to fetch Global account data
        curve_state: Parsed bonding curve state containing is_mayhem_mode flag

    Returns:
        Appropriate fee recipient pubkey (mayhem or standard)
    """
    if not curve_state.is_mayhem_mode:
        return PUMP_FEE

    # Fetch Global account to get reserved_fee_recipient for mayhem mode tokens
    response = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
    if not response.value or not response.value.data:
        # Fallback to standard fee if Global account cannot be fetched
        return PUMP_FEE

    data = response.value.data

    # Parse reserved_fee_recipient from Global account
    # Offset calculation based on pump_fun_idl.json Global struct:
    # discriminator(8) + initialized(1) + authority(32) + fee_recipient(32) +
    # initial_virtual_token_reserves(8) + initial_virtual_sol_reserves(8) +
    # initial_real_token_reserves(8) + token_total_supply(8) + fee_basis_points(8) +
    # withdraw_authority(32) + enable_migrate(1) + pool_migration_fee(8) +
    # creator_fee_basis_points(8) + fee_recipients[7](224) + set_creator_authority(32) +
    # admin_set_creator_authority(32) + create_v2_enabled(1) + whitelist_pda(32) = 483
    RESERVED_FEE_RECIPIENT_OFFSET = 483

    if len(data) < RESERVED_FEE_RECIPIENT_OFFSET + 32:
        # Fallback if account data is too short
        return PUMP_FEE

    reserved_fee_recipient_bytes = data[
        RESERVED_FEE_RECIPIENT_OFFSET : RESERVED_FEE_RECIPIENT_OFFSET + 32
    ]
    return Pubkey.from_bytes(reserved_fee_recipient_bytes)


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """
    Create SetLoadedAccountsDataSizeLimit instruction to reduce CU consumption.

    Solana defaults to 64MB loaded data limit (16k CU cost: 8 CU per 32KB).
    By setting a lower limit, you reduce CU consumption and improve tx priority.

    Args:
        bytes_limit: Max account data size in bytes (e.g., 512_000 = 512KB)

    Returns:
        Compute Budget instruction (discriminator 4)
    """
    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


async def buy_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    token_program: Pubkey,
    amount: float,
    slippage: float = 0.25,
    max_retries=5,
):
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        # Fetch bonding curve state to calculate price and determine fee recipient
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        token_amount = amount / token_price_sol

        # Determine fee recipient based on whether token uses mayhem mode

        # buy_v2 takes 27 mandatory accounts in a fixed order for every coin.
        quote_mint = pump_v2.normalize_quote_mint(
            getattr(curve_state, "quote_mint", None)
        )
        quote_unit = pump_v2.quote_units(quote_mint)
        print(f"Quote asset: {quote_mint}")

        buy_ix = pump_v2.build_buy_v2_instruction(
            base_mint=mint,
            creator=curve_state.creator,
            user=payer.pubkey(),
            token_amount_raw=int(token_amount * 10**TOKEN_DECIMALS),
            max_quote_cost_raw=int(amount * quote_unit * (1 + slippage)),
            quote_mint=quote_mint,
            base_token_program=token_program,
            is_mayhem_mode=curve_state.is_mayhem_mode,
        )
        idempotent_ata_ix = create_idempotent_associated_token_account(
            payer.pubkey(), payer.pubkey(), mint, token_program_id=token_program
        )

        # CU OPTIMIZATION: Limit account data to 512KB (down from 64MB default)
        # This reduces CU cost from 16k to ~128 CU and improves tx priority.
        # Must be placed FIRST in the instruction list.
        # 16MB limit: works for cashback-coin Token-2022 buys on mainnet.
        # Smaller values (4–8MB) trigger MaxLoadedAccountsDataSizeExceeded for
        # Token-2022 mints with extensions. Still 4× smaller than the 64MB default.
        cu_limit_ix = set_loaded_accounts_data_size_limit(16_384_000)

        msg = Message(
            [cu_limit_ix, set_compute_unit_price(1_000), idempotent_ata_ix, buy_ix],
            payer.pubkey(),
        )
        recent_blockhash = await client.get_latest_blockhash()
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)

        for attempt in range(max_retries):
            try:
                tx_buy = await client.send_transaction(
                    Transaction(
                        [payer],
                        msg,
                        recent_blockhash.value.blockhash,
                    ),
                    opts=opts,
                )
                tx_hash = tx_buy.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await tx_status.confirm_and_assert(client, tx_hash)
                print("Transaction confirmed")
                return  # Success, exit the function
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {str(e)[:50]}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    print(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")


def load_idl(file_path):
    with open(file_path) as f:
        return json.load(f)


def calculate_discriminator(instruction_name):
    sha = hashlib.sha256()
    sha.update(instruction_name.encode("utf-8"))
    return struct.unpack("<Q", sha.digest()[:8])[0]


def decode_create_instruction(ix_data, ix_def, accounts):
    """Decode create instruction from transaction data."""
    args = {}
    offset = 8
    for arg in ix_def["args"]:
        t = arg["type"]
        if t == "string":
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif t == "pubkey":
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        elif t == "bool" or (isinstance(t, dict) and "defined" in t):
            # `bool` and `OptionBool` (struct {bool}) both serialize as 1 byte
            value = bool(ix_data[offset])
            offset += 1
        else:
            raise ValueError(f"Unsupported type: {t}")
        args[arg["name"]] = value

    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])

    return args


async def listen_for_create_transaction():
    """Listen for new token creation on pump.fun."""
    idl_path = os.path.join(os.path.dirname(__file__), "..", "idl", "pump_fun_idl.json")
    idl = load_idl(idl_path)
    create_discriminator = calculate_discriminator("global:create")
    create_v2_discriminator = calculate_discriminator("global:create_v2")

    async with websockets.connect(
        RPC_WEBSOCKET, max_size=WEBSOCKET_MAX_MESSAGE_BYTES
    ) as websocket:
        subscription_message = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "blockSubscribe",
                "params": [
                    {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
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
        print(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM}")

        while True:
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

                                    for ix in transaction.message.instructions:
                                        if str(
                                            transaction.message.account_keys[
                                                ix.program_id_index
                                            ]
                                        ) == str(PUMP_PROGRAM):
                                            ix_data = bytes(ix.data)
                                            discriminator = struct.unpack(
                                                "<Q", ix_data[:8]
                                            )[0]

                                            # Check which create instruction was used
                                            instruction_name = None
                                            token_program = None

                                            if discriminator == create_discriminator:
                                                instruction_name = "create"
                                                token_program = SYSTEM_TOKEN_PROGRAM
                                            elif (
                                                discriminator == create_v2_discriminator
                                            ):
                                                instruction_name = "create_v2"
                                                token_program = TOKEN_2022_PROGRAM

                                            if instruction_name:
                                                create_ix = next(
                                                    instr
                                                    for instr in idl["instructions"]
                                                    if instr["name"] == instruction_name
                                                )
                                                # Skip txs that use Address Lookup Tables — their
                                                # instruction account indices reference ALT-loaded keys
                                                # not present in transaction.message.account_keys.
                                                static_keys = (
                                                    transaction.message.account_keys
                                                )
                                                if any(
                                                    idx >= len(static_keys)
                                                    for idx in ix.accounts
                                                ):
                                                    continue
                                                account_keys = [
                                                    str(
                                                        transaction.message.account_keys[
                                                            index
                                                        ]
                                                    )
                                                    for index in ix.accounts
                                                ]
                                                decoded_args = (
                                                    decode_create_instruction(
                                                        ix_data, create_ix, account_keys
                                                    )
                                                )
                                                # Add token program info to decoded args
                                                decoded_args["token_program"] = str(
                                                    token_program
                                                )
                                                decoded_args["is_token_2022"] = (
                                                    token_program == TOKEN_2022_PROGRAM
                                                )
                                                return decoded_args


async def main():
    print("Waiting for a new token creation...")
    token_data = await listen_for_create_transaction()
    print("New token created:")
    print(json.dumps(token_data, indent=2))

    print("\nWaiting 15 seconds for things to stabilize...")
    await asyncio.sleep(15)

    mint = Pubkey.from_string(token_data["mint"])
    bonding_curve = Pubkey.from_string(token_data["bondingCurve"])
    associated_bonding_curve = Pubkey.from_string(token_data["associatedBondingCurve"])
    creator_vault = pump_v2.find_creator_vault(
        Pubkey.from_string(token_data["creator"])
    )
    token_program = Pubkey.from_string(token_data["token_program"])

    # Fetch the token price
    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)

    # Amount of SOL to spend (adjust as needed)
    amount = 0.000_001  # 0.00001 SOL
    slippage = 0.3  # 30% slippage tolerance

    print(f"Bonding curve address: {bonding_curve}")
    print(
        f"Token Program: {token_program} ({'Token2022' if token_data['is_token_2022'] else 'Standard Token'})"
    )
    print(f"Token price: {token_price_sol:.10f} SOL")
    print(
        f"Buying {amount:.6f} SOL worth of the new token with {slippage * 100:.1f}% slippage tolerance..."
    )
    print("CU Optimization: Enabled (16MB account data limit)")
    await buy_token(
        mint,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        token_program,
        amount,
        slippage,
    )


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import json
import os
import struct
import sys

import base58
import grpc
import pump_v2
import tx_status
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.geyser.generated import (
    geyser_pb2,
    geyser_pb2_grpc,
)

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
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
SYSTEM_TOKEN_2022_PROGRAM = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000

# RPC ENDPOINTS
RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
# Geyser endpoints
GEYSER_ENDPOINT = os.environ.get("GEYSER_ENDPOINT")
GEYSER_API_TOKEN = os.environ.get("GEYSER_API_TOKEN")
AUTH_TYPE = os.environ.get("GEYSER_AUTH_TYPE", "x-token")  # Default to x-token

PUMP_CREATE_DISCRIMINATOR = struct.pack("<Q", 8576854823835016728)
PUMP_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])


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


async def create_geyser_connection():
    """Establish a secure connection to the Geyser endpoint using the configured auth type."""
    if AUTH_TYPE == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback((("x-token", GEYSER_API_TOKEN),), None)
        )
    else:  # Default to basic auth
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback(
                (("authorization", f"Basic {GEYSER_API_TOKEN}"),), None
            )
        )

    creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
    channel = grpc.aio.secure_channel(GEYSER_ENDPOINT, creds)
    return geyser_pb2_grpc.GeyserStub(channel)


def create_subscription_request():
    """Create a subscription request for Pump.fun transactions."""
    request = geyser_pb2.SubscribeRequest()
    request.transactions["pump_filter"].account_include.append(str(PUMP_PROGRAM))
    request.transactions["pump_filter"].failed = False
    request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return request


def decode_create_instruction_geyser(ix_data: bytes, keys, accounts) -> dict:
    """Decode a create instruction from Geyser transaction data."""
    # Skip past the 8-byte discriminator prefix
    offset = 8

    # Extract account keys in base58 format
    def get_account_key(index):
        if index >= len(accounts):
            return "N/A"
        account_index = accounts[index]
        return base58.b58encode(keys[account_index]).decode()

    # Read string fields (prefixed with length)
    def read_string():
        nonlocal offset
        # Get string length (4-byte uint)
        length = struct.unpack_from("<I", ix_data, offset)[0]
        offset += 4
        # Extract and decode the string
        value = ix_data[offset : offset + length].decode()
        offset += length
        return value

    def read_pubkey():
        nonlocal offset
        value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
        offset += 32
        return value

    name = read_string()
    symbol = read_string()
    uri = read_string()
    creator = read_pubkey()

    return {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "creator": creator,
        "mint": get_account_key(0),
        "bondingCurve": get_account_key(2),
        "associatedBondingCurve": get_account_key(3),
        "user": get_account_key(7),
    }


async def listen_for_create_transaction_geyser():
    """Listen for new token creation using Geyser."""
    print(f"Connecting to Geyser using {AUTH_TYPE.upper()} authentication...")
    stub = await create_geyser_connection()
    request = create_subscription_request()

    print("Subscribed to Pump.fun transactions via Geyser")

    async for update in stub.Subscribe(iter([request])):
        # Skip non-transaction updates
        if not update.HasField("transaction"):
            continue

        tx = update.transaction.transaction.transaction
        msg = getattr(tx, "message", None)
        if msg is None:
            continue

        # Check each instruction in the transaction
        for ix in msg.instructions:
            # Check which create instruction was used
            token_program = None
            if ix.data.startswith(PUMP_CREATE_DISCRIMINATOR):
                token_program = SYSTEM_TOKEN_PROGRAM
            elif ix.data.startswith(PUMP_CREATE_V2_DISCRIMINATOR):
                token_program = SYSTEM_TOKEN_2022_PROGRAM
            else:
                continue

            # Skip txs whose create ix references ALT-loaded accounts not in
            # msg.account_keys (indices >= len(msg.account_keys)).
            if any(idx >= len(msg.account_keys) for idx in ix.accounts):
                continue

            # Found a create instruction
            token_data = decode_create_instruction_geyser(
                ix.data, msg.account_keys, ix.accounts
            )

            # Add token program info to decoded args
            token_data["token_program"] = str(token_program)
            token_data["is_token_2022"] = token_program == SYSTEM_TOKEN_2022_PROGRAM

            signature = base58.b58encode(
                bytes(update.transaction.transaction.signature)
            ).decode()
            print(f"Transaction signature: {signature}")

            return token_data


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
        # Fetch bonding curve state for price, mayhem mode and quote asset.
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        token_amount = amount / token_price_sol

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
            payer.pubkey(), payer.pubkey(), mint, token_program
        )
        msg = Message(
            [set_compute_unit_price(1_000), idempotent_ata_ix, buy_ix], payer.pubkey()
        )
        recent_blockhash = await client.get_latest_blockhash()
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)

        print("Simulating transaction...")
        try:
            sim_result = await client.simulate_transaction(
                Transaction(
                    [payer],
                    msg,
                    recent_blockhash.value.blockhash,
                ),
            )
            print(f"Simulation result: {sim_result}")
            if sim_result.value.err:
                print(f"Simulation error: {sim_result.value.err}")
        except Exception as e:
            print(f"Simulation failed: {e}")

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


async def main():
    print("Waiting for a new token creation...")
    token_data = await listen_for_create_transaction_geyser()
    print("New token created:")
    print(json.dumps(token_data, indent=2))

    sleep_duration_sec = 15
    print(
        f"Waiting {sleep_duration_sec}s for the bonding curve account to propagate..."
    )
    await asyncio.sleep(sleep_duration_sec)

    mint = Pubkey.from_string(token_data["mint"])
    bonding_curve = Pubkey.from_string(token_data["bondingCurve"])
    associated_bonding_curve = Pubkey.from_string(token_data["associatedBondingCurve"])
    creator_vault = pump_v2.find_creator_vault(
        Pubkey.from_string(token_data["creator"])
    )
    token_program = Pubkey.from_string(token_data["token_program"])

    # Fetch the token price
    # async with AsyncClient(RPC_ENDPOINT) as client:
    #    curve_state = await get_pump_curve_state(client, bonding_curve)
    #    token_price_sol = calculate_pump_curve_price(curve_state)

    # Amount of SOL to spend (adjust as needed)
    amount = 0.000_01  # 0.00001 SOL
    slippage = 0.3  # 30% slippage tolerance

    print(f"Bonding curve address: {bonding_curve}")
    print(
        f"Token Program: {token_program} ({'Token2022' if token_data['is_token_2022'] else 'Standard Token'})"
    )
    # print(f"Token price: {token_price_sol:.10f} SOL")
    print(
        f"Buying {amount:.6f} SOL worth of the new token with {slippage * 100:.1f}% slippage tolerance..."
    )
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

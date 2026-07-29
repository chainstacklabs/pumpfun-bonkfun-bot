"""Buy the next pump.fun coin to be created, using buy_v2.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run learning-examples/manual_buy.py
    uv run learning-examples/manual_buy.py --cu-optimized

`--cu-optimized` adds a SetLoadedAccountsDataSizeLimit instruction. A transaction
may load up to 64 MB of account data by default, which is billed at 16k CU toward
the fee and priority calculation. Declaring a smaller ceiling lowers that share.
The saving does not show up in a transaction's reported `unitsConsumed`, which
only covers execution, so it is hard to measure directly from a receipt.

Do not lower the limit too far: 16 MB is still 4x smaller than the default and
leaves room for Token-2022 mints with extensions, while 512 KB is rejected with
MaxLoadedAccountsDataSizeExceeded on exactly those coins.

Reference: https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
"""

import asyncio
import base64
import hashlib
import json
import os
import struct
import sys

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

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR = pump_v2.BONDING_CURVE_DISCRIMINATOR
TOKEN_DECIMALS = 6

COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
# 16 MB. Enough for Token-2022 mints carrying extensions, and still 4x below the
# 64 MB default; 4-8 MB is rejected with MaxLoadedAccountsDataSizeExceeded.
LOADED_ACCOUNTS_DATA_SIZE_LIMIT = 16_384_000

# Global constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000


# RPC ENDPOINTS
load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
RPC_WEBSOCKET = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")

# logsSubscribe frames exceed the websockets library's 1 MiB default, which
# closes the connection with 1009 ("message too big").
WEBSOCKET_MAX_MESSAGE_BYTES = 32 * 1024 * 1024


# The bonding curve account and the v2 instruction layout live in pump_v2 so
# every example shares one copy. See learning-examples/pump_v2.py.
BondingCurveState = pump_v2.BondingCurveState


async def get_pump_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> pump_v2.BondingCurveState:
    """Fetch and parse a bonding curve account.

    Args:
        conn: Solana RPC client
        curve_address: Bonding curve address

    Returns:
        Parsed curve state

    Raises:
        ValueError: If the account is missing or not a bonding curve
    """
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    return pump_v2.BondingCurveState(response.value.data)


def calculate_pump_curve_price(curve_state: pump_v2.BondingCurveState) -> float:
    """Price of one whole token in whole quote units.

    Args:
        curve_state: Parsed curve state

    Returns:
        Price in the curve's quote asset

    Raises:
        ValueError: If reserves are empty
    """
    price = curve_state.price_per_token()
    if price <= 0:
        raise ValueError("Invalid reserve state")
    return price


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """Build a SetLoadedAccountsDataSizeLimit compute-budget instruction.

    solders does not ship a helper for this one, so encode it by hand: the
    compute-budget program takes a 1-byte discriminator (4) and a u32 limit.

    Args:
        bytes_limit: Max account data the transaction may load, in bytes

    Returns:
        The compute-budget instruction
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
    *,
    cu_optimized: bool = False,
):
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        # Fetch bonding curve state for price, mayhem mode and quote asset.
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)

        # Amounts are denominated in the curve's quote asset, which is not
        # necessarily SOL any more.
        quote_mint = pump_v2.normalize_quote_mint(
            getattr(curve_state, "quote_mint", None)
        )
        quote_unit = pump_v2.quote_units(quote_mint)
        token_amount = amount / token_price_sol
        max_quote_cost = int(amount * quote_unit * (1 + slippage))

        print(f"Quote asset: {quote_mint}")
        print(f"Buying {token_amount:.6f} tokens, max cost {max_quote_cost} raw units")

        # buy_v2 takes 27 mandatory accounts in a fixed order for every coin.
        buy_ix = pump_v2.build_buy_v2_instruction(
            base_mint=mint,
            creator=curve_state.creator,
            user=payer.pubkey(),
            token_amount_raw=int(token_amount * 10**TOKEN_DECIMALS),
            max_quote_cost_raw=max_quote_cost,
            quote_mint=quote_mint,
            base_token_program=token_program,
            is_mayhem_mode=curve_state.is_mayhem_mode,
        )

        instructions = []
        if cu_optimized:
            # Must come first, before the instructions it applies to.
            instructions.append(
                set_loaded_accounts_data_size_limit(LOADED_ACCOUNTS_DATA_SIZE_LIMIT)
            )
        instructions += [
            set_compute_unit_price(1_000),
            create_idempotent_associated_token_account(
                payer.pubkey(), payer.pubkey(), mint, token_program_id=token_program
            ),
        ]
        # SOL-paired coins settle in native SOL and only seed-check the quote
        # ATA, so creating it would waste rent. Other quotes need a real account.
        if not pump_v2.is_sol_paired(quote_mint):
            instructions.append(
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    quote_mint,
                    token_program_id=pump_v2.quote_token_program(quote_mint),
                )
            )
        instructions.append(buy_ix)

        msg = Message(instructions, payer.pubkey())
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
            except tx_status.TransactionRevertedError as e:
                # The signature is already on chain and reverted. The message and
                # blockhash below are fixed, so a retry would resubmit identical
                # bytes and revert identically — stop instead of burning attempts.
                print(f"Transaction reverted on-chain, not retrying: {e}")
                return
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
    args = {}
    offset = 8  # Skip 8-byte discriminator

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
        elif t == "bool":
            value = bool(ix_data[offset])
            offset += 1
        elif isinstance(t, dict) and "defined" in t:
            # OptionBool = struct { bool } = 1 byte
            value = bool(ix_data[offset])
            offset += 1
        else:
            raise ValueError(f"Unsupported type: {t}")

        args[arg["name"]] = value

    # Add accounts
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])

    return args


async def listen_for_create_transaction():
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
                                                    str(static_keys[index])
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


async def main(*, cu_optimized: bool = False):
    if cu_optimized:
        print("Compute-unit optimization enabled (SetLoadedAccountsDataSizeLimit)")
    print("Waiting for a new token creation...")
    token_data = await listen_for_create_transaction()
    print("New token created:")
    print(json.dumps(token_data, indent=2))

    sleep_duration_sec = 15
    print(f"Waiting for {sleep_duration_sec} seconds for things to stabilize...")
    await asyncio.sleep(sleep_duration_sec)

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
    await buy_token(
        mint,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        token_program,
        amount,
        slippage,
        cu_optimized=cu_optimized,
    )


if __name__ == "__main__":
    asyncio.run(main(cu_optimized="--cu-optimized" in sys.argv))

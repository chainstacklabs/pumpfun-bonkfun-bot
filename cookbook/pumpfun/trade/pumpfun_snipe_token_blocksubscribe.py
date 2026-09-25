"""Buy the next pump.fun coin to be created, using buy_v2.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_snipe_token_blocksubscribe.py
    uv run cookbook/pumpfun/trade/pumpfun_snipe_token_blocksubscribe.py --cu-optimized

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

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
from pathlib import Path

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions_v2.py sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions_v2 as pump_v2
import solana_transaction_status as tx_status
import websockets
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.compute_budget import set_compute_unit_price
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
)

# Here and later all the discriminators are precalculated. See cookbook/solana/anchor_calculate_discriminator.py
EXPECTED_DISCRIMINATOR = pump_v2.BONDING_CURVE_DISCRIMINATOR
TOKEN_DECIMALS = 6

COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
# 16 MB. Enough for Token-2022 mints carrying extensions, and still 4x below the
# 64 MB default; 4-8 MB is rejected with MaxLoadedAccountsDataSizeExceeded.
# Defaults for the command line below, not fixed settings.
DEFAULT_BUY_AMOUNT_SOL = 0.000_001
DEFAULT_SLIPPAGE = 0.3

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
# every example shares one copy. See cookbook/pumpfun/trade/pumpfun_instructions_v2.py.
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


async def _get_account_info(conn: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping AsyncClient's `.value` envelope.

    Adapter for `pump_v2.resolve_quote_token_program`, which expects a
    getter returning the account object (with an `.owner` attribute)
    directly rather than solana-py's RPC response wrapper.

    Args:
        conn: Solana RPC client
        address: Account to fetch

    Raises:
        ValueError: If the account does not exist
    """
    response = await conn.get_account_info(address)
    if response.value is None:
        raise ValueError(f"Could not fetch account info for {address}")
    return response.value


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

        # Amounts are denominated in the curve's quote asset, which is not
        # necessarily SOL any more.
        quote_mint = pump_v2.normalize_quote_mint(
            getattr(curve_state, "quote_mint", None)
        )

        # Resolve the quote mint before pricing: one read gives both the token
        # program -- Token-2022 for every tokenized equity pump.fun admits -- and
        # the decimals the price and cap are in. Price first and both numbers are
        # off by a power of ten in the same direction, so they compound.
        quote_token_program_id = await pump_v2.resolve_quote_token_program(
            quote_mint, lambda pk: _get_account_info(client, pk)
        )
        quote_unit = pump_v2.quote_units(quote_mint)

        token_price_sol = calculate_pump_curve_price(curve_state)
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
            quote_token_program_id=quote_token_program_id,
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
                    token_program_id=quote_token_program_id,
                )
            )
        instructions.append(buy_ix)

        recent_blockhash = await client.get_latest_blockhash()
        # The blockhash is fixed for every attempt, so the transaction is built
        # once: each retry resubmits identical bytes.
        transaction = VersionedTransaction(
            MessageV0.try_compile(
                payer.pubkey(), instructions, [], recent_blockhash.value.blockhash
            ),
            [payer],
        )
        opts = TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed)

        for attempt in range(max_retries):
            try:
                tx_buy = await client.send_transaction(
                    transaction,
                    opts=opts,
                )
                tx_hash = tx_buy.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await tx_status.confirm_and_assert(client, tx_hash)
                print("Transaction confirmed")
                return  # Success, exit the function
            except tx_status.TransactionRevertedError as e:
                # The signature is already on chain and reverted. The
                # transaction above is fixed, so a retry would resubmit
                # identical bytes and revert identically — stop instead of
                # burning attempts.
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


# First 8 bytes of sha256("event:CreateEvent"). Anchor emits the event as a
# base64 "Program data:" log line, already decoded by the RPC, so reading it
# works for every transaction version.
CREATE_EVENT_DISCRIMINATOR = bytes([27, 114, 169, 77, 222, 235, 99, 118])

# The two log lines the pump.fun program writes when it creates a coin.
CREATE_INSTRUCTION_LOGS = (
    "Program log: Instruction: Create",
    "Program log: Instruction: CreateV2",
)

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
]


def parse_create_event(data):
    """Decode a CreateEvent payload into a dict.

    Args:
        data: Raw event bytes, discriminator included

    Returns:
        The decoded fields, or None if this is not a CreateEvent
    """
    if len(data) < 8 or data[:8] != CREATE_EVENT_DISCRIMINATOR:
        return None
    offset = 8
    parsed = {}
    for name, kind in _CREATE_EVENT_FIELDS:
        try:
            if kind == "string":
                length = struct.unpack_from("<I", data, offset)[0]
                offset += 4
                parsed[name] = data[offset : offset + length].decode("utf-8", "replace")
                offset += length
            elif kind == "publicKey":
                parsed[name] = str(Pubkey.from_bytes(data[offset : offset + 32]))
                offset += 32
            elif kind == "u64":
                offset += 8
            elif kind == "i64":
                offset += 8
        except (struct.error, IndexError):
            break
    return parsed


def token_info_from_logs(logs):
    """Build the sniper's token dict from a transaction's log messages.

    The version-agnostic route. The event carries the mint, the curve and the
    creator; the associated bonding curve is an ordinary ATA of the curve, so it
    is derived rather than read out of the instruction's account list.

    Args:
        logs: `meta.logMessages` for one transaction

    Returns:
        The token fields the buy path needs, or None if no coin was created
    """
    # Matched whole: Anchor writes `Instruction: <Name>` for every program, so
    # a substring test also accepts CreateTokenAccount, CreatePool and the rest
    # from whichever programs share the transaction.
    if not any(log in CREATE_INSTRUCTION_LOGS for log in logs):
        return None
    for log in logs:
        if "Program data:" not in log:
            continue
        try:
            payload = base64.b64decode(log.split(": ", 1)[1])
        except (ValueError, IndexError):
            continue
        event = parse_create_event(payload)
        if not event or "bondingCurve" not in event:
            continue

        token_program = Pubkey.from_string(
            event.get("token_program", str(TOKEN_2022_PROGRAM))
        )
        mint = Pubkey.from_string(event["mint"])
        curve = Pubkey.from_string(event["bondingCurve"])
        event["associatedBondingCurve"] = str(
            pump_v2.find_associated_token_account(curve, mint, token_program)
        )
        event["token_program"] = str(token_program)
        event["is_token_2022"] = token_program == TOKEN_2022_PROGRAM
        return event
    return None


async def listen_for_create_transaction():
    """Wait for the next coin creation and return what the buy path needs.

    Returns:
        The token fields decoded from the CreateEvent in a block's logs
    """
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
                        "maxSupportedTransactionVersion": 1,
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
                        # `block` is null for a skipped or unavailable slot:
                        # the key is present, the value is not.
                        if block and "transactions" in block:
                            for tx in block["transactions"]:
                                if not isinstance(tx, dict):
                                    continue
                                # Route on logs: deserializing the envelope here
                                # skips every transaction version solders
                                # cannot read, so a coin created in one is
                                # never sniped.
                                meta = tx.get("meta") or {}
                                token_data = token_info_from_logs(
                                    meta.get("logMessages") or []
                                )
                                if token_data:
                                    return token_data


async def snipe(amount: float, slippage: float, *, cu_optimized: bool = False):
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


def main() -> None:
    """Parse the command line and snipe the next coin."""
    parser = argparse.ArgumentParser(
        description="Wait for the next pump.fun coin, then buy it"
    )
    parser.add_argument(
        "amount",
        nargs="?",
        type=float,
        default=DEFAULT_BUY_AMOUNT_SOL,
        help=f"SOL to spend (default {DEFAULT_BUY_AMOUNT_SOL})",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Slippage tolerance (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--cu-optimized",
        action="store_true",
        help="Add a SetLoadedAccountsDataSizeLimit instruction",
    )
    args = parser.parse_args()

    asyncio.run(snipe(args.amount, args.slippage, cu_optimized=args.cu_optimized))


if __name__ == "__main__":
    main()

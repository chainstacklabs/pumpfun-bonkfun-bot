"""Create a pump.fun coin with create_v2 and buy it, in two v0 transactions.

WARNING: this submits real transactions and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2_txv0.py
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2_txv0.py --mayhem
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2_txv0.py --creator-fee-bps 300
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2_txv0.py --holder-reward

Create and buy travel as two separate v0 transactions, because together they do
not fit what a v0 transaction can carry. **Anyone watching the create can buy
in the gap between them**; that is the cost of staying on v0, and it is the
reason the v1 script exists.

Use this one when the endpoint will not take a v1 send. Everything about the
coin is the same either way — the quote asset, the creator fee, holder rewards
and mayhem mode all behave identically.

`pumpfun_create_and_buy_token_v2_txv1.py` does it in a single transaction, and
the coin is then bought by the transaction that creates it.

`--amount` is in whole units of the coin's quote asset -- SOL by default, and
whatever `--quote-mint` names otherwise. For a non-SOL quote the wallet must
already hold that asset in its associated token account; this script does not
acquire it.

**A creator fee only takes effect on a coin priced in something other than
SOL.** pump.fun accepts `--creator-fee-bps` on a SOL-paired coin and stores
zero, so it is refused there rather than silently ignored.

`pumpfun_create_token_v2.py` is the create half on its own, and
`pumpfun_buy_token_v2.py` the buy half.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Final

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions_v2.py sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions_v2 as pump_v2
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import create_idempotent_associated_token_account

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

BPS_DENOMINATOR: Final[int] = 10_000
COMPUTE_UNIT_LIMIT: Final[int] = 350_000
# v0 states the priority fee as micro-lamports per compute unit, through a
# ComputeBudget instruction. The v1 script states one absolute lamport total.
PRIORITY_FEE_MICROLAMPORTS: Final[int] = 37_037

# Defaults for the command line below, not fixed settings. Mayhem is off because
# the plain coin is the one a reader gets by typing nothing.
DEFAULT_TOKEN_NAME = "Test Token V2"
DEFAULT_TOKEN_SYMBOL = "TEST2"
DEFAULT_TOKEN_URI = "https://example.com/token-v2.json"
DEFAULT_BUY_AMOUNT_SOL = 0.0001
DEFAULT_SLIPPAGE = 0.3
DEFAULT_MAYHEM = False


async def get_account(client: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping solana-py's `.value` envelope.

    Raises:
        ValueError: If the account does not exist
    """
    response = await client.get_account_info(address, encoding="base64")
    if response.value is None:
        msg = f"Account not found: {address}"
        raise ValueError(msg)
    return response.value


def check_creator_fee(
    global_state: dict, creator_fee_bps: int | None, quote_mint: Pubkey
) -> None:
    """Refuse a creator fee the program will not accept or will not apply.

    Bounds come from Global rather than a literal, because both the switch and
    the ceiling move under `update_creator_fee_config`.

    Args:
        global_state: Decoded Global account
        creator_fee_bps: Requested fee, or None if the arg is being omitted
        quote_mint: Asset the coin is priced in

    Raises:
        ValueError: If a fee is requested on a SOL-paired coin, while fees are
            not configurable, or above the current ceiling
    """
    if not creator_fee_bps:
        return
    if pump_v2.is_sol_paired(quote_mint) or quote_mint == pump_v2.WSOL_MINT:
        msg = (
            "A creator fee only takes effect on a coin priced in something "
            "other than SOL; pump.fun would accept the argument and store 0. "
            "Pass --quote-mint."
        )
        raise ValueError(msg)
    if not global_state.get("creator_fee_configurable"):
        msg = (
            "Global.creator_fee_configurable is false; the program is not "
            "accepting a configurable creator fee right now"
        )
        raise ValueError(msg)
    ceiling = global_state.get("max_configurable_creator_fee_bps", 0)
    if creator_fee_bps > ceiling:
        msg = (
            f"--creator-fee-bps {creator_fee_bps} is above Global's current "
            f"max_configurable_creator_fee_bps of {ceiling}"
        )
        raise ValueError(msg)


def size_opening_buy(
    global_state: dict,
    amount_raw: int,
    opening_quote_reserves: int,
    creator_fee_bps: int | None,
) -> int:
    """Tokens to ask for, given what the opening curve and the fees will be.

    The reserves and the two fee rates are read, not assumed: the opening quote
    reserve differs per quote asset by orders of magnitude, and the fee total
    moves per coin as soon as a creator fee is set.

    Args:
        global_state: Decoded Global account
        amount_raw: Spend, in the quote mint's raw units
        opening_quote_reserves: What the curve opens with, same raw units
        creator_fee_bps: Creator fee for this coin, or None for Global's rate

    Returns:
        Base tokens to request, raw units
    """
    virtual_tokens = global_state["initial_virtual_token_reserves"]
    creator_rate = (
        global_state["creator_fee_basis_points"]
        if creator_fee_bps is None
        else creator_fee_bps
    )
    fee_bps = global_state["fee_basis_points"] + creator_rate

    reaching_curve = amount_raw * (BPS_DENOMINATOR - fee_bps) // BPS_DENOMINATOR
    # Linear at the opening price rather than constant-product. At these sizes
    # against the opening reserves the curvature is below rounding; the slippage
    # cap is what actually bounds the spend.
    return reaching_curve * virtual_tokens // opening_quote_reserves


async def check_quote_balance(
    client: AsyncClient,
    user: Pubkey,
    quote_mint: Pubkey,
    quote_program: Pubkey,
    needed_raw: int,
) -> None:
    """Refuse a buy the wallet cannot pay for in the coin's quote asset.

    A SOL-paired coin settles in native SOL and its quote ATA is only
    seed-constrained, so it is never read or created. Every other quote asset
    has to be held already; this script does not acquire one.

    Args:
        client: Solana RPC client
        user: Buying wallet
        quote_mint: Asset the coin is priced in
        quote_program: Token program owning quote_mint
        needed_raw: Spend cap, in the quote mint's raw units

    Raises:
        ValueError: If the wallet's quote account is missing or short
    """
    if pump_v2.is_sol_paired(quote_mint) or quote_mint == pump_v2.WSOL_MINT:
        return
    ata = pump_v2.find_associated_token_account(user, quote_mint, quote_program)
    try:
        balance = await client.get_token_account_balance(ata)
        held = int(balance.value.amount) if balance.value else 0
    except Exception:  # noqa: BLE001 - a missing account is the same answer
        held = 0
    if held < needed_raw:
        msg = (
            f"Wallet holds {held} raw units of {quote_mint} but the buy caps "
            f"at {needed_raw}. Fund {ata} first."
        )
        raise ValueError(msg)


async def run(  # noqa: PLR0913
    *,
    name: str,
    symbol: str,
    uri: str,
    creator: Pubkey | None,
    buy_amount: float,
    slippage: float,
    mayhem: bool,
    creator_fee_bps: int | None,
    holder_reward: bool | None,
    quote_mint: Pubkey,
) -> None:
    """Create a coin, then buy it in a second transaction.

    Args:
        name: Coin name
        symbol: Coin ticker
        uri: Metadata URI
        creator: Creator written into the curve; the payer if None
        buy_amount: SOL to spend on the buy
        slippage: Fraction of headroom allowed above the quoted cost
        mayhem: Whether to opt into mayhem mode
        creator_fee_bps: Creator fee; None omits the argument
        holder_reward: Whether the creator fee goes to holders; None omits it
        quote_mint: Asset the coin is priced in, and that buy_amount is in
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    mint_keypair = Keypair()
    mint = mint_keypair.pubkey()
    creator = creator or payer.pubkey()
    bonding_curve = pump_v2.find_bonding_curve(mint)

    async with AsyncClient(RPC_ENDPOINT) as client:
        global_state = await pump_v2.fetch_global(lambda pk: get_account(client, pk))
        check_creator_fee(global_state, creator_fee_bps, quote_mint)

        # Resolve before pricing: this read gives the token program the quote
        # accounts derive under and the decimals the amounts are scaled by.
        quote_program = await pump_v2.resolve_quote_token_program(
            quote_mint, lambda pk: get_account(client, pk)
        )
        registry = (
            {}
            if quote_mint == pump_v2.WSOL_MINT
            else await pump_v2.fetch_quote_control(lambda pk: get_account(client, pk))
        )
        opening = pump_v2.opening_quote_reserves(registry, quote_mint, global_state)
        quote_unit = pump_v2.quote_units(quote_mint)

        # The creator the curve will carry, which on a holder-reward coin is a
        # PDA the program substitutes rather than anything the caller passed.
        # The buy derives creator_vault from this, so taking it from the flag
        # instead of the wallet is what keeps the seeds matching (2006).
        on_curve_creator = pump_v2.curve_creator(
            mint, creator, is_holder_reward=bool(holder_reward)
        )

        amount_raw = int(buy_amount * quote_unit)
        expected_tokens = size_opening_buy(
            global_state, amount_raw, opening, creator_fee_bps
        )
        max_quote_cost = int(amount_raw * (1 + slippage))

        await check_quote_balance(
            client, payer.pubkey(), quote_mint, quote_program, max_quote_cost
        )

        print(f"Mint:     {mint}")
        print(f"Curve:    {bonding_curve}")
        print(f"Payer:    {payer.pubkey()}")
        print(f"Creator:  {creator}")
        if on_curve_creator != creator:
            print(f"  -> curve will carry {on_curve_creator} (holder rewards PDA)")
        print(f"Name:     {name} ({symbol})")
        print(
            f"Mayhem:   {mayhem}   Creator fee: "
            f"{'omitted' if creator_fee_bps is None else f'{creator_fee_bps} bps'}"
            f"   Holder reward: {'omitted' if holder_reward is None else holder_reward}"
        )
        print(f"Quote:    {quote_mint}")
        print(
            f"Buying:   {buy_amount} (raw {amount_raw}), cap {max_quote_cost} raw units"
        )
        print(f"Expecting {expected_tokens / 10**pump_v2.TOKEN_DECIMALS:,.6f} tokens")

        create_instructions = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            pump_v2.build_create_v2_instruction(
                mint=mint,
                user=payer.pubkey(),
                creator=creator,
                name=name,
                symbol=symbol,
                uri=uri,
                is_mayhem_mode=mayhem,
                creator_fee_bps=creator_fee_bps,
                is_holder_reward=holder_reward,
                quote_mint=quote_mint,
                quote_token_program=quote_program,
            ),
            pump_v2.build_extend_account_instruction(bonding_curve, payer.pubkey()),
        ]

        buy_instructions = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                payer.pubkey(),
                payer.pubkey(),
                mint,
                pump_v2.TOKEN_2022_PROGRAM,
            ),
            pump_v2.build_buy_v2_instruction(
                base_mint=mint,
                creator=on_curve_creator,
                user=payer.pubkey(),
                token_amount_raw=expected_tokens,
                max_quote_cost_raw=max_quote_cost,
                quote_mint=quote_mint,
                is_mayhem_mode=mayhem,
                base_token_program=pump_v2.TOKEN_2022_PROGRAM,
                quote_token_program_id=quote_program,
            ),
        ]

        opts = TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed)

        blockhash = (await client.get_latest_blockhash()).value.blockhash
        # The mint signs too - it is a brand-new account being created.
        create_tx = VersionedTransaction(
            MessageV0.try_compile(payer.pubkey(), create_instructions, [], blockhash),
            [payer, mint_keypair],
        )
        print("\nSending create...")
        create_sig = (await client.send_transaction(create_tx, opts)).value
        print(f"Create sent: https://solscan.io/tx/{create_sig}")
        await client.confirm_transaction(create_sig, commitment=Confirmed)
        await tx_status.assert_transaction_succeeded(client, create_sig)
        print("Create confirmed")

        buy_blockhash = (await client.get_latest_blockhash()).value.blockhash
        buy_tx = VersionedTransaction(
            MessageV0.try_compile(payer.pubkey(), buy_instructions, [], buy_blockhash),
            [payer],
        )
        print("\nSending buy...")
        buy_sig = (await client.send_transaction(buy_tx, opts)).value
        print(f"Buy sent: https://solscan.io/tx/{buy_sig}")
        await client.confirm_transaction(buy_sig, commitment=Confirmed)
        await tx_status.assert_transaction_succeeded(client, buy_sig)
        print("Buy confirmed")


def main() -> None:
    """Parse the command line and run the create-and-buy."""
    parser = argparse.ArgumentParser(
        description="Create a pump.fun coin with create_v2 and buy it"
    )
    parser.add_argument("--name", default=DEFAULT_TOKEN_NAME, help="Coin name")
    parser.add_argument("--symbol", default=DEFAULT_TOKEN_SYMBOL, help="Coin ticker")
    parser.add_argument("--uri", default=DEFAULT_TOKEN_URI, help="Metadata URI")
    parser.add_argument(
        "--creator",
        type=Pubkey.from_string,
        default=None,
        help="Creator written into the curve (default: the paying wallet)",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=DEFAULT_BUY_AMOUNT_SOL,
        help=(
            "Amount to spend on the buy, in whole units of the quote asset "
            f"(default {DEFAULT_BUY_AMOUNT_SOL})"
        ),
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Slippage tolerance (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--quote-mint",
        type=Pubkey.from_string,
        default=pump_v2.WSOL_MINT,
        help=(
            "Asset the coin is priced in and that --amount is denominated in "
            "(default wrapped SOL)"
        ),
    )
    parser.add_argument(
        "--mayhem",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAYHEM,
        help=f"Enable mayhem mode (default {DEFAULT_MAYHEM})",
    )
    parser.add_argument(
        "--creator-fee-bps",
        type=int,
        default=None,
        help=(
            "Creator fee in basis points, validated against Global. Omitted "
            "entirely by default; pass 0 to send an explicit zero"
        ),
    )
    parser.add_argument(
        "--holder-reward",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Set the creator fee aside for holders. Omitted entirely by "
            "default; --no-holder-reward sends an explicit false"
        ),
    )
    args = parser.parse_args()

    asyncio.run(
        run(
            name=args.name,
            symbol=args.symbol,
            uri=args.uri,
            creator=args.creator,
            buy_amount=args.amount,
            slippage=args.slippage,
            mayhem=args.mayhem,
            creator_fee_bps=args.creator_fee_bps,
            holder_reward=args.holder_reward,
            quote_mint=args.quote_mint,
        )
    )


if __name__ == "__main__":
    main()

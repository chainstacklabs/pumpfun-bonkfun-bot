"""Create a pump.fun coin with create_v2 and buy it, in two transactions.

WARNING: this submits real transactions and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2.py
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2.py --mayhem
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2.py --creator-fee-bps 300
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2.py --holder-reward

It is two transactions rather than one because `buy_v2` takes 27 accounts, which
pushes a combined create+buy message to 1479 bytes, past the 1232-byte limit a
v0 transaction is held to. The legacy 18-account `buy` used to fit.

The buy here spends SOL, so this script only launches SOL-paired coins. That
also puts `--creator-fee-bps` out of reach: pump.fun applies a creator fee only
to a coin priced in something other than SOL, and stores zero otherwise. To
launch a coin with a fee, use `pumpfun_create_token_v2.py --quote-mint`, then
buy it with `pumpfun_buy_token_v2.py`.

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

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
BPS_DENOMINATOR: Final[int] = 10_000
COMPUTE_UNIT_LIMIT: Final[int] = 350_000
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


def check_creator_fee(creator_fee_bps: int | None) -> None:
    """Refuse a creator fee, which this script cannot make effective.

    pump.fun applies `creator_fee_bps` only to a coin priced in something other
    than SOL; on a SOL-paired coin it accepts the argument and stores zero. This
    script buys with SOL, so it would always be the silent-zero case. The check
    runs before anything is sent, so no coin is created and then found wanting.

    Args:
        creator_fee_bps: Requested fee, or None if the arg is being omitted

    Raises:
        ValueError: If any non-zero fee is requested
    """
    if not creator_fee_bps:
        return
    msg = (
        "A creator fee only takes effect on a coin priced in something other "
        "than SOL, and this script buys with SOL. pump.fun would accept the "
        "argument and store 0. Create it with "
        "pumpfun_create_token_v2.py --quote-mint <MINT> --creator-fee-bps "
        f"{creator_fee_bps}, then buy with pumpfun_buy_token_v2.py."
    )
    raise ValueError(msg)


def size_opening_buy(
    global_state: dict, buy_amount_sol: float, creator_fee_bps: int | None
) -> int:
    """Tokens to ask for, given what the opening curve and the fees will be.

    Every input comes from Global: the opening reserves set the price, and the
    two fee rates set how much of the spend never reaches the curve. Hardcoding
    any of them works only until `set_params` moves it, and the fee total moves
    per coin as soon as a creator fee is set.

    Args:
        global_state: Decoded Global account
        buy_amount_sol: SOL to spend
        creator_fee_bps: Creator fee for this coin, or None to use Global's
            default rate

    Returns:
        Base tokens to request, raw units
    """
    virtual_tokens = global_state["initial_virtual_token_reserves"]
    virtual_sol = global_state["initial_virtual_sol_reserves"]
    creator_rate = (
        global_state["creator_fee_basis_points"]
        if creator_fee_bps is None
        else creator_fee_bps
    )
    fee_bps = global_state["fee_basis_points"] + creator_rate

    lamports = int(buy_amount_sol * LAMPORTS_PER_SOL)
    reaching_curve = lamports * (BPS_DENOMINATOR - fee_bps) // BPS_DENOMINATOR
    # Linear at the opening price rather than constant-product. At these sizes
    # against the opening reserves the curvature is below rounding; the slippage
    # cap is what actually bounds the spend.
    return reaching_curve * virtual_tokens // virtual_sol


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
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    mint_keypair = Keypair()
    mint = mint_keypair.pubkey()
    creator = creator or payer.pubkey()
    bonding_curve = pump_v2.find_bonding_curve(mint)

    async with AsyncClient(RPC_ENDPOINT) as client:
        check_creator_fee(creator_fee_bps)
        global_state = await pump_v2.fetch_global(lambda pk: get_account(client, pk))

        # The creator the curve will carry, which on a holder-reward coin is a
        # PDA the program substitutes rather than anything the caller passed.
        # The buy derives creator_vault from this, so taking it from the flag
        # instead of the wallet is what keeps the seeds matching (2006).
        on_curve_creator = pump_v2.curve_creator(
            mint, creator, is_holder_reward=bool(holder_reward)
        )

        expected_tokens = size_opening_buy(global_state, buy_amount, creator_fee_bps)
        max_sol_cost = int(buy_amount * LAMPORTS_PER_SOL * (1 + slippage))

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
        print(f"Buying:   {buy_amount} SOL, cap {max_sol_cost / LAMPORTS_PER_SOL:.6f}")
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
                max_quote_cost_raw=max_sol_cost,
                quote_mint=pump_v2.WSOL_MINT,
                is_mayhem_mode=mayhem,
                base_token_program=pump_v2.TOKEN_2022_PROGRAM,
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
        print("\nSending buy (buy_v2)...")
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
        help=f"SOL to spend on the buy (default {DEFAULT_BUY_AMOUNT_SOL})",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Slippage tolerance (default {DEFAULT_SLIPPAGE})",
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
        )
    )


if __name__ == "__main__":
    main()

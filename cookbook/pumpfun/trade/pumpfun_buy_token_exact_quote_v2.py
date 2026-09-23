"""Spend an exact amount of a coin's quote asset on it, using buy_exact_quote_in_v2.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_buy_token_exact_quote_v2.py <MINT> <AMOUNT>
    uv run cookbook/pumpfun/trade/pumpfun_buy_token_exact_quote_v2.py <MINT> 1.0 --dry-run

The mirror of `pumpfun_buy_token_v2.py`. Both buy the same coin; they differ in
which side of the trade you pin down:

    buy_v2                 "give me exactly N tokens, spend at most X"
    buy_exact_quote_in_v2  "spend exactly X, give me at least N tokens"

Pin the spend when the quote asset is a budget you hold rather than a number you
derived. pump.fun coins can be priced in USDC, in another coin, or in a tokenized
equity, and "spend exactly one AAPLx" is a thing you can mean directly, whereas
"buy 41,238.9 tokens" is a number you had to compute from a price that moved
while you were computing it.

Fees come out of the amount you name, so the whole of it leaves your wallet.
`--slippage` sets how far below the quoted token count you will still accept;
that floor is the only protection the program enforces.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions_v2.py
# sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions_v2 as pump_v2
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.account import Account
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import create_idempotent_associated_token_account

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

DEFAULT_SLIPPAGE = 0.25
PRIORITY_FEE_MICROLAMPORTS = 1_000


async def get_account(client: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping solana-py's `.value` envelope.

    Args:
        client: Solana RPC client
        address: Account to fetch

    Returns:
        The account object

    Raises:
        ValueError: If the account does not exist
    """
    response = await client.get_account_info(address, encoding="base64")
    if response.value is None:
        raise ValueError(f"Account not found: {address}")
    return response.value


async def resolve_base_token_program(client: AsyncClient, mint: Pubkey) -> Pubkey:
    """Read which token program owns a coin's mint.

    Args:
        client: Solana RPC client
        mint: The coin's mint

    Returns:
        The owning token program

    Raises:
        ValueError: If the mint is owned by neither token program
    """
    owner = (await get_account(client, mint)).owner
    if owner not in (pump_v2.TOKEN_PROGRAM, pump_v2.TOKEN_2022_PROGRAM):
        raise ValueError(f"Mint {mint} is owned by an unexpected program: {owner}")
    return owner


async def buy_exact_quote(
    mint: Pubkey, spend: float, slippage: float, *, dry_run: bool = False
) -> None:
    """Spend exactly `spend` of the curve's quote asset on one coin.

    Args:
        mint: The coin to buy
        spend: Exact amount to spend, in whole quote units
        slippage: How far below the quoted token count is still acceptable
        dry_run: Simulate instead of submitting
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))

    async with AsyncClient(RPC_ENDPOINT) as client:
        bonding_curve = pump_v2.find_bonding_curve(mint)
        curve = pump_v2.BondingCurveState(
            (await get_account(client, bonding_curve)).data
        )

        if curve.complete:
            print(f"{mint} has graduated to PumpSwap — see pumpswap/ instead.")
            return

        quote_mint = curve.quote_mint

        # Resolve before pricing: this read gives both the quote mint's token
        # program and its decimals, and the amount you named is in those units.
        quote_token_program = await pump_v2.resolve_quote_token_program(
            quote_mint, lambda pk: get_account(client, pk)
        )
        quote_unit = pump_v2.quote_units(quote_mint)
        base_token_program = await resolve_base_token_program(client, mint)

        price = curve.price_per_token()
        if price <= 0:
            raise ValueError("Curve has no virtual token reserves; nothing to price")

        spendable_raw = int(spend * quote_unit)
        # The floor, not a prediction: the program only checks that at least
        # this many tokens come out.
        expected_tokens = spend / price
        min_tokens_raw = int(
            expected_tokens * (1 - slippage) * 10**pump_v2.TOKEN_DECIMALS
        )

        print(f"Mint:         {mint}")
        print(f"Quote asset:  {quote_mint}")
        print(f"Price:        {price:.10f} per token")
        print(f"Spending:     {spend} ({spendable_raw} raw quote units)")
        print(f"Expecting:    ~{expected_tokens:.6f} tokens")
        print(
            f"Accepting:    >= {min_tokens_raw / 10**pump_v2.TOKEN_DECIMALS:.6f} tokens"
        )

        instructions = [
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                payer.pubkey(),
                payer.pubkey(),
                mint,
                token_program_id=base_token_program,
            ),
        ]
        # A SOL-paired coin settles in native SOL and only seed-checks the quote
        # ATA. Any other quote — a stablecoin, a coin, an equity — needs a real
        # account, and you must already hold the asset you are spending.
        if not pump_v2.is_sol_paired(quote_mint):
            instructions.append(
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    quote_mint,
                    token_program_id=quote_token_program,
                )
            )
        instructions.append(
            pump_v2.build_buy_exact_quote_in_v2_instruction(
                base_mint=mint,
                creator=curve.creator,
                user=payer.pubkey(),
                spendable_quote_in_raw=spendable_raw,
                min_tokens_out_raw=min_tokens_raw,
                quote_mint=quote_mint,
                base_token_program=base_token_program,
                is_mayhem_mode=curve.is_mayhem_mode,
                quote_token_program_id=quote_token_program,
            )
        )

        blockhash = (await client.get_latest_blockhash()).value.blockhash
        transaction = Transaction(
            [payer], Message(instructions, payer.pubkey()), blockhash
        )

        if dry_run:
            result = await client.simulate_transaction(transaction)
            print(f"\nSimulated: error={result.value.err}")
            print(f"Compute units: {result.value.units_consumed}")
            return

        signature = (
            await client.send_transaction(
                transaction,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed),
            )
        ).value
        print(f"Sent: https://explorer.solana.com/tx/{signature}")
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")


def main() -> None:
    """Parse the command line and run the buy."""
    parser = argparse.ArgumentParser(
        description="Spend an exact amount of a coin's quote asset on it"
    )
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument(
        "amount", type=float, help="Exact amount to spend, in the quote asset"
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"How far below the quoted token count to accept (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the buy instead of submitting it — spends nothing",
    )
    args = parser.parse_args()

    asyncio.run(
        buy_exact_quote(
            Pubkey.from_string(args.mint),
            args.amount,
            args.slippage,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

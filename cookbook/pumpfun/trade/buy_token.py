"""Buy a pump.fun coin that already exists, using buy_v2.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/buy_token.py <MINT>
    uv run cookbook/pumpfun/trade/buy_token.py <MINT> 0.001
    uv run cookbook/pumpfun/trade/buy_token.py <MINT> 0.001 --slippage 0.3
    uv run cookbook/pumpfun/trade/buy_token.py <MINT> --dry-run   # spends nothing

This is the smallest complete buy: you hand it a mint, it derives everything
else. `manual_buy.py` is the same trade wrapped in a listener that waits for a
brand-new coin — start here if you already know what you want to buy.

Amounts are in the curve's own quote asset. Most coins are SOL-paired, but a
USDC-paired coin spends USDC, and `0.001` then means 0.001 USDC. The curve
tells you which; nothing here assumes SOL.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# pump_v2.py and tx_status.py are shared helpers at the cookbook root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import base58
import pump_v2
import tx_status
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

DEFAULT_AMOUNT = 0.001
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

    Coins created with `create_v2` are Token-2022; older ones are SPL Token.
    The associated bonding curve is an ordinary ATA, so guessing wrong produces
    a valid-looking address that does not exist on chain.

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


async def buy(
    mint: Pubkey, amount: float, slippage: float, *, dry_run: bool = False
) -> None:
    """Buy `amount` of the curve's quote asset worth of one coin.

    Args:
        mint: The coin to buy
        amount: How much to spend, in whole quote units
        slippage: Fraction of headroom allowed above the quoted cost
        dry_run: Run the transaction through simulateTransaction instead of
            submitting it. The program executes against live state and reports
            what it would have done, but nothing is signed onto the chain.
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))

    async with AsyncClient(RPC_ENDPOINT) as client:
        # The curve address is a PDA of the mint, so no lookup is needed to
        # find it — only to read it.
        bonding_curve = pump_v2.find_bonding_curve(mint)
        curve = pump_v2.BondingCurveState(
            (await get_account(client, bonding_curve)).data
        )

        if curve.complete:
            print(f"{mint} has graduated to PumpSwap — see pumpswap/ instead.")
            return

        price = curve.price_per_token()
        if price <= 0:
            raise ValueError("Curve has no virtual token reserves; nothing to price")

        # A curve's quote_mint is all zeros when the coin is SOL-paired, but the
        # v2 instructions still want wrapped SOL passed explicitly.
        quote_mint = curve.quote_mint
        quote_unit = pump_v2.quote_units(quote_mint)
        base_token_program = await resolve_base_token_program(client, mint)
        quote_token_program = await pump_v2.resolve_quote_token_program(
            quote_mint, lambda pk: get_account(client, pk)
        )

        token_amount = amount / price
        max_quote_cost = int(amount * quote_unit * (1 + slippage))

        print(f"Mint:        {mint}")
        print(f"Curve:       {bonding_curve}")
        print(f"Quote asset: {quote_mint}")
        print(f"Price:       {price:.10f} per token")
        print(f"Buying:      {token_amount:.6f} tokens")
        print(f"Max cost:    {max_quote_cost} raw quote units")

        instructions = [
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                payer.pubkey(),
                payer.pubkey(),
                mint,
                token_program_id=base_token_program,
            ),
        ]
        # SOL-paired coins settle in native SOL and the quote ATA is only
        # seed-checked, so creating it would burn rent for nothing.
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
            pump_v2.build_buy_v2_instruction(
                base_mint=mint,
                creator=curve.creator,
                user=payer.pubkey(),
                token_amount_raw=int(token_amount * 10**pump_v2.TOKEN_DECIMALS),
                max_quote_cost_raw=max_quote_cost,
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
        # Landing in a block is not success: confirm_and_assert reads meta.err
        # and raises if the transaction reverted.
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")


def main() -> None:
    """Parse the command line and run the buy."""
    parser = argparse.ArgumentParser(description="Buy one pump.fun coin")
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument(
        "amount",
        nargs="?",
        type=float,
        default=DEFAULT_AMOUNT,
        help=f"How much to spend, in the curve's quote asset (default {DEFAULT_AMOUNT})",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Headroom above the quoted cost (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the buy instead of submitting it — spends nothing",
    )
    args = parser.parse_args()

    asyncio.run(
        buy(
            Pubkey.from_string(args.mint),
            args.amount,
            args.slippage,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

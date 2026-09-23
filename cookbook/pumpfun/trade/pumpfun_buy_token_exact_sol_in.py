"""Spend an exact amount of SOL on a coin, using buy_exact_sol_in.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_buy_token_exact_sol_in.py <MINT> <SOL>
    uv run cookbook/pumpfun/trade/pumpfun_buy_token_exact_sol_in.py <MINT> 0.001 --dry-run

SOL only. This instruction pre-dates non-SOL quote assets and has no quote
accounts at all, so it cannot trade a coin priced in USDC, in another coin, or in
a tokenized equity — `pumpfun_buy_token_exact_quote_v2.py` is the one that can.

**The IDL lists 16 accounts and the program requires 18.** The missing two are
the `bonding-curve-v2` PDA and a buyback fee recipient; sending the IDL's list
fails with AnchorError 6062 (BuybackFeeRecipientMissing), which names the account
but not where it belongs. The v2 instructions are complete in the IDL, this
family is not, so cross-check anything outside v2 against a real transaction.

**Fees come out of what you send**, not on top of it. The IDL documents the
arithmetic on `buy_exact_sol_in`:

    net_sol    = floor(spendable_sol_in * 10_000 / (10_000 + total_fee_bps))
    fees       = ceil(net_sol * protocol_fee_bps / 10_000)
               + ceil(net_sol * creator_fee_bps / 10_000)
    tokens_out = floor((net_sol - 1) * virtual_token_reserves
                       / (virtual_sol_reserves + net_sol - 1))

so the tokens you receive are priced off `net_sol`, not off the amount you named.
The estimate printed below ignores the fee split and is slightly optimistic;
`min_tokens_out` is what the program enforces, and what slippage controls.
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
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import create_idempotent_associated_token_account

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_SLIPPAGE = 0.25
PRIORITY_FEE_MICROLAMPORTS = 1_000


async def get_account(client: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping solana-py's `.value` envelope.

    Args:
        client: Solana RPC client
        address: Account to fetch

    Raises:
        ValueError: If the account does not exist
    """
    response = await client.get_account_info(address, encoding="base64")
    if response.value is None:
        raise ValueError(f"Account not found: {address}")
    return response.value


async def buy_exact_sol(
    mint: Pubkey, sol: float, slippage: float, *, dry_run: bool = False
) -> None:
    """Spend exactly `sol` SOL on one coin.

    Args:
        mint: The coin to buy
        sol: Exact SOL to spend, fees included
        slippage: How far below the estimated token count is still acceptable
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
        if not pump_v2.is_sol_paired(curve.quote_mint):
            print(
                f"{mint} is priced in {curve.quote_mint}, not SOL.\n"
                f"buy_exact_sol_in has no quote accounts and cannot trade it — use\n"
                f"pumpfun_buy_token_exact_quote_v2.py instead."
            )
            return

        base_token_program = (await get_account(client, mint)).owner
        price = curve.price_per_token()
        if price <= 0:
            raise ValueError("Curve has no virtual token reserves; nothing to price")

        lamports = int(sol * LAMPORTS_PER_SOL)
        # Optimistic: it ignores the fee taken out of `lamports` before the curve
        # prices anything. The floor below is what the program enforces.
        estimated_tokens = sol / price
        min_tokens_raw = int(
            estimated_tokens * (1 - slippage) * 10**pump_v2.TOKEN_DECIMALS
        )

        print(f"Mint:       {mint}")
        print(f"Price:      {price:.10f} SOL per token")
        print(f"Spending:   {sol} SOL ({lamports} lamports, fees included)")
        print(f"Estimate:   ~{estimated_tokens:.6f} tokens, before fees")
        print(
            f"Accepting:  >= {min_tokens_raw / 10**pump_v2.TOKEN_DECIMALS:.6f} tokens"
        )

        instructions = [
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                payer.pubkey(),
                payer.pubkey(),
                mint,
                token_program_id=base_token_program,
            ),
            pump_v2.build_buy_exact_sol_in_instruction(
                mint=mint,
                creator=curve.creator,
                user=payer.pubkey(),
                spendable_sol_in_lamports=lamports,
                min_tokens_out_raw=min_tokens_raw,
                base_token_program=base_token_program,
                is_mayhem_mode=curve.is_mayhem_mode,
            ),
        ]

        blockhash = (await client.get_latest_blockhash()).value.blockhash
        transaction = VersionedTransaction(
            MessageV0.try_compile(payer.pubkey(), instructions, [], blockhash), [payer]
        )

        if dry_run:
            result = await client.simulate_transaction(transaction)
            print(f"\nSimulated: error={result.value.err}")
            print(f"Compute units: {result.value.units_consumed}")
            return

        signature = (
            await client.send_transaction(
                transaction,
                opts=TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed),
            )
        ).value
        print(f"Sent: https://explorer.solana.com/tx/{signature}")
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")


def main() -> None:
    """Parse the command line and run the buy."""
    parser = argparse.ArgumentParser(
        description="Spend an exact amount of SOL on a coin"
    )
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument("sol", type=float, help="Exact SOL to spend, fees included")
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"How far below the estimate to accept (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the buy instead of submitting it — spends nothing",
    )
    args = parser.parse_args()

    asyncio.run(
        buy_exact_sol(
            Pubkey.from_string(args.mint),
            args.sol,
            args.slippage,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

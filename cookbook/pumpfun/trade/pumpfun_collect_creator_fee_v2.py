"""Sweep the creator fees your coins have accrued into your wallet.

WARNING: this submits a real transaction and spends real funds (the network fee).

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_collect_creator_fee_v2.py
    uv run cookbook/pumpfun/trade/pumpfun_collect_creator_fee_v2.py --quote <QUOTE_MINT>
    uv run cookbook/pumpfun/trade/pumpfun_collect_creator_fee_v2.py --creator <PUBKEY>

Every trade on a coin pays its creator a fee, and that fee accumulates in a
`creator-vault` PDA rather than landing in the creator's wallet. It sits there
until somebody runs this.

Two things about it that catch people out:

- **A vault is per creator and per quote asset.** If your coins are priced in
  SOL and in USDC, that is two vaults and two runs, one `--quote` each. The
  default is SOL.
- **There is no signer.** The IDL marks no account as a signer, because the
  money can only ever move to the wallet it already belongs to — so anyone can
  run this for anyone, and `--creator` collects on someone else's behalf. They
  get the funds; you pay the network fee.

On a holder-reward coin the curve's `creator` is a `holder-rewards` PDA rather
than a person, so the fees collect onto that PDA and are paid out separately
with `distribute_fee_to_holders`.
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
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

PRIORITY_FEE_MICROLAMPORTS = 1_000


async def collect(creator: Pubkey, quote_mint: Pubkey, *, dry_run: bool) -> None:
    """Sweep one creator vault into the creator's token account.

    Args:
        creator: The creator whose vault to sweep
        quote_mint: Quote asset the fees accrued in
        dry_run: Simulate instead of submitting
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))

    async with AsyncClient(RPC_ENDPOINT) as client:

        async def get_account(address: Pubkey) -> object:
            response = await client.get_account_info(address, encoding="base64")
            if response.value is None:
                raise ValueError(f"Account not found: {address}")
            return response.value

        quote_token_program = await pump_v2.resolve_quote_token_program(
            quote_mint, get_account
        )
        vault = pump_v2.find_creator_vault(creator)

        print(f"Creator:     {creator}")
        print(f"Quote asset: {quote_mint}")
        print(f"Vault:       {vault}")

        # A SOL vault holds lamports directly; a token vault holds them in the
        # vault's ATA for the quote mint.
        if pump_v2.is_sol_paired(quote_mint):
            balance = (await client.get_balance(vault)).value
            print(f"Vault holds: {balance} lamports (rent included)")

        instructions = [
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            pump_v2.build_collect_creator_fee_v2_instruction(
                creator=creator,
                quote_mint=quote_mint,
                quote_token_program_id=quote_token_program,
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
    """Parse the command line and collect."""
    parser = argparse.ArgumentParser(description="Collect accrued creator fees")
    parser.add_argument(
        "--creator",
        help="Creator to collect for (defaults to your own wallet)",
    )
    parser.add_argument(
        "--quote",
        help="Quote mint the fees accrued in (defaults to SOL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate instead of submitting",
    )
    args = parser.parse_args()

    creator = (
        Pubkey.from_string(args.creator)
        if args.creator
        else Keypair.from_bytes(base58.b58decode(PRIVATE_KEY)).pubkey()
    )
    quote_mint = (
        pump_v2.normalize_quote_mint(Pubkey.from_string(args.quote))
        if args.quote
        else pump_v2.WSOL_MINT
    )

    asyncio.run(collect(creator, quote_mint, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

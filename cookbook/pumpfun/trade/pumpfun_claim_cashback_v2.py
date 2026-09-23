"""Pay out the cashback your trading has accrued.

WARNING: this submits a real transaction and spends real funds (the network fee).

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_claim_cashback_v2.py
    uv run cookbook/pumpfun/trade/pumpfun_claim_cashback_v2.py --quote <QUOTE_MINT>
    uv run cookbook/pumpfun/trade/pumpfun_claim_cashback_v2.py --user <PUBKEY>

Trading a cashback coin credits your `user_volume_accumulator`, and this moves
what has built up there into your token account.

`create_v2` rejects `is_cashback_enabled = true` with error 6082
`CashbackDeprecated`, so no new cashback coin can be minted. Coins created
earlier keep trading, keep accruing and stay claimable, which is why this path
and the cashback handling elsewhere in this repo are not dead code.

As with creator fees, the accumulator is per quote asset, and the instruction
takes no signer: the funds can only reach the wallet they belong to, so anyone
may run it for anyone. `--user` claims on someone else's behalf, at your expense
for the network fee.
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


async def claim(user: Pubkey, quote_mint: Pubkey, *, dry_run: bool) -> None:
    """Pay out one user's accrued cashback for one quote asset.

    Args:
        user: Wallet whose cashback to claim
        quote_mint: Quote asset the cashback accrued in
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
        accumulator = pump_v2.find_user_volume_accumulator(user)

        print(f"User:        {user}")
        print(f"Quote asset: {quote_mint}")
        print(f"Accumulator: {accumulator}")

        response = await client.get_account_info(accumulator, encoding="base64")
        if response.value is None:
            print(
                "\nNo volume accumulator — this wallet has not traded a cashback coin."
            )
            return

        instructions = [
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            pump_v2.build_claim_cashback_v2_instruction(
                user=user,
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
    """Parse the command line and claim."""
    parser = argparse.ArgumentParser(description="Claim accrued cashback")
    parser.add_argument("--user", help="Wallet to claim for (defaults to your own)")
    parser.add_argument(
        "--quote", help="Quote mint the cashback accrued in (default SOL)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate instead of submitting"
    )
    args = parser.parse_args()

    user = (
        Pubkey.from_string(args.user)
        if args.user
        else Keypair.from_bytes(base58.b58decode(PRIVATE_KEY)).pubkey()
    )
    quote_mint = (
        pump_v2.normalize_quote_mint(Pubkey.from_string(args.quote))
        if args.quote
        else pump_v2.WSOL_MINT
    )

    asyncio.run(claim(user, quote_mint, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

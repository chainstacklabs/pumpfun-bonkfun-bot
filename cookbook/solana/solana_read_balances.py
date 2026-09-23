"""Print the wallet's SOL balance and every token it holds.

Usage:
    uv run cookbook/solana/solana_read_balances.py              # the wallet from .env
    uv run cookbook/solana/solana_read_balances.py <PUBKEY>     # any wallet

**Token accounts live under two different programs.** Coins created with
`create_v2` are Token-2022; older coins and most other SPL tokens are legacy SPL
Token. `getTokenAccountsByOwner` takes one program at a time and silently returns
an empty list for the other, so asking only SPL Token hides every modern pump.fun
coin you own. This queries both and says which program each account came from.

An account showing a zero balance is still an open account holding ~0.002 SOL
of rent. `tools/cleanup_accounts.py` closes those and refunds it.
"""

import argparse
import asyncio
import os

import base58
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

LAMPORTS_PER_SOL = 1_000_000_000

TOKEN_PROGRAMS = {
    "SPL Token": Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
    "Token-2022": Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
}


async def show_balances(owner: Pubkey) -> None:
    """Print the SOL balance and all token accounts for one wallet.

    Args:
        owner: The wallet to inspect
    """
    async with AsyncClient(RPC_ENDPOINT) as client:
        lamports = (await client.get_balance(owner)).value
        print(f"Wallet: {owner}")
        print(f"SOL:    {lamports / LAMPORTS_PER_SOL:.9f} ({lamports} lamports)\n")

        found = False
        for program_name, program_id in TOKEN_PROGRAMS.items():
            response = await client.get_token_accounts_by_owner_json_parsed(
                owner, TokenAccountOpts(program_id=program_id)
            )
            for account in response.value:
                info = account.account.data.parsed["info"]
                amount = info["tokenAmount"]
                # uiAmountString keeps the mint's own decimals; the raw amount
                # is what a sell instruction actually takes.
                print(f"{info['mint']}")
                print(
                    f"  {amount['uiAmountString']:>24}  "
                    f"raw {amount['amount']}  ({program_name})"
                )
                found = True

        if not found:
            print("No token accounts.")


def main() -> None:
    """Parse the command line and print the balances."""
    parser = argparse.ArgumentParser(description="Show SOL and token balances")
    parser.add_argument(
        "pubkey",
        nargs="?",
        help="Wallet to inspect (defaults to SOLANA_PRIVATE_KEY's wallet)",
    )
    args = parser.parse_args()

    if args.pubkey:
        owner = Pubkey.from_string(args.pubkey)
    else:
        owner = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY)).pubkey()

    asyncio.run(show_balances(owner))


if __name__ == "__main__":
    main()

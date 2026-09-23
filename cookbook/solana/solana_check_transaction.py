"""Tell whether a transaction succeeded, reverted, or is not visible yet.

Usage:
    uv run cookbook/solana/solana_check_transaction.py <SIGNATURE>

"Confirmed" and "succeeded" are different questions, and conflating them is the
single most expensive mistake in a trading script. `confirmTransaction` answers
only the first: the signature landed in a block. A landed transaction can still
have reverted, and RPC says so only in `meta.err` — so a script that stops at
confirmation prints "success" while the wallet balance never moves.

This prints all three states separately:

    SUCCESS      landed, meta.err is null
    REVERTED     landed, meta.err is set — the fee was paid, nothing else happened
    UNCONFIRMED  the node cannot see it (yet, or ever)

A null result is not a failure. On a load-balanced endpoint the node answering
`getTransaction` is not necessarily the one that just confirmed the signature,
so a perfectly good transaction reads back as "not found" for a moment. Deciding
"not found means failed" is how a landed buy gets reported as a failed buy.

Note the `max_supported_transaction_version=1`: without it the RPC refuses to
return any transaction in the v1 format that has been live since 2026-09-15.
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


async def check(signature: Signature) -> None:
    """Fetch a transaction and print what actually happened to it.

    Args:
        signature: The transaction signature to inspect
    """
    async with AsyncClient(RPC_ENDPOINT) as client:
        response = await client.get_transaction(
            signature, commitment="confirmed", max_supported_transaction_version=1
        )

        if response.value is None:
            print(f"UNCONFIRMED  {signature}")
            print("The node has not seen this signature. It may still land.")
            return

        meta = response.value.transaction.meta
        if meta is None:
            # No metadata means the outcome is unknown, not that it worked.
            print(f"UNCONFIRMED  {signature}")
            print("Returned without execution metadata; the outcome is unknown.")
            return

        if meta.err is not None:
            print(f"REVERTED     {signature}")
            print(f"Error: {meta.err}")
        else:
            print(f"SUCCESS      {signature}")

        print(f"Slot:        {response.value.slot}")
        print(f"Fee:         {meta.fee} lamports")
        if meta.log_messages:
            print("\nProgram logs:")
            for line in meta.log_messages:
                print(f"  {line}")


def main() -> None:
    """Parse the command line and check the signature."""
    parser = argparse.ArgumentParser(description="Check one transaction's outcome")
    parser.add_argument("signature", help="Transaction signature, base58")
    args = parser.parse_args()

    asyncio.run(check(Signature.from_string(args.signature)))


if __name__ == "__main__":
    main()

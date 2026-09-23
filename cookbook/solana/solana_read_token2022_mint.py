"""Read a Token-2022 mint's extensions, and its real scaled-UI multiplier.

Usage:
    uv run cookbook/solana/solana_read_token2022_mint.py <MINT>

Token-2022 mints carry extensions the original SPL Token program has no concept
of. Four of them change what a balance means or whether a transfer works at all,
and every tokenized equity pump.fun accepts as a quote asset carries all four:

- **scaled UI amount** — the raw balance is multiplied by a number the issuer
  sets, which is how stock splits and dividends are applied without touching
  anyone's account. Raw is what instructions take; raw x multiplier is what a
  human should see.
- **pausable** — the issuer can freeze all transfers. While a stock mint is
  paused, every trade on every coin priced in it fails.
- **permanent delegate** — an authority that can move tokens out of any account
  holding this mint, forever.
- **transfer hook** — a program invoked on every transfer. pump.fun only accepts
  a quote mint whose hook has no program set, so these read as `None`.

The trap this script exists for is the multiplier. The RPC returns two of them:

    "multiplier": "1.0026642075893797"
    "newMultiplier": "1.0032690125398187"
    "newMultiplierEffectiveTimestamp": 1786149000

The field called `multiplier` is the **old** one. Once the effective timestamp
has passed — and on AAPLx it passed on 2026-08-08, weeks before this script was
written — the live multiplier is `newMultiplier`, and nothing renames the fields
to tell you. Reading the obvious field gives a number that is quietly wrong and
drifts further at every corporate action. This script compares against the clock
and prints which one is actually in force.
"""

import argparse
import asyncio
import os
import time

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def effective_multiplier(scaled: dict, now: int) -> tuple[float, str]:
    """Pick the scaled-UI multiplier actually in force right now.

    Args:
        scaled: The `scaledUiAmountConfig` extension state
        now: Current unix time in seconds

    Returns:
        The live multiplier, and a note saying which field it came from
    """
    current = float(scaled.get("multiplier", 1) or 1)
    upcoming = float(scaled.get("newMultiplier", current) or current)
    effective_at = int(scaled.get("newMultiplierEffectiveTimestamp", 0) or 0)

    if effective_at and now >= effective_at:
        return upcoming, "newMultiplier (its effective time has passed)"
    if effective_at and upcoming != current:
        return current, f"multiplier (newMultiplier takes over at {effective_at})"
    return current, "multiplier"


async def read_mint(mint: Pubkey) -> None:
    """Print one mint's basics and every extension it carries.

    Args:
        mint: The mint to read
    """
    async with AsyncClient(RPC_ENDPOINT) as client:
        response = await client.get_account_info_json_parsed(mint)
        if response.value is None:
            print(f"{mint} does not exist on this cluster.")
            return

        owner = response.value.owner
        if owner == TOKEN_PROGRAM:
            print(f"{mint} is an SPL Token mint — it carries no extensions.")
            return
        if owner != TOKEN_2022_PROGRAM:
            print(f"{mint} is owned by {owner}, which is not a token program.")
            return

        info = response.value.data.parsed["info"]
        extensions = {
            e.get("extension"): e.get("state", {})
            for e in (info.get("extensions") or [])
            if isinstance(e, dict)
        }
        metadata = extensions.get("tokenMetadata", {}) or {}
        decimals = info["decimals"]

        print(f"Mint:     {mint}")
        print(f"Name:     {metadata.get('name', '(none)')}")
        print(f"Symbol:   {metadata.get('symbol', '(none)')}")
        print(f"Decimals: {decimals}")
        print(f"Supply:   {info['supply']} raw")

        print_extensions(extensions, decimals, int(info["supply"]))


def print_extensions(extensions: dict, decimals: int, raw_supply: int) -> None:
    """Print the extensions that change what a balance means or if it can move.

    Args:
        extensions: Extension name -> state, from the jsonParsed mint
        decimals: The mint's decimal count
        raw_supply: The mint's supply in raw units
    """
    if "scaledUiAmountConfig" in extensions:
        scaled = extensions["scaledUiAmountConfig"]
        multiplier, source = effective_multiplier(scaled, int(time.time()))
        supply = raw_supply / 10**decimals
        print("\nScaled UI amount")
        print(f"  in force now:  {multiplier}   <- from {source}")
        print(f"  raw field says: {scaled.get('multiplier')}")
        print(f"  supply shown to a human: {supply * multiplier:,.6f}")
        print("  Instructions take raw amounts; only display uses the multiplier.")

    if "pausableConfig" in extensions:
        paused = extensions["pausableConfig"].get("paused")
        note = "  ALL TRANSFERS FROZEN" if paused else ""
        print(f"\nPausable: paused={paused}{note}")

    if "permanentDelegate" in extensions:
        delegate = extensions["permanentDelegate"].get("delegate")
        print(f"\nPermanent delegate: {delegate}")
        print("  This authority can move these tokens out of any account.")

    if "transferHook" in extensions:
        program = extensions["transferHook"].get("programId")
        print(f"\nTransfer hook program: {program or 'none'}")
        if program:
            print("  Every transfer calls it, and may need extra accounts.")

    if "defaultAccountState" in extensions:
        state = extensions["defaultAccountState"].get("accountState")
        print(f"\nDefault account state: {state}")
        if state == "frozen":
            print("  A new token account starts frozen and needs thawing.")

    others = sorted(
        set(extensions)
        - {
            "scaledUiAmountConfig",
            "pausableConfig",
            "permanentDelegate",
            "transferHook",
            "defaultAccountState",
            "tokenMetadata",
        }
    )
    if others:
        print(f"\nOther extensions: {', '.join(others)}")


def main() -> None:
    """Parse the command line and read the mint."""
    parser = argparse.ArgumentParser(description="Read a Token-2022 mint")
    parser.add_argument("mint", help="The mint address")
    args = parser.parse_args()

    asyncio.run(read_mint(Pubkey.from_string(args.mint)))


if __name__ == "__main__":
    main()

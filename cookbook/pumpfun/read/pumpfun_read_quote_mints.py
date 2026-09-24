"""List every asset a pump.fun coin is allowed to be priced in.

Usage:
    uv run cookbook/pumpfun/read/pumpfun_read_quote_mints.py
    uv run cookbook/pumpfun/read/pumpfun_read_quote_mints.py --stocks

A coin's bonding curve carries a `quote_mint`, and buys spend that asset — which
can be USDC, another coin, or a tokenized equity.

There are two registries, and this reads the one that is not obvious:

- `Global.whitelisted_quote_mints` is the original list, small and hardcoded.
- `QuoteControl`, a PDA at seed `["quote-control"]`, has its own admin and its
  own list. A coin can be paired with a mint `Global` has never heard of, so
  reading only `Global` under-reports.

Each entry carries `initial_virtual_quote_reserves`, what a new curve paired with
that mint starts with instead of the `Global` default — the coin's opening price
in its quote asset.

`--stocks` narrows the output to the tokenized equities, identified by the
Token-2022 extension set `create_v2` requires of them rather than by name.
`solana_read_token2022_mint.py` prints those extensions for one mint.

Two columns decide whether you can trade a coin priced in one of these:

- **DEC** — xStocks are 8 decimals, Backpack Securities 6, SOL 9, USDC 6. Trade
  amounts are in the quote mint's raw units, so this decides whether your
  slippage cap means what you think.
- **PAUSED** — a stock mint can be paused by its issuer, and every trade on every
  coin paired with it fails while it is.
"""

import argparse
import asyncio
import os
import struct
import sys

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

# QuoteControl: 8 discriminator + 32 admin + 64 reserved, then a Borsh vec of
# QuoteControlMint { mint: pubkey, initial_virtual_quote_reserves: u64 }.
_MINTS_OFFSET = 8 + 32 + 64
_ENTRY_SIZE = 32 + 8

# The extension set `create_v2` accepts on a Token-2022 quote mint. A mint
# carrying all of these is a tokenized equity.
_STOCK_EXTENSIONS = {
    "confidentialTransferMint",
    "defaultAccountState",
    "metadataPointer",
    "pausableConfig",
    "permanentDelegate",
    "scaledUiAmountConfig",
    "tokenMetadata",
    "transferHook",
}

_BATCH = 100


def find_quote_control() -> Pubkey:
    """Derive the QuoteControl PDA."""
    return Pubkey.find_program_address([b"quote-control"], PUMP_PROGRAM)[0]


def decode_quote_control(data: bytes) -> tuple[Pubkey, list[tuple[Pubkey, int]]]:
    """Decode the admin and the mint list out of a QuoteControl account.

    Args:
        data: Raw account data, discriminator included

    Returns:
        The admin, and one (mint, initial_virtual_quote_reserves) per entry

    Raises:
        ValueError: If the account is too short to hold the vec length
    """
    if len(data) < _MINTS_OFFSET + 4:
        raise ValueError(f"QuoteControl account is only {len(data)} bytes")

    admin = Pubkey.from_bytes(data[8:40])
    count = struct.unpack_from("<I", data, _MINTS_OFFSET)[0]
    start = _MINTS_OFFSET + 4

    entries = []
    for i in range(count):
        at = start + i * _ENTRY_SIZE
        mint = Pubkey.from_bytes(data[at : at + 32])
        reserves = struct.unpack_from("<Q", data, at + 32)[0]
        entries.append((mint, reserves))
    return admin, entries


def describe(account: object) -> dict:
    """Pull the interesting fields out of a jsonParsed mint account.

    Args:
        account: One entry from a getMultipleAccounts jsonParsed response

    Returns:
        symbol, decimals, is_stock, paused and multiplier (empty if unreadable)
    """
    try:
        info = account.data.parsed["info"]
    except (AttributeError, KeyError, TypeError):
        return {}

    extensions = {
        e.get("extension"): e.get("state", {})
        for e in (info.get("extensions") or [])
        if isinstance(e, dict)
    }
    metadata = extensions.get("tokenMetadata", {}) or {}
    scaled = extensions.get("scaledUiAmountConfig", {}) or {}
    return {
        "symbol": metadata.get("symbol", ""),
        "decimals": info.get("decimals"),
        "token_2022": str(account.owner) == str(TOKEN_2022_PROGRAM),
        "is_stock": _STOCK_EXTENSIONS <= set(extensions),
        "paused": (extensions.get("pausableConfig", {}) or {}).get("paused"),
        "multiplier": scaled.get("multiplier"),
    }


async def read_quote_mints(*, stocks_only: bool) -> None:
    """Print every mint admitted through QuoteControl.

    Args:
        stocks_only: Show only the tokenized equities
    """
    address = find_quote_control()
    async with AsyncClient(RPC_ENDPOINT) as client:
        response = await client.get_account_info(address, encoding="base64")
        if response.value is None:
            print(f"QuoteControl ({address}) does not exist on this cluster.")
            return

        admin, entries = decode_quote_control(bytes(response.value.data))
        print(f"QuoteControl: {address}")
        print(f"Admin:        {admin}")
        print(f"Mints:        {len(entries)}\n")

        # One jsonParsed read per 100 mints gives decimals, owner and every
        # Token-2022 extension in one go.
        details = {}
        mints = [m for m, _ in entries]
        for i in range(0, len(mints), _BATCH):
            batch = mints[i : i + _BATCH]
            parsed = await client.get_multiple_accounts_json_parsed(batch)
            for mint, account in zip(batch, parsed.value, strict=True):
                details[mint] = describe(account) if account is not None else {}

        rows = [(m, r, details.get(m, {})) for m, r in entries]
        if stocks_only:
            rows = [row for row in rows if row[2].get("is_stock")]
            print(f"Showing {len(rows)} tokenized equities.\n")

        header = f"{'SYMBOL':10} {'DEC':>3} {'PROGRAM':10} {'PAUSED':6} {'MULTIPLIER':>12}  INITIAL VIRTUAL QUOTE  MINT"
        print(header)
        print("-" * len(header))
        for mint, reserves, info in sorted(
            rows, key=lambda r: r[2].get("symbol") or "~"
        ):
            program = {True: "Token-2022", False: "SPL Token"}.get(
                info.get("token_2022"), "-"
            )
            paused = {True: "YES", False: "no"}.get(info.get("paused"), "-")
            multiplier = info.get("multiplier") or "-"
            decimals = info.get("decimals")
            scaled = f"{reserves / 10**decimals:.6f}" if decimals is not None else "?"
            print(
                f"{(info.get('symbol') or '?')[:10]:10} {decimals!s:>3} "
                f"{program:10} {paused:6} {str(multiplier)[:12]:>12}  "
                f"{scaled:>21}  {mint}"
            )


def main() -> None:
    """Parse the command line and print the registry."""
    parser = argparse.ArgumentParser(
        description="List the quote assets pump.fun coins can be priced in"
    )
    parser.add_argument(
        "--stocks",
        action="store_true",
        help="Show only the tokenized equities",
    )
    args = parser.parse_args()

    if not RPC_ENDPOINT:
        print("Set SOLANA_NODE_RPC_ENDPOINT in .env first.")
        sys.exit(1)

    asyncio.run(read_quote_mints(stocks_only=args.stocks))


if __name__ == "__main__":
    main()

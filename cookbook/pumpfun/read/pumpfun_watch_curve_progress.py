"""Track a pump.fun bonding curve's progress toward graduation.

Usage:
    uv run cookbook/pumpfun/read/pumpfun_watch_curve_progress.py <MINT>

Polls the curve every POLL_INTERVAL seconds. Progress is measured against
`Global.initial_real_token_reserves` read from chain rather than a hardcoded
constant, because a mayhem coin can be launched with different virtual params
(`set_mayhem_virtual_params`) and would otherwise show the wrong percentage.
"""

import argparse
import asyncio
import os
import struct
from typing import Final

from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

load_dotenv()

# Constants
RPC_URL: Final[str] = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMP_GLOBAL: Final[Pubkey] = Pubkey.from_string(
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
)
TOKEN_DECIMALS: Final[int] = 6
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack(
    "<Q", 6966180631402821399
)  # Pump.fun bonding curve discriminator
POLL_INTERVAL: Final[int] = 10  # Seconds between each status check
_MIN_ARGC: Final[int] = 2

# Quote assets. `quote_mint` is all zeros on SOL-paired coins, and the quote-side
# reserves are in that mint's raw units — 1e9 for SOL, 1e6 for USDC.
DEFAULT_QUOTE_MINT: Final[Pubkey] = Pubkey.from_bytes(bytes(32))
WSOL_MINT: Final[Pubkey] = Pubkey.from_string(
    "So11111111111111111111111111111111111111112"
)
USDC_MINT: Final[Pubkey] = Pubkey.from_string(
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)
QUOTE_DECIMALS: Final[dict[Pubkey, int]] = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS: Final[dict[Pubkey, str]] = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}

# Same offset in SPL Token and Token-2022: extensions are appended after it.
_MINT_DECIMALS_OFFSET: Final[int] = 44

# Data lengths excluding the 8-byte discriminator, per curve layout version.
_LEN_WITH_CREATOR: Final[int] = 73
_LEN_WITH_MAYHEM: Final[int] = 74
_LEN_WITH_CASHBACK: Final[int] = 75
_LEN_WITH_QUOTE_MINT: Final[int] = 107

_NO_BASELINE_MSG: Final[str] = (
    "Cannot read initial_real_token_reserves from the pump.fun Global account"
)


async def read_quote_decimals(conn: AsyncClient, quote_mint: Pubkey) -> int:
    """Read a quote mint's decimals from chain.

    `QuoteControl` admits mints from 4 to 12 decimals, so a default of 9 is
    wrong for most of them and misscales every quote-side figure silently.

    Raises:
        ValueError: If the mint is missing or too short to be a mint
    """
    if quote_mint in QUOTE_DECIMALS:
        return QUOTE_DECIMALS[quote_mint]

    response = await conn.get_account_info(quote_mint, encoding="base64")
    if response.value is None:
        raise ValueError(f"Quote mint {quote_mint} does not exist on chain")
    data = bytes(response.value.data)
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(
            f"Account {quote_mint} is only {len(data)} bytes, too short to be a mint"
        )
    return data[_MINT_DECIMALS_OFFSET]


def get_bonding_curve_address(mint: Pubkey, program_id: Pubkey) -> Pubkey:
    """Derive the bonding curve PDA address from a mint address.

    Args:
        mint: The token mint address
        program_id: The program ID for the bonding curve
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], program_id)[0]


async def get_account_data(client: AsyncClient, pubkey: Pubkey) -> bytes:
    """Fetch raw account data for a given public key.

    Args:
        client: AsyncClient connection to Solana RPC
        pubkey: The public key of the account to fetch

    Returns:
        The raw account data as bytes

    Raises:
        ValueError: If the account is not found or has no data
    """
    resp = await client.get_account_info(pubkey, encoding="base64")
    if not resp.value or not resp.value.data:
        raise ValueError(f"Account {pubkey} not found or has no data")

    return resp.value.data


def parse_curve_state(data: bytes) -> dict:
    """Decode bonding curve account data into a readable format.

    Args:
        data: The raw bonding curve account data

    Returns:
        Parsed fields. Token reserves in whole tokens, quote reserves raw.

    Raises:
        ValueError: If the account discriminator is invalid
    """
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid discriminator for bonding curve")

    # Parse common fields (present in all versions)
    fields = struct.unpack_from("<QQQQQ?", data, 8)
    data_length = len(data) - 8

    quote_mint = DEFAULT_QUOTE_MINT
    if data_length >= _LEN_WITH_QUOTE_MINT:  # Has quote_mint
        quote_mint = Pubkey.from_bytes(data[83:115])
    effective_quote_mint = WSOL_MINT if quote_mint == DEFAULT_QUOTE_MINT else quote_mint

    # Quote-side reserves stay raw: only the quote mint's decimals scale them.
    result = {
        "virtual_token_reserves": fields[0] / 10**TOKEN_DECIMALS,
        "virtual_quote_reserves_raw": fields[1],
        "real_token_reserves": fields[2] / 10**TOKEN_DECIMALS,
        "real_quote_reserves_raw": fields[3],
        "token_total_supply": fields[4] / 10**TOKEN_DECIMALS,
        "complete": fields[5],
        "quote_mint": effective_quote_mint,
        "quote_symbol": QUOTE_SYMBOLS.get(
            effective_quote_mint, str(effective_quote_mint)
        ),
    }

    # Parse creator field if present
    if data_length >= _LEN_WITH_CREATOR:  # Has creator field
        creator_bytes = data[49:81]  # 8 (discriminator) + 41 (base fields) = 49
        result["creator"] = Pubkey.from_bytes(creator_bytes)

    # Both flags are absent from a curve laid out before they were added. Leave
    # them out of the result rather than reporting an unset field as false.
    if data_length >= _LEN_WITH_MAYHEM:
        result["is_mayhem_mode"] = bool(data[81])

    # is_cashback_coin arrived with the late-Feb 2026 cashback upgrade.
    if data_length >= _LEN_WITH_CASHBACK:
        result["is_cashback_coin"] = bool(data[82])

    return result


async def fetch_initial_real_token_reserves(client: AsyncClient) -> float:
    """Read the launch-time real token reserves from the Global account.

    Global layout up to this field: discriminator(8) + initialized(1) +
    authority(32) + fee_recipient(32) + initial_virtual_token_reserves(8) +
    initial_virtual_sol_reserves(8), so initial_real_token_reserves sits at 89.

    Args:
        client: Connected RPC client

    Returns:
        Initial real token reserves in whole tokens

    Raises:
        ValueError: If Global is missing or carries a zero at that offset. The
            baseline is the 0% mark every progress figure is measured against,
            so a guessed one misreports every coin the run touches.
    """
    resp = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
    if resp.value is None or len(resp.value.data) < 89 + 8:
        raise ValueError(_NO_BASELINE_MSG)
    raw = struct.unpack_from("<Q", resp.value.data, 89)[0]
    if not raw:
        raise ValueError(_NO_BASELINE_MSG)
    return raw / 10**TOKEN_DECIMALS


def print_curve_status(state: dict, baseline: float, quote_unit: int) -> None:
    """Print the current status of the bonding curve in a readable format.

    Args:
        state: The parsed bonding curve state dictionary
        baseline: Launch-time real token reserves, used as the 0% mark
        quote_unit: Raw units per whole unit of the coin's quote asset
    """
    progress = 0.0
    if state["complete"]:
        progress = 100.0
    elif baseline > 0:
        left_tokens = state["real_token_reserves"]
        progress = 100 - (left_tokens * 100) / baseline
        # A coin can hold more real tokens than Global's baseline — verified on a live
        # mayhem/USDC coin with 797.17M against Global's 793.10M — which would print a
        # negative percentage. Its launch reserves are not recoverable from the curve
        # account alone, so treat that case as "nothing sold yet" rather than guess.
        progress = max(progress, 0.0)

    print("=" * 30)
    print(f"Complete: {'✅' if state['complete'] else '❌'}")
    print(f"Progress: {progress:.2f}%")
    print(f"Token reserves: {state['real_token_reserves']:.4f}")
    print(
        f"{state['quote_symbol']} reserves:   "
        f"{state['real_quote_reserves_raw'] / quote_unit:.6f}"
    )
    print("=" * 30, "\n")


async def track_curve(token_mint: str) -> None:
    """Continuously track and display the state of a bonding curve.

    Args:
        token_mint: The mint address of the coin to follow
    """
    if not RPC_URL:
        print("❌ Set SOLANA_NODE_RPC_ENDPOINT in .env")
        return

    mint_pubkey: Pubkey = Pubkey.from_string(token_mint)
    curve_pubkey: Pubkey = get_bonding_curve_address(mint_pubkey, PUMP_PROGRAM_ID)

    print("Tracking bonding curve for:", mint_pubkey)
    print("Curve address:", curve_pubkey, "\n")

    async with AsyncClient(RPC_URL) as client:
        baseline = await fetch_initial_real_token_reserves(client)
        print(f"Graduation baseline: {baseline:,.0f} tokens (from Global)\n")

        # A curve's quote mint never changes: resolve its scale once, not per poll.
        first = parse_curve_state(await get_account_data(client, curve_pubkey))
        quote_unit = 10 ** await read_quote_decimals(client, first["quote_mint"])

        while True:
            try:
                data = await get_account_data(client, curve_pubkey)
                state = parse_curve_state(data)
                # Never let a coin that launched above Global's baseline read as
                # negative progress; see the note in print_curve_status.
                baseline = max(baseline, state["real_token_reserves"])
                print_curve_status(state, baseline, quote_unit)
            except Exception as e:
                print(f"⚠️ Error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


def main() -> None:
    """Parse the command line and watch the curve."""
    parser = argparse.ArgumentParser(
        description="Watch a coin's progress toward graduation"
    )
    parser.add_argument("mint", help="The coin's mint address")
    args = parser.parse_args()

    asyncio.run(track_curve(args.mint))


if __name__ == "__main__":
    main()

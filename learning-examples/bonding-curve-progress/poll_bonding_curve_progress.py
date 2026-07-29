"""Track a pump.fun bonding curve's progress toward graduation.

Usage:
    uv run learning-examples/bonding-curve-progress/poll_bonding_curve_progress.py <MINT>

Polls the curve every POLL_INTERVAL seconds. Progress is measured against
`Global.initial_real_token_reserves` read from chain rather than a hardcoded
constant, because a mayhem coin can be launched with different virtual params
(`set_mayhem_virtual_params`) and would otherwise show the wrong percentage.
"""

import asyncio
import os
import struct
import sys
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

# Data lengths excluding the 8-byte discriminator, per curve layout version.
_LEN_WITH_CREATOR: Final[int] = 73
_LEN_WITH_MAYHEM: Final[int] = 74
_LEN_WITH_CASHBACK: Final[int] = 75
_LEN_WITH_QUOTE_MINT: Final[int] = 107

# Only used if the Global account cannot be read: 1B supply less 206.9M reserved.
FALLBACK_INITIAL_REAL_TOKEN_RESERVES: Final[float] = 793_100_000.0


def get_bonding_curve_address(mint: Pubkey, program_id: Pubkey) -> Pubkey:
    """
    Derive the bonding curve PDA address from a mint address.

    Args:
        mint: The token mint address
        program_id: The program ID for the bonding curve

    Returns:
        The bonding curve address
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], program_id)[0]


async def get_account_data(client: AsyncClient, pubkey: Pubkey) -> bytes:
    """
    Fetch raw account data for a given public key.

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
    """
    Decode bonding curve account data into a readable format.

    Args:
        data: The raw bonding curve account data

    Returns:
        A dictionary containing parsed bonding curve fields

    Raises:
        ValueError: If the account discriminator is invalid
    """
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid discriminator for bonding curve")

    # Parse common fields (present in all versions)
    fields = struct.unpack_from("<QQQQQ?", data, 8)
    data_length = len(data) - 8

    # quote_mint decides the scale of the quote-side reserves, so read it before
    # converting anything.
    quote_mint = DEFAULT_QUOTE_MINT
    if data_length >= _LEN_WITH_QUOTE_MINT:  # Has quote_mint
        quote_mint = Pubkey.from_bytes(data[83:115])
    effective_quote_mint = WSOL_MINT if quote_mint == DEFAULT_QUOTE_MINT else quote_mint
    quote_unit = 10 ** QUOTE_DECIMALS.get(effective_quote_mint, 9)

    result = {
        "virtual_token_reserves": fields[0] / 10**TOKEN_DECIMALS,
        "virtual_quote_reserves": fields[1] / quote_unit,
        "real_token_reserves": fields[2] / 10**TOKEN_DECIMALS,
        "real_quote_reserves": fields[3] / quote_unit,
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

    # Parse is_mayhem_mode if present
    if data_length >= _LEN_WITH_MAYHEM:  # Has mayhem mode field
        result["is_mayhem_mode"] = bool(data[81])
    else:
        result["is_mayhem_mode"] = False

    # Parse is_cashback_coin if present (added in late-Feb 2026 cashback upgrade)
    if data_length >= _LEN_WITH_CASHBACK:
        result["is_cashback_coin"] = bool(data[82])
    else:
        result["is_cashback_coin"] = False

    return result


async def fetch_initial_real_token_reserves(client: AsyncClient) -> float:
    """Read the launch-time real token reserves from the Global account.

    Global layout up to this field: discriminator(8) + initialized(1) +
    authority(32) + fee_recipient(32) + initial_virtual_token_reserves(8) +
    initial_virtual_sol_reserves(8), so initial_real_token_reserves sits at 89.

    Args:
        client: Connected RPC client

    Returns:
        Initial real token reserves in whole tokens, or the fallback constant
    """
    try:
        resp = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
        data = resp.value.data
        raw = struct.unpack_from("<Q", data, 89)[0]
        if raw:
            return raw / 10**TOKEN_DECIMALS
    except Exception as e:  # noqa: BLE001 - fall back rather than abort the poller
        print(f"⚠️ Could not read Global, using the fallback baseline: {e}")
    return FALLBACK_INITIAL_REAL_TOKEN_RESERVES


def print_curve_status(state: dict, baseline: float) -> None:
    """
    Print the current status of the bonding curve in a readable format.

    Args:
        state: The parsed bonding curve state dictionary
        baseline: Launch-time real token reserves, used as the 0% mark
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
    print(f"{state['quote_symbol']} reserves:   {state['real_quote_reserves']:.6f}")
    print("=" * 30, "\n")


async def track_curve(token_mint: str) -> None:
    """
    Continuously track and display the state of a bonding curve.

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

        while True:
            try:
                data = await get_account_data(client, curve_pubkey)
                state = parse_curve_state(data)
                # Never let a coin that launched above Global's baseline read as
                # negative progress; see the note in print_curve_status.
                baseline = max(baseline, state["real_token_reserves"])
                print_curve_status(state, baseline)
            except Exception as e:
                print(f"⚠️ Error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) < _MIN_ARGC:
        print(
            "Usage: uv run learning-examples/bonding-curve-progress/"
            "poll_bonding_curve_progress.py <MINT>"
        )
        sys.exit(1)
    asyncio.run(track_curve(sys.argv[1]))

"""Execute one real buy_v2 + sell_v2 round trip on mainnet.

WARNING: this submits real transactions and spends real funds. It exists to
cover the last gap the simulation scripts cannot reach — actual submission,
confirmation, and the post-trade accounting that parses a confirmed
transaction. Keep BUY_AMOUNT_SOL tiny.

Uses the bot's own PlatformAwareBuyer / PlatformAwareSeller, so a pass here
means the production path works end to end.

Usage:
    uv run learning-examples/live_v2_round_trip.py            # needs confirmation
    uv run learning-examples/live_v2_round_trip.py --yes      # skip the prompt
"""

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import LAMPORTS_PER_SOL  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

BUY_AMOUNT_SOL = 0.0001
EXTREME_FAST_TOKEN_AMOUNT = 20
HOLD_SECONDS = 5
PRIORITY_FEE = 1_000_000


async def wait_for_token(timeout_seconds: float = 120.0) -> TokenInfo | None:
    """Wait for the bot's geyser listener to report a new coin.

    Args:
        timeout_seconds: How long to wait

    Returns:
        First TokenInfo seen, or None on timeout
    """
    listener = ListenerFactory.create_listener(
        listener_type="geyser",
        geyser_endpoint=os.environ["GEYSER_ENDPOINT"],
        geyser_api_token=os.environ["GEYSER_API_TOKEN"],
        geyser_auth_type=os.environ.get("GEYSER_AUTH_TYPE", "x-token"),
        platforms=[Platform.PUMP_FUN],
    )

    seen: list[TokenInfo] = []

    async def on_token(token_info: TokenInfo) -> None:
        seen.append(token_info)

    task = asyncio.create_task(listener.listen_for_tokens(on_token))
    try:
        for _ in range(int(timeout_seconds / 0.5)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()

    return seen[0] if seen else None


async def main() -> int:
    """Run one live buy/sell round trip.

    Returns:
        Process exit code (0 if both legs confirmed)
    """
    if "--yes" not in sys.argv:
        print(
            f"This spends real funds ({BUY_AMOUNT_SOL} SOL plus fees). "
            f"Re-run with --yes to proceed."
        )
        return 2

    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])
    priority_fee_manager = PriorityFeeManager(
        client=client,
        enable_dynamic_fee=False,
        enable_fixed_fee=True,
        fixed_fee=PRIORITY_FEE,
        extra_fee=0.0,
        hard_cap=PRIORITY_FEE,
    )

    try:
        start_lamports = (await client.get_account_info(wallet.pubkey)).lamports
        print(f"wallet:        {wallet.pubkey}")
        print(f"start balance: {start_lamports / LAMPORTS_PER_SOL:.9f} SOL\n")

        print("Waiting for a fresh pump.fun coin...")
        token_info = await wait_for_token()
        if token_info is None:
            print("No coin detected before timeout.")
            return 2

        print(f"detected: {token_info.symbol} ({token_info.mint})")
        print(f"quote:    {token_info.quote_mint}\n")

        buyer = PlatformAwareBuyer(
            client,
            wallet,
            priority_fee_manager,
            BUY_AMOUNT_SOL,
            slippage=0.3,
            max_retries=1,
            extreme_fast_token_amount=EXTREME_FAST_TOKEN_AMOUNT,
            extreme_fast_mode=True,
        )
        seller = PlatformAwareSeller(
            client, wallet, priority_fee_manager, slippage=0.3, max_retries=1
        )

        print("--- BUY (buy_v2) ---")
        buy = await buyer.execute(token_info)
        print(f"success={buy.success} tx={buy.tx_signature}")
        if not buy.success:
            print(f"error: {buy.error_message}")
            return 1
        print(f"tokens={buy.amount} price={buy.price:.10f}\n")

        print(f"holding {HOLD_SECONDS}s...")
        await asyncio.sleep(HOLD_SECONDS)

        print("\n--- SELL (sell_v2) ---")
        sell = await seller.execute(token_info, buy.amount, buy.price)
        print(f"success={sell.success} tx={sell.tx_signature}")
        if not sell.success:
            print(f"error: {sell.error_message}")

        end_lamports = (await client.get_account_info(wallet.pubkey)).lamports
        delta = (end_lamports - start_lamports) / LAMPORTS_PER_SOL
        print(f"\nend balance:   {end_lamports / LAMPORTS_PER_SOL:.9f} SOL")
        print(f"net change:    {delta:+.9f} SOL")
        print(
            "\nNote: the base-token ATA still holds rent (~0.002 SOL) until a "
            "cleanup run closes it."
        )

        return 0 if (buy.success and sell.success) else 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

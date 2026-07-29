"""Dry-run the bot's real buy path against a freshly created coin.

Detects a new pump.fun coin with the bot's own listener, runs the same
PlatformAwareBuyer code the bot uses (including the extreme_fast_mode curve
refresh), but intercepts the transaction just before submission and simulates it
instead. This exercises the listener -> event parser -> curve manager ->
address provider -> instruction builder chain as a unit.

No funds move: `build_and_send_transaction` is monkeypatched to simulate.

Usage:
    uv run learning-examples/simulate_bot_buy_path.py
    uv run learning-examples/simulate_bot_buy_path.py --no-extreme-fast
"""

import asyncio
import os
import sys
from base64 import b64encode
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

BUY_AMOUNT_SOL = 0.0001
EXTREME_FAST_TOKEN_AMOUNT = 20
# Matches retries.wait_after_creation in the bot configs. Only used when
# extreme_fast_mode is off, where the buyer reads the curve at `confirmed`.
CURVE_STABILIZE_SECONDS = 15


async def wait_for_token(timeout_seconds: float = 90.0) -> TokenInfo | None:
    """Wait for the bot's geyser listener to report a new coin.

    Args:
        timeout_seconds: How long to wait

    Returns:
        The first TokenInfo seen, or None on timeout
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


def install_simulation_hook(client: SolanaClient) -> dict:
    """Replace transaction submission with simulation.

    Args:
        client: Client whose send path should be intercepted

    Returns:
        Dict that will be populated with the simulation outcome
    """
    outcome: dict = {}

    async def simulate_instead(
        instructions,
        signer_keypair,
        skip_preflight=True,
        max_retries=3,
        priority_fee=None,
        compute_unit_limit=None,
        account_data_size_limit=None,
    ):
        preamble = []
        if compute_unit_limit:
            preamble.append(set_compute_unit_limit(compute_unit_limit))
        if priority_fee:
            preamble.append(set_compute_unit_price(priority_fee))

        blockhash = await client.get_latest_blockhash()
        message = Message.new_with_blockhash(
            [*preamble, *instructions], signer_keypair.pubkey(), blockhash
        )
        transaction = Transaction([signer_keypair], message, blockhash)

        response = await client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [
                    b64encode(bytes(transaction)).decode(),
                    {
                        "encoding": "base64",
                        "sigVerify": False,
                        "replaceRecentBlockhash": True,
                        "commitment": "processed",
                    },
                ],
            }
        )
        value = (response or {}).get("result", {}).get("value", {})
        outcome.update(
            {
                "err": value.get("err"),
                "units": value.get("unitsConsumed"),
                "logs": value.get("logs") or [],
                "cu_limit": compute_unit_limit,
                "priority_fee": priority_fee,
                "instruction_count": len(instructions),
                "account_count": len(instructions[-1].accounts),
            }
        )
        # Returning a sentinel signature: confirm_transaction is stubbed below.
        return "SIMULATED"

    async def never_confirm(_signature, **_kwargs):
        return False

    client.build_and_send_transaction = simulate_instead
    client.confirm_transaction = never_confirm
    return outcome


async def main() -> int:
    """Run the bot's buy path in simulation mode.

    Returns:
        Process exit code (0 if the simulated buy had no program error)
    """
    extreme_fast = "--no-extreme-fast" not in sys.argv

    print("Waiting for a fresh pump.fun coin via the bot's geyser listener...")
    token_info = await wait_for_token()
    if token_info is None:
        print("No coin detected before timeout.")
        return 2

    print(f"\ndetected:   {token_info.symbol} ({token_info.mint})")
    print(f"quote_mint (from CreateEvent): {token_info.quote_mint}")
    print(f"token program: {token_info.token_program_id}")
    print(f"mayhem={token_info.is_mayhem_mode} cashback={token_info.is_cashback_coin}")
    print(f"state_from_event={token_info.state_from_event} (True = zero-RPC buy path)")
    print(f"extreme_fast_mode={extreme_fast}\n")

    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])
    priority_fee_manager = PriorityFeeManager(
        client=client,
        enable_dynamic_fee=False,
        enable_fixed_fee=True,
        fixed_fee=1_000_000,
        extra_fee=0.0,
        hard_cap=1_000_000,
    )

    outcome = install_simulation_hook(client)
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        priority_fee_manager,
        BUY_AMOUNT_SOL,
        slippage=0.3,
        max_retries=1,
        extreme_fast_token_amount=EXTREME_FAST_TOKEN_AMOUNT,
        extreme_fast_mode=extreme_fast,
    )

    if not extreme_fast:
        # Mirror the bot's retries.wait_after_creation pause. Without it the
        # curve read races the account's confirmation and fails before any
        # instruction is built.
        print(f"Waiting {CURVE_STABILIZE_SECONDS}s for the curve to stabilize...")
        await asyncio.sleep(CURVE_STABILIZE_SECONDS)

    try:
        result = await buyer.execute(token_info)
    finally:
        await client.close()

    if not outcome:
        print(f"Buy path never reached transaction submission: {result.error_message}")
        return 1

    print("simulated buy:")
    print(f"  instructions:  {outcome['instruction_count']}")
    print(f"  trade accounts: {outcome['account_count']}")
    print(f"  cu_limit:      {outcome['cu_limit']}")
    print(f"  unitsConsumed: {outcome['units']}")
    print(f"  err:           {outcome['err']}")

    if outcome["err"]:
        for line in outcome["logs"]:
            if "Error" in line or "failed" in line or "Instruction:" in line:
                print(f"    {line}")
        return 1

    headroom = outcome["cu_limit"] - (outcome["units"] or 0)
    print(f"\nCU headroom: {headroom} ({headroom / outcome['cu_limit']:.0%})")
    print("Buy path validated end to end against live mainnet state.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Dry-run the bot's real buy path against a freshly created coin.

Detects a new pump.fun coin with the bot's own listener, runs the same
PlatformAwareBuyer code the bot uses (the zero-RPC path for
CreateEvent-sourced tokens, or the curve refresh otherwise — watch the
state_from_event line in the output), but intercepts the transaction just
before submission and simulates it instead. This exercises the listener ->
event parser -> curve manager -> address provider -> instruction builder
chain as a unit.

No funds move: the RPC client's `send_transaction` is monkeypatched to
simulate whatever the bot handed it.

Usage:
    uv run tools/simulate_bot_buy_path.py
    uv run tools/simulate_bot_buy_path.py --no-extreme-fast
"""

import asyncio
import contextlib
import os
import struct
import sys
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import USDC_MINT  # noqa: E402
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
# Without a trade.quote_amounts entry, PlatformAwareBuyer trades only SOL-paired
# coins and skips the rest with "No configured buy amount". USDC is the one
# non-SOL quote mint with a well-known amount scale; any other quote mint needs
# its own entry here, keyed by the exact mint.
QUOTE_AMOUNTS = {USDC_MINT: 0.01}


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
            if seen or task.done():
                break
            await asyncio.sleep(0.5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Bad credentials, a dead endpoint and a quiet market all leave `seen`
    # empty. Re-raise what the listener hit so they stay distinguishable.
    if task.done() and not task.cancelled():
        task.result()

    return seen[0] if seen else None


COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
# Compute Budget instruction tags, from the program's own enum.
_SET_CU_LIMIT = 2
_SET_CU_PRICE = 3
_SET_DATA_SIZE_LIMIT = 4


def read_compute_budget(message) -> dict:
    """Recover the compute budget settings from a compiled transaction.

    Args:
        message: The compiled `MessageV0` about to be submitted

    Returns:
        cu_limit, priority_fee and data_size_limit, each None if the
        transaction carries no instruction setting it
    """
    found: dict = {"cu_limit": None, "priority_fee": None, "data_size_limit": None}
    for ix in message.instructions:
        try:
            program = message.account_keys[ix.program_id_index]
        except IndexError:
            continue
        if program != COMPUTE_BUDGET_PROGRAM:
            continue
        data = bytes(ix.data)
        if not data:
            continue
        if data[0] == _SET_CU_LIMIT and len(data) >= 5:
            found["cu_limit"] = struct.unpack_from("<I", data, 1)[0]
        elif data[0] == _SET_CU_PRICE and len(data) >= 9:
            found["priority_fee"] = struct.unpack_from("<Q", data, 1)[0]
        elif data[0] == _SET_DATA_SIZE_LIMIT and len(data) >= 5:
            found["data_size_limit"] = struct.unpack_from("<I", data, 1)[0]
    return found


async def install_simulation_hook(client: SolanaClient) -> dict:
    """Simulate the transaction the bot built, instead of sending it.

    Hooks the RPC client's `send_transaction`, so the compute budget preamble,
    the blockhash and the message compilation are all the bot's own work. A
    hook on `build_and_send_transaction` has to rebuild those, and a rebuild
    that misses an instruction simulates something the bot would never send.

    Args:
        client: Client whose send path should be intercepted

    Returns:
        Dict that will be populated with the simulation outcome
    """
    outcome: dict = {}
    rpc = await client.get_client()

    async def simulate_instead(transaction, *_args, **_kwargs):
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
        message = transaction.message
        outcome.update(
            {
                "err": value.get("err"),
                "units": value.get("unitsConsumed"),
                "logs": value.get("logs") or [],
                "instruction_count": len(message.instructions),
                "account_count": len(message.instructions[-1].accounts),
                **read_compute_budget(message),
            }
        )
        return SimpleNamespace(value=Signature.default())

    async def never_confirm(_signature, **_kwargs):
        return False

    rpc.send_transaction = simulate_instead
    client.confirm_transaction = never_confirm
    return outcome


async def main() -> int:
    """Run the bot's buy path in simulation mode.

    Returns:
        Process exit code (0 if the simulated buy had no program error)
    """
    extreme_fast = "--no-extreme-fast" not in sys.argv

    # Built before detection, as the bot builds it at startup: the client's
    # blockhash updater needs a cycle to land, and build_and_send_transaction
    # reads the cached value rather than fetching one.
    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])

    print("Waiting for a fresh pump.fun coin via the bot's geyser listener...")
    try:
        token_info = await wait_for_token()
    except BaseException:
        await client.close()
        raise
    if token_info is None:
        print("No coin detected before timeout.")
        await client.close()
        return 2

    print(f"\ndetected:   {token_info.symbol} ({token_info.mint})")
    print(f"quote_mint (from CreateEvent): {token_info.quote_mint}")
    print(f"token program: {token_info.token_program_id}")
    print(f"mayhem={token_info.is_mayhem_mode} cashback={token_info.is_cashback_coin}")
    print(f"state_from_event={token_info.state_from_event} (True = zero-RPC buy path)")
    print(f"extreme_fast_mode={extreme_fast}\n")

    priority_fee_manager = PriorityFeeManager(
        client=client,
        enable_dynamic_fee=False,
        enable_fixed_fee=True,
        fixed_fee=1_000_000,
        extra_fee=0.0,
        hard_cap=1_000_000,
    )

    outcome = await install_simulation_hook(client)
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        priority_fee_manager,
        BUY_AMOUNT_SOL,
        slippage=0.3,
        max_retries=1,
        extreme_fast_token_amount=EXTREME_FAST_TOKEN_AMOUNT,
        extreme_fast_mode=extreme_fast,
        quote_amounts=QUOTE_AMOUNTS,
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
    print(f"  instructions:   {outcome['instruction_count']}")
    print(f"  trade accounts: {outcome['account_count']}")
    print(f"  cu_limit:       {outcome['cu_limit']}")
    print(f"  data size cap:  {outcome['data_size_limit']}")
    print(f"  priority fee:   {outcome['priority_fee']}")
    print(f"  unitsConsumed:  {outcome['units']}")
    print(f"  err:            {outcome['err']}")

    if outcome["err"]:
        for line in outcome["logs"]:
            if "Error" in line or "failed" in line or "Instruction:" in line:
                print(f"    {line}")
        return 1

    cu_limit = outcome["cu_limit"]
    if cu_limit:
        headroom = cu_limit - (outcome["units"] or 0)
        print(f"\nCU headroom: {headroom} ({headroom / cu_limit:.0%})")
    print("Buy path validated end to end against live mainnet state.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

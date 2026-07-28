"""Simulate pump.fun buy_v2 / sell_v2 against mainnet without spending funds.

Builds both instructions through the bot's real code path (address provider,
instruction builder, curve manager) and runs them through
`simulateTransaction`, which executes the program against live state but never
submits. Reports the program error (if any) and the compute units consumed —
use the latter to tune `get_buy_compute_unit_limit` / `get_sell_compute_unit_limit`.

The wallet is only used to derive a pubkey and sign nothing: simulation runs
with sigVerify disabled.

Usage:
    # simulate against a specific coin
    uv run learning-examples/simulate_v2_trades.py <MINT>

    # discover a fresh coin via geyser, then simulate against it
    uv run learning-examples/simulate_v2_trades.py
"""

import asyncio
import os
import sys
from base64 import b64encode
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solders.compute_budget import set_compute_unit_limit  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import (  # noqa: E402
    TOKEN_DECIMALS,
    SystemAddresses,
    normalize_quote_mint,
    quote_token_program,
    quote_units_per_token,
)
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

# Buy this many whole tokens in the simulation. Small enough that the quote cost
# stays well under a test wallet's balance on a fresh curve.
SIMULATED_TOKEN_AMOUNT = 20
# Generous ceiling so simulation reports true consumption rather than hitting
# the limit. The real bot uses the builder's tuned values.
SIMULATION_CU_LIMIT = 400_000


async def discover_mint(timeout_seconds: float = 45.0) -> Pubkey | None:
    """Listen for a freshly created pump.fun coin via geyser.

    Args:
        timeout_seconds: How long to wait for a creation event

    Returns:
        Mint of the first coin seen, or None on timeout
    """
    from monitoring.listener_factory import ListenerFactory

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
        deadline = timeout_seconds / 0.5
        for _ in range(int(deadline)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()

    return seen[0].mint if seen else None


async def build_token_info(
    mint: Pubkey, client: SolanaClient
) -> tuple[TokenInfo, dict]:
    """Assemble TokenInfo for an existing coin from its on-chain curve state.

    Args:
        mint: Base token mint
        client: Solana RPC client

    Returns:
        Tuple of (TokenInfo, decoded curve state)
    """
    implementations = get_platform_implementations(Platform.PUMP_FUN, client)
    provider = implementations.address_provider
    curve_manager = implementations.curve_manager

    bonding_curve = provider.derive_pool_address(mint)
    state = await curve_manager.get_pool_state(bonding_curve, commitment="processed")

    creator = state["creator"]
    creator = Pubkey.from_string(creator) if isinstance(creator, str) else creator
    quote_mint = normalize_quote_mint(state["quote_mint"])

    # Which token program owns the base mint decides the base ATA derivation.
    account = await client.get_account_info(mint)
    base_token_program = (
        SystemAddresses.TOKEN_2022_PROGRAM
        if str(account.owner) == str(SystemAddresses.TOKEN_2022_PROGRAM)
        else SystemAddresses.TOKEN_PROGRAM
    )

    token_info = TokenInfo(
        name="simulation",
        symbol="SIM",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            mint, bonding_curve, base_token_program
        ),
        creator=creator,
        creator_vault=provider.derive_creator_vault(creator),
        token_program_id=base_token_program,
        is_mayhem_mode=state["is_mayhem_mode"],
        is_cashback_coin=state["is_cashback_coin"],
        quote_mint=quote_mint,
        quote_token_program_id=quote_token_program(quote_mint),
    )
    return token_info, state


async def simulate(
    client: SolanaClient, wallet: Wallet, instructions: list, label: str
) -> bool:
    """Run a set of instructions through simulateTransaction and report.

    Args:
        client: Solana RPC client
        wallet: Wallet whose pubkey pays and signs
        instructions: Instructions to simulate
        label: Human-readable name for output

    Returns:
        True if the simulation reported no program error
    """
    blockhash = await client.get_latest_blockhash()
    message = Message.new_with_blockhash(
        [set_compute_unit_limit(SIMULATION_CU_LIMIT), *instructions],
        wallet.pubkey,
        blockhash,
    )
    transaction = Transaction([wallet.keypair], message, blockhash)

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

    if not response or "result" not in response:
        print(f"  {label}: RPC call failed: {response}")
        return False

    value = response["result"]["value"]
    err = value.get("err")
    units = value.get("unitsConsumed")
    account_count = len(instructions[-1].accounts)

    print(f"  {label}: accounts={account_count} unitsConsumed={units} err={err}")
    if err:
        for line in value.get("logs") or []:
            if "Error" in line or "failed" in line or "Instruction:" in line:
                print(f"      {line}")
    return err is None


async def main() -> int:
    """Simulate a buy_v2 and sell_v2 for one coin.

    Returns:
        Process exit code (0 if the buy simulation succeeded)
    """
    mint_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if mint_arg:
        mint = Pubkey.from_string(mint_arg)
    else:
        print("No mint given; listening for a fresh pump.fun coin via geyser...")
        mint = await discover_mint()
        if mint is None:
            print("No coin seen before timeout. Pass a mint explicitly.")
            return 2

    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])

    try:
        token_info, state = await build_token_info(mint, client)
        quote_mint = token_info.quote_mint
        quote_unit = quote_units_per_token(quote_mint)

        print(f"\nmint:          {mint}")
        print(f"bonding_curve: {token_info.bonding_curve}")
        print(f"quote_mint:    {quote_mint}")
        print(f"base program:  {token_info.token_program_id}")
        print(
            f"mayhem={token_info.is_mayhem_mode} "
            f"cashback={token_info.is_cashback_coin} "
            f"complete={state['complete']}"
        )
        print(f"price:         {state['price_per_token']:.10f} quote/token")
        print(f"wallet:        {wallet.pubkey}\n")

        implementations = get_platform_implementations(Platform.PUMP_FUN, client)
        provider = implementations.address_provider
        builder = implementations.instruction_builder

        token_raw = SIMULATED_TOKEN_AMOUNT * 10**TOKEN_DECIMALS
        # Cap the quote spend generously; simulation only needs it affordable.
        max_quote_raw = int(
            SIMULATED_TOKEN_AMOUNT * state["price_per_token"] * quote_unit * 2
        ) or int(0.001 * quote_unit)

        print("simulating:")
        buy_instructions = await builder.build_buy_v2_instruction(
            token_info, wallet.pubkey, max_quote_raw, token_raw, provider
        )
        buy_ok = await simulate(client, wallet, buy_instructions, "buy_v2 ")

        sell_instructions = await builder.build_sell_v2_instruction(
            token_info, wallet.pubkey, token_raw, 1, provider
        )
        await simulate(client, wallet, sell_instructions, "sell_v2")
        print(
            "      ^ expected to fail with AccountNotInitialized on "
            "associated_base_user unless\n        the wallet already holds this "
            "coin. That error means all 26 accounts validated."
        )

        # Buy and sell in one transaction so the position exists mid-tx. This is
        # the only way to get a representative sell_v2 CU number without
        # actually holding the coin. Sell slightly less than bought to stay
        # inside the realised balance.
        combined = [*buy_instructions, *sell_instructions[:-1]]
        resell_raw = int(token_raw * 0.9)
        combined.append(
            (
                await builder.build_sell_v2_instruction(
                    token_info, wallet.pubkey, resell_raw, 1, provider
                )
            )[-1]
        )
        combined_ok = await simulate(client, wallet, combined, "buy+sell")
        if combined_ok:
            print("      ^ subtract the buy_v2 figure above to estimate sell_v2 CU")

        return 0 if buy_ok else 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

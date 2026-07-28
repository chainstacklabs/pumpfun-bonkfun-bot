"""Verify every listener with a real buy_v2 + sell_v2 + ATA cleanup on mainnet.

WARNING: this submits real transactions and spends real funds.

For each listener (geyser, logs, blocks, pumpportal) it detects a live token,
buys it, sells it, then closes the base-token ATA so the ~0.002 SOL of rent is
reclaimed rather than stranded. Tests run sequentially so the same funds are
recycled across listeners.

Each listener exercises a different event-parsing path into the same v2 trade
code, which is the point: pumpportal in particular carries no mayhem/cashback/
quote_mint flags, so it relies entirely on the on-chain curve refresh.

Usage:
    uv run learning-examples/live_listener_matrix.py --yes
    uv run learning-examples/live_listener_matrix.py --yes --listeners geyser,logs
    uv run learning-examples/live_listener_matrix.py --cleanup-only <MINT> [<MINT>...]
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from cleanup.manager import AccountCleanupManager  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import LAMPORTS_PER_SOL, SystemAddresses  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

BUY_AMOUNT_SOL = 0.0001
EXTREME_FAST_TOKEN_AMOUNT = 20
HOLD_SECONDS = 5
PRIORITY_FEE = 1_000_000
# This harness verifies plumbing, not profitability. Brand-new coins routinely
# lose 90% of their curve SOL within seconds of creation, and a realistic
# sell_slippage of 0.3 then makes the program reject the sell with
# TooLittleSolReceived (6003) — a correct guard, but it stops us proving the
# 26-account sell_v2 actually lands. Accept almost any payout instead; the
# position is worth well under a lamport-thousandth of a SOL either way.
SELL_SLIPPAGE = 0.95
BUY_SLIPPAGE = 0.3
DETECT_TIMEOUT_SECONDS = 150.0
# Time to let a close/sell finalize before reading balances or account state.
SETTLE_SECONDS = 20
ALL_LISTENERS = ("geyser", "logs", "blocks", "pumpportal")


@dataclass
class LegResult:
    """Outcome of one listener's buy/sell/cleanup cycle."""

    listener: str
    detected: str | None = None
    symbol: str | None = None
    quote_mint: str | None = None
    buy_ok: bool = False
    buy_sig: str | None = None
    sell_ok: bool = False
    sell_sig: str | None = None
    cleanup_ok: bool = False
    start_sol: float = 0.0
    end_sol: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether the whole cycle succeeded."""
        return self.buy_ok and self.sell_ok and self.cleanup_ok


def make_listener(listener_type: str):
    """Build a listener of the given type wired to this project's endpoints.

    Args:
        listener_type: One of geyser, logs, blocks, pumpportal

    Returns:
        Configured listener instance
    """
    return ListenerFactory.create_listener(
        listener_type=listener_type,
        wss_endpoint=os.environ.get("SOLANA_NODE_WSS_ENDPOINT"),
        geyser_endpoint=os.environ.get("GEYSER_ENDPOINT"),
        geyser_api_token=os.environ.get("GEYSER_API_TOKEN"),
        geyser_auth_type=os.environ.get("GEYSER_AUTH_TYPE", "x-token"),
        platforms=[Platform.PUMP_FUN],
    )


async def detect(listener_type: str) -> TokenInfo | None:
    """Wait for one new pump.fun token from the given listener.

    Args:
        listener_type: Listener to use

    Returns:
        TokenInfo, or None on timeout
    """
    listener = make_listener(listener_type)
    seen: list[TokenInfo] = []

    async def on_token(token_info: TokenInfo) -> None:
        seen.append(token_info)

    task = asyncio.create_task(listener.listen_for_tokens(on_token))
    try:
        for _ in range(int(DETECT_TIMEOUT_SECONDS / 0.5)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()
        await asyncio.sleep(0)

    return seen[0] if seen else None


async def sol_balance(client: SolanaClient, wallet: Wallet) -> float:
    """Read the wallet's SOL balance at finalized commitment.

    Reads immediately after a transaction confirms can still serve a slot that
    predates it, which silently produces "net change: 0" style nonsense. Ask for
    finalized explicitly so the reported numbers mean something.

    Args:
        client: RPC client
        wallet: Wallet to query

    Returns:
        Balance in SOL
    """
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(wallet.pubkey), {"commitment": "finalized"}],
        }
    )
    lamports = (response or {}).get("result", {}).get("value")
    if lamports is None:
        raise RuntimeError(f"getBalance failed for {wallet.pubkey}: {response}")
    return lamports / LAMPORTS_PER_SOL


async def ata_is_closed(client: SolanaClient, ata: Pubkey) -> bool:
    """Check whether an ATA is closed, at finalized commitment.

    Args:
        client: RPC client
        ata: Associated token account address

    Returns:
        True if the account no longer exists
    """
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [str(ata), {"encoding": "base64", "commitment": "finalized"}],
        }
    )
    return (response or {}).get("result", {}).get("value") is None


async def run_leg(
    listener_type: str,
    client: SolanaClient,
    wallet: Wallet,
    priority_fee_manager: PriorityFeeManager,
) -> LegResult:
    """Detect, buy, sell and clean up for one listener.

    Args:
        listener_type: Listener under test
        client: RPC client
        wallet: Trading wallet
        priority_fee_manager: Priority fee strategy

    Returns:
        LegResult describing the cycle
    """
    result = LegResult(listener=listener_type)
    result.start_sol = await sol_balance(client, wallet)

    print(
        f"\n{'=' * 62}\n{listener_type.upper()}  (start {result.start_sol:.9f} SOL)\n{'=' * 62}"
    )

    print(
        f"[{listener_type}] waiting for a token (up to {DETECT_TIMEOUT_SECONDS:.0f}s)..."
    )
    token_info = await detect(listener_type)
    if token_info is None:
        result.notes.append("no token detected before timeout")
        print(f"[{listener_type}] no token detected")
        return result

    result.detected = str(token_info.mint)
    result.symbol = token_info.symbol
    result.quote_mint = str(token_info.quote_mint)
    print(f"[{listener_type}] detected {token_info.symbol} ({token_info.mint})")
    print(f"[{listener_type}] quote_mint from listener: {token_info.quote_mint}")

    buyer = PlatformAwareBuyer(
        client,
        wallet,
        priority_fee_manager,
        BUY_AMOUNT_SOL,
        slippage=BUY_SLIPPAGE,
        max_retries=1,
        extreme_fast_token_amount=EXTREME_FAST_TOKEN_AMOUNT,
        extreme_fast_mode=True,
    )
    seller = PlatformAwareSeller(
        client, wallet, priority_fee_manager, slippage=SELL_SLIPPAGE, max_retries=1
    )

    buy = await buyer.execute(token_info)
    result.buy_ok = buy.success
    result.buy_sig = str(buy.tx_signature) if buy.tx_signature else None
    print(f"[{listener_type}] BUY  ok={buy.success} tx={buy.tx_signature}")
    if not buy.success:
        result.notes.append(f"buy failed: {buy.error_message}")
    else:
        await asyncio.sleep(HOLD_SECONDS)
        sell = await seller.execute(token_info, buy.amount, buy.price)
        result.sell_ok = sell.success
        result.sell_sig = str(sell.tx_signature) if sell.tx_signature else None
        print(f"[{listener_type}] SELL ok={sell.success} tx={sell.tx_signature}")
        if not sell.success:
            result.notes.append(f"sell failed: {sell.error_message}")

    # Always attempt cleanup, even after a failed buy: a partially-created ATA
    # still holds rent.
    token_program = token_info.token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
    manager = AccountCleanupManager(
        client, wallet, priority_fee_manager, use_priority_fee=False, force_burn=True
    )
    print(f"[{listener_type}] cleaning up ATA for {token_info.mint}...")
    await manager.cleanup_ata(token_info.mint, token_program)

    # Let the close finalize before judging it; otherwise the check races the
    # very transaction it is verifying.
    await asyncio.sleep(SETTLE_SECONDS)

    ata = wallet.get_associated_token_address(token_info.mint, token_program)
    result.cleanup_ok = await ata_is_closed(client, ata)
    if not result.cleanup_ok:
        result.notes.append(f"ATA {ata} still open after cleanup")

    result.end_sol = await sol_balance(client, wallet)
    print(
        f"[{listener_type}] cleanup ok={result.cleanup_ok} "
        f"end {result.end_sol:.9f} SOL "
        f"(net {result.end_sol - result.start_sol:+.9f})"
    )
    return result


async def cleanup_only(mints: list[str]) -> int:
    """Close ATAs for the given mints and exit.

    Args:
        mints: Base mint addresses whose ATAs should be closed

    Returns:
        Process exit code
    """
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
        before = await sol_balance(client, wallet)
        print(f"balance before cleanup: {before:.9f} SOL")
        manager = AccountCleanupManager(
            client,
            wallet,
            priority_fee_manager,
            use_priority_fee=False,
            force_burn=True,
        )
        for mint_str in mints:
            mint = Pubkey.from_string(mint_str)
            for program in (
                SystemAddresses.TOKEN_2022_PROGRAM,
                SystemAddresses.TOKEN_PROGRAM,
            ):
                ata = wallet.get_associated_token_address(mint, program)
                if await ata_is_closed(client, ata):
                    continue
                print(f"closing {ata} (mint {mint_str}, program {program})")
                await manager.cleanup_ata(mint, program)
                await asyncio.sleep(SETTLE_SECONDS)
                print(f"  closed: {await ata_is_closed(client, ata)}")
        after = await sol_balance(client, wallet)
        print(f"balance after cleanup:  {after:.9f} SOL (net {after - before:+.9f})")
        return 0
    finally:
        await client.close()


async def main() -> int:
    """Run the listener matrix.

    Returns:
        Process exit code (0 if every requested listener passed)
    """
    if "--cleanup-only" in sys.argv:
        index = sys.argv.index("--cleanup-only")
        mints = sys.argv[index + 1 :]
        if not mints:
            print("--cleanup-only needs at least one mint")
            return 2
        return await cleanup_only(mints)

    if "--yes" not in sys.argv:
        print("This spends real funds. Re-run with --yes to proceed.")
        return 2

    listeners = list(ALL_LISTENERS)
    if "--listeners" in sys.argv:
        listeners = sys.argv[sys.argv.index("--listeners") + 1].split(",")

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

    results: list[LegResult] = []
    try:
        opening = await sol_balance(client, wallet)
        print(f"wallet: {wallet.pubkey}\nopening balance: {opening:.9f} SOL")
        print(f"listeners under test: {', '.join(listeners)}")

        for listener_type in listeners:
            try:
                results.append(
                    await run_leg(listener_type, client, wallet, priority_fee_manager)
                )
            except Exception as error:  # noqa: BLE001
                failed = LegResult(listener=listener_type)
                failed.notes.append(f"crashed: {error}")
                results.append(failed)
                print(f"[{listener_type}] CRASHED: {error}")

        closing = await sol_balance(client, wallet)
    finally:
        await client.close()

    print(f"\n{'=' * 62}\nSUMMARY\n{'=' * 62}")
    header = (
        f"{'listener':<12} {'detect':<7} {'buy':<5} {'sell':<5} {'cleanup':<8} symbol"
    )
    print(header)
    for item in results:
        print(
            f"{item.listener:<12} "
            f"{'yes' if item.detected else 'no':<7} "
            f"{'ok' if item.buy_ok else '-':<5} "
            f"{'ok' if item.sell_ok else '-':<5} "
            f"{'ok' if item.cleanup_ok else '-':<8} "
            f"{item.symbol or ''}"
        )
        for note in item.notes:
            print(f"{'':<12} note: {note}")

    print(f"\nopening {opening:.9f} SOL -> closing {closing:.9f} SOL")
    print(f"total net change: {closing - opening:+.9f} SOL")
    for item in results:
        if item.buy_sig:
            print(f"  {item.listener} buy:  {item.buy_sig}")
        if item.sell_sig:
            print(f"  {item.listener} sell: {item.sell_sig}")

    passed = [item for item in results if item.passed]
    print(f"\n{len(passed)}/{len(results)} listeners passed the full cycle")
    return 0 if len(passed) == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

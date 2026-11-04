"""Verify post-session cleanup closes every traded coin's account within the shutdown budget.

Cleanup waits for the RPC node to settle before it reads an account, so it sees
the balance after the last sell. It waited before every account, one after the
other, and shutdown stops cleanup after `_CLEANUP_SHUTDOWN_BUDGET` seconds: a
session that held several coins at once closed only the first few accounts, and
the rest kept their rent.

Offline machine checks, no network and no funds moved. The RPC is a stub with a
fixed delay per call, and every wait runs a hundred times faster than it says,
the shutdown budget with them:

  A. Post-session cleanup of many accounts waits to settle once, before the
     first account read, and closes every account.
  B. It finishes for a long session's worth of coins within the shutdown budget.
  C. One account (a one-position session, or cleanup after a sell) still waits
     the full settle time before reading it, then closes it.
  D. An account that fails to close, or one still holding tokens without force
     burn, does not stop the others from closing.

Usage:
    uv run tests/regression/verify_cleanup_closes_every_account.py
"""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from cleanup.modes import (  # noqa: E402
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.pubkeys import SystemAddresses  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

SPEEDUP = 100
RPC_DELAY = 1.0 / SPEEDUP  # a mainnet RPC round trip, sped up
SETTLE = 15.0  # the settle wait cleanup has always logged
LONG_SESSION = 60
CLOSE_ACCOUNT = bytes([9])

_real_sleep = asyncio.sleep


class _SendError(Exception):
    """The stub's send failure."""


class _Clock:
    """Speeds up every asyncio.sleep and records the long ones in order."""

    def __init__(self, events: list) -> None:
        self.events = events

    async def sleep(self, seconds: float, *args: object) -> None:
        if seconds >= 1:
            self.events.append(("settle", seconds))
        await _real_sleep(seconds / SPEEDUP, *args)


class _Client:
    """Token accounts by address; records reads and closes in order."""

    def __init__(self, events: list, balances: dict, failing: set) -> None:
        self.events = events
        self.balances = balances
        self.failing = failing
        self.closed: list[Pubkey] = []

    async def get_account_info(self, address: Pubkey) -> SimpleNamespace:
        await _real_sleep(RPC_DELAY)
        self.events.append(("read", address))
        return SimpleNamespace(data=bytes(165), owner=SystemAddresses.TOKEN_PROGRAM)

    async def get_token_account_balance(self, address: Pubkey) -> int:
        await _real_sleep(RPC_DELAY)
        return self.balances.get(address, 0)

    async def build_and_send_transaction(
        self, instructions: list, *_a: object, **_k: object
    ) -> str:
        await _real_sleep(RPC_DELAY)
        account = instructions[-1].accounts[0].pubkey
        if account in self.failing:
            raise _SendError
        if bytes(instructions[-1].data) == CLOSE_ACCOUNT:
            self.closed.append(account)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a: object, **_k: object) -> bool:
        await _real_sleep(RPC_DELAY * 2)
        return True


def _wallet() -> SimpleNamespace:
    owner = Keypair().pubkey()
    # The ATA is the mint itself, so a close names the coin it belongs to.
    return SimpleNamespace(
        pubkey=owner,
        keypair=None,
        get_associated_token_address=lambda mint, _program: mint,
    )


def _run(coro_factory: Callable[[], Awaitable[None]], events: list) -> float:
    clock = _Clock(events)
    asyncio.sleep = clock.sleep
    try:
        started = time.monotonic()
        asyncio.run(coro_factory())
        return time.monotonic() - started
    finally:
        asyncio.sleep = _real_sleep


def _post_session(
    mints: list[Pubkey], balances: dict | None = None, failing: set | None = None
) -> tuple[list, _Client, float]:
    events: list = []
    client = _Client(events, balances or {}, failing or set())

    async def go() -> None:
        await handle_cleanup_post_session(
            client,
            _wallet(),
            mints,
            [SystemAddresses.TOKEN_PROGRAM] * len(mints),
            None,
            "post_session",
            cleanup_with_prior_fee=False,
            force_burn=False,
        )

    elapsed = _run(go, events)
    return events, client, elapsed


def _mints(n: int) -> list[Pubkey]:
    return [Keypair().pubkey() for _ in range(n)]


def check_a_one_settle_wait() -> bool:
    """A: one settle wait, before any read, and every account closed."""
    mints = _mints(12)
    events, client, _ = _post_session(mints)
    settles = [e for e in events if e[0] == "settle"]
    first_read = next((i for i, e in enumerate(events) if e[0] == "read"), None)
    ok = (
        settles == [("settle", SETTLE)]
        and first_read is not None
        and events.index(settles[0]) < first_read
        and sorted(map(str, client.closed)) == sorted(map(str, mints))
    )
    if not ok:
        print(f"    {len(settles)} settle waits, closed {len(client.closed)}/12")
    return ok


def check_b_long_session_within_budget() -> bool:
    """B: a long session's accounts all close within the shutdown budget."""
    budget = UniversalTrader._CLEANUP_SHUTDOWN_BUDGET / SPEEDUP  # noqa: SLF001
    mints = _mints(LONG_SESSION)
    events: list = []
    client = _Client(events, {}, set())

    async def go() -> None:
        try:
            await asyncio.wait_for(
                handle_cleanup_post_session(
                    client,
                    _wallet(),
                    mints,
                    [SystemAddresses.TOKEN_PROGRAM] * len(mints),
                    None,
                    "post_session",
                    cleanup_with_prior_fee=False,
                    force_burn=False,
                ),
                timeout=budget,
            )
        except TimeoutError:
            pass

    elapsed = _run(go, events)
    ok = len(client.closed) == LONG_SESSION
    if not ok:
        print(
            f"    closed {len(client.closed)}/{LONG_SESSION} within the "
            f"{budget * SPEEDUP:g}s budget ({elapsed * SPEEDUP:.0f}s at mainnet speed)"
        )
    return ok


def check_c_single_account() -> bool:
    """C: one account waits the full settle time before its read, then closes."""
    ok = True
    (mint,) = _mints(1)
    events, client, _ = _post_session([mint])
    if events[:2] != [("settle", SETTLE), ("read", mint)] or client.closed != [mint]:
        print(f"    post_session: {events}, closed {client.closed}")
        ok = False

    events = []
    client = _Client(events, {}, set())

    async def after_sell() -> None:
        await handle_cleanup_after_sell(
            client,
            _wallet(),
            mint,
            SystemAddresses.TOKEN_PROGRAM,
            None,
            "after_sell",
            cleanup_with_prior_fee=False,
            force_burn=False,
        )

    _run(after_sell, events)
    if events[:2] != [("settle", SETTLE), ("read", mint)] or client.closed != [mint]:
        print(f"    after_sell: {events}, closed {client.closed}")
        ok = False
    return ok


def check_d_failures_stay_local() -> bool:
    """D: a failing close and a held balance leave the other accounts closing."""
    mints = _mints(8)
    failing, held = mints[1], mints[5]
    _, client, _ = _post_session(mints, balances={held: 1000}, failing={failing})
    expected = sorted(str(m) for m in mints if m not in (failing, held))
    ok = sorted(map(str, client.closed)) == expected
    if not ok:
        print(f"    closed {len(client.closed)}, expected {len(expected)}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: one settle wait, then every account closed", check_a_one_settle_wait),
        (
            "B: a long session closes within the shutdown budget",
            check_b_long_session_within_budget,
        ),
        ("C: a single account still settles first", check_c_single_account),
        ("D: one failing account does not stop the rest", check_d_failures_stay_local),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

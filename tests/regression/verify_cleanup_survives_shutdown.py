"""Verify shutdown cleanup finishes instead of being aborted by its own cancellation.

`UniversalTrader.start` reclaims account rent from a `finally` that awaits
`_cleanup_resources`. Shutdown reaches that coroutine as a cancellation, and
cleanup awaits once per account — a 15s settle wait, then an RPC read — so the
first of those re-raised `CancelledError` and every account after it kept its
rent. The run then ended without even logging that it shut down, so nothing in
the log said cleanup had been cut short.

Observed live on 2026-09-25: a run interrupted during cleanup's settle wait
stopped after two of its accounts and never printed `Universal Trader has shut
down`. Three other runs interrupted a moment earlier finished cleanly, so this is
a race, not a certainty — which is what makes it worth pinning.

The rent is recoverable by hand with `tools/cleanup_accounts.py <MINT>`, so the
budget cuts cleanup off rather than holding the process open on a stuck RPC.

Offline machine checks, no network and no funds moved. `_cleanup_resources` is
replaced with a stub that awaits, so the cancellation lands mid-cleanup:

  1. Cleanup started before the cancellation still runs to completion.
  2. The caller returns rather than propagating, so the shutdown line is reached.
  3. Cleanup that overruns the budget is cancelled, and the warning names the
     command that reclaims the rent.
  4. A cleanup that raises is logged, not propagated.
  5. The budget is a positive, finite number — an unbounded wait would hang a
     shutdown behind a stuck RPC.

Usage:
    uv run tests/regression/verify_cleanup_survives_shutdown.py
"""

import asyncio
import logging
import math
import sys
from pathlib import Path

_STUB_FAILURE = "stub cleanup failure"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402


class RecordCapture(logging.Handler):
    """Collects records at WARNING or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _shutdown_with(  # noqa: ANN202
    cleanup,  # noqa: ANN001
    budget=None,  # noqa: ANN001
):
    """Cancel a caller parked in _cleanup_without_interruption, as start() does.

    Returns:
        Tuple of (how the caller ended, WARNING+ records emitted)
    """
    trader = object.__new__(UniversalTrader)
    trader._cleanup_resources = cleanup  # noqa: SLF001
    if budget is not None:
        trader._CLEANUP_SHUTDOWN_BUDGET = budget  # noqa: SLF001

    capture = RecordCapture()
    logger = logging.getLogger("trading.universal_trader")
    logger.addHandler(capture)
    try:
        task = asyncio.create_task(trader._cleanup_without_interruption())  # noqa: SLF001
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
            outcome = None
        except BaseException as exc:  # noqa: BLE001
            outcome = exc
    finally:
        logger.removeHandler(capture)
    return outcome, capture.records


async def check_cleanup_completes_through_cancellation() -> bool:
    print("\n1. Cleanup started before the cancellation still finishes")
    finished = []

    async def cleanup() -> None:
        await asyncio.sleep(0.3)
        finished.append("done")

    await _shutdown_with(cleanup)
    return _check(
        "cleanup ran to completion",
        finished == ["done"],
        "finished" if finished else "aborted at its first await",
    )


async def check_caller_returns_normally() -> bool:
    print("\n2. The caller returns, so the shutdown line is reached")

    async def cleanup() -> None:
        await asyncio.sleep(0.2)

    outcome, _ = await _shutdown_with(cleanup)
    return _check(
        "caller outcome",
        outcome is None,
        "returned normally"
        if outcome is None
        else f"{type(outcome).__name__} propagated — shutdown line skipped",
    )


async def check_budget_cuts_off_a_hung_cleanup() -> bool:
    print("\n3. Cleanup that overruns the budget is cut off, with a usable warning")

    async def cleanup() -> None:
        await asyncio.sleep(3600)

    outcome, records = await _shutdown_with(cleanup, budget=0.3)
    messages = " ".join(r.getMessage() for r in records)
    named = "cleanup_accounts.py" in messages
    return _check(
        "returned within the budget",
        outcome is None and named,
        f"returned={outcome is None}, warning names the recovery command={named}",
    )


async def check_failing_cleanup_is_logged_not_raised() -> bool:
    print("\n4. A cleanup that raises is logged, not propagated")

    async def cleanup() -> None:
        await asyncio.sleep(0.1)
        raise RuntimeError(_STUB_FAILURE)

    outcome, records = await _shutdown_with(cleanup)
    logged = any("Cleanup failed" in r.getMessage() for r in records)
    return _check(
        "caller outcome",
        outcome is None and logged,
        f"returned={outcome is None}, logged={logged}",
    )


def check_budget_is_bounded() -> bool:
    print("\n5. The budget is positive and finite")
    budget = UniversalTrader._CLEANUP_SHUTDOWN_BUDGET  # noqa: SLF001
    ok = isinstance(budget, int | float) and budget > 0 and math.isfinite(budget)
    return _check("_CLEANUP_SHUTDOWN_BUDGET", ok, f"{budget!r}")


async def main() -> None:
    print("=" * 72)
    print("Verifying shutdown cleanup is not aborted by its own cancellation")
    print("=" * 72)

    results = [
        await check_cleanup_completes_through_cancellation(),
        await check_caller_returns_normally(),
        await check_budget_cuts_off_a_hung_cleanup(),
        await check_failing_cleanup_is_logged_not_raised(),
        check_budget_is_bounded(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""Verify cancelling the token queue processor ends in CancelledError, not ValueError.

`UniversalTrader._process_token_queue` called `task_done()` from a `finally` that
also runs when `queue.get()` itself is cancelled — at which point no item was ever
taken, so the call was unbalanced:

    ValueError: task_done() called too many times

Cancelling that task is how `start()` shuts yolo mode down, and the cancellation
almost always lands while the task is parked on `get()`. `start()` catches only
`CancelledError` around the await, so the ValueError propagated to the outer
handler and every clean yolo-mode shutdown was logged as `Trading stopped due to
error` with a traceback. Nothing was lost — `_cleanup_resources()` still ran —
but the log said a good run had failed.

The fix has to stay balanced in both directions: the stale-token `continue` path
did take an item and must still call `task_done()`, or `join()` would never return.

Offline machine checks, no network and no funds moved:

  1. Cancelling a processor parked on an empty queue ends it without raising.
  2. Nothing is logged at ERROR while it shuts down.
  3. A processed token still calls `task_done()`, so `join()` returns.
  4. A token skipped as too old also calls `task_done()`.
  5. Cancelling mid-handler, with an item taken, calls `task_done()` exactly once
     and still ends without raising.

The processor catches `CancelledError` and breaks, so a cancelled task finishes
by returning rather than by propagating — either ending is fine to `start()`,
which catches `CancelledError` around the await. What must never happen is a
different exception type, since that is the one `start()` reports as a failure.

Usage:
    uv run tests/regression/verify_token_queue_shutdown.py
"""

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402

MAX_TOKEN_AGE = 30.0


class ErrorCapture(logging.Handler):
    """Collects records logged at ERROR or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_trader() -> UniversalTrader:
    """Build the queue processor's state without running __init__.

    `UniversalTrader.__init__` builds an RPC client and a platform registry.
    `_process_token_queue` touches none of that, so the attributes it does read
    are set directly.
    """
    trader = object.__new__(UniversalTrader)
    trader.token_queue = asyncio.Queue()
    trader.processed_tokens = set()
    trader.token_timestamps = {}
    trader.max_token_age = MAX_TOKEN_AGE
    trader._handle_token = lambda _token_info: asyncio.sleep(0)  # noqa: SLF001
    return trader


def _token(mint: str = "StubMint") -> SimpleNamespace:
    """A stand-in for TokenInfo carrying only what the processor reads."""
    return SimpleNamespace(mint=mint, symbol="STUB")


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _cancel_parked() -> tuple[BaseException | None, list[logging.LogRecord]]:
    """Cancel a processor parked on an empty queue, returning how it ended."""
    trader = _make_trader()
    capture = ErrorCapture()
    logger = logging.getLogger("trading.universal_trader")
    logger.addHandler(capture)
    try:
        task = asyncio.create_task(trader._process_token_queue())  # noqa: SLF001
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


async def check_cancel_does_not_raise() -> bool:
    print("\n1. Cancelling a parked processor ends it without raising")
    outcome, _ = await _cancel_parked()
    passed = outcome is None or isinstance(outcome, asyncio.CancelledError)
    return _check(
        "task outcome",
        passed,
        "returned normally"
        if outcome is None
        else f"{type(outcome).__name__}: {outcome}",
    )


async def check_shutdown_logs_no_error() -> bool:
    print("\n2. A cancelled shutdown logs nothing at ERROR")
    _, records = await _cancel_parked()
    messages = [r.getMessage() for r in records]
    return _check(
        "ERROR records emitted",
        not records,
        "none" if not records else f"{len(records)}: {messages}",
    )


async def check_processed_token_marks_done() -> bool:
    print("\n3. A processed token still balances the queue")
    trader = _make_trader()
    task = asyncio.create_task(trader._process_token_queue())  # noqa: SLF001
    await trader.token_queue.put(_token())
    try:
        await asyncio.wait_for(trader.token_queue.join(), timeout=1.0)
        joined = True
    except TimeoutError:
        joined = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return _check(
        "queue.join() returned",
        joined,
        "task_done() was called" if joined else "join() hung — task_done() missing",
    )


async def check_stale_token_marks_done() -> bool:
    print("\n4. A token skipped as too old still balances the queue")
    trader = _make_trader()
    token = _token("StaleMint")
    # Backdate the discovery so the freshness check takes the `continue` path.
    trader.token_timestamps[str(token.mint)] = -MAX_TOKEN_AGE * 2
    task = asyncio.create_task(trader._process_token_queue())  # noqa: SLF001
    await trader.token_queue.put(token)
    try:
        await asyncio.wait_for(trader.token_queue.join(), timeout=1.0)
        joined = True
    except TimeoutError:
        joined = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return _check(
        "queue.join() returned",
        joined,
        "task_done() was called on the skip path"
        if joined
        else "join() hung — the stale-token path lost its task_done()",
    )


async def check_cancel_mid_handler_marks_done() -> bool:
    print("\n5. Cancelling with an item in hand still balances the queue exactly once")
    trader = _make_trader()
    started = asyncio.Event()

    async def slow_handler(token_info: object) -> None:  # noqa: ARG001
        started.set()
        await asyncio.sleep(3600)

    trader._handle_token = slow_handler  # noqa: SLF001
    done_calls = 0
    real_task_done = trader.token_queue.task_done

    def counting_task_done() -> None:
        nonlocal done_calls
        done_calls += 1
        real_task_done()

    trader.token_queue.task_done = counting_task_done

    task = asyncio.create_task(trader._process_token_queue())  # noqa: SLF001
    await trader.token_queue.put(_token("HeldMint"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    ended_cleanly = outcome is None or isinstance(outcome, asyncio.CancelledError)
    return _check(
        "task_done() calls",
        done_calls == 1 and ended_cleanly,
        f"{done_calls} call(s), ended as "
        f"{'normal return' if outcome is None else type(outcome).__name__}",
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying the token queue processor shuts down cleanly")
    print("=" * 72)

    results = [
        await check_cancel_does_not_raise(),
        await check_shutdown_logs_no_error(),
        await check_processed_token_marks_done(),
        await check_stale_token_marks_done(),
        await check_cancel_mid_handler_marks_done(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

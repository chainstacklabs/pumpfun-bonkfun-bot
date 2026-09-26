"""Verify a WebSocket closing does not make a clean shutdown look like a failure.

Every WebSocket listener runs a `_ping_loop` that caught `asyncio.CancelledError`
and then fell through to a broad `except Exception: logger.exception("Ping error")`.
When the connection closes normally, `websocket.ping()` raises `ConnectionClosedOK`
(clean 1000 close both ways) or `ConnectionClosedError` (1000 sent, no close frame
back). Neither is a `CancelledError`, so both landed in the broad handler and were
logged at ERROR with a full traceback:

    ERROR:monitoring.universal_logs_listener:Ping error
    websockets.exceptions.ConnectionClosedError: sent 1000 (OK); no close frame received

Close code 1000 is a normal closure. Nothing was wrong, but anyone reading the logs
— or alerting on ERROR — saw a completed run report a failure.

#206 narrowed the race by cancelling the ping task in a `finally` on the read loop,
so on most shutdowns the cancellation lands first. It does not close it: the ping can
still fire between the connection closing and the cancellation arriving, and a
server-initiated close during normal operation reaches `_ping_loop` the same way it
always did.

Offline machine checks, no network and no funds moved. A stub websocket whose
`ping()` raises the real exception types drives each listener's real `_ping_loop`:

  1. A clean close (ConnectionClosedOK) logs nothing at ERROR, in every listener.
  2. A close with no close frame (ConnectionClosedError) does the same.
  3. The loop still returns rather than spinning on a dead connection.
  4. A genuinely unexpected failure is still logged at ERROR — the broad handler
     was kept, not widened away.
  5. Every class in `monitoring` with a `_ping_loop` is covered, so a new listener
     cannot be added without this check noticing.

Usage:
    uv run tests/regression/verify_ping_loop_close_is_quiet.py
"""

import asyncio
import inspect
import logging
import sys
from pathlib import Path

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import monitoring  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from monitoring.universal_logs_listener import UniversalLogsListener  # noqa: E402

LISTENERS = (
    UniversalLogsListener,
    UniversalBlockListener,
)

NORMAL_CLOSE = Close(1000, "OK")


def _clean_close() -> ConnectionClosedOK:
    """Both peers sent 1000 — "sent 1000 (OK); then received 1000 (OK)"."""
    return ConnectionClosedOK(NORMAL_CLOSE, NORMAL_CLOSE, rcvd_then_sent=False)


def _close_without_frame() -> ConnectionClosedError:
    """We sent 1000, the peer went away — "no close frame received"."""
    return ConnectionClosedError(None, NORMAL_CLOSE)


class ClosedWebSocket:
    """A websocket whose ping() always raises the given exception."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def ping(self):  # noqa: ANN201
        raise self.error

    async def close(self) -> None:
        return None


class ErrorCapture(logging.Handler):
    """Collects records logged at ERROR or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _run_ping_loop(
    listener_cls: type, error: BaseException
) -> tuple[bool, list[logging.LogRecord]]:
    """Drive one listener's real _ping_loop against a websocket that is closing.

    Returns:
        Tuple of (loop returned within its deadline, ERROR records emitted)
    """
    listener = object.__new__(listener_cls)
    listener.ping_interval = 0.001
    capture = ErrorCapture()
    logger = logging.getLogger(listener_cls.__module__)
    logger.addHandler(capture)
    try:
        try:
            await asyncio.wait_for(
                listener._ping_loop(ClosedWebSocket(error)),  # noqa: SLF001
                timeout=1.0,
            )
            returned = True
        except TimeoutError:
            returned = False
    finally:
        logger.removeHandler(capture)
    return returned, capture.records


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _check_close_is_quiet(label: str, make_error) -> bool:  # noqa: ANN001
    results = []
    for cls in LISTENERS:
        _, records = await _run_ping_loop(cls, make_error())
        messages = [r.getMessage() for r in records]
        results.append(
            _check(
                cls.__name__,
                not records,
                "no ERROR records"
                if not records
                else f"{len(records)} ERROR record(s): {messages}",
            )
        )
    print(f"     ({label})")
    return all(results)


async def check_clean_close_is_quiet() -> bool:
    print("\n1. A clean 1000 close is not an error")
    return await _check_close_is_quiet("ConnectionClosedOK", _clean_close)


async def check_close_without_frame_is_quiet() -> bool:
    print("\n2. A close with no close frame back is not an error either")
    return await _check_close_is_quiet("ConnectionClosedError", _close_without_frame)


async def check_loop_returns() -> bool:
    print("\n3. The loop ends rather than spinning on a dead connection")
    results = []
    for cls in LISTENERS:
        returned, _ = await _run_ping_loop(cls, _clean_close())
        results.append(
            _check(
                cls.__name__,
                returned,
                "returned" if returned else "still running after the close",
            )
        )
    return all(results)


async def check_unexpected_failure_still_logged() -> bool:
    print("\n4. An unexpected failure is still reported at ERROR")
    results = []
    for cls in LISTENERS:
        _, records = await _run_ping_loop(cls, RuntimeError("stub ping failure"))
        results.append(
            _check(
                cls.__name__,
                len(records) == 1,
                f"{len(records)} ERROR record(s) — the broad handler is intact",
            )
        )
    return all(results)


def check_every_ping_loop_is_covered() -> bool:
    print("\n5. Every listener that runs a ping loop is checked here")
    package_dir = Path(monitoring.__file__).parent
    with_ping_loop = set()
    for module_path in sorted(package_dir.glob("*.py")):
        if module_path.stem.startswith("__"):
            continue
        module = __import__(f"monitoring.{module_path.stem}", fromlist=["_"])
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and hasattr(obj, "_ping_loop"):
                with_ping_loop.add(obj.__name__)
    covered = {cls.__name__ for cls in LISTENERS}
    missing = sorted(with_ping_loop - covered)
    return _check(
        "listeners with a _ping_loop",
        not missing,
        f"{sorted(with_ping_loop)} all covered"
        if not missing
        else f"not covered: {missing}",
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying a closing WebSocket does not log a ping error")
    print("=" * 72)

    results = [
        await check_clean_close_is_quiet(),
        await check_close_without_frame_is_quiet(),
        await check_loop_returns(),
        await check_unexpected_failure_still_logged(),
        check_every_ping_loop_is_covered(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

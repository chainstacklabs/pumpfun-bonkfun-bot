"""Verify a WebSocket listener stops when its task is cancelled.

Both WebSocket listeners read with `asyncio.wait_for(websocket.recv(), ...)`
inside a `while True` loop guarded by a broad `except Exception`. Cancelling
that task — which is exactly how single-token mode shuts the listener down once
it has its coin — does not reliably raise `CancelledError` at the call site: if
the cancellation lands while the websockets library is assembling frames, the
library raises

    AssertionError: cannot reset() while queue isn't empty

instead. `AssertionError` is an `Exception`, so the broad handler swallowed it
and the shutdown request was lost. The loop then kept calling `recv()` on a
connection whose frame state was now corrupt, so every later read tripped
`assert frame.opcode is OP_TEXT or frame.opcode is OP_BINARY` — thousands of
identical ERROR lines, and `_wait_for_token` never returning, so the bot hung
after detecting a token instead of buying it.

Observed live: a blocks-listener run produced 86 consecutive AssertionErrors
starting on the line after "Found token", and never exited.

Offline machine checks, no network and no funds moved. A stub websocket raises
what the real library raises, against the real listeners:

  1. The blocks listener's read handler re-raises when its task is cancelled,
     even though the library reported an AssertionError.
  2. The logs listener does the same.
  3. An AssertionError that is NOT a cancellation still ends the connection,
     so the caller reconnects rather than spinning on a corrupt stream.
  4. A routine 30-second read timeout is still tolerated in place.
  5. A frame that is not valid JSON is still tolerated in place.
  6. Cancelling a full `listen_for_tokens` run actually finishes the task.
  7. The ping loop is cancelled on every exit from the read loop, not only on a
     closed connection, so it cannot go on pinging a dead socket.

Usage:
    uv run learning-examples/verify_listener_cancellation.py
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import monitoring.universal_block_listener as block_listener_module  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from monitoring.universal_logs_listener import UniversalLogsListener  # noqa: E402

# What websockets raises when a read is cancelled mid-frame-assembly.
CANCELLED_MID_FRAME = AssertionError("cannot reset() while queue isn't empty")
# What every later read on that corrupted connection raises.
CORRUPT_STREAM = AssertionError()

CANCEL_TIMEOUT = 5  # a listener that honours cancellation stops in milliseconds


def _make_block_listener() -> UniversalBlockListener:
    return UniversalBlockListener(
        wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
    )


def _make_logs_listener() -> UniversalLogsListener:
    return UniversalLogsListener(
        wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
    )


def _raising_socket(error: BaseException) -> SimpleNamespace:
    """A websocket whose recv() always raises `error`."""

    async def recv() -> str:
        raise error

    return SimpleNamespace(recv=recv)


def _socket_that_swallows_cancellation() -> SimpleNamespace:
    """A websocket that behaves the way websockets does under cancellation.

    It parks in recv() like a live connection waiting for a frame, and when the
    read is cancelled it raises AssertionError instead of letting
    CancelledError through — which is what the library does when the
    cancellation interrupts frame assembly.
    """

    async def recv() -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise CANCELLED_MID_FRAME from None
        return ""

    return SimpleNamespace(recv=recv)


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _read_once_while_cancelled(listener: object) -> str:
    """Cancel a task whose only job is one read, and report what happened.

    Returns:
        "cancelled" if the task honoured the cancellation, "swallowed" if the
        read returned normally, or the exception's class name.
    """
    started = asyncio.Event()

    async def run() -> None:
        started.set()
        # Parks in recv() like a live connection, then reports the
        # cancellation as AssertionError — exactly the live failure.
        await listener._wait_for_token_creation(  # noqa: SLF001
            _socket_that_swallows_cancellation()
        )

    task = asyncio.create_task(run())
    await started.wait()
    await asyncio.sleep(0)  # let the task reach the parked read
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=CANCEL_TIMEOUT)
    except asyncio.CancelledError:
        return "cancelled"
    except TimeoutError:
        return "hung"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
    return "swallowed"


async def check_block_listener_honours_cancellation() -> bool:
    print("\n1. The blocks listener stops when its task is cancelled")
    outcome = await _read_once_while_cancelled(_make_block_listener())
    return _check(
        "outcome of cancelling the read",
        outcome == "cancelled",
        f"{outcome} (the library reported AssertionError, not CancelledError)",
    )


async def check_logs_listener_honours_cancellation() -> bool:
    print("\n2. The logs listener stops when its task is cancelled")
    outcome = await _read_once_while_cancelled(_make_logs_listener())
    return _check(
        "outcome of cancelling the read",
        outcome == "cancelled",
        f"{outcome}",
    )


async def check_corrupt_stream_ends_the_connection() -> bool:
    print("\n3. An unexpected read error ends the connection instead of spinning")
    listener = _make_block_listener()
    raised = None
    try:
        await listener._wait_for_token_creation(  # noqa: SLF001
            _raising_socket(CORRUPT_STREAM)
        )
    except Exception as e:  # noqa: BLE001
        raised = type(e).__name__
    return _check(
        "propagated to the reconnect handler",
        raised is not None,
        f"{raised} — the caller reconnects rather than re-reading a corrupt stream",
    )


async def check_read_timeout_is_tolerated() -> bool:
    print("\n4. A routine 30-second read timeout is tolerated in place")
    listener = _make_block_listener()
    result = await listener._wait_for_token_creation(  # noqa: SLF001
        _raising_socket(TimeoutError())
    )
    return _check(
        "handler return value",
        result is None,
        f"{result!r} — an idle connection is not dropped",
    )


async def check_bad_json_is_tolerated() -> bool:
    print("\n5. A frame that is not valid JSON is tolerated in place")
    listener = _make_block_listener()

    async def recv() -> str:
        return "not json at all"

    result = await listener._wait_for_token_creation(  # noqa: SLF001
        SimpleNamespace(recv=recv)
    )
    return _check(
        "handler return value",
        result is None,
        f"{result!r} — one malformed frame does not drop the connection",
    )


async def check_full_listen_loop_stops() -> bool:
    print("\n6. Cancelling a full listen_for_tokens run finishes the task")
    listener = _make_block_listener()

    async def never_returns(*_args: object, **_kwargs: object) -> None:
        raise CANCELLED_MID_FRAME

    listener._wait_for_token_creation = never_returns  # noqa: SLF001
    listener._subscribe_to_programs = _noop  # noqa: SLF001
    listener._ping_loop = _noop  # noqa: SLF001

    # Keep the connect() out of the picture; only the loop is under test.
    class _Conn:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, *_args: object) -> bool:
            return False

    original = block_listener_module.websockets.connect
    block_listener_module.websockets.connect = lambda *_a, **_k: _Conn()
    try:
        task = asyncio.create_task(listener.listen_for_tokens(_noop))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=CANCEL_TIMEOUT)
            outcome = "swallowed"
        except asyncio.CancelledError:
            outcome = "cancelled"
        except TimeoutError:
            outcome = "hung"
    finally:
        block_listener_module.websockets.connect = original

    return _check(
        "outcome of cancelling the listener",
        outcome == "cancelled",
        f"{outcome} — single-token mode can shut the listener down",
    )


async def check_ping_task_is_cancelled_on_any_exit() -> bool:
    print("\n7. The ping loop is cancelled when a read error forces a reconnect")
    listener = _make_block_listener()
    ping_started = asyncio.Event()
    ping_cancelled = asyncio.Event()

    async def ping_loop(_websocket: object) -> None:
        ping_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            ping_cancelled.set()
            raise

    async def failing_read(*_args: object, **_kwargs: object) -> None:
        await ping_started.wait()
        # Not ConnectionClosed: the path that used to skip ping_task.cancel().
        raise CORRUPT_STREAM

    listener._ping_loop = ping_loop  # noqa: SLF001
    listener._subscribe_to_programs = _noop  # noqa: SLF001
    listener._wait_for_token_creation = failing_read  # noqa: SLF001

    class _Conn:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace()

        async def __aexit__(self, *_args: object) -> bool:
            return False

    original = block_listener_module.websockets.connect
    block_listener_module.websockets.connect = lambda *_a, **_k: _Conn()
    try:
        task = asyncio.create_task(listener.listen_for_tokens(_noop))
        # The listener sleeps 5s before reconnecting; one reconnect is enough.
        try:
            await asyncio.wait_for(ping_cancelled.wait(), timeout=CANCEL_TIMEOUT)
        except TimeoutError:
            pass
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=CANCEL_TIMEOUT)
    finally:
        block_listener_module.websockets.connect = original

    return _check(
        "ping loop cancelled",
        ping_cancelled.is_set(),
        "yes"
        if ping_cancelled.is_set()
        else "no — it would keep pinging a dead socket for up to ping_interval",
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


async def main() -> None:
    print("=" * 72)
    print("Verifying WebSocket listeners honour cancellation")
    print("=" * 72)

    results = [
        await check_block_listener_honours_cancellation(),
        await check_logs_listener_honours_cancellation(),
        await check_corrupt_stream_ends_the_connection(),
        await check_read_timeout_is_tolerated(),
        await check_bad_json_is_tolerated(),
        await check_full_listen_loop_stops(),
        await check_ping_task_is_cancelled_on_any_exit(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())

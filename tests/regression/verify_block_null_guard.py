"""Verify the blocks listener survives a blockSubscribe notification with a null block.

`blockSubscribe` delivers a notification whose `value.block` is `null` for a
skipped or otherwise unavailable slot — the key is present, the value is not.
The guard in `UniversalBlockListener._wait_for_token_creation` only checked that
the key existed, so the next line ran a membership test against `None`:

    TypeError: argument of type 'NoneType' is not iterable

The per-message handler catches it, so the listener keeps running, but the
notification is dropped and logged as an ERROR with a traceback. Any coin created
in that slot is never detected, which reads as the `blocks` listener quietly
finding fewer coins than `logs` or `geyser` over the same window.

Offline machine checks, no network and no funds moved. A stub websocket replays
scripted frames into the real `_wait_for_token_creation`:

  1. A null block returns None instead of raising TypeError.
  2. Nothing is logged at ERROR while handling it — a skipped slot is routine.
  3. A block with no `transactions` key still returns None (unchanged).
  4. A well-formed block is still processed, so the guard did not swallow
     real notifications.

Usage:
    uv run tests/regression/verify_block_null_guard.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402

NULL_BLOCK_FRAME = {
    "jsonrpc": "2.0",
    "method": "blockNotification",
    "params": {
        "result": {"context": {"slot": 372_000_000}, "value": {"block": None}},
        "subscription": 1,
    },
}

BLOCK_WITHOUT_TRANSACTIONS_FRAME = {
    "jsonrpc": "2.0",
    "method": "blockNotification",
    "params": {
        "result": {
            "context": {"slot": 372_000_001},
            "value": {"block": {"blockhash": "stub", "parentSlot": 372_000_000}},
        },
        "subscription": 1,
    },
}

BLOCK_WITH_TRANSACTIONS_FRAME = {
    "jsonrpc": "2.0",
    "method": "blockNotification",
    "params": {
        "result": {
            "context": {"slot": 372_000_002},
            "value": {"block": {"transactions": []}},
        },
        "subscription": 1,
    },
}


class StubWebSocket:
    """Serves one scripted frame, then blocks so recv() never returns twice."""

    def __init__(self, frame: dict) -> None:
        self.frame = frame
        self.sent = False

    async def recv(self) -> str:
        if self.sent:
            await asyncio.sleep(3600)
        self.sent = True
        return json.dumps(self.frame)


class ErrorCapture(logging.Handler):
    """Collects records logged at ERROR or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_listener() -> UniversalBlockListener:
    """Build a listener without touching the network."""
    return UniversalBlockListener(
        wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
    )


async def _feed(frame: dict) -> tuple[object, list[logging.LogRecord]]:
    """Run one frame through the real handler, capturing ERROR logs.

    Returns:
        Tuple of (handler return value, ERROR records emitted)
    """
    listener = _make_listener()
    capture = ErrorCapture()
    logger = logging.getLogger("monitoring.universal_block_listener")
    logger.addHandler(capture)
    try:
        result = await listener._wait_for_token_creation(  # noqa: SLF001
            SimpleNamespace(recv=StubWebSocket(frame).recv)
        )
    finally:
        logger.removeHandler(capture)
    return result, capture.records


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def check_null_block_returns_none() -> bool:
    print("\n1. A null block is skipped, not crashed on")
    result, _ = await _feed(NULL_BLOCK_FRAME)
    return _check(
        "handler return value", result is None, f"{result!r} (no TypeError raised)"
    )


async def check_null_block_is_not_an_error() -> bool:
    print("\n2. A skipped slot is not logged as an ERROR")
    _, records = await _feed(NULL_BLOCK_FRAME)
    messages = [r.getMessage() for r in records]
    return _check(
        "ERROR records emitted",
        not records,
        "none" if not records else f"{len(records)}: {messages}",
    )


async def check_block_without_transactions_returns_none() -> bool:
    print("\n3. A block carrying no transactions still returns None")
    result, records = await _feed(BLOCK_WITHOUT_TRANSACTIONS_FRAME)
    return _check(
        "handler return value",
        result is None and not records,
        f"{result!r}, {len(records)} ERROR record(s)",
    )


async def check_real_block_is_still_processed() -> bool:
    print("\n4. A well-formed block still reaches transaction processing")
    listener = _make_listener()
    seen: list[list] = []
    listener._process_block_transactions = lambda txs: seen.append(txs)  # noqa: SLF001
    await listener._wait_for_token_creation(  # noqa: SLF001
        SimpleNamespace(recv=StubWebSocket(BLOCK_WITH_TRANSACTIONS_FRAME).recv)
    )
    return _check(
        "_process_block_transactions called",
        len(seen) == 1,
        f"{len(seen)} call(s) — the guard did not swallow a real notification",
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying the blocks listener null-block guard")
    print("=" * 72)

    results = [
        await check_null_block_returns_none(),
        await check_null_block_is_not_an_error(),
        await check_block_without_transactions_returns_none(),
        await check_real_block_is_still_processed(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""Helpers several verifiers share, each one identical in every file that used it.

Only pure helpers belong here: a function or stub that keeps no state between
verifiers and is the same wherever it is used. A helper that one verifier needs
in a different shape stays in that verifier. `run_all.py` runs only
`verify_*.py`, so this module is never run as a verifier. The verifiers import
it from their own folder, which is on `sys.path` when one runs alone.

Importing it pulls in nothing from `src/`: the two helpers that build `src/`
types import them when called, after the caller has put `src/` on the path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import TYPE_CHECKING

import base58

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from core.trade_deltas import TradeDeltas
    from geyser.generated import geyser_pb2


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.005)
    return True


async def _no_fee(_accounts: list) -> None:
    return None


def _deltas(tokens_raw: int | None, spent: int | None) -> TradeDeltas | None:
    """A buy's transaction as the client reads it: `spent` is the venue's fill."""
    from core.trade_deltas import TradeDeltas  # noqa: PLC0415 - see module docstring

    if tokens_raw is None:
        return None
    return TradeDeltas(
        tokens_raw=tokens_raw,
        quote_raw=None if spent is None else -spent,
        network_fee_lamports=0,
        venue_quote_raw=spent,
    )


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _geyser_update(tx: dict) -> geyser_pb2.SubscribeUpdate:
    """A geyser frame carrying the same transaction as the JSON fixture."""
    from geyser.generated import geyser_pb2  # noqa: PLC0415 - see module docstring

    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    message = info.transaction.message
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    loaded = tx["meta"].get("loadedAddresses") or {}
    info.meta.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.meta.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    for group in tx["meta"].get("innerInstructions") or []:
        inner = info.meta.inner_instructions.add()
        inner.index = group["index"]
        for ix in group["instructions"]:
            compiled = inner.instructions.add()
            compiled.program_id_index = ix["programIdIndex"]
            compiled.accounts = bytes(ix["accounts"])
            compiled.data = base58.b58decode(ix["data"])
    return update


def _logs_from_fixture(path: Path) -> list[str] | None:
    """Pull `meta.logMessages` out of a captured transaction fixture.

    Returns:
        The log lines, or None if the fixture holds no transaction meta.
    """

    def walk(node: object) -> Iterator[list[str]]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("logMessages", "log_messages") and value:
                    yield value
                else:
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return next(walk(json.loads(path.read_text())), None)


class ErrorCapture(logging.Handler):
    """Collects records logged at ERROR or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FeedEnd(BaseException):
    """Ends a listener loop once its scripted frames run out."""


class _FakeSocket:
    """A websocket that answers subscriptions, then plays frames, then ends."""

    def __init__(self, frames: list[dict]) -> None:
        self.replies: deque[str] = deque()
        self.frames = deque(json.dumps(frame) for frame in frames)

    async def send(self, message: str) -> None:
        self.replies.append(json.dumps({"id": json.loads(message)["id"], "result": 7}))

    async def recv(self) -> str:
        if self.replies:
            return self.replies.popleft()
        if self.frames:
            return self.frames.popleft()
        raise _FeedEnd

    async def ping(self) -> None:
        await asyncio.sleep(3600)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _FakeSocket:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeChannel:
    async def close(self) -> None:
        return None

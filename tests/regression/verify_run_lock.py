"""Verify one process at a time uses a positions file.

The bot rewrites its whole positions file from memory on every change. A second
bot on the same file, or `pump_bot_positions sell-all` while the bot runs,
would undo each other's writes and could sell one coin twice. A lock file next
to the positions file keeps them apart; the operating system drops it when its
holder exits, so a crash leaves no stale lock.

Offline machine checks, no network and no funds moved:

  1. A second lock on the same file fails while the first is held, in the
     same process and from another process.
  2. It succeeds once the holder releases it, and once the holding process
     has been killed.
  3. `start_bot` does not start a bot whose positions file is locked, and
     says why in one line.

Usage:
    uv run tests/regression/verify_run_lock.py
"""

import asyncio
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402

import bot_runner  # noqa: E402
from trading.run_lock import RunLock  # noqa: E402

HOLDER = """
import sys, time
sys.path.insert(0, {src!r})
from trading.run_lock import RunLock
lock = RunLock({path!r})
assert lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


def check_same_process() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.json"
        first, second = RunLock(path), RunLock(path)
        got_first = first.acquire()
        refused = not second.acquire()
        first.release()
        after = second.acquire()
        second.release()
        third = RunLock(path)
        free = third.acquire()
        third.release()
    ok = got_first and refused and after and free
    return _check(
        "a second lock in one process waits for release",
        ok,
        f"first {got_first}, refused {refused}, after {after}, free {free}",
    )


def check_other_process() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.json"
        holder = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                HOLDER.format(src=str(PROJECT_ROOT / "src"), path=str(path)),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            ready = holder.stdout.readline().strip() == "held"
            refused = not RunLock(path).acquire()
        finally:
            holder.kill()
            holder.wait()
        time.sleep(0.1)
        lock = RunLock(path)
        after_kill = lock.acquire()
        lock.release()
    ok = ready and refused and after_kill
    return _check(
        "another process's lock refuses, a killed holder frees it",
        ok,
        f"ready {ready}, refused {refused}, after kill {after_kill}",
    )


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def check_start_bot_refuses() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        positions = Path(tmp) / "bot.json"
        cfg = {"name": "locked-bot", "rpc_endpoint": "https://rpc.invalid"}
        lock = RunLock(positions)
        lock.acquire()
        built = []
        original = bot_runner._startable_platform  # noqa: SLF001
        bot_runner._startable_platform = lambda _cfg: built.append(1)  # noqa: SLF001
        handler = _Lines()
        logging.getLogger("bot_runner").addHandler(handler)
        try:
            started = asyncio.run(
                bot_runner._start_loaded(cfg, str(positions))  # noqa: SLF001
            )
        finally:
            bot_runner._startable_platform = original  # noqa: SLF001
            logging.getLogger("bot_runner").removeHandler(handler)
            lock.release()
    said = [line for line in handler.lines if "in use" in line]
    ok = started is False and not built and len(said) == 1
    return _check(
        "start_bot leaves a bot with a locked file unstarted",
        ok,
        f"started {started}, went on {bool(built)}, said {said}",
    )


def main() -> int:
    print("Positions file run lock")
    results = [check_same_process(), check_other_process(), check_start_bot_refuses()]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

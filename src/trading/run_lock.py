"""One process at a time per positions file.

The bot rewrites its whole positions file from memory, so two processes on one
file undo each other's writes. The lock is an OS lock on `<positions file>.lock`,
which the system drops when its holder exits, so a crash leaves none behind.
"""

from __future__ import annotations

from pathlib import Path

from utils.logger import get_logger

try:
    import fcntl
except ImportError:  # Windows has no flock
    fcntl = None

logger = get_logger(__name__)
_warned = False


class RunLock:
    """The lock on one positions file."""

    def __init__(self, positions_file: Path | str) -> None:
        self.path = Path(f"{positions_file}.lock")
        self._file = None

    def acquire(self) -> bool:
        """Take the lock without waiting; False when another holder has it.

        Where the system has no `flock` (Windows) this warns once and returns
        True: nothing keeps two processes apart there.
        """
        global _warned  # noqa: PLW0603 - one warning per process
        if fcntl is None:
            if not _warned:
                logger.warning(
                    "This system cannot lock the positions file: do not run "
                    "two bots, or a bot and pump_bot_positions sell-all, on "
                    "one positions file at once"
                )
                _warned = True
            return True
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file, fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    @staticmethod
    def held(positions_file: Path | str) -> bool:
        """Whether another holder has the lock right now."""
        lock = RunLock(positions_file)
        if not lock.acquire():
            return True
        lock.release()
        return False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()

"""One process at a time per positions file.

The bot rewrites its whole positions file from memory, so two processes on one
file undo each other's writes. The lock is an OS lock on `<positions file>.lock`
(`flock`, or `msvcrt.locking` on Windows), which the system drops when its
holder exits, so a crash leaves none behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

from utils.logger import get_logger

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None

logger = get_logger(__name__)
_warned = False


def open_lock_file(path: Path) -> IO:
    """The lock file, opened for locking; created when missing."""
    return path.open("a+")


class RunLock:
    """The lock on one positions file."""

    def __init__(self, positions_file: Path | str) -> None:
        # Resolved, so a symlink to the file meets the same lock.
        self.path = Path(f"{Path(positions_file).resolve()}.lock")
        self._file: IO | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting; False when another holder has it.

        Where the system has neither `flock` nor `msvcrt` this warns once and
        returns True: nothing keeps two processes apart there.

        Raises:
            OSError: The lock file can't be created or opened
        """
        global _warned  # noqa: PLW0603 - one warning per process
        if fcntl is None and msvcrt is None:
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
        handle = open_lock_file(self.path)
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                # The first byte stands for the whole file; Windows locks ranges.
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if fcntl is not None:
            fcntl.flock(self._file, fcntl.LOCK_UN)
        elif msvcrt is not None:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()

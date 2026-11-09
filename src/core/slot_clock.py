"""When a slot happened, estimated from the cluster's current slot."""

import asyncio
from time import monotonic

from core.client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)

#: Seconds per slot the cluster targets; real slots run a little longer.
SLOT_SECONDS = 0.4


class SlotClock:
    """Tracks the cluster's newest slot in the background.

    Two sources feed it: the node's processed slot, read every
    `REFRESH_SECONDS`, and the slot of every create the listener delivers
    (`observe`). A stream can run slots ahead of the node, so the clock keeps
    whichever reading puts the newest slot furthest ahead. `age` makes no RPC
    call, so it can sit on the buy path.
    """

    #: Seconds between reads of the current slot.
    REFRESH_SECONDS = 2.0
    #: Seconds after which a reading is too old to count from: real slots run
    #: longer than `SLOT_SECONDS`, so the estimate drifts the longer it runs.
    STALE_SECONDS = 10.0

    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        # By source ("node", "stream"): the newest slot it gave and the
        # monotonic time that arrived.
        self._readings: dict[str, tuple[int, float]] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start reading the current slot; a second call does nothing."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="slot-clock")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        failing = False
        while True:
            try:
                self._readings["node"] = (await self._client.get_slot(), monotonic())
                failing = False
            except Exception as e:  # noqa: BLE001 - the last reading stays in use
                if not failing:
                    logger.warning(
                        f"Could not read the current slot ({e!s}); coins count "
                        f"from detection once the last reading is "
                        f"{self.STALE_SECONDS:g}s old"
                    )
                failing = True
            await asyncio.sleep(self.REFRESH_SECONDS)

    def observe(self, slot: int | None) -> None:
        """Count a slot the listener just delivered; None does nothing."""
        if slot is None:
            return
        now = monotonic()
        last = self._readings.get("stream")
        if last is None or slot >= last[0] or now - last[1] > self.STALE_SECONDS:
            self._readings["stream"] = (slot, now)

    def age(self, slot: int | None) -> float | None:
        """Seconds since `slot`, at `SLOT_SECONDS` a slot.

        Never below 0. None when `slot` is None, no current slot has been read
        yet, or the last reading is older than `STALE_SECONDS`.
        """
        if slot is None:
            return None
        now = monotonic()
        # Each fresh reading's estimate of the time since `slot`; the largest
        # comes from the reading that puts the newest slot furthest ahead.
        ages = [
            now - read_at + (reading - slot) * SLOT_SECONDS
            for reading, read_at in self._readings.values()
            if now - read_at <= self.STALE_SECONDS
        ]
        if not ages:
            return None
        return max(0.0, *ages)

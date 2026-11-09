"""When a slot happened, estimated from the cluster's current slot."""

import asyncio
from time import monotonic

from core.client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)

#: Seconds per slot the cluster targets; real slots run a little longer.
SLOT_SECONDS = 0.4


class SlotClock:
    """Tracks the cluster's processed slot in the background.

    `age` makes no RPC call: it reads the last slot fetched and counts forward
    from it, so it can sit on the buy path.
    """

    #: Seconds between reads of the current slot.
    REFRESH_SECONDS = 2.0
    #: Seconds after which a reading is too old to count from: real slots run
    #: longer than `SLOT_SECONDS`, so the estimate drifts the longer it runs.
    STALE_SECONDS = 10.0

    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        # The last slot read and the monotonic time it arrived.
        self._reference: tuple[int, float] | None = None
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
                slot = await self._client.get_slot()
                self._reference = (slot, monotonic())
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

    def age(self, slot: int | None) -> float | None:
        """Seconds since `slot`, at `SLOT_SECONDS` a slot.

        None when `slot` is None, no current slot has been read yet, or the
        last reading is older than `STALE_SECONDS`.
        """
        if slot is None or self._reference is None:
            return None
        reference_slot, read_at = self._reference
        since_read = monotonic() - read_at
        if since_read > self.STALE_SECONDS:
            return None
        return since_read + (reference_slot - slot) * SLOT_SECONDS

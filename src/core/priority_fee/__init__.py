from abc import ABC, abstractmethod


class PriorityFeePlugin(ABC):
    """Base class for priority fee calculation plugins."""

    @abstractmethod
    async def get_priority_fee(self) -> int | None:
        """Calculate the priority fee.

        Returns:
            Optional[int]: Priority fee in lamports, or None if no fee should be applied.
        """
        pass

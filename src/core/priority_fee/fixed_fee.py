from . import PriorityFeePlugin


class FixedPriorityFee(PriorityFeePlugin):
    """Fixed priority fee plugin."""

    def __init__(self, fixed_fee: int):
        """Initialize the fixed fee plugin.

        Args:
            fixed_fee: Fixed priority fee in microlamports.
        """
        self.fixed_fee = fixed_fee

    async def get_priority_fee(self) -> int | None:
        """Return the fixed priority fee.

        Returns:
            Optional[int]: Fixed priority fee in microlamports, or None if fixed_fee is 0.
        """
        if self.fixed_fee == 0:
            return None
        return self.fixed_fee

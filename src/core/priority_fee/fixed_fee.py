from . import PriorityFeePlugin

MAX_U64 = (1 << 64) - 1


class FixedPriorityFee(PriorityFeePlugin):
    """Fixed priority fee plugin."""

    def __init__(self, fixed_fee: int):
        """
        Initialize the fixed fee plugin.

        Args:
            fixed_fee: Fixed priority fee in microlamports.

        Raises:
            TypeError: If ``fixed_fee`` is not an integer.
            ValueError: If ``fixed_fee`` is outside the unsigned 64-bit range.
        """
        if isinstance(fixed_fee, bool) or not isinstance(fixed_fee, int):
            raise TypeError("fixed_fee must be an integer")
        if not 0 <= fixed_fee <= MAX_U64:
            raise ValueError("fixed_fee must be between 0 and 2^64 - 1")
        self.fixed_fee = fixed_fee

    async def get_priority_fee(self) -> int | None:
        """
        Return the fixed priority fee.

        Returns:
            Optional[int]: Fixed priority fee in microlamports, or None if fixed_fee is 0.
        """
        if self.fixed_fee == 0:
            return None
        return self.fixed_fee

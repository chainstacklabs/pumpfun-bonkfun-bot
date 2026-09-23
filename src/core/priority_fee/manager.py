from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.dynamic_fee import DynamicPriorityFee
from core.priority_fee.fixed_fee import FixedPriorityFee
from utils.logger import get_logger

logger = get_logger(__name__)


class PriorityFeeManager:
    """Manager for priority fee calculation and validation."""

    def __init__(
        self,
        client: SolanaClient,
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,
        extra_fee: float,
        hard_cap: int,
    ):
        """Initialize the priority fee manager.

        Args:
            client: Solana RPC client for dynamic fee calculation.
            enable_dynamic_fee: Whether to enable dynamic fee calculation.
            enable_fixed_fee: Whether to enable fixed fee.
            fixed_fee: Fixed priority fee in microlamports.
            extra_fee: Percentage increase to apply to the base fee.
            hard_cap: Maximum allowed priority fee in microlamports.
        """
        self.client = client
        self.enable_dynamic_fee = enable_dynamic_fee
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee = fixed_fee
        self.extra_fee = extra_fee
        self.hard_cap = hard_cap

        # Initialize plugins
        self.dynamic_fee_plugin = DynamicPriorityFee(client)
        self.fixed_fee_plugin = FixedPriorityFee(fixed_fee)

    async def calculate_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """Calculate the priority fee based on the configuration.

        Args:
            accounts: List of accounts to consider for dynamic fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            Optional[int]: Calculated priority fee in microlamports, or None if no fee should be applied.
        """
        base_fee = await self._get_base_fee(accounts)
        if base_fee is None:
            return None

        # Apply extra fee (percentage increase)
        final_fee = int(base_fee * (1 + self.extra_fee))

        # Enforce hard cap
        if final_fee > self.hard_cap:
            logger.warning(
                f"Calculated priority fee {final_fee} exceeds hard cap {self.hard_cap}. Applying hard cap."
            )
            final_fee = self.hard_cap

        return final_fee

    async def _get_base_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        """Determine the base fee based on the configuration.

        Returns:
            Optional[int]: Base fee in microlamports, or None if no fee should be applied.
        """
        # Prefer dynamic fee if both are enabled
        if self.enable_dynamic_fee:
            dynamic_fee = await self.dynamic_fee_plugin.get_priority_fee(accounts)
            if dynamic_fee is not None:
                return dynamic_fee

        # Fall back to fixed fee if enabled
        if self.enable_fixed_fee:
            return await self.fixed_fee_plugin.get_priority_fee()

        # No priority fee if both are disabled
        return None

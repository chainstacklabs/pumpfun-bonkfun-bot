from decimal import Decimal

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
        total_lamport_cap: int | None = None,
    ) -> None:
        """
        Initialize the priority fee manager.

        Args:
            client: Solana RPC client for dynamic fee calculation.
            enable_dynamic_fee: Whether to enable dynamic fee calculation.
            enable_fixed_fee: Whether to enable fixed fee.
            fixed_fee: Fixed priority fee in microlamports per compute unit.
            extra_fee: Non-negative percentage increase to apply to the base fee.
            hard_cap: Maximum fee in microlamports per compute unit.
            total_lamport_cap: Optional total priority-fee ceiling in lamports.
                A compute-unit limit must be supplied when this cap is enforced.
        """
        if not isinstance(enable_dynamic_fee, bool):
            raise TypeError("enable_dynamic_fee must be a boolean")
        if not isinstance(enable_fixed_fee, bool):
            raise TypeError("enable_fixed_fee must be a boolean")
        self._validate_u64("fixed_fee", fixed_fee)
        self._validate_u64("hard_cap", hard_cap)
        self._validate_optional_u64("total_lamport_cap", total_lamport_cap)
        if isinstance(extra_fee, bool) or not isinstance(extra_fee, (int, float)):
            raise TypeError("extra_fee must be a number")
        if not Decimal(str(extra_fee)).is_finite() or extra_fee < 0:
            raise ValueError("extra_fee must be finite and non-negative")

        self.client = client
        self.enable_dynamic_fee = enable_dynamic_fee
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee = fixed_fee
        self.extra_fee = float(extra_fee)
        self.hard_cap = hard_cap
        self.total_lamport_cap = total_lamport_cap

        self.dynamic_fee_plugin = DynamicPriorityFee(client)
        self.fixed_fee_plugin = FixedPriorityFee(fixed_fee)

    async def calculate_priority_fee(
        self,
        accounts: list[Pubkey] | None = None,
        compute_unit_limit: int | None = None,
        total_lamport_cap: int | None = None,
    ) -> int | None:
        """Calculate a bounded priority fee in microlamports per compute unit.

        ``compute_unit_limit`` is optional for backward compatibility, but is
        required whenever a configured or per-call total-lamport cap applies.
        """
        self._validate_accounts(accounts)
        base_fee = await self._get_base_fee(accounts)
        if base_fee is None:
            return None
        self._validate_u64("calculated priority fee", base_fee)

        multiplier = Decimal(1) + Decimal(str(self.extra_fee))
        final_fee = int(Decimal(base_fee) * multiplier)
        if final_fee > self.hard_cap:
            logger.warning(
                "Priority fee exceeded per-CU cap; applying cap",
                extra={"event_id": "priority_fee_per_cu_capped"},
            )
            final_fee = self.hard_cap

        effective_total_cap = (
            total_lamport_cap
            if total_lamport_cap is not None
            else self.total_lamport_cap
        )
        return self.validate_fee_budget(
            final_fee,
            compute_unit_limit=compute_unit_limit,
            total_lamport_cap=effective_total_cap,
        )

    def validate_fee_budget(
        self,
        priority_fee: int,
        compute_unit_limit: int | None,
        total_lamport_cap: int | None = None,
    ) -> int:
        """Validate and enforce the optional total priority-fee budget.

        Returns the highest safe microlamport-per-CU fee no greater than
        ``priority_fee``. Without a total cap, the validated fee is unchanged.
        """
        self._validate_u64("priority_fee", priority_fee)
        effective_total_cap = (
            total_lamport_cap
            if total_lamport_cap is not None
            else self.total_lamport_cap
        )
        self._validate_optional_u64("total_lamport_cap", effective_total_cap)
        if effective_total_cap is None:
            if compute_unit_limit is not None:
                self._validate_compute_unit_limit(compute_unit_limit)
            return priority_fee
        if compute_unit_limit is None:
            raise ValueError(
                "compute_unit_limit is required when total_lamport_cap is set"
            )
        self._validate_compute_unit_limit(compute_unit_limit)

        max_per_cu = effective_total_cap * 1_000_000 // compute_unit_limit
        if priority_fee > max_per_cu:
            logger.warning(
                "Priority fee exceeded total-lamport cap; applying cap",
                extra={"event_id": "priority_fee_total_capped"},
            )
            return max_per_cu
        return priority_fee

    @classmethod
    def calculate_total_fee_lamports(
        cls, priority_fee: int, compute_unit_limit: int
    ) -> int:
        """Return the total priority fee, rounded up to whole lamports."""
        cls._validate_u64("priority_fee", priority_fee)
        cls._validate_compute_unit_limit(compute_unit_limit)
        return (priority_fee * compute_unit_limit + 999_999) // 1_000_000

    @staticmethod
    def _validate_accounts(accounts: list[Pubkey] | None) -> None:
        if accounts is None:
            return
        if not isinstance(accounts, list):
            raise TypeError("accounts must be a list of Pubkey values or None")
        if len(accounts) > DynamicPriorityFee.MAX_ACCOUNTS:
            raise ValueError(
                f"accounts cannot contain more than "
                f"{DynamicPriorityFee.MAX_ACCOUNTS} entries"
            )
        if any(not isinstance(account, Pubkey) for account in accounts):
            raise TypeError("accounts must contain only Pubkey values")

    @staticmethod
    def _validate_compute_unit_limit(compute_unit_limit: int) -> None:
        if isinstance(compute_unit_limit, bool) or not isinstance(
            compute_unit_limit, int
        ):
            raise TypeError("compute_unit_limit must be an integer")
        if compute_unit_limit <= 0:
            raise ValueError("compute_unit_limit must be positive")

    @staticmethod
    def _validate_u64(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if not 0 <= value <= (1 << 64) - 1:
            raise ValueError(f"{name} must be between 0 and 2^64 - 1")

    @classmethod
    def _validate_optional_u64(cls, name: str, value: int | None) -> None:
        if value is not None:
            cls._validate_u64(name, value)

    async def _get_base_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        """
        Determine the base fee based on the configuration.

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

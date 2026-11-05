"""Base interfaces for trading operations, with platform support."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from solders.pubkey import Pubkey

from core.trade_deltas import TradeDeltas

# Import from interfaces to avoid duplication
from interfaces.core import Platform, TokenInfo, TradeFailureReason


@dataclass
class TradeResult:
    """Enhanced result of a trading operation with platform support."""

    success: bool
    platform: Platform = Platform.PUMP_FUN  # Add platform tracking
    tx_signature: str | None = None
    error_message: str | None = None
    amount: float | None = None
    #: `amount` in raw token units, exact. None when only an estimate exists.
    amount_raw: int | None = None
    price: float | None = None
    # Why a failed trade failed, when the caller has to decide whether
    # resubmitting is safe. None on success, and on failures that predate the
    # distinction — treat a missing reason as "unknown", not as "reverted".
    failure_reason: TradeFailureReason | None = None
    #: What the landed transaction did to the wallet. None when it was not
    #: read, and `price` and `amount` are then estimates.
    deltas: TradeDeltas | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization.

        Returns:
            Dictionary representation of the trade result
        """
        return {
            "success": self.success,
            "platform": self.platform.value,
            "tx_signature": self.tx_signature,
            "error_message": self.error_message,
            "amount": self.amount,
            "amount_raw": self.amount_raw,
            "price": self.price,
            "failure_reason": (
                self.failure_reason.value if self.failure_reason else None
            ),
            "deltas": asdict(self.deltas) if self.deltas else None,
        }


@dataclass(frozen=True)
class PendingBuy:
    """What resolving a buy sent but not settled needs beyond its signature."""

    #: The most raw tokens it can have delivered; a balance read never claims
    #: more, so a coin also held for another reason is not sold with it.
    max_amount_raw: int | None = None
    #: The account its spend lands in, to read the spend back; None: the curve.
    quote_destination: str | None = None
    #: An exact-in buy's spend in raw quote units, known before it was sent.
    spend_raw: int | None = None
    #: The block height past which it can no longer land; None when unknown.
    last_valid_block_height: int | None = None


class Trader(ABC):
    """Enhanced base interface for trading operations with platform support."""

    @abstractmethod
    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute trading operation.

        Args:
            token_info: Enhanced token information with platform support

        Returns:
            TradeResult with operation outcome including platform info
        """
        pass

    def _get_relevant_accounts(self, token_info: TokenInfo) -> list[Pubkey]:
        """Get the list of accounts relevant for calculating the priority fee.

        This is now platform-agnostic and should be overridden by platform-specific traders.

        Args:
            token_info: Enhanced token information

        Returns:
            List of relevant accounts (basic implementation)
        """
        # Basic implementation - platform-specific traders should override this
        accounts = [token_info.mint]

        if token_info.bonding_curve:
            accounts.append(token_info.bonding_curve)

        if token_info.pool_state:  # For other platforms
            accounts.append(token_info.pool_state)

        return accounts


__all__ = [
    "Platform",
    "TokenInfo",
    "TradeResult",
    "Trader",
]

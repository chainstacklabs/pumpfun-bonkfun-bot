"""
Position management for take profit/stop loss functionality.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from solders.pubkey import Pubkey


class ExitReason(Enum):
    """Reasons for position exit."""

    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MAX_HOLD_TIME = "max_hold_time"
    MANUAL = "manual"


@dataclass
class Position:
    """Represents an active trading position."""

    # Token information
    mint: Pubkey
    symbol: str

    # Position details
    entry_price: float
    quantity: float
    entry_time: datetime

    # Exit conditions
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    max_hold_time: int | None = None  # seconds

    # Status
    is_active: bool = True
    exit_reason: ExitReason | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None

    @classmethod
    def create_from_buy_result(
        cls,
        mint: Pubkey,
        symbol: str,
        entry_price: float,
        quantity: float,
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
    ) -> "Position":
        """Create a position from a successful buy transaction.

        Args:
            mint: Token mint address
            symbol: Token symbol
            entry_price: Price at which position was entered
            quantity: Quantity of tokens purchased
            take_profit_percentage: Take profit percentage (0.5 = 50% profit)
            stop_loss_percentage: Stop loss percentage (0.2 = 20% loss)
            max_hold_time: Maximum hold time in seconds

        Returns:
            Position instance
        """
        take_profit_price = None
        if take_profit_percentage is not None:
            take_profit_price = entry_price * (1 + take_profit_percentage)

        stop_loss_price = None
        if stop_loss_percentage is not None:
            stop_loss_price = entry_price * (1 - stop_loss_percentage)

        return cls(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=datetime.utcnow(),
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            max_hold_time=max_hold_time,
        )

    def should_exit(self, current_price: float) -> tuple[bool, ExitReason | None]:
        """Check if position should be exited based on current conditions.

        Args:
            current_price: Current token price

        Returns:
            Tuple of (should_exit, exit_reason)
        """
        if not self.is_active:
            return False, None

        # Check take profit
        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT

        # Check stop loss
        if self.stop_loss_price and current_price <= self.stop_loss_price:
            return True, ExitReason.STOP_LOSS

        # Check max hold time
        if self.should_exit_on_time():
            return True, ExitReason.MAX_HOLD_TIME

        return False, None

    def should_exit_on_time(self) -> bool:
        """Check the one exit condition that needs no price.

        `should_exit` cannot answer anything without a current price, so a
        caller whose price read just failed has no way to ask about
        `max_hold_time` — which does not depend on a price at all. Split out so
        a dead price feed cannot hold a position open past its deadline.

        Returns:
            Whether an active position has been held for at least max_hold_time
        """
        if not self.is_active or not self.max_hold_time:
            return False
        elapsed_time = (datetime.utcnow() - self.entry_time).total_seconds()
        return elapsed_time >= self.max_hold_time

    def close_position(self, exit_price: float, exit_reason: ExitReason) -> None:
        """Close the position with exit details.

        Args:
            exit_price: Price at which position was exited
            exit_reason: Reason for exit
        """
        self.is_active = False
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.utcnow()

    def get_pnl(self, current_price: float | None = None) -> dict:
        """Calculate profit/loss for the position.

        Args:
            current_price: Current price (uses exit_price if position is closed)

        Returns:
            Dictionary with PnL information
        """
        if self.is_active and current_price is None:
            raise ValueError("current_price required for active position")

        price_to_use = self.exit_price if not self.is_active else current_price
        if price_to_use is None:
            raise ValueError("No price available for PnL calculation")

        price_change = price_to_use - self.entry_price
        price_change_pct = (price_change / self.entry_price) * 100
        unrealized_pnl = price_change * self.quantity

        return {
            "entry_price": self.entry_price,
            "current_price": price_to_use,
            "price_change": price_change,
            "price_change_pct": price_change_pct,
            "unrealized_pnl_sol": unrealized_pnl,
            "quantity": self.quantity,
        }

    def __str__(self) -> str:
        """String representation of position."""
        if self.is_active:
            status = "ACTIVE"
        elif self.exit_reason:
            status = f"CLOSED ({self.exit_reason.value})"
        else:
            status = "CLOSED (UNKNOWN)"
        return f"Position({self.symbol}: {self.quantity:.6f} @ {self.entry_price:.8f} SOL - {status})"

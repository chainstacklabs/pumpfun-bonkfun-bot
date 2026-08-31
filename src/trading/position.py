"""
Position management for take profit/stop loss functionality.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from typing import Any

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

    # Position details. Floats are retained for presentation compatibility;
    # execution uses the corresponding raw integer fields when available.
    entry_price: float
    quantity: float
    entry_time: datetime
    position_id: str | None = None
    quantity_raw: int | None = None
    quote_amount_raw: int | None = None
    account_balance_baseline_raw: int | None = None

    # Exit conditions
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    max_hold_time: int | None = None  # seconds

    # Status
    is_active: bool = True
    exit_reason: ExitReason | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    pending_exit_signature: str | None = None
    pending_exit_reason: ExitReason | None = None
    pending_exit_intent_id: str | None = None
    pending_exit_price: float | None = None
    exit_attempt_sequence: int = 0

    def __post_init__(self) -> None:
        """Validate invariants and normalize timestamps to aware UTC values."""
        if (
            isinstance(self.entry_price, bool)
            or not isinstance(self.entry_price, int | float)
            or not isfinite(self.entry_price)
            or self.entry_price <= 0
        ):
            raise ValueError("entry_price must be finite and positive")
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int | float)
            or not isfinite(self.quantity)
            or self.quantity <= 0
        ):
            raise ValueError("quantity must be finite and positive")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(self.entry_time, datetime):
            raise ValueError("entry_time must be a datetime")
        for field_name, value in (
            ("take_profit_price", self.take_profit_price),
            ("stop_loss_price", self.stop_loss_price),
            ("exit_price", self.exit_price),
            ("pending_exit_price", self.pending_exit_price),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be finite and positive")
        for field_name, value, allow_zero in (
            ("quantity_raw", self.quantity_raw, False),
            ("quote_amount_raw", self.quote_amount_raw, True),
            (
                "account_balance_baseline_raw",
                self.account_balance_baseline_raw,
                True,
            ),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{field_name} must be a {qualifier} integer")
        if self.max_hold_time is not None and (
            isinstance(self.max_hold_time, bool)
            or not isinstance(self.max_hold_time, int)
            or self.max_hold_time <= 0
        ):
            raise ValueError("max_hold_time must be a positive integer")
        if self.position_id is not None and (
            not isinstance(self.position_id, str) or not self.position_id
        ):
            raise ValueError("position_id must be a non-empty string")
        if (
            isinstance(self.exit_attempt_sequence, bool)
            or not isinstance(self.exit_attempt_sequence, int)
            or self.exit_attempt_sequence < 0
        ):
            raise ValueError("exit_attempt_sequence must be a non-negative integer")
        if not isinstance(self.is_active, bool):
            raise ValueError("is_active must be a boolean")
        pending_values = (
            self.pending_exit_intent_id,
            self.pending_exit_signature,
            self.pending_exit_reason,
            self.pending_exit_price,
        )
        if any(value is not None for value in pending_values):
            if self.pending_exit_intent_id is None or self.pending_exit_reason is None:
                raise ValueError("pending exit requires an intent id and exit reason")
        if self.is_active:
            if any(
                value is not None
                for value in (self.exit_reason, self.exit_price, self.exit_time)
            ):
                raise ValueError("active position cannot contain closed exit fields")
        else:
            if (
                self.exit_reason is None
                or self.exit_price is None
                or self.exit_time is None
            ):
                raise ValueError(
                    "closed position requires exit reason, price, and time"
                )
            if any(value is not None for value in pending_values):
                raise ValueError("closed position cannot contain a pending exit")
        self.entry_time = self._as_utc(self.entry_time)
        if self.exit_time is not None:
            self.exit_time = self._as_utc(self.exit_time)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Return an aware UTC timestamp, accepting legacy naive values."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

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
        *,
        quantity_raw: int | None = None,
        quote_amount_raw: int | None = None,
        account_balance_baseline_raw: int | None = None,
        position_id: str | None = None,
    ) -> "Position":
        """Create a validated position from a confirmed buy receipt."""
        if take_profit_percentage is not None and (
            isinstance(take_profit_percentage, bool)
            or not isinstance(take_profit_percentage, int | float)
            or not isfinite(take_profit_percentage)
            or take_profit_percentage <= 0
        ):
            raise ValueError("take_profit_percentage must be finite and positive")
        if stop_loss_percentage is not None and (
            isinstance(stop_loss_percentage, bool)
            or not isinstance(stop_loss_percentage, int | float)
            or not isfinite(stop_loss_percentage)
            or not 0 < stop_loss_percentage < 1
        ):
            raise ValueError("stop_loss_percentage must be finite and between 0 and 1")

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
            entry_time=datetime.now(UTC),
            position_id=position_id,
            quantity_raw=quantity_raw,
            quote_amount_raw=quote_amount_raw,
            account_balance_baseline_raw=account_balance_baseline_raw,
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
        if (
            isinstance(current_price, bool)
            or not isinstance(current_price, int | float)
            or not isfinite(current_price)
            or current_price <= 0
        ):
            raise ValueError("current_price must be finite and positive")
        if not self.is_active:
            return False, None

        # Check take profit
        if self.take_profit_price and current_price >= self.take_profit_price:
            return True, ExitReason.TAKE_PROFIT

        # Check stop loss
        if self.stop_loss_price and current_price <= self.stop_loss_price:
            return True, ExitReason.STOP_LOSS

        if self.max_hold_time:
            elapsed_time = (datetime.now(UTC) - self.entry_time).total_seconds()
            if elapsed_time >= self.max_hold_time:
                return True, ExitReason.MAX_HOLD_TIME

        return False, None

    def close_position(self, exit_price: float, exit_reason: ExitReason) -> None:
        """Close the position with exit details.

        Args:
            exit_price: Price at which position was exited
            exit_reason: Reason for exit
        """
        if (
            isinstance(exit_price, bool)
            or not isinstance(exit_price, int | float)
            or not isfinite(exit_price)
            or exit_price <= 0
        ):
            raise ValueError("exit_price must be finite and positive")
        if not isinstance(exit_reason, ExitReason):
            raise ValueError("exit_reason must be an ExitReason")
        self.is_active = False
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.now(UTC)
        self.pending_exit_signature = None
        self.pending_exit_reason = None
        self.pending_exit_intent_id = None
        self.pending_exit_price = None

    def mark_exit_intent(
        self,
        intent_id: str,
        exit_reason: ExitReason,
        trigger_price: float,
    ) -> None:
        """Persist a sell intent and its observed trigger price before submission."""
        if not intent_id:
            raise ValueError("exit intent id must not be empty")
        if (
            isinstance(trigger_price, bool)
            or not isinstance(trigger_price, int | float)
            or not isfinite(trigger_price)
            or trigger_price <= 0
        ):
            raise ValueError("exit trigger price must be finite and positive")
        self.pending_exit_intent_id = intent_id
        self.pending_exit_reason = exit_reason
        self.pending_exit_price = trigger_price

    def mark_exit_pending(self, signature: str, exit_reason: ExitReason) -> None:
        """Record an unresolved sell without closing or abandoning the position."""
        if not signature:
            raise ValueError("pending exit signature must not be empty")
        if self.pending_exit_intent_id is None:
            raise ValueError("pending exit signature requires a persisted intent")
        self.pending_exit_signature = signature
        self.pending_exit_reason = exit_reason

    def clear_pending_exit(self) -> None:
        """Allow a new sell only after the prior signature is terminal."""
        self.pending_exit_signature = None
        self.pending_exit_reason = None
        self.pending_exit_intent_id = None
        self.pending_exit_price = None

    def next_exit_attempt(self) -> int:
        """Allocate a durable monotonic sell-attempt sequence number."""
        self.exit_attempt_sequence += 1
        return self.exit_attempt_sequence

    def to_dict(self) -> dict[str, Any]:
        """Serialize the position for the durable local journal."""
        return {
            "mint": str(self.mint),
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "position_id": self.position_id,
            "quantity_raw": self.quantity_raw,
            "quote_amount_raw": self.quote_amount_raw,
            "account_balance_baseline_raw": self.account_balance_baseline_raw,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "max_hold_time": self.max_hold_time,
            "is_active": self.is_active,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "pending_exit_signature": self.pending_exit_signature,
            "pending_exit_reason": (
                self.pending_exit_reason.value if self.pending_exit_reason else None
            ),
            "pending_exit_intent_id": self.pending_exit_intent_id,
            "pending_exit_price": self.pending_exit_price,
            "exit_attempt_sequence": self.exit_attempt_sequence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Position":
        """Restore a position from a journal record without coercing corruption."""
        if not isinstance(raw, dict):
            raise ValueError("position journal record must be an object")
        is_active = raw.get("is_active", True)
        if not isinstance(is_active, bool):
            raise ValueError("position journal is_active must be a boolean")
        exit_attempt_sequence = raw.get("exit_attempt_sequence", 0)
        if isinstance(exit_attempt_sequence, bool) or not isinstance(
            exit_attempt_sequence, int
        ):
            raise ValueError(
                "position journal exit_attempt_sequence must be an integer"
            )
        entry_time = raw.get("entry_time")
        exit_time = raw.get("exit_time")
        if not isinstance(entry_time, str):
            raise ValueError("position journal entry_time must be a string")
        if exit_time is not None and not isinstance(exit_time, str):
            raise ValueError("position journal exit_time must be a string")
        exit_reason = raw.get("exit_reason")
        pending_reason = raw.get("pending_exit_reason")
        return cls(
            mint=Pubkey.from_string(raw["mint"]),
            symbol=raw["symbol"],
            entry_price=raw["entry_price"],
            quantity=raw["quantity"],
            entry_time=datetime.fromisoformat(entry_time),
            position_id=raw.get("position_id"),
            quantity_raw=raw.get("quantity_raw"),
            quote_amount_raw=raw.get("quote_amount_raw"),
            account_balance_baseline_raw=raw.get("account_balance_baseline_raw"),
            take_profit_price=raw.get("take_profit_price"),
            stop_loss_price=raw.get("stop_loss_price"),
            max_hold_time=raw.get("max_hold_time"),
            is_active=is_active,
            exit_reason=ExitReason(exit_reason) if exit_reason else None,
            exit_price=raw.get("exit_price"),
            exit_time=datetime.fromisoformat(exit_time) if exit_time else None,
            pending_exit_signature=raw.get("pending_exit_signature"),
            pending_exit_reason=(
                ExitReason(pending_reason) if pending_reason else None
            ),
            pending_exit_intent_id=raw.get("pending_exit_intent_id"),
            pending_exit_price=raw.get("pending_exit_price"),
            exit_attempt_sequence=exit_attempt_sequence,
        )

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

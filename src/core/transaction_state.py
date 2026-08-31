"""Typed transaction confirmation states.

A missing RPC answer is not evidence that a transaction reverted.  These types
keep that distinction explicit at recovery and trading boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TransactionStatus(StrEnum):
    """Terminal status observed for a submitted transaction."""

    SUCCESS = "success"
    REVERTED = "reverted"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransactionOutcome:
    """The best evidence available for a submitted transaction."""

    status: TransactionStatus
    signature: str
    error: str | None = None
    slot: int | None = None

    @property
    def succeeded(self) -> bool:
        """Whether on-chain execution was positively verified as successful."""
        return self.status is TransactionStatus.SUCCESS

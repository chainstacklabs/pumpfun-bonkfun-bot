"""Explicit execution policy for fund-moving operations.

The policy is deliberately independent of the Solana client so it can be
constructed and tested before any network or signer objects are created.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite
from typing import Any

from solders.pubkey import Pubkey


class ExecutionMode(StrEnum):
    """Permitted runtime modes."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class ExecutionPolicyError(RuntimeError):
    """Base error for execution-policy violations."""


class ExecutionBlocked(ExecutionPolicyError):
    """Raised when a signing or submission operation is not authorized."""


class TradeLimitExceeded(ExecutionPolicyError):
    """Raised when a trade exceeds a configured risk budget."""


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Immutable policy governing signing, submission, and destructive cleanup."""

    mode: ExecutionMode = ExecutionMode.DRY_RUN
    live_authorized: bool = False
    expected_wallet: str | None = None
    max_trade_quote_raw: int | None = None
    max_total_fee_lamports: int | None = None
    allow_skip_preflight: bool = False
    allow_force_burn: bool = False

    def __post_init__(self) -> None:
        """Validate policy invariants at construction time."""
        if not isinstance(self.mode, ExecutionMode):
            try:
                object.__setattr__(self, "mode", ExecutionMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise ValueError("mode must be a supported execution mode") from exc
        if not isinstance(self.live_authorized, bool):
            raise ValueError("live_authorized must be a boolean")
        if not isinstance(self.allow_skip_preflight, bool):
            raise ValueError("allow_skip_preflight must be a boolean")
        if not isinstance(self.allow_force_burn, bool):
            raise ValueError("allow_force_burn must be a boolean")
        if self.mode is not ExecutionMode.LIVE and self.live_authorized:
            raise ValueError("live_authorized requires execution mode 'live'")
        for name, value in (
            ("max_trade_quote_raw", self.max_trade_quote_raw),
            ("max_total_fee_lamports", self.max_total_fee_lamports),
        ):
            if value is not None:
                _validate_nonnegative_int(value, name)
        if self.mode is ExecutionMode.LIVE:
            if self.max_trade_quote_raw is None:
                raise ValueError("max_trade_quote_raw is required for live execution")
            if self.max_total_fee_lamports is None:
                raise ValueError(
                    "max_total_fee_lamports is required for live execution"
                )
            if self.expected_wallet is None:
                raise ValueError("expected_wallet is required for live execution")
        if self.expected_wallet is not None:
            if not isinstance(self.expected_wallet, str):
                raise ValueError("expected_wallet must be a string")
            if not self.expected_wallet.strip():
                raise ValueError("expected_wallet must not be empty")
            try:
                canonical_wallet = str(Pubkey.from_string(self.expected_wallet))
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_wallet must be a valid public key") from exc
            object.__setattr__(self, "expected_wallet", canonical_wallet)

    @property
    def can_submit(self) -> bool:
        """Whether this policy permits a transaction submission."""
        return self.mode is ExecutionMode.LIVE and self.live_authorized

    def authorize_live(self) -> ExecutionPolicy:
        """Return a live-authorized copy after explicit runtime acknowledgement."""
        if self.mode is not ExecutionMode.LIVE:
            raise ExecutionBlocked(
                "Live authorization requires execution.mode='live' in configuration"
            )
        return replace(self, live_authorized=True)

    def require_submission(self) -> None:
        """Raise unless the caller is explicitly authorized to submit."""
        if not self.can_submit:
            raise ExecutionBlocked(
                "Transaction submission blocked: explicit runtime authorization "
                "is required for live execution"
            )

    def validate_wallet(self, wallet: Any) -> None:
        """Ensure the signer matches the configured public-key identity."""
        if self.expected_wallet is None:
            return
        try:
            actual = str(Pubkey.from_string(str(wallet)))
        except (TypeError, ValueError) as exc:
            raise ExecutionBlocked("Signer wallet is not a valid public key") from exc
        if actual != self.expected_wallet:
            raise ExecutionBlocked(
                f"Configured wallet does not match signer wallet ({actual})"
            )

    def validate_budgets(self, quote_amount_raw: int, fee_lamports: int) -> None:
        """Enforce per-trade quote and total-fee budgets."""
        try:
            _validate_nonnegative_int(quote_amount_raw, "trade quote amount")
            _validate_nonnegative_int(fee_lamports, "transaction fee")
        except ValueError as exc:
            raise TradeLimitExceeded(str(exc)) from exc
        if (
            self.max_trade_quote_raw is not None
            and quote_amount_raw > self.max_trade_quote_raw
        ):
            raise TradeLimitExceeded(
                f"trade quote amount {quote_amount_raw} exceeds "
                f"limit {self.max_trade_quote_raw}"
            )
        if (
            self.max_total_fee_lamports is not None
            and fee_lamports > self.max_total_fee_lamports
        ):
            raise TradeLimitExceeded(
                f"transaction fee {fee_lamports} exceeds "
                f"limit {self.max_total_fee_lamports}"
            )

    def validate_preflight(self, skip_preflight: bool) -> None:
        """Prevent callers from bypassing simulation without a policy grant."""
        if skip_preflight and not self.allow_skip_preflight:
            raise ExecutionBlocked(
                "skip_preflight is disabled by execution policy; enable it only "
                "after an explicit live-risk review"
            )

    def validate_force_burn(self, requested: bool) -> None:
        """Prevent destructive cleanup unless explicitly permitted."""
        if requested and not self.allow_force_burn:
            raise ExecutionBlocked("force-close burn is disabled by execution policy")

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        live_authorized: bool = False,
    ) -> ExecutionPolicy:
        """Build a policy from a validated configuration mapping."""
        if not isinstance(config, dict):
            raise ValueError("configuration must be a mapping")
        if not isinstance(live_authorized, bool):
            raise ValueError("live_authorized must be a boolean")
        raw = config.get("execution", {})
        if not isinstance(raw, dict):
            raise ValueError("execution must be a mapping")
        try:
            mode = ExecutionMode(raw.get("mode", ExecutionMode.DRY_RUN.value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "execution.mode must be one of: "
                f"{', '.join(item.value for item in ExecutionMode)}"
            ) from exc
        return cls(
            mode=mode,
            live_authorized=live_authorized,
            expected_wallet=raw.get("expected_wallet"),
            max_trade_quote_raw=raw.get("max_trade_quote_raw"),
            max_total_fee_lamports=raw.get("max_total_fee_lamports"),
            allow_skip_preflight=raw.get("allow_skip_preflight", False),
            allow_force_burn=raw.get("allow_force_burn", False),
        )


def _validate_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def validate_finite_number(value: object, field_name: str) -> None:
    """Validate a finite integer/float configuration value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    if not isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")

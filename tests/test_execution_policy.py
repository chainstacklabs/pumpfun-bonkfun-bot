from __future__ import annotations

import pytest
from solders.pubkey import Pubkey

from core.execution_policy import (
    ExecutionBlocked,
    ExecutionMode,
    ExecutionPolicy,
    TradeLimitExceeded,
)

WALLET = Pubkey.from_string("11111111111111111111111111111111")


def test_policy_defaults_to_dry_run_and_blocks_submission() -> None:
    policy = ExecutionPolicy()

    assert policy.mode is ExecutionMode.DRY_RUN
    assert policy.can_submit is False
    with pytest.raises(ExecutionBlocked):
        policy.require_submission()


def test_live_policy_requires_explicit_runtime_authorization() -> None:
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        live_authorized=False,
        expected_wallet=str(WALLET),
        max_trade_quote_raw=100,
        max_total_fee_lamports=10,
    )

    with pytest.raises(ExecutionBlocked):
        policy.require_submission()

    authorized = policy.authorize_live()
    assert authorized.can_submit is True
    authorized.require_submission()


def test_policy_rejects_wallet_mismatch() -> None:
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        live_authorized=True,
        expected_wallet=str(WALLET),
        max_trade_quote_raw=100,
        max_total_fee_lamports=10,
    )

    with pytest.raises(ExecutionBlocked, match="wallet"):
        policy.validate_wallet(
            Pubkey.from_string("SysvarRent111111111111111111111111111111111")
        )


def test_policy_enforces_trade_and_fee_budgets() -> None:
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        live_authorized=True,
        max_trade_quote_raw=100,
        max_total_fee_lamports=10,
        expected_wallet=str(WALLET),
    )

    policy.validate_budgets(100, 10)
    with pytest.raises(TradeLimitExceeded, match="trade"):
        policy.validate_budgets(101, 1)
    with pytest.raises(TradeLimitExceeded, match="fee"):
        policy.validate_budgets(1, 11)


def test_policy_rejects_non_integer_limits_and_boolean_switches() -> None:
    with pytest.raises(ValueError, match="max_trade_quote_raw"):
        ExecutionPolicy(max_trade_quote_raw=1.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="allow_skip_preflight"):
        ExecutionPolicy(allow_skip_preflight="yes")  # type: ignore[arg-type]

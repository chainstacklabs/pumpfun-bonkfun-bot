from __future__ import annotations

import pytest

from monitoring.base_listener import ReconnectLimitError, ReconnectPolicy


def test_reconnect_policy_rejects_invalid_types_and_nonfinite_values() -> None:
    with pytest.raises((TypeError, ValueError)):
        ReconnectPolicy(max_attempts=True)
    with pytest.raises((TypeError, ValueError)):
        ReconnectPolicy(initial_delay=float("nan"))
    with pytest.raises((TypeError, ValueError)):
        ReconnectPolicy(jitter_ratio=float("inf"))


def test_reconnect_policy_caps_attempts_and_delay() -> None:
    policy = ReconnectPolicy(max_attempts=2, initial_delay=1, max_delay=2)
    assert policy.delay(2, jitter=1) == 2
    with pytest.raises(ReconnectLimitError):
        policy.delay(3, jitter=0)

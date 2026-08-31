from __future__ import annotations

import pytest

from utils.logger import _redact


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "mnemonic: 'alpha bravo charlie delta'",
            "mnemonic: [REDACTED]",
        ),
        (
            'seed phrase = "alpha bravo charlie delta" followed',
            "seed phrase = [REDACTED] followed",
        ),
        ("private_key='single-token'", "private_key=[REDACTED]"),
        (
            "mnemonic: 'alpha bravo charlie delta",
            "mnemonic: [REDACTED]",
        ),
        (
            'seed phrase = "alpha bravo charlie delta',
            "seed phrase = [REDACTED]",
        ),
    ],
)
def test_sensitive_labels_redact_quoted_and_single_token_values(
    message: str,
    expected: str,
) -> None:
    assert _redact(message) == expected

from __future__ import annotations

import pytest

from core.client import set_loaded_accounts_data_size_limit


def test_loaded_account_data_size_limit_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        set_loaded_accounts_data_size_limit(0)
    with pytest.raises(ValueError):
        set_loaded_accounts_data_size_limit(16 * 1024 * 1024 + 1)


def test_loaded_account_data_size_limit_encodes_valid_value() -> None:
    instruction = set_loaded_accounts_data_size_limit(16 * 1024 * 1024)
    assert instruction.data == bytes([4]) + (16 * 1024 * 1024).to_bytes(4, "little")

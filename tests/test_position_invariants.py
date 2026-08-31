from __future__ import annotations

import pytest
from solders.pubkey import Pubkey

from trading.position import Position


@pytest.mark.parametrize("symbol", ["", "   ", 7, None])
def test_position_rejects_invalid_symbol(symbol: object) -> None:
    with pytest.raises(ValueError, match="symbol"):
        Position.create_from_buy_result(
            mint=Pubkey.new_unique(),
            symbol=symbol,  # type: ignore[arg-type]
            entry_price=1.0,
            quantity=1.0,
        )


@pytest.mark.parametrize("max_hold_time", [1.5, True, "1"])
def test_position_rejects_non_integer_max_hold_time(max_hold_time: object) -> None:
    with pytest.raises(ValueError, match="max_hold_time"):
        Position.create_from_buy_result(
            mint=Pubkey.new_unique(),
            symbol="TOKEN",
            entry_price=1.0,
            quantity=1.0,
            max_hold_time=max_hold_time,  # type: ignore[arg-type]
        )

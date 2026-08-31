from __future__ import annotations

import pytest

from core.quote_engine import (
    QuoteError,
    constant_product_exact_in,
    minimum_output_with_slippage,
)


def test_constant_product_quote_accounts_for_fee_and_price_impact() -> None:
    quote = constant_product_exact_in(
        amount_in=100,
        reserve_in=10_000,
        reserve_out=20_000,
        fee_bps=100,
    )

    assert quote.amount_in_raw == 100
    assert quote.fee_raw == 1
    assert quote.amount_out_raw == 196
    assert quote.reserve_in_raw == 10_000
    assert quote.reserve_out_raw == 20_000


def test_transfer_fee_rounds_up_for_a_one_unit_fraction() -> None:
    quote = constant_product_exact_in(
        amount_in=1,
        reserve_in=1,
        reserve_out=4,
        output_transfer_fee_bps=1,
        output_transfer_fee_maximum_raw=10,
    )

    assert quote.amount_out_before_transfer_fee_raw == 2
    assert quote.output_transfer_fee_raw == 1
    assert quote.amount_out_raw == 1


def test_transfer_fee_is_capped_and_input_fee_is_exposed() -> None:
    quote = constant_product_exact_in(
        amount_in=2,
        reserve_in=100,
        reserve_out=1_000,
        input_transfer_fee_bps=1,
        input_transfer_fee_maximum_raw=10,
        output_transfer_fee_bps=9_999,
        output_transfer_fee_maximum_raw=1,
    )

    assert quote.input_transfer_fee_raw == 1
    assert quote.effective_amount_in_raw == 1
    assert quote.amount_out_before_transfer_fee_raw == 9
    assert quote.output_transfer_fee_raw == 1
    assert quote.amount_out_raw == 8


def test_trading_fee_is_charged_after_input_transfer_fee() -> None:
    quote = constant_product_exact_in(
        amount_in=100,
        reserve_in=1_000,
        reserve_out=1_000,
        fee_bps=100,
        input_transfer_fee_bps=100,
        input_transfer_fee_maximum_raw=100,
    )

    assert quote.input_transfer_fee_raw == 1
    assert quote.protocol_fee_raw == 0
    assert quote.effective_amount_in_raw == 99
    assert quote.amount_out_raw == 90


def test_exact_precomputed_output_transfer_fee_is_supported() -> None:
    quote = constant_product_exact_in(
        amount_in=1,
        reserve_in=1,
        reserve_out=6,
        output_transfer_fee_raw=2,
    )

    assert quote.amount_out_before_transfer_fee_raw == 3
    assert quote.output_transfer_fee_raw == 2
    assert quote.amount_out_raw == 1


def test_minimum_output_rounds_down_and_rejects_invalid_slippage() -> None:
    assert minimum_output_with_slippage(196, 500) == 186

    with pytest.raises(QuoteError, match="slippage"):
        minimum_output_with_slippage(196, 10_000)


def test_quote_rejects_empty_reserves_and_zero_output() -> None:
    with pytest.raises(QuoteError, match="reserve"):
        constant_product_exact_in(1, 0, 100, fee_bps=0)

    with pytest.raises(QuoteError, match="output"):
        constant_product_exact_in(1, 100, 1, fee_bps=9_999)

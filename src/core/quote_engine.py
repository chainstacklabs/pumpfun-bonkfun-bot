"""Deterministic integer-unit quote calculations.

This module contains venue-neutral constant-product math. Protocol adapters are
responsible for supplying the correct reserves and fee schedule; callers must
not use a marginal spot price as an executable minimum-output bound.
"""

from __future__ import annotations

from dataclasses import dataclass

BASIS_POINTS = 10_000


class QuoteError(ValueError):
    """Raised when a quote cannot be calculated safely."""


@dataclass(frozen=True, slots=True)
class ConstantProductQuote:
    """Exact-in quote expressed entirely in raw integer units."""

    amount_in_raw: int
    effective_amount_in_raw: int
    fee_raw: int
    protocol_fee_raw: int
    input_transfer_fee_raw: int
    amount_out_before_transfer_fee_raw: int
    output_transfer_fee_raw: int
    amount_out_raw: int
    reserve_in_raw: int
    reserve_out_raw: int


def _validate_nonnegative(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuoteError(f"{field_name} must be a non-negative integer")


def calculate_transfer_fee_raw(
    amount_raw: int, basis_points: int, maximum_fee_raw: int
) -> int:
    """Calculate an SPL Token-2022 transfer fee using ceiling and maximum cap."""
    for value, field_name in (
        (amount_raw, "amount_raw"),
        (basis_points, "basis_points"),
        (maximum_fee_raw, "maximum_fee_raw"),
    ):
        _validate_nonnegative(value, field_name)
    if basis_points > BASIS_POINTS:
        raise QuoteError("basis_points must not exceed 10000")
    if amount_raw == 0 or basis_points == 0 or maximum_fee_raw == 0:
        return 0
    uncapped_fee = (amount_raw * basis_points + BASIS_POINTS - 1) // BASIS_POINTS
    return min(uncapped_fee, maximum_fee_raw)


def constant_product_exact_in(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    *,
    fee_bps: int = 0,
    input_transfer_fee_raw: int = 0,
    input_transfer_fee_bps: int = 0,
    input_transfer_fee_maximum_raw: int | None = None,
    output_transfer_fee_raw: int | None = None,
    output_transfer_fee_bps: int = 0,
    output_transfer_fee_maximum_raw: int | None = None,
) -> ConstantProductQuote:
    """Calculate an exact-in constant-product quote with integer rounding.

    Fee and transfer-fee parameters are explicit so a Token-2022-aware adapter
    can provide the mint's actual schedule rather than relying on decimals or a
    floating-point spot price.
    """
    for value, field_name in (
        (amount_in, "amount_in"),
        (reserve_in, "reserve_in"),
        (reserve_out, "reserve_out"),
        (fee_bps, "fee_bps"),
        (input_transfer_fee_raw, "input_transfer_fee_raw"),
        (input_transfer_fee_bps, "input_transfer_fee_bps"),
        (output_transfer_fee_bps, "output_transfer_fee_bps"),
    ):
        _validate_nonnegative(value, field_name)
    for value, field_name in (
        (input_transfer_fee_maximum_raw, "input_transfer_fee_maximum_raw"),
        (output_transfer_fee_raw, "output_transfer_fee_raw"),
        (output_transfer_fee_maximum_raw, "output_transfer_fee_maximum_raw"),
    ):
        if value is not None:
            _validate_nonnegative(value, field_name)

    if amount_in == 0:
        raise QuoteError("amount_in must be positive")
    if reserve_in == 0 or reserve_out == 0:
        raise QuoteError("reserves must be positive")
    if fee_bps >= BASIS_POINTS:
        raise QuoteError("fee_bps must be less than 10000")
    if input_transfer_fee_bps > BASIS_POINTS:
        raise QuoteError("input_transfer_fee_bps must not exceed 10000")
    if output_transfer_fee_bps > BASIS_POINTS:
        raise QuoteError("output_transfer_fee_bps must not exceed 10000")

    has_input_schedule = (
        input_transfer_fee_bps != 0 or input_transfer_fee_maximum_raw is not None
    )
    if has_input_schedule:
        if input_transfer_fee_raw != 0:
            raise QuoteError(
                "input transfer fee cannot use both exact and scheduled values"
            )
        input_fee_raw = calculate_transfer_fee_raw(
            amount_in,
            input_transfer_fee_bps,
            (
                input_transfer_fee_maximum_raw
                if input_transfer_fee_maximum_raw is not None
                else amount_in
            ),
        )
    else:
        input_fee_raw = input_transfer_fee_raw
    if input_fee_raw >= amount_in:
        raise QuoteError("input transfer fee consumes the entire input")

    amount_in_after_transfer_fee = amount_in - input_fee_raw
    protocol_fee_raw = amount_in_after_transfer_fee * fee_bps // BASIS_POINTS
    effective_amount_in_raw = amount_in_after_transfer_fee - protocol_fee_raw
    if effective_amount_in_raw <= 0:
        raise QuoteError("effective input is zero")

    amount_out_before_transfer_fee = (effective_amount_in_raw * reserve_out) // (
        reserve_in + effective_amount_in_raw
    )
    has_output_schedule = (
        output_transfer_fee_bps != 0 or output_transfer_fee_maximum_raw is not None
    )
    if output_transfer_fee_raw is not None and has_output_schedule:
        raise QuoteError(
            "output transfer fee cannot use both exact and scheduled values"
        )
    if output_transfer_fee_raw is not None:
        output_fee_raw = output_transfer_fee_raw
    else:
        output_fee_raw = calculate_transfer_fee_raw(
            amount_out_before_transfer_fee,
            output_transfer_fee_bps,
            (
                output_transfer_fee_maximum_raw
                if output_transfer_fee_maximum_raw is not None
                else amount_out_before_transfer_fee
            ),
        )
    if output_fee_raw > amount_out_before_transfer_fee:
        raise QuoteError("output transfer fee exceeds the gross output")
    amount_out_raw = amount_out_before_transfer_fee - output_fee_raw
    if amount_out_raw <= 0:
        raise QuoteError("quote output is zero")

    return ConstantProductQuote(
        amount_in_raw=amount_in,
        effective_amount_in_raw=effective_amount_in_raw,
        fee_raw=protocol_fee_raw + input_fee_raw,
        protocol_fee_raw=protocol_fee_raw,
        input_transfer_fee_raw=input_fee_raw,
        amount_out_before_transfer_fee_raw=amount_out_before_transfer_fee,
        output_transfer_fee_raw=output_fee_raw,
        amount_out_raw=amount_out_raw,
        reserve_in_raw=reserve_in,
        reserve_out_raw=reserve_out,
    )


def minimum_output_with_slippage(amount_out_raw: int, slippage_bps: int) -> int:
    """Round a quote down to a safe minimum output bound."""
    _validate_nonnegative(amount_out_raw, "amount_out_raw")
    _validate_nonnegative(slippage_bps, "slippage_bps")
    if amount_out_raw == 0:
        raise QuoteError("amount_out_raw must be positive")
    if slippage_bps >= BASIS_POINTS:
        raise QuoteError("slippage must be less than 10000 basis points")
    minimum = amount_out_raw * (BASIS_POINTS - slippage_bps) // BASIS_POINTS
    if minimum <= 0:
        raise QuoteError("slippage leaves zero minimum output")
    return minimum

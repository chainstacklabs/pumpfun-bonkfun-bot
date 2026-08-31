from __future__ import annotations

from collections.abc import Callable

import pytest
from solders.pubkey import Pubkey

from core.pubkeys import (
    DEFAULT_PUBKEY,
    TOKEN_PROGRAM,
    USDC_MINT,
    WSOL_MINT,
    UnknownQuoteAsset,
    get_quote_asset,
    quote_decimals,
    quote_token_program,
    quote_units_per_token,
)


def test_known_quote_assets_have_explicit_metadata() -> None:
    sol = get_quote_asset(WSOL_MINT)
    usdc = get_quote_asset(USDC_MINT)

    assert sol.decimals == 9
    assert sol.token_program == TOKEN_PROGRAM
    assert usdc.decimals == 6
    assert usdc.token_program == TOKEN_PROGRAM


def test_unknown_quote_asset_fails_closed() -> None:
    unknown = Pubkey.new_unique()

    with pytest.raises(UnknownQuoteAsset):
        get_quote_asset(unknown)


@pytest.mark.parametrize(
    "helper",
    (quote_decimals, quote_units_per_token, quote_token_program),
)
def test_public_quote_helpers_reject_unknown_metadata(
    helper: Callable[[Pubkey], object],
) -> None:
    unknown = Pubkey.new_unique()

    with pytest.raises(UnknownQuoteAsset, match="metadata is unavailable"):
        helper(unknown)


@pytest.mark.parametrize("native_mint", (None, DEFAULT_PUBKEY, WSOL_MINT))
def test_public_quote_helpers_preserve_native_sol_metadata(
    native_mint: Pubkey | None,
) -> None:
    assert quote_decimals(native_mint) == 9
    assert quote_units_per_token(native_mint) == 1_000_000_000
    assert quote_token_program(native_mint) == TOKEN_PROGRAM


def test_public_quote_helpers_preserve_known_usdc_metadata() -> None:
    assert quote_decimals(USDC_MINT) == 6
    assert quote_units_per_token(USDC_MINT) == 1_000_000
    assert quote_token_program(USDC_MINT) == TOKEN_PROGRAM

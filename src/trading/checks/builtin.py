"""The checks the bot ships with.

The quote, transfer-fee, name and creator checks are the `filters` settings;
the intake builds them from those keys and they never show up in
`filters.checks`. The rest are listed there by name and are off unless listed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.pubkeys import normalize_quote_mint
from trading.checks.base import CheckContext, PreBuyCheck

if TYPE_CHECKING:
    from solders.pubkey import Pubkey

    from interfaces.core import TokenInfo


class QuoteAllowedCheck(PreBuyCheck):
    """`filters.allowed_quote_mints`; None allows every quote mint."""

    name = "allowed_quote_mints"

    def __init__(self, allowed: set[Pubkey] | None) -> None:
        self.allowed = allowed

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        quote_mint = normalize_quote_mint(coin.quote_mint)
        if self.allowed is not None and quote_mint not in self.allowed:
            return f"quote mint {quote_mint} not in allowed_quote_mints"
        return None


class QuoteAmountCheck(PreBuyCheck):
    """A buy amount is configured for the coin's quote mint."""

    name = "quote_amounts"

    def __init__(self, amounts: dict[Pubkey, float]) -> None:
        self.amounts = amounts

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        quote_mint = normalize_quote_mint(coin.quote_mint)
        if quote_mint not in self.amounts:
            return f"no buy amount configured for quote mint {quote_mint}"
        return None


class TransferFeeCheck(PreBuyCheck):
    """`filters.max_transfer_fee_bps`; None takes every coin."""

    name = "max_transfer_fee_bps"

    def __init__(self, max_bps: int | None) -> None:
        self.max_bps = max_bps

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        if self.max_bps is not None and coin.transfer_fee_bps > self.max_bps:
            return (
                f"transfer fee {coin.transfer_fee_bps} bps exceeds "
                f"max_transfer_fee_bps {self.max_bps}"
            )
        return None


class NameMatchCheck(PreBuyCheck):
    """`filters.match_string`, in the coin's name or symbol, ignoring case."""

    name = "match_string"

    def __init__(self, match: str | None) -> None:
        self.match = match

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        if self.match and not (
            self.match.lower() in coin.name.lower()
            or self.match.lower() in coin.symbol.lower()
        ):
            return f"does not match filter '{self.match}'"
        return None


class CreatorCheck(PreBuyCheck):
    """`filters.bro_address`: only coins this address created."""

    name = "bro_address"

    def __init__(self, creator: str | None) -> None:
        self.creator = creator

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        if self.creator and str(coin.creator) != self.creator:
            return f"not created by {self.creator}"
        return None


class MayhemCheck(PreBuyCheck):
    """Skip pump.fun coins in mayhem mode; the listeners already read the flag."""

    name = "mayhem"

    async def check(self, coin: TokenInfo, _ctx: CheckContext) -> str | None:
        if coin.is_mayhem_mode:
            return "the coin is in mayhem mode"
        return None


#: Checks `filters.checks` can list by name.
BUILTINS: dict[str, type[PreBuyCheck]] = {MayhemCheck.name: MayhemCheck}

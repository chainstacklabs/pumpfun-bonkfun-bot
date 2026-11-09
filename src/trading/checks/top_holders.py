"""A check that reads the chain: skip coins whose top wallets hold too much."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from trading.checks.base import CheckContext, CheckUnavailable, PreBuyCheck

if TYPE_CHECKING:
    from interfaces.core import TokenInfo

# A token account's owner sits after its 32-byte mint, in both token programs.
_OWNER = slice(32, 64)


def _rpc(method: str, mint: Pubkey) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": [str(mint), {"commitment": "confirmed"}],
    }


def _value(response: dict | None) -> object:
    if not response or "result" not in response:
        raise CheckUnavailable
    return response["result"]["value"]


class TopHoldersCheck(PreBuyCheck):
    """Skip a coin when its top wallets hold more than `max_share` of the supply.

    Accounts owned by a program address (the bonding curve, an AMM pool's or a
    LaunchLab vault) are left out, and a wallet's accounts count together, so
    the share counts wallets only. A fresh
    launch is held by its creator and its curve alone, so this suits migration
    entries and buys that wait after creation; run it best effort, since a
    coin the shreds listener reports before its create has run has no
    accounts to read yet.

    Args:
        top: How many of the largest wallets to add up. The node returns the 20
            largest token accounts only, so a wallet whose balance is spread
            over smaller ones counts what is among those 20
        max_share: Fraction of the supply above which the coin is skipped, in (0, 1]
    """

    name = "top_holders"
    reads_chain = True
    required_by_default = False

    def __init__(self, max_share: float, top: int = 10) -> None:
        if isinstance(max_share, bool) or not 0 < max_share <= 1:
            raise ValueError(f"max_share must be in (0, 1], not {max_share!r}")  # noqa: TRY003
        if isinstance(top, bool) or not isinstance(top, int) or top < 1:
            raise ValueError(f"top must be a whole number above 0, not {top!r}")  # noqa: TRY003
        self.max_share = max_share
        self.top = top

    async def check(self, coin: TokenInfo, ctx: CheckContext) -> str | None:
        largest, supply = await asyncio.gather(
            ctx.client.post_rpc(_rpc("getTokenLargestAccounts", coin.mint)),
            ctx.client.post_rpc(_rpc("getTokenSupply", coin.mint)),
        )
        holders = _value(largest)
        total = int(_value(supply)["amount"])
        if not holders or total <= 0:
            raise CheckUnavailable
        addresses = [Pubkey.from_string(h["address"]) for h in holders]
        accounts = await ctx.client.get_multiple_accounts(
            addresses, commitment="confirmed"
        )
        by_wallet: dict[Pubkey, int] = {}
        for holder, account in zip(holders, accounts, strict=True):
            if account is None or len(account.data) < _OWNER.stop:
                # Gone or unreadable: it may be a large holder, so say nothing.
                raise CheckUnavailable
            owner = Pubkey.from_bytes(bytes(account.data[_OWNER]))
            if owner.is_on_curve():
                by_wallet[owner] = by_wallet.get(owner, 0) + int(holder["amount"])
        held = sum(sorted(by_wallet.values(), reverse=True)[: self.top])
        share = held / total
        if share > self.max_share:
            return (
                f"top {self.top} wallets hold {share:.2%} of the supply, above "
                f"max_share {self.max_share:.2%}"
            )
        return None

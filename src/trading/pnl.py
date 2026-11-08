"""Realised profit and loss: what a position's buy cost and its sales paid, from the chain.

Amounts come from each transaction's effect on the wallet (`TradeResult.deltas`).
For a SOL quote the network fees are part of the PnL; for any other quote they
are SOL and are reported beside it. Rent parked in the wallet's own token
accounts is never a trading loss: it comes back when the account closes, which
cleanup does later in its own transaction, so neither that close's fee nor its
refund is counted here.

Rent for accounts the wallet does not own (a fresh creator vault, a program's
quote account) is a cost of the transaction that paid it. Charged on the buy,
it is spread over every token bought; charged on a sale, it is that sale's. So
the parts of a position sold in parts can carry it unevenly, while the whole
position's total is exact.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT, is_sol_paired, quote_units_per_token
from trading.base import TradeResult

if TYPE_CHECKING:
    import asyncio

_UNREAD_BUY = "the buy's transaction was not read, so its cost is its price"
_NO_DECIMALS = "the quote mint's decimals are unknown"


def format_price(price: float | None) -> str:
    """A price to six significant digits, so a fresh coin's tiny prices still differ."""
    return "unknown" if price is None else f"{price:.6g}"


def lamports_to_sol(lamports: int) -> float:
    return lamports / quote_units_per_token(WSOL_MINT)


def _units(quote_mint: Pubkey) -> int | None:
    try:
        return quote_units_per_token(quote_mint)
    except ValueError:
        return None


@dataclass(frozen=True)
class CostBasis:
    """What each whole token of a position cost, in whole quote units."""

    #: Protocol fees included, and for a SOL quote the buy's network fee too.
    per_token: float
    #: False: this is the buy's price, for the reason in `why`.
    from_chain: bool
    #: For a non-SOL quote: the buy's network fee and other SOL it cost, per
    #: whole token. 0 for a SOL quote.
    sol_per_token: float = 0.0
    why: str = _UNREAD_BUY


def cost_basis(buy: TradeResult, quote_mint: Pubkey) -> CostBasis | None:
    """A position's cost per token, from its buy; None when the buy has no price."""
    deltas = buy.deltas
    unit = _units(quote_mint)
    read = (
        deltas is not None
        and deltas.tokens_raw > 0
        and deltas.quote_raw is not None
        and deltas.quote_raw < 0
    )
    if read and unit is not None:
        tokens = deltas.tokens_raw / 10**TOKEN_DECIMALS
        if is_sol_paired(quote_mint):
            spent = -deltas.quote_raw + deltas.network_fee_lamports
            return CostBasis(spent / unit / tokens, from_chain=True)
        sol_cost = deltas.network_fee_lamports - deltas.other_lamports
        return CostBasis(
            -deltas.quote_raw / unit / tokens,
            from_chain=True,
            sol_per_token=lamports_to_sol(sol_cost) / tokens,
        )
    if buy.price is None:
        return None
    why = f"{_NO_DECIMALS}, so its cost is its price" if read else _UNREAD_BUY
    return CostBasis(buy.price, from_chain=False, why=why)


@dataclass(frozen=True)
class SalePnl:
    """The realised PnL of one sale, against the cost of the tokens it sold."""

    #: Whole quote units; for a SOL quote network fees are taken off.
    pnl: float
    pct: float
    #: Whole tokens sold.
    sold: float
    sale_from_chain: bool
    cost_from_chain: bool
    sol_quote: bool
    #: For a non-SOL quote: the SOL these tokens' buy and sale cost besides
    #: rent. 0 for a SOL quote, where `pnl` has it.
    sol_costs: float = 0.0
    #: Why it is an estimate; None when it is not.
    why: str | None = None

    @property
    def from_chain(self) -> bool:
        return self.sale_from_chain and self.cost_from_chain


def sale_pnl(
    cost: CostBasis, sale: TradeResult, quote_mint: Pubkey, reference_price: float
) -> SalePnl:
    """The PnL of a landed sale.

    Args:
        cost: The position's cost basis
        sale: The landed sale; without `deltas` it is priced at `price`
        reference_price: The seller's reference price, for a sale that carries
            no price at all
    """
    sol_quote = is_sol_paired(quote_mint)
    deltas = sale.deltas
    unit = _units(quote_mint)
    read = deltas is not None and deltas.tokens_raw < 0 and deltas.quote_raw is not None
    why = None
    priced = read and unit is not None
    if priced:
        sold = -deltas.tokens_raw / 10**TOKEN_DECIMALS
        fee = deltas.network_fee_lamports
        received = (deltas.quote_raw - (fee if sol_quote else 0)) / unit
        sol_costs = 0.0 if sol_quote else lamports_to_sol(fee - deltas.other_lamports)
        spent = cost.per_token * sold
        pnl = received - spent
    else:
        if sale.amount is not None:
            sold = sale.amount
        else:
            sold = (sale.amount_raw or 0) / 10**TOKEN_DECIMALS
        price = sale.price if sale.price is not None else reference_price
        sol_costs = 0.0
        spent = cost.per_token * sold
        pnl = (price - cost.per_token) * sold
        why = (
            _NO_DECIMALS if read else "the sell's transaction could not be read"
        ) + ", so it is priced at the seller's reference price"
    if why is None and not cost.from_chain:
        why = cost.why
    return SalePnl(
        pnl=pnl,
        pct=pnl / spent * 100 if spent else 0.0,
        sold=sold,
        sale_from_chain=priced,
        cost_from_chain=cost.from_chain,
        sol_quote=sol_quote,
        sol_costs=sol_costs + cost.sol_per_token * sold,
        why=why,
    )


def pnl_text(label: str, pnl: SalePnl, quote_label: str) -> str:
    """One log line for a sale's PnL, saying whether it is an estimate."""
    text = f"{label}: {pnl.pct:.2f}% ({pnl.pnl:.6f} {quote_label}"
    if not pnl.from_chain:
        return f"{text}, an estimate: {pnl.why})"
    if pnl.sol_quote:
        text += " from the chain, network fees included"
    else:
        text += (
            f" from the chain; network fees and other SOL costs "
            f"{pnl.sol_costs:.6f} SOL on top"
        )
    return text + "; account rent left out)"


@dataclass
class Realised:
    """A position's sales so far, for the whole position's PnL."""

    pnl: float = 0.0
    sales: int = 0
    from_chain: bool = True
    sol_quote: bool = True
    #: For a non-SOL quote: the SOL its buy and sales cost besides rent.
    sol_costs: float = 0.0
    #: The tasks reporting its sales (`PositionManager._after_sale`), so the
    #: last one totals them in order.
    reports: list["asyncio.Task"] = field(default_factory=list)
    #: Sales that could not be priced, so the total leaves them out.
    missing: int = 0

    def add(self, sale: SalePnl) -> None:
        self.pnl += sale.pnl
        self.sales += 1
        self.from_chain = self.from_chain and sale.from_chain
        self.sol_quote = sale.sol_quote
        self.sol_costs += sale.sol_costs

    def suffix(self, quote_label: str) -> str:
        """What to add to the last sale's line; empty for a single sale."""
        if self.sales + self.missing <= 1:
            return ""
        estimate = "" if self.from_chain else ", an estimate"
        if self.missing:
            estimate += (
                f", incomplete: {self.missing} of {self.sales + self.missing} "
                f"sales could not be priced"
            )
        sol = (
            ""
            if self.sol_quote
            else f", network fees and other SOL costs {self.sol_costs:.6f} SOL on top"
        )
        return (
            f"; the whole position over {self.sales + self.missing} sales: "
            f"{self.pnl:.6f} {quote_label}{sol}{estimate}"
        )

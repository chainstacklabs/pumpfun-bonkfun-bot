"""Realised profit and loss: what a position's buy cost and its sales paid, from the chain.

Amounts come from each transaction's effect on the wallet (`TradeResult.deltas`).
For a SOL quote the network fees are part of the PnL; for any other quote they
are SOL and are reported beside it. Rent parked in the wallet's own token
accounts is never a trading loss: it comes back when the account closes, which
cleanup does later in its own transaction, so neither that close's fee nor its
refund is counted here.
"""

from dataclasses import dataclass

from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT, is_sol_paired, quote_units_per_token
from trading.base import TradeResult


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
    #: False: the buy's transaction was not read and this is its price.
    from_chain: bool
    #: For a non-SOL quote: the buy's network fee and other SOL it cost, per
    #: whole token. 0 for a SOL quote.
    sol_per_token: float = 0.0


def cost_basis(buy: TradeResult, quote_mint: Pubkey) -> CostBasis | None:
    """A position's cost per token, from its buy; None when the buy has no price."""
    deltas = buy.deltas
    unit = _units(quote_mint)
    if (
        deltas is not None
        and unit is not None
        and deltas.tokens_raw > 0
        and deltas.quote_raw is not None
        and deltas.quote_raw < 0
    ):
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
    return CostBasis(buy.price, from_chain=False)


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
        reference_price: The price the sale was floored at, for a sale that
            carries no price at all
    """
    sol_quote = is_sol_paired(quote_mint)
    deltas = sale.deltas
    unit = _units(quote_mint)
    if (
        deltas is not None
        and unit is not None
        and deltas.tokens_raw < 0
        and deltas.quote_raw is not None
    ):
        sold = -deltas.tokens_raw / 10**TOKEN_DECIMALS
        fee = deltas.network_fee_lamports
        received = (deltas.quote_raw - (fee if sol_quote else 0)) / unit
        sol_costs = 0.0 if sol_quote else lamports_to_sol(fee - deltas.other_lamports)
        from_chain = True
        spent = cost.per_token * sold
        pnl = received - spent
    else:
        if sale.amount is not None:
            sold = sale.amount
        else:
            sold = (sale.amount_raw or 0) / 10**TOKEN_DECIMALS
        price = sale.price if sale.price is not None else reference_price
        sol_costs, from_chain = 0.0, False
        spent = cost.per_token * sold
        pnl = (price - cost.per_token) * sold
    return SalePnl(
        pnl=pnl,
        pct=pnl / spent * 100 if spent else 0.0,
        sold=sold,
        sale_from_chain=from_chain,
        cost_from_chain=cost.from_chain,
        sol_quote=sol_quote,
        sol_costs=sol_costs + cost.sol_per_token * sold,
    )


def pnl_text(label: str, pnl: SalePnl, quote_label: str) -> str:
    """One log line for a sale's PnL, saying whether it is an estimate."""
    text = f"{label}: {pnl.pct:.2f}% ({pnl.pnl:.6f} {quote_label}"
    if pnl.from_chain:
        if pnl.sol_quote:
            text += " from the chain, network fees included"
        else:
            text += (
                f" from the chain; network fees and other SOL costs "
                f"{pnl.sol_costs:.6f} SOL on top"
            )
        return text + "; account rent left out)"
    if not pnl.sale_from_chain:
        return text + (
            ", an estimate: the sell's transaction could not be read, so it is "
            "priced at the price the sell was floored at)"
        )
    return text + (
        ", an estimate: the buy's transaction was not read, so its cost is its price)"
    )


@dataclass
class Realised:
    """A position's sales so far, for the whole position's PnL."""

    pnl: float = 0.0
    sales: int = 0
    from_chain: bool = True

    def add(self, sale: SalePnl) -> None:
        self.pnl += sale.pnl
        self.sales += 1
        self.from_chain = self.from_chain and sale.from_chain

    def suffix(self, quote_label: str) -> str:
        """What to add to the last sale's line; empty for a single sale."""
        if self.sales <= 1:
            return ""
        estimate = "" if self.from_chain else ", an estimate"
        return (
            f"; the whole position over {self.sales} sales: "
            f"{self.pnl:.6f} {quote_label}{estimate}"
        )

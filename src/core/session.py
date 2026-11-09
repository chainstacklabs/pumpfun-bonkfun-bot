"""What one bot's session did, for the summary printed when it ends.

Each process runs its bots one after another, so one tally per process is
enough: `start` begins a bot's, and every trade, exit and account close of that
bot adds to it until the next `start`.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT, quote_units_per_token
from interfaces.core import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GivenUp:
    """A position the bot stopped monitoring while it still held the coin."""

    mint: Pubkey
    symbol: str
    reason: str
    #: Raw tokens held; None when unknown.
    amount_raw: int | None
    #: Lamports its token account holds; None until read.
    rent_lamports: int | None = None


@dataclass
class Session:
    """One bot's trades, exits and closes since `start`."""

    #: The bot's name; None when no session was started, and nothing is reported.
    bot: str | None = None
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    buys: int = 0
    sales: int = 0
    #: Exits that sold a position, by what triggered them.
    exits: Counter = field(default_factory=Counter)
    #: Realised PnL of the priced sales, by quote asset label.
    pnl: dict[str, float] = field(default_factory=dict)
    #: Sales that could not be priced, so `pnl` leaves them out.
    unpriced: int = 0
    #: Sales priced from estimates rather than their transaction.
    estimated: int = 0
    network_fees_sol: float = 0.0
    rent_deposited_sol: float = 0.0
    #: Rent the trades themselves returned, and the account closes after them.
    rent_refunded_sol: float = 0.0
    rent_closed_sol: float = 0.0
    closes: int = 0
    given_up: list[GivenUp] = field(default_factory=list)

    def add_trade(  # noqa: PLR0913
        self,
        action: str,
        *,
        network_fee_sol: float | None,
        rent_deposit_sol: float | None,
        rent_refund_sol: float | None,
        quote_label: str = "SOL",
        pnl: float | None = None,
        pnl_from_chain: bool = True,
    ) -> None:
        """Count a buy or sell row of `trades.log`; amounts None when unread."""
        if action == "buy":
            self.buys += 1
        else:
            self.sales += 1
            if pnl is None:
                self.unpriced += 1
            else:
                self.pnl[quote_label] = self.pnl.get(quote_label, 0.0) + pnl
                self.estimated += not pnl_from_chain
        self.network_fees_sol += network_fee_sol or 0.0
        self.rent_deposited_sol += rent_deposit_sol or 0.0
        self.rent_refunded_sol += rent_refund_sol or 0.0

    def add_exit(self, trigger: str) -> None:
        self.exits[trigger] += 1

    def add_given_up(
        self, token_info: TokenInfo, amount_raw: int | None, reason: str
    ) -> None:
        """Count a position the bot gave up on; a coin given up twice counts once."""
        self.given_up = [g for g in self.given_up if g.mint != token_info.mint]
        self.given_up.append(
            GivenUp(token_info.mint, token_info.symbol, reason, amount_raw)
        )

    def drop_given_up(self, mint: Pubkey) -> None:
        """Forget a coin given up on that is no longer held, e.g. once burned."""
        self.given_up = [g for g in self.given_up if g.mint != mint]

    def set_given_up_rent(self, mint: Pubkey, lamports: int | None) -> None:
        """The lamports a given-up coin's token account holds."""
        for given_up in self.given_up:
            if given_up.mint == mint:
                given_up.rent_lamports = lamports

    def add_close(self, lamports: int | None) -> None:
        """Count a token account closed; `lamports` it held, None when unknown."""
        self.closes += 1
        self.rent_closed_sol += (lamports or 0) / quote_units_per_token(WSOL_MINT)

    def as_dict(self, *, held: int, pending: int) -> dict:
        """The summary as one JSON object.

        Args:
            held: Positions still held, kept in the positions file
            pending: Buys sent but never confirmed, kept there too
        """
        return {
            "bot": self.bot,
            "started": self.started.isoformat(),
            "ended": datetime.now(UTC).isoformat(),
            "buys": self.buys,
            "sales": self.sales,
            "exits": dict(self.exits),
            "pnl": self.pnl,
            "unpriced_sales": self.unpriced,
            "estimated_sales": self.estimated,
            "network_fees_sol": self.network_fees_sol,
            "rent_deposited_sol": self.rent_deposited_sol,
            "rent_refunded_sol": self.rent_refunded_sol + self.rent_closed_sol,
            "accounts_closed": self.closes,
            "positions_held": held,
            "buys_pending": pending,
            "given_up": [
                {
                    "mint": str(g.mint),
                    "symbol": g.symbol,
                    "reason": g.reason,
                    "amount_raw": g.amount_raw,
                    "rent_lamports": g.rent_lamports,
                }
                for g in self.given_up
            ],
        }

    def lines(self, *, held: int, pending: int) -> list[str]:
        """The summary as log lines."""
        exits = ", ".join(f"{n} {why}" for why, n in sorted(self.exits.items()))
        lines = [
            f"Session summary: {self.buys} buy(s), {self.sales} sale(s)"
            + (f"; exits: {exits}" if exits else "")
        ]
        if self.pnl or self.unpriced:
            totals = ", ".join(f"{v:+.6f} {k}" for k, v in sorted(self.pnl.items()))
            notes = []
            if self.estimated:
                notes.append(f"{self.estimated} estimated")
            if self.unpriced:
                notes.append(f"{self.unpriced} sale(s) unpriced and left out")
            lines.append(
                f"Session PnL: {totals or 'none priced'}"
                + (f" ({'; '.join(notes)})" if notes else "")
                + "; SOL-quoted PnL already counts its network fees"
            )
        lines.append(
            f"Session costs: network fees {self.network_fees_sol:.6f} SOL; rent "
            f"{self.rent_deposited_sol:.6f} SOL into token accounts, "
            f"{self.rent_refunded_sol + self.rent_closed_sol:.6f} SOL back "
            f"({self.closes} account(s) closed)"
        )
        if held or pending:
            lines.append(
                f"Session left {held} position(s) held and {pending} buy(s) "
                f"pending in the positions file, for the next start"
            )
        sol_unit = quote_units_per_token(WSOL_MINT)
        for g in self.given_up:
            amount = (
                "an unknown amount of"
                if g.amount_raw is None
                else f"{g.amount_raw / 10**TOKEN_DECIMALS:g}"
            )
            rent = (
                "unknown"
                if g.rent_lamports is None
                else f"{g.rent_lamports / sol_unit:.6f}"
            )
            lines.append(
                f"Gave up on {g.symbol} ({g.mint}): {g.reason}; {amount} tokens "
                f"held, {rent} SOL rent locked"
            )
        return lines


_current = Session()


def start(bot: str) -> None:
    """Begin a new tally for `bot`, replacing the last one."""
    global _current  # noqa: PLW0603 - one tally per process, by design
    _current = Session(bot=bot)


def current() -> Session:
    return _current


def report(*, held: int, pending: int) -> None:
    """Log the session's summary and append it to `trades/sessions.log`.

    Does nothing when no session was started. A failed write is logged.
    """
    session = _current
    if session.bot is None:
        return
    for line in session.lines(held=held, pending=pending):
        logger.info(line)
    try:
        path = Path("trades")
        path.mkdir(exist_ok=True)
        with (path / "sessions.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(session.as_dict(held=held, pending=pending)) + "\n")
    except OSError:
        logger.exception("Could not write the session summary")

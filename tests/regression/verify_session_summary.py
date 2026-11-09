"""Verify the session summary printed when a bot stops.

A session ended with no summary: how it went had to be added up from the
per-trade lines by hand, and `trades/trades.log` rows did not say which bot,
of several run from one folder, made each trade.

Offline machine checks, no network and no funds moved; trades are logged in a
temporary folder:

  A. Each `trades.log` row names its bot; outside a bot's session, null.
  B. The session counts buys and sales, totals the priced sales' PnL by quote
     asset, counts estimated and unpriced sales apart, and adds up network
     fees and rent into and back from token accounts.
  C. A position's exit counts what triggered it: take profit or stop loss
     under `tp_sl`, `time_based` when the wait ran out. A stopped exit counts
     none.
  D. Each token account cleanup closes counts, with the rent it returned.
  E. `report` logs the summary, with the positions held and buys pending
     left for the next start, and appends it as one JSON line to
     `trades/sessions.log`; without a session it does nothing. The summary
     lines show on the terminal without `--verbose`.
  F. At shutdown the trader reports the positions its file still holds and
     the buys still pending there; a position sold and waiting for its close,
     or a pending buy cleared as never landed, counts as neither.

Usage:
    uv run tests/regression/verify_session_summary.py
"""

import asyncio
import json
import logging
import os
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import base58  # noqa: E402
from _fakes import _report  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from cleanup.manager import AccountCleanupManager  # noqa: E402
from core import session  # noqa: E402
from core.pubkeys import TOKEN_PROGRAM, USDC_MINT, WSOL_MINT, quote_symbol  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading import trade_log  # noqa: E402
from trading.base import PendingBuy, TradeResult  # noqa: E402
from trading.pnl import SalePnl  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402
from utils import console  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
SOL = 1_000_000_000
RENT = 2_039_280


def _coin(quote: Pubkey = WSOL_MINT) -> TokenInfo:
    return TokenInfo(
        name="C",
        symbol="C",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        quote_mint=quote,
        token_program_id=TOKEN_PROGRAM,
    )


def _rows() -> list[dict]:
    path = Path("trades") / "trades.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _sale(pnl: float, *, from_chain: bool = True, sol: bool = True) -> SalePnl:
    return SalePnl(
        pnl=pnl,
        pct=0.0,
        sold=1.0,
        sale_from_chain=from_chain,
        cost_from_chain=True,
        sol_quote=sol,
    )


def check_a_rows_name_their_bot() -> bool:
    print("A: each trades.log row names its bot.")
    problems = []
    session._current = session.Session()  # noqa: SLF001 - no session started
    trade_log.log_trade("buy", _coin(), 1.0, 1.0, "s")
    session.start("bot-one")
    trade_log.log_trade("buy", _coin(), 1.0, 1.0, "s")
    bots = [row.get("bot", "missing") for row in _rows()]
    if bots != [None, "bot-one"]:
        problems.append(f"rows name {bots}, want [None, 'bot-one']")
    return _report(problems)


def check_b_totals() -> bool:
    print("B: the session totals its trades.")
    problems = []
    session.start("bot-two")
    buy = TradeDeltas(1_000_000, -SOL // 100, 10_000, rent_deposited_lamports=RENT)
    sell = TradeDeltas(-1_000_000, SOL // 50, 5_000, rent_refunded_lamports=0)
    trade_log.log_trade("buy", _coin(), 1.0, 1.0, "b", deltas=buy)
    trade_log.log_trade("sell", _coin(), 1.0, 1.0, "s", deltas=sell, pnl=_sale(0.01))
    trade_log.log_trade(
        "sell", _coin(), 1.0, 1.0, "s", pnl=_sale(-0.004, from_chain=False)
    )
    trade_log.log_trade(
        "sell", _coin(USDC_MINT), 1.0, 1.0, "s", pnl=_sale(1.5, sol=False)
    )
    trade_log.log_trade("sell", _coin(), 1.0, 1.0, "s")
    s = session.current()
    got = (s.buys, s.sales, s.unpriced, s.estimated)
    if got != (1, 4, 1, 1):
        problems.append(f"buys, sales, unpriced, estimated = {got}")
    usdc = quote_symbol(USDC_MINT)
    if abs(s.pnl.get("SOL", 0) - 0.006) > 1e-12 or s.pnl.get(usdc) != 1.5:  # noqa: PLR2004
        problems.append(f"PnL by quote {s.pnl}")
    if abs(s.network_fees_sol - 15_000 / SOL) > 1e-15:  # noqa: PLR2004
        problems.append(f"network fees {s.network_fees_sol}")
    if abs(s.rent_deposited_sol - RENT / SOL) > 1e-15:  # noqa: PLR2004
        problems.append(f"rent deposited {s.rent_deposited_sol}")
    return _report(problems)


class _Seller:
    async def execute(self, _token_info: TokenInfo, **kw: object) -> TradeResult:
        return TradeResult(
            success=True,
            tx_signature="sold",
            amount=kw["token_amount"],
            amount_raw=kw.get("token_amount_raw"),
            price=kw["token_price"],
        )


def _manager(**settings: object) -> PositionManager:
    async def price(_token: TokenInfo) -> float:
        return settings.pop("_price", 3.0)

    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "buy_amount": 0.01,
        "buy_slippage": 0.1,
        "sell_slippage": 0.1,
        "take_profit_percentage": 0.5,
        "stop_loss_percentage": 0.4,
        "max_hold_time": 60,
        "price_check_interval": 0.01,
        "wait_time_after_buy": 0,
    }
    manager = PositionManager(
        TraderSettings(**(fields | settings)),
        seller=_Seller(),
        solana_client=SimpleNamespace(),
        wallet=SimpleNamespace(pubkey=Pubkey.new_unique()),
        priority_fee_manager=None,
        platform_implementations=SimpleNamespace(
            curve_market=SimpleNamespace(get_price=price), graduated_market=None
        ),
    )

    async def read_sale(
        _token: TokenInfo, _sell: TradeResult, price: float, *_a: object
    ) -> TradeResult:
        return TradeResult(success=True, tx_signature="sold", amount=1.0, price=price)

    manager._read_sale = read_sale  # noqa: SLF001
    return manager


async def _exits() -> list[str]:
    problems = []
    buy = TradeResult(success=True, price=1.0, amount=1.0, amount_raw=1_000_000)
    session.start("bot-three")
    tp = _manager(exit_strategy="tp_sl")
    await asyncio.wait_for(tp.manage(_coin(), buy), 5)
    timed = _manager(exit_strategy="time_based")
    await asyncio.wait_for(timed.manage(_coin(), buy), 5)
    await tp.wait_all()
    await timed.wait_all()
    exits = dict(session.current().exits)
    if exits != {"take_profit": 1, "time_based": 1}:
        problems.append(f"exits {exits}")
    return problems


def check_c_exits() -> bool:
    print("C: each exit counts what triggered it.")
    return _report(asyncio.run(_exits()))


class _CloseClient:
    """One emptied token account, closed on the first send."""

    def __init__(self, mint: Pubkey, owner: Pubkey) -> None:
        data = bytearray(165)
        data[0:32], data[32:64] = bytes(mint), bytes(owner)
        struct.pack_into("<Q", data, 64, 0)
        data[108] = 1
        self.account = SimpleNamespace(
            data=bytes(data), owner=TOKEN_PROGRAM, lamports=RENT
        )

    async def get_account_info(self, *_a: object, **_k: object) -> SimpleNamespace:
        return self.account

    async def build_and_send_transaction(self, *_a: object, **_k: object) -> str:
        return "closed"

    async def confirm_transaction(self, *_a: object, **_k: object) -> bool:
        return True


def check_d_closes() -> bool:
    print("D: each account close counts with the rent it returned.")
    session.start("bot-four")
    mint, owner = Pubkey.new_unique(), Pubkey.new_unique()
    address = Pubkey.new_unique()
    wallet = SimpleNamespace(
        pubkey=owner, keypair=None, get_associated_token_address=lambda *_a: address
    )
    manager = AccountCleanupManager(
        _CloseClient(mint, owner), wallet, priority_fee_manager=None
    )
    asyncio.run(manager.cleanup_ata(mint, TOKEN_PROGRAM, after_slot=1))
    s = session.current()
    problems = []
    if (s.closes, s.rent_closed_sol) != (1, RENT / SOL):
        problems.append(f"closes {s.closes}, rent back {s.rent_closed_sol}")
    return _report(problems)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def check_e_report() -> bool:
    print("E: report logs the summary and appends it to sessions.log.")
    problems = []
    lines = _Lines()
    logging.getLogger().addHandler(lines)
    try:
        session._current = session.Session()  # noqa: SLF001
        session.report(held=0, pending=0)
        if Path("trades/sessions.log").exists() or lines.records:
            problems.append("reported without a session")
        session.start("bot-five")
        trade_log.log_trade("sell", _coin(), 1.0, 1.0, "s", pnl=_sale(0.02))
        session.current().add_exit("stop_loss")
        session.report(held=2, pending=1)
    finally:
        logging.getLogger().removeHandler(lines)
    texts = [r.getMessage() for r in lines.records]
    if not any(t.startswith("Session summary") and "1 stop_loss" in t for t in texts):
        problems.append(f"no summary line naming the exit: {texts}")
    if not any("2 position(s) held and 1 buy(s) pending" in t for t in texts):
        problems.append("no line naming what is left for the next start")
    hidden = [
        r.getMessage()
        for r in lines.records
        if console.classify(r.name, r.getMessage())[1]
    ]
    if hidden:
        problems.append(f"summary lines hidden without --verbose: {hidden}")
    rows = [json.loads(x) for x in Path("trades/sessions.log").read_text().splitlines()]
    if (
        len(rows) != 1
        or rows[0]["bot"] != "bot-five"
        or rows[0]["pnl"] != {"SOL": 0.02}
    ):
        problems.append(f"sessions.log holds {rows}")
    elif (rows[0]["positions_held"], rows[0]["buys_pending"]) != (2, 1):
        problems.append(f"sessions.log counts {rows[0]}")
    return _report(problems)


def check_f_trader_reports_what_is_left() -> bool:
    print("F: the trader reports what its positions file still holds.")
    session.start("bot-six")
    store = PositionStore(Path("f.json"))
    buy = TradeResult(success=True, price=1.0, amount=1.0, tx_signature="b")
    held, pending, sold, cleared = _coin(), _coin(), _coin(), _coin()
    now = datetime(2026, 1, 1)
    store.add(held, buy, now)
    store.add(pending, buy, now, pending=PendingBuy())
    store.add(sold, buy, now)
    store.mark_sold(sold.mint)
    store.add(cleared, buy, now, pending=PendingBuy())
    store.mark_sold(cleared.mint)
    trader = object.__new__(UniversalTrader)
    trader.positions = SimpleNamespace(store=store)
    trader._report_session()  # noqa: SLF001
    row = json.loads(Path("trades/sessions.log").read_text().splitlines()[-1])
    problems = []
    if (row["bot"], row["positions_held"], row["buys_pending"]) != ("bot-six", 1, 1):
        problems.append(f"reported {row}")
    return _report(problems)


def main() -> int:
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            results = [
                check_a_rows_name_their_bot(),
                check_b_totals(),
                check_c_exits(),
                check_d_closes(),
                check_e_report(),
                check_f_trader_reports_what_is_left(),
            ]
        finally:
            os.chdir(cwd)
    for ok in results:
        print("  PASS" if ok else "  FAIL")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

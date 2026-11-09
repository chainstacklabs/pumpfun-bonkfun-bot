"""Verify profit and loss come from what each trade's transaction did to the wallet.

`trades.log` sell rows carried the price the sell was floored at, which for a
`time_based` exit's first attempt is the buy price, and no row said how much
quote changed hands. The `Final PnL` line was the price move at the exit check
times the amount, leaving out fees and the move between trigger and fill, so a
session's logged PnL did not add up to what the wallet lost. Take-profit and
stop-loss targets printed at 8 decimals, so a fresh coin's two targets read the
same.

Fixture: `recorded_trades_from_gettransaction.json`, landed mainnet buys and
sells read with `getTransaction`: pump.fun curve (SOL and USDC quotes, a buy
that also paid a fresh creator vault's rent), PumpSwap, Raydium CPMM and a
LaunchLab curve, SPL Token and Token-2022 coins, a transfer-fee coin bought and
another sold for a Token-2022 quote, and one wallet's buy sold in two parts,
the second closing the coin's account in the same transaction.

Offline machine checks, no network and no funds moved. The real buyer, seller,
position manager, trader and positions file run against a client that serves
the recorded transactions; only the venue's instructions are stubbed, since
the read is the same for every venue and the transactions are not:

  A. Every recorded buy reports the coin the wallet received (net of a
     transfer fee), the quote it spent with protocol fees, the network fee and
     the rent put into its token accounts apart, and the venue's fill as the
     price exits measure from; the trader's `trades.log` buy row carries them.
  B. Every recorded sale, sold through the seller and manager, is logged at
     what the wallet received per token, never the seller's reference price,
     with the same amounts read.
  C. A `time_based` and a `tp_sl` round trip log a sell row with the chain's
     price and quote and the sale's PnL, and a `Final PnL` equal to quote
     received minus quote spent minus both network fees.
  D. A buy sold in two parts logs each part's PnL against its share of the
     cost, and the last line the whole position's, equal to the wallet's
     change over the three transactions.
  E. Rent stays out of PnL: a buy's account rent and a sale's refund are their
     own fields, and a USDC position's PnL is in USDC with the SOL it cost, a
     program account's rent included, reported beside it.
  F. A sale or buy whose transaction can't be read logs a `Final PnL` saying it
     is an estimate, and a sell row that says it is not from the chain.
  G. Take-profit and stop-loss targets, and the price lines, print enough
     digits to tell a fresh coin's prices apart.
  H. A position kept in the positions file keeps its buy's amounts, so a
     restart counts PnL from the same cost.
  I. A sale whose confirmation came late, found landed on the re-check, is read
     back and logged from the chain like any other, in both strategies.
  J. A landed sale is settled first: when its transaction is read the
     position is out of the positions file and holds no slot, and `stop`
     waits for the read and the row.
  K. A report that fails (the trade log raising) leaves the sale settled, is
     logged, and the account is still closed after it.
  L. In a sale made in two parts, a part that cannot be priced is still
     waited for in order, and the whole position's total says it is incomplete.

Usage:
    uv run tests/regression/verify_pnl_from_chain.py
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _no_fee  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402

from core import pubkeys  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from trading import platform_aware, position_manager, trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

HERE = Path(__file__).resolve().parent
TRADES = json.loads((HERE / "recorded_trades_from_gettransaction.json").read_text())
BY_SIG = {t["signature"]: t for t in TRADES}

SOL = 10**9
TOKEN = 10**6
USDC = pubkeys.USDC_MINT

# Per transaction, read off its balances by hand: coin, quote (protocol fees
# in), network fee, rent into and back from the wallet's token accounts, other
# lamports (non-SOL quote), and what the venue took in (buys).
KNOWN = {
    "yw6xTRd2": (107_300_000_000, -3_688_045, 41_000, 1_513_840, 0, 0, 3_000_301),
    "4VQj9Wm7": (-107_300_000_000, 2_970_499, 29_000, 0, 0, 0, None),
    "4G4wGX2X": (97_288_110_802, -2_979_992, 41_000, 1_513_840, 0, 0, 2_943_201),
    "3TpwEzRD": (-97_288_110_802, 2_776_752, 29_000, 0, 0, 0, None),
    "63J4x5G1": (226_534_397_991, -506_359, 41_000, 1_513_840, 0, -1_488_440, 500_106),
    "4j2icCNo": (-226_534_397_991, 155_658, 29_000, 0, 0, 0, None),
    "9tXUvaqu": (185_115_496_467, -3_000_000, 55_000, 1_513_840, 0, 0, 2_963_555),
    "2K7wBTXi": (-185_115_496_467, 2_249_772, 45_000, 0, 0, 0, None),
    "4xRYhMBC": (7_389_672_425, -3_000_000, 35_000, 1_488_440, 0, 0, 3_000_000),
    "3VCmbpfH": (-7_389_672_425, 2_593_216, 35_000, 0, 0, 0, None),
    "mHgezvRg": (103_006_526_837, -3_000_000, 35_000, 1_513_840, 0, 0, 2_970_000),
    "5Ewyv4AB": (-103_006_526_837, 3_871_270, 35_000, 0, 0, 0, None),
    "61qHTKt6": (
        11_876_633_851_989,
        -351_261_795,
        393_363,
        1_513_840,
        0,
        0,
        348_926_261,
    ),
    "3bsnEFMJ": (-10_535_472_671_801, 338_531_720, 717_793, 0, 0, 0, None),
    "2wj1Gwjb": (-1_341_161_180_188, 40_274_634, 400_840, 0, 1_513_840, 0, None),
    "5RJgcMjw": (2_937_887_259_973, -68_496_559, 15_001, 1_574_800, 0, 0, 68_496_559),
    "37hc3CBz": (-5_861_090_092_873, 37_205_441, 25_150, 0, 0, 0, None),
}


def _trade(prefix: str) -> dict:
    return next(t for t in TRADES if t["signature"].startswith(prefix))


def _seed_quote_decimals() -> None:
    """Every quote mint's decimals, from the recorded balances, as startup would."""
    for trade in TRADES:
        mint = Pubkey.from_string(trade["quote_mint"])
        meta = trade["transaction"]["meta"]
        for balance in meta["preTokenBalances"] + meta["postTokenBalances"]:
            if balance["mint"] == trade["quote_mint"]:
                pubkeys._QUOTE_DECIMALS_CACHE[mint] = balance["uiTokenAmount"][  # noqa: SLF001
                    "decimals"
                ]


class _Chain(SolanaClient):
    """A client whose transactions are the recorded ones; sends land in order."""

    def __init__(
        self,
        sends: list[str],
        unreadable: set[str] | None = None,
        late: set[str] | None = None,
    ) -> None:
        self.sends = list(sends)
        self.unreadable = unreadable or set()
        # Sends whose confirmation does not arrive; the re-check finds them.
        self.late = late or set()
        self.on_read = None

    async def _get_transaction_result(
        self, signature: object, *_a: object, **_k: object
    ) -> dict | None:
        if self.on_read is not None:
            await self.on_read(str(signature))
        if str(signature) in self.unreadable:
            return None
        trade = BY_SIG.get(str(signature))
        return trade["transaction"] if trade else None

    async def build_and_send_transaction(self, *_a: object, **_k: object) -> Signature:
        return Signature.from_string(self.sends.pop(0))

    async def confirm_transaction_detailed(  # noqa: ANN202
        self, signature: object, *_a: object, **_k: object
    ):
        if str(signature) in self.late:
            return ConfirmationStatus.UNCONFIRMED
        return ConfirmationStatus.SUCCESS

    async def get_token_account_balance(self, *_a: object, **_k: object) -> None:
        return None


class _Venue:
    """Stub instructions and a curve priced at `price`; the read is the real one."""

    spends_exact_amount_in = False
    program_id = None

    def __init__(self, quote_mint: Pubkey, price: float) -> None:
        self.quote_mint, self.price = quote_mint, price

    async def get_pool_state(self, *_a: object, **_k: object) -> dict:
        return {"price_per_token": self.price, "quote_mint": self.quote_mint}

    async def calculate_price(self, *_a: object) -> float:
        return self.price

    async def build_buy_instruction(self, *_a: object) -> list:
        return []

    async def build_sell_instruction(self, *_a: object) -> list:
        return []

    def get_required_accounts_for_buy(self, *_a: object) -> list:
        return []

    def get_required_accounts_for_sell(self, *_a: object) -> list:
        return []

    def get_buy_compute_unit_limit(self, *_a: object) -> int:
        return 100_000

    def get_sell_compute_unit_limit(self, *_a: object) -> int:
        return 100_000


def _wire(venue: _Venue) -> PlatformImplementations:
    implementations = PlatformImplementations(
        address_provider=venue,
        instruction_builder=venue,
        curve_manager=venue,
        event_parser=None,
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations
    return implementations


def _token(trade: dict) -> TokenInfo:
    venue = Pubkey.from_string(trade.get("venue_account", trade["mint"]))
    pump = trade["venue"] in ("curve", "pumpswap")
    return TokenInfo(
        name="T",
        symbol=trade["signature"][:6],
        uri="",
        mint=Pubkey.from_string(trade["mint"]),
        platform=Platform.PUMP_FUN if pump else Platform.LAUNCHLAB,
        bonding_curve=venue if pump else None,
        pool_state=None if pump else venue,
        quote_vault=None if pump else venue,
        quote_mint=Pubkey.from_string(trade["quote_mint"]),
    )


def _wallet(trade: dict) -> SimpleNamespace:
    return SimpleNamespace(
        pubkey=Pubkey.from_string(trade["wallet"]),
        keypair=None,
        get_associated_token_address=lambda *_a: Pubkey.default(),
    )


def _fill_price(prefix: str, quote_mint: Pubkey) -> float:
    tokens, *_, venue = KNOWN[prefix]
    return venue / pubkeys.quote_units_per_token(quote_mint) / (tokens / TOKEN)


def _buy(trade: dict, chain: _Chain) -> TradeResult:
    """Run the real buyer over a recorded buy."""
    quote = Pubkey.from_string(trade["quote_mint"])
    _wire(_Venue(quote, _fill_price(trade["signature"][:8], quote)))
    buyer = PlatformAwareBuyer(
        chain,
        _wallet(trade),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        amount=0.003,
        slippage=0.3,
        max_retries=1,
        quote_amounts={USDC: 0.5},
    )
    return asyncio.run(buyer.execute(_token(trade)))


def _seller(trade: dict, chain: _Chain) -> PlatformAwareSeller:
    return PlatformAwareSeller(
        chain,
        _wallet(trade),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=0.5,
        max_retries=1,
    )


def _deltas_match(result: TradeResult, prefix: str) -> list[str]:
    tokens, quote, fee, rent_in, rent_back, other, venue = KNOWN[prefix]
    deltas = getattr(result, "deltas", None)
    if deltas is None:
        return [f"{prefix}: no transaction deltas on the result"]
    got = (
        deltas.tokens_raw,
        deltas.quote_raw,
        deltas.network_fee_lamports,
        deltas.rent_deposited_lamports,
        deltas.rent_refunded_lamports,
        deltas.other_lamports,
    )
    want = (tokens, quote, fee, rent_in, rent_back, other)
    bad = [] if got == want else [f"{prefix}: deltas {got} != {want}"]
    if venue is not None and deltas.venue_quote_raw != venue:
        bad.append(f"{prefix}: venue fill {deltas.venue_quote_raw} != {venue}")
    return bad


def _close(a: float, b: float, tolerance: float = 1e-12) -> bool:
    return abs(a - b) <= tolerance


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _in_tmp(run):  # noqa: ANN001, ANN202
    """Run `run()` in a fresh folder, with every bot log line captured."""
    logs = _Logs()
    root = logging.getLogger()
    root.addHandler(logs)
    level = root.level
    root.setLevel(logging.INFO)
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            out = run()
            rows_file = Path("trades") / "trades.log"
            rows = (
                [json.loads(line) for line in rows_file.read_text().splitlines()]
                if rows_file.exists()
                else []
            )
        finally:
            os.chdir(cwd)
            root.removeHandler(logs)
            root.setLevel(level)
    return out, rows, logs.lines


def _log_buy(token: TokenInfo, buy: TradeResult) -> None:
    """The trader's own handling of a landed buy, which writes its row."""
    trader = SimpleNamespace(
        _track_traded=lambda _t: None,
        positions=SimpleNamespace(open=lambda *_a, **_k: None),
    )
    asyncio.run(UniversalTrader._handle_successful_buy(trader, token, buy))  # noqa: SLF001


def _manager(
    strategy: str, seller: object, chain: _Chain, wallet: SimpleNamespace
) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.seller = seller
    manager.solana_client = chain
    manager.wallet = wallet
    manager.priority_fee_manager = None
    manager.exit_strategy = strategy
    manager.marry_mode = False
    manager.take_profit_percentage = 0.15
    manager.stop_loss_percentage = 0.15
    manager.max_hold_time = None
    manager.wait_time_after_buy = 0
    manager.price_check_interval = 0
    manager.max_exit_sell_attempts = 1
    manager.cleanup_mode = "disabled"
    manager.cleanup_with_priority_fee = False
    manager.cleanup_force_close_with_burn = False
    manager._graduated_mints = set()  # noqa: SLF001
    manager._open = set()  # noqa: SLF001
    return manager


async def _exit_and_report(
    manager: PositionManager, token: TokenInfo, buy: TradeResult
) -> None:
    """Run the exit, then wait for its sales' reports, as the trader does."""
    await manager.manage(token, buy)
    await manager.wait_all()


def _round_trip(  # noqa: PLR0913
    buy_prefix: str,
    sell_prefixes: list[str],
    strategy: str,
    *,
    unreadable: set[str] | None = None,
    caps: list[int] | None = None,
    exit_price: float | None = None,
    late: set[str] | None = None,
    slow: set[str] | None = None,
) -> SimpleNamespace:
    """Buy through the real buyer and trader, then exit through the real manager.

    Args:
        caps: Raw tokens each sale may sell at most, standing in for a curve
            that can pay for only part
        exit_price: The price the venue reads at the exit; under the stop loss
            by default
    """
    buy_trade = _trade(buy_prefix)
    sells = [_trade(p)["signature"] for p in sell_prefixes]
    chain = _Chain([buy_trade["signature"], *sells], unreadable, late)

    async def on_read(signature: str) -> None:
        if signature in (slow or ()):
            await asyncio.sleep(0.3)

    chain.on_read = on_read
    token = _token(buy_trade)
    quote = Pubkey.from_string(buy_trade["quote_mint"])

    def run() -> SimpleNamespace:
        buy = _buy(buy_trade, chain)
        _log_buy(token, buy)
        price = exit_price if exit_price is not None else buy.price * 0.5
        implementations = _wire(_Venue(quote, price))
        real = _seller(buy_trade, chain)
        limits = list(caps or [])

        class _CurveCaps:
            async def execute(self, info, token_amount, token_price, token_amount_raw):  # noqa: ANN001, ANN202
                raw = token_amount_raw
                if raw is None or not limits:
                    return await real.execute(info, token_amount, token_price, raw)
                raw = min(raw, limits.pop(0))
                return await real.execute(info, raw / TOKEN, token_price, raw)

        manager = _manager(strategy, _CurveCaps(), chain, _wallet(buy_trade))
        manager.platform_implementations = implementations
        with tempfile.TemporaryDirectory() as tmp:
            manager.store = PositionStore(Path(tmp) / "positions.json")
            manager.store.add(token, buy, datetime.now(UTC).replace(tzinfo=None))
            asyncio.run(asyncio.wait_for(_exit_and_report(manager, token, buy), 30))
        return SimpleNamespace(buy=buy, price=price)

    out, rows, lines = _in_tmp(run)
    out.rows, out.lines = rows, lines
    return out


def _report(name: str, bad: list[str]) -> bool:
    for line in bad:
        print(f"    {name}: {line}")
    return not bad


def _unpaid_not_read() -> list[str]:
    """Only a transaction the wallet paid for is read: the bot's own.

    The curve owns a token account in FLOAT's buy but paid for nothing.
    """
    trade = _trade("yw6xTRd2")
    reader = getattr(_Chain([]), "get_trade_deltas", None)
    if reader is None:
        return ["the client reads no trade from the chain"]
    other = asyncio.run(
        reader(
            trade["signature"],
            Pubkey.from_string(trade["venue_account"]),
            Pubkey.from_string(trade["mint"]),
            pubkeys.WSOL_MINT,
        )
    )
    return [] if other is None else [f"read a transaction it did not pay for: {other}"]


def check_a_buys() -> bool:
    """A: every recorded buy, through the real buyer and the trader's row."""
    bad: list[str] = []
    for trade in TRADES:
        if trade["side"] != "buy":
            continue
        prefix = trade["signature"][:8]
        tokens, quote, fee, rent_in, *_ = KNOWN[prefix]
        quote_mint = Pubkey.from_string(trade["quote_mint"])
        chain = _Chain([trade["signature"]])

        def run(trade: dict = trade, chain: _Chain = chain) -> TradeResult:
            buy = _buy(trade, chain)
            _log_buy(_token(trade), buy)
            return buy

        buy, rows, _ = _in_tmp(run)
        bad += _deltas_match(buy, prefix)
        if buy.amount_raw != tokens:
            bad.append(f"{prefix}: amount_raw {buy.amount_raw} != {tokens}")
        if not _close(buy.price, _fill_price(prefix, quote_mint), 1e-18):
            bad.append(f"{prefix}: price {buy.price} is not the venue's fill")
        row = rows[0] if rows else {}
        unit = pubkeys.quote_units_per_token(quote_mint)
        want = {
            "from_chain": True,
            "amount_raw": tokens,
            "quote_amount": quote / unit,
            "network_fee_sol": fee / SOL,
            "rent_deposit_sol": rent_in / SOL,
        }
        for key, value in want.items():
            if key not in row or not (
                row[key] == value
                or (isinstance(value, float) and _close(row[key] or 0, value))
            ):
                bad.append(f"{prefix}: buy row {key}={row.get(key)!r}, want {value!r}")
    # A transfer fee is withheld outside the wallet: the venue gave more.
    for prefix in ("5RJgcMjw", "37hc3CBz"):
        trade = _trade(prefix)
        meta = trade["transaction"]["meta"]
        pre = {b["accountIndex"]: b for b in meta["preTokenBalances"]}
        moved = [
            int(b["uiTokenAmount"]["amount"])
            - int(
                pre.get(b["accountIndex"], {}).get("uiTokenAmount", {}).get("amount", 0)
            )
            for b in meta["postTokenBalances"]
            if b["mint"] == trade["mint"] and b.get("owner") != trade["wallet"]
        ]
        wallet = KNOWN[prefix][0]
        venue = max((-m for m in moved), key=abs) if wallet > 0 else max(moved)
        # Bought: the pool gave more than arrived. Sold: more left than arrived.
        if not (abs(wallet) < venue if wallet > 0 else abs(wallet) > venue):
            bad.append(f"{prefix}: fixture shows no withheld transfer fee")
    bad += _unpaid_not_read()
    return _report("A", bad)


def _sold_rows(trade: dict) -> list[dict]:
    """Sell a recorded sale's tokens through the real seller and manager."""
    tokens = -KNOWN[trade["signature"][:8]][0]
    quote_mint = Pubkey.from_string(trade["quote_mint"])
    token = _token(trade)
    chain = _Chain([trade["signature"]])
    buy = TradeResult(
        success=True,
        platform=token.platform,
        tx_signature="BUY",
        amount=tokens / TOKEN,
        amount_raw=tokens,
        price=1e-9,
    )

    def run() -> None:
        implementations = _wire(_Venue(quote_mint, 1e-9))
        manager = _manager("time_based", _seller(trade, chain), chain, _wallet(trade))
        manager.platform_implementations = implementations
        asyncio.run(asyncio.wait_for(_exit_and_report(manager, token, buy), 30))

    return [r for r in _in_tmp(run)[1] if r["action"] == "sell"]


def check_b_sales() -> bool:
    """B: every recorded sale, through the real seller and manager."""
    bad: list[str] = []
    for trade in TRADES:
        if trade["side"] != "sell":
            continue
        prefix = trade["signature"][:8]
        tokens, quote, fee, _, rent_back, other, _ = KNOWN[prefix]
        quote_mint = Pubkey.from_string(trade["quote_mint"])
        unit = pubkeys.quote_units_per_token(quote_mint)
        rows = _sold_rows(trade)
        row = rows[0] if rows else {}
        want = {
            "from_chain": True,
            "amount_raw": -tokens,
            "quote_amount": quote / unit,
            "network_fee_sol": fee / SOL,
            "rent_refund_sol": rent_back / SOL,
            "other_sol": other / SOL,
            "price": quote / unit / (-tokens / TOKEN),
        }
        for key, value in want.items():
            got = row.get(key)
            tolerance = abs(value) * 1e-12 + 1e-18 if isinstance(value, float) else 0
            if got != value and not (
                isinstance(value, float) and _close(got or 0, value, tolerance)
            ):
                bad.append(
                    f"{prefix}: sell row {key}={got!r}, the chain says {value!r}"
                )
    return _report("B", bad)


def _final(lines: list[str]) -> str:
    return next((line for line in reversed(lines) if line.startswith("Final PnL")), "")


def check_c_round_trips(*, late: bool = False) -> bool:
    """C: time_based and tp_sl rows and Final PnL equal the chain.

    Args:
        late: The sale's confirmation does not arrive, and its re-check finds
            it landed (check I)
    """
    bad: list[str] = []
    received, fee_out = 2_970_499, 29_000
    spent, fee_in = 3_688_045, 41_000
    chain_pnl = (received - fee_out - spent - fee_in) / SOL
    sale = _trade("4VQj9Wm7")["signature"]
    for strategy in ("time_based", "tp_sl"):
        run = _round_trip(
            "yw6xTRd2", ["4VQj9Wm7"], strategy, late={sale} if late else None
        )
        if late and not any("confirmed late" in line for line in run.lines):
            bad.append(f"{strategy}: the sale was not taken as a late confirmation")
        sells = [r for r in run.rows if r["action"] == "sell"]
        buys = [r for r in run.rows if r["action"] == "buy"]
        price = received / SOL / 107_300
        if not (sells and _close(sells[0]["price"], price, 1e-18)):
            bad.append(
                f"{strategy}: sell row price {sells and sells[0]['price']}, chain {price}"
            )
        if sells and buys and sells[0]["price"] == buys[0]["price"]:
            bad.append(f"{strategy}: sell row carries the buy price")
        if not (sells and _close(sells[0].get("quote_amount") or 0, received / SOL)):
            bad.append(
                f"{strategy}: sell row quote_amount {sells and sells[0].get('quote_amount')}"
            )
        if not (sells and _close(sells[0].get("pnl") or 0, chain_pnl, 1e-15)):
            bad.append(
                f"{strategy}: sell row pnl {sells and sells[0].get('pnl')}, chain {chain_pnl}"
            )
        final = _final(run.lines)
        if f"({chain_pnl:.6f} SOL from the chain" not in final:
            bad.append(f"{strategy}: {final!r}, chain {chain_pnl:.9f}")
    return _report("I" if late else "C", bad)


def check_i_late_sale() -> bool:
    """I: a sale confirmed late is read back like any other."""
    return check_c_round_trips(late=True)


def check_j_settled_then_read() -> bool:
    """J: the store is settled and the slot free before the sale is read; stop waits."""
    buy_trade = _trade("yw6xTRd2")
    sale = _trade("4VQj9Wm7")["signature"]
    chain = _Chain([buy_trade["signature"], sale])
    token = _token(buy_trade)
    quote = Pubkey.from_string(buy_trade["quote_mint"])
    seen: dict = {}
    held: dict = {}

    async def on_read(signature: str) -> None:
        if signature == sale and not seen:
            manager = held["manager"]
            seen["stored"] = str(token.mint) in manager.store.mints()
            seen["open"] = manager.open_count
            await asyncio.sleep(0.3)

    def run() -> None:
        buy = _buy(buy_trade, chain)
        chain.on_read = on_read
        implementations = _wire(_Venue(quote, buy.price * 0.5))
        manager = _manager(
            "time_based", _seller(buy_trade, chain), chain, _wallet(buy_trade)
        )
        manager.platform_implementations = implementations
        held["manager"] = manager
        with tempfile.TemporaryDirectory() as tmp:
            manager.store = PositionStore(Path(tmp) / "positions.json")

            async def go() -> None:
                await manager.open(token, buy)
                await manager.stop()

            asyncio.run(asyncio.wait_for(go(), 30))

    rows = [r for r in _in_tmp(run)[1] if r["action"] == "sell"]
    bad = []
    if seen.get("stored") is not False:
        bad.append(f"the sale was read with the position still stored: {seen}")
    if seen.get("open") != 0:
        bad.append(f"the sale was read holding the position's slot: {seen}")
    if not rows:
        bad.append("stop returned before the sale's row was written")
    return _report("J", bad)


def check_l_parts_totalled_in_order() -> bool:
    """L: a part that cannot be priced is waited for in order and named in the total."""
    sells = ["3bsnEFMJ", "2wj1Gwjb"]
    first, last = (_trade(p)["signature"] for p in sells)
    real = position_manager.sale_pnl
    bad = []
    for broken, slow, case in (
        (first, None, "first part"),
        (last, {first}, "last part"),
    ):

        def pricing(
            cost: object, sale: TradeResult, *args: object, broken: str = broken
        ) -> object:
            if sale.tx_signature == broken:
                raise RuntimeError
            return real(cost, sale, *args)

        position_manager.sale_pnl = pricing
        try:
            run = _round_trip(
                "61qHTKt6", sells, "tp_sl", caps=[10_535_472_671_801], slow=slow
            )
        finally:
            position_manager.sale_pnl = real
        final = _final(run.lines)
        if "incomplete: 1 of 2 sales could not be priced" not in final:
            bad.append(f"{case} unpriced: final line {final!r}")
        if case == "last part":
            part = next(
                (i for i, x in enumerate(run.lines) if x.startswith("PnL on the part")),
                None,
            )
            end = run.lines.index(final) if final in run.lines else -1
            if part is None or part > end:
                bad.append(
                    f"{case} unpriced: the total did not wait for the first part"
                )
    return _report("L", bad)


def check_k_report_failure_keeps_settlement() -> bool:
    """K: a failing report leaves the sale settled and still closes the account."""
    buy_trade = _trade("yw6xTRd2")
    sale = _trade("4VQj9Wm7")["signature"]
    chain = _Chain([buy_trade["signature"], sale])
    token = _token(buy_trade)
    quote = Pubkey.from_string(buy_trade["quote_mint"])
    closes: list = []
    out: dict = {}

    async def close(*args: object, **_k: object) -> None:
        closes.append(args)

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError

    def run() -> None:
        buy = _buy(buy_trade, chain)
        implementations = _wire(_Venue(quote, buy.price * 0.5))
        manager = _manager(
            "time_based", _seller(buy_trade, chain), chain, _wallet(buy_trade)
        )
        manager.platform_implementations = implementations
        manager.cleanup_mode = "after_sell"
        originals = (position_manager.handle_cleanup_after_sell, trade_log.log_trade)
        position_manager.handle_cleanup_after_sell = close
        trade_log.log_trade = broken
        try:
            with tempfile.TemporaryDirectory() as tmp:
                manager.store = PositionStore(Path(tmp) / "positions.json")
                manager.store.add(token, buy, datetime.now(UTC).replace(tzinfo=None))
                asyncio.run(asyncio.wait_for(_exit_and_report(manager, token, buy), 30))
                out["stored"] = manager.store.mints()
        finally:
            position_manager.handle_cleanup_after_sell = originals[0]
            trade_log.log_trade = originals[1]

    lines = _in_tmp(run)[2]
    bad = []
    if out.get("stored") != set():
        bad.append(f"the sale is not settled: {out}")
    if len(closes) != 1:
        bad.append(f"{len(closes)} account close(s) after the failed report")
    if not any(line.startswith("Could not report the sale") for line in lines):
        bad.append("the failed report was not logged")
    return _report("K", bad)


def check_d_partial_sale() -> bool:
    """D: a buy sold in two parts, each part's PnL and the whole position's."""
    run = _round_trip(
        "61qHTKt6", ["3bsnEFMJ", "2wj1Gwjb"], "tp_sl", caps=[10_535_472_671_801]
    )
    bad: list[str] = []
    bought, spent, buy_fee = 11_876_633_851_989, 351_261_795, 393_363
    per_token = (spent + buy_fee) / SOL / (bought / TOKEN)
    parts = [
        (10_535_472_671_801, 338_531_720, 717_793),
        (1_341_161_180_188, 40_274_634, 400_840),
    ]
    want = [(got - fee) / SOL - per_token * sold / TOKEN for sold, got, fee in parts]
    sells = [r for r in run.rows if r["action"] == "sell"]
    if len(sells) != len(parts) or not all(
        _close(row.get("pnl") or 0, value, 1e-12)
        for row, value in zip(sells, want, strict=False)
    ):
        bad.append(f"sale rows pnl {[r.get('pnl') for r in sells]}, want {want}")
    part = next(
        (line for line in run.lines if line.startswith("PnL on the part sold")), ""
    )
    if f"({want[0]:.6f} SOL from the chain" not in part:
        bad.append(f"part line {part!r}, want {want[0]:.6f}")
    # Over the three transactions the wallet's SOL changed by exactly this; the
    # account's rent went in on the buy and came back on the last sale.
    wallet_change = 26_032_563 / SOL
    final = _final(run.lines)
    if f"({want[1]:.6f} SOL from the chain" not in final:
        bad.append(f"final line {final!r}, want {want[1]:.6f}")
    if f"the whole position over 2 sales: {wallet_change:.6f} SOL" not in final:
        bad.append(f"final line {final!r} lacks the whole position {wallet_change:.6f}")
    return _report("D", bad)


def check_e_rent_apart() -> bool:
    """E: rent in its own fields, a USDC position's SOL costs beside its PnL."""
    bad: list[str] = []
    run = _round_trip(
        "61qHTKt6", ["3bsnEFMJ", "2wj1Gwjb"], "tp_sl", caps=[10_535_472_671_801]
    )
    last = [r for r in run.rows if r["action"] == "sell"][-1:]
    if not (last and _close(last[0].get("rent_refund_sol") or 0, 1_513_840 / SOL)):
        bad.append(
            f"last sale rent_refund_sol {last and last[0].get('rent_refund_sol')}"
        )
    if last and not _close(last[0].get("quote_amount") or 0, 40_274_634 / SOL):
        bad.append(
            f"last sale quote_amount {last[0].get('quote_amount')} holds the refund"
        )
    run = _round_trip("63J4x5G1", ["4j2icCNo"], "time_based")
    pnl = (155_658 - 506_359) / 10**6
    sol_costs = (41_000 + 1_488_440 + 29_000) / SOL
    final = _final(run.lines)
    want = f"({pnl:.6f} {pubkeys.quote_symbol(USDC)} from the chain; network fees and other SOL costs {sol_costs:.6f} SOL"
    if want not in final:
        bad.append(f"USDC final {final!r}, want {want!r}")
    buy = [r for r in run.rows if r["action"] == "buy"][:1]
    if not (buy and _close(buy[0].get("other_sol") or 0, -1_488_440 / SOL)):
        bad.append(f"USDC buy other_sol {buy and buy[0].get('other_sol')}")
    return _report("E", bad)


def _unknown_decimals_named() -> list[str]:
    """A sale read but in a quote whose decimals are unknown says so."""
    try:
        from trading.pnl import CostBasis, pnl_text, sale_pnl  # noqa: PLC0415
    except ImportError:
        return ["no PnL from the chain, so no estimate names its cause"]
    sale = _trade("37hc3CBz")
    reader = getattr(_Chain([sale["signature"]]), "get_trade_deltas", None)
    if reader is None:
        return ["the client reads no trade from the chain"]
    deltas = asyncio.run(
        reader(
            sale["signature"],
            Pubkey.from_string(sale["wallet"]),
            Pubkey.from_string(sale["mint"]),
            Pubkey.from_string(sale["quote_mint"]),
        )
    )
    pnl = sale_pnl(
        CostBasis(1e-8, from_chain=True),
        TradeResult(success=True, amount=1.0, price=1e-8, deltas=deltas),
        Pubkey.new_unique(),
        1e-8,
    )
    text = pnl_text("Final PnL", pnl, "X")
    if "decimals are unknown" not in text or "could not be read" in text:
        return [f"unknown decimals: {text!r}"]
    return []


def check_f_estimates() -> bool:
    """F: an unread sale or buy says its PnL is an estimate."""
    bad: list[str] = []
    sale = _trade("4VQj9Wm7")["signature"]
    run = _round_trip("yw6xTRd2", ["4VQj9Wm7"], "time_based", unreadable={sale})
    final = _final(run.lines)
    if "an estimate" not in final or "from the chain" in final:
        bad.append(f"unread sale: {final!r}")
    sells = [r for r in run.rows if r["action"] == "sell"]
    if (
        not sells
        or sells[0].get("from_chain") is not False
        or sells[0].get("pnl_from_chain") is not False
    ):
        bad.append(f"unread sale row {sells[:1]}")
    elif not _close(sells[0]["price"], run.buy.price, 1e-18):
        bad.append(
            f"unread sale row price {sells[0]['price']} is not its floor {run.buy.price}"
        )
    buy = _trade("yw6xTRd2")["signature"]
    run = _round_trip("yw6xTRd2", ["4VQj9Wm7"], "time_based", unreadable={buy})
    final = _final(run.lines)
    if "an estimate" not in final:
        bad.append(f"unread buy: {final!r}")
    bad += _unknown_decimals_named()
    return _report("F", bad)


def check_g_digits() -> bool:
    """G: TP/SL targets and price lines tell a fresh coin's prices apart."""
    run = _round_trip("4G4wGX2X", ["3TpwEzRD"], "tp_sl")
    bad: list[str] = []
    entry = run.buy.price
    targets = {}
    for name, factor in (("Take profit target", 1.15), ("Stop loss target", 0.85)):
        line = next((x for x in run.lines if x.startswith(name)), "")
        match = re.search(r": ([0-9.e+-]+) SOL", line)
        value = float(match.group(1)) if match else None
        targets[name] = value
        if value is None or not _close(value, entry * factor, entry * factor * 1e-5):
            bad.append(f"{line!r} does not read back {entry * factor}")
    if len(set(targets.values())) != len(targets):
        bad.append(f"targets read the same: {targets}")
    line = next((x for x in run.lines if x.startswith("Current price")), "")
    match = re.search(r": ([0-9.e+-]+) SOL", line)
    if not match or not _close(float(match.group(1)), run.price, run.price * 1e-5):
        bad.append(f"{line!r} does not read back {run.price}")
    return _report("G", bad)


def check_h_restart_keeps_cost() -> bool:
    """H: the positions file keeps the buy's amounts."""
    trade = _trade("yw6xTRd2")
    buy = _in_tmp(lambda: _buy(trade, _Chain([trade["signature"]])))[0]
    with tempfile.TemporaryDirectory() as tmp:
        store = PositionStore(Path(tmp) / "positions.json")
        store.add(_token(trade), buy, datetime.now(UTC).replace(tzinfo=None))
        store.reduce(_token(trade).mint, 50_000.0, 50_000_000_000)
        loaded = PositionStore(Path(tmp) / "positions.json").load()
        # A record whose stored amounts are malformed is refused, not resumed.
        path = Path(tmp) / "positions.json"
        data = json.loads(path.read_text())
        stored = data["positions"][0]["buy"].get("deltas")
        malformed = None
        if stored is not None:
            stored["tokens_raw"] = None
            path.write_text(json.dumps(data))
            malformed = PositionStore(path).load()
    kept = loaded[0].buy_result if loaded else None
    bad = []
    if kept is None or getattr(kept, "deltas", None) != getattr(buy, "deltas", 0):
        bad.append(f"stored buy {kept}")
    if malformed:
        bad.append("a record with tokens_raw null was resumed")
    return _report("H", bad)


def main() -> int:
    _seed_quote_decimals()
    # Each check prints its own failures; the bot's log lines stay off stdout.
    logging.getLogger().handlers.clear()
    checks = [
        ("A: every buy reads the wallet's amounts and the venue's fill", check_a_buys),
        ("B: every sale is priced at what the wallet received", check_b_sales),
        ("C: a round trip's row and Final PnL equal the chain", check_c_round_trips),
        (
            "D: a sale in two parts, each part and the whole position",
            check_d_partial_sale,
        ),
        (
            "E: rent apart from PnL; a USDC position's SOL costs beside it",
            check_e_rent_apart,
        ),
        (
            "F: an unread transaction makes the PnL an estimate, said so",
            check_f_estimates,
        ),
        ("G: TP/SL targets and prices keep their digits", check_g_digits),
        ("H: a kept position keeps its buy's amounts", check_h_restart_keeps_cost),
        ("I: a sale confirmed late is read from the chain", check_i_late_sale),
        (
            "J: settled and slot free before the sale is read; stop waits",
            check_j_settled_then_read,
        ),
        (
            "K: a failing report keeps the sale settled and closes the account",
            check_k_report_failure_keeps_settlement,
        ),
        (
            "L: an unpriced part is totalled in order and named",
            check_l_parts_totalled_in_order,
        ),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

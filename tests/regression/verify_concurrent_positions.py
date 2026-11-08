"""Verify the trader holds up to `trade.max_open_positions` coins at once.

The trader used to await a coin's buy and then its whole exit inline, so it held
one coin at a time and every launch during the hold went by. Each position now
runs as its own task, and the intake takes the next coin while fewer than
`max_open_positions` are open:

  A. `trade.max_open_positions` validates as an integer from 1 to 100, and a
     config without it gets 1.
  B. With the default of 1 the order is today's: a coin is bought, held to its
     exit, `wait_before_new_token` is waited, and only then is the next coin
     taken.
  C. With 3, three coins are bought while none has exited, a fourth waits for a
     free slot, and positions exit in their own order, not the order bought.
  D. Single-token mode finishes, and cleans up, only after its position exits.
  E. When the listener stops, open positions run to their exit before cleanup.
  F. Shutdown cancels every open position and waits until each has ended
     before cleanup runs, also when the shutdown arrives a second time; no
     task is left running.
  G. A position whose exit raises ends alone: the others exit and its slot is
     freed.
  H. Positions under `manual` take no slot, as before: with the default of 1
     every coin is still bought.

Offline: the listener plays launches, the buyer and each position's exit are
recorders; the trader, intake and position tasks are the real ones.

Usage:
    uv run tests/regression/verify_concurrent_positions.py
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from _fakes import _report, _until  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

import bot_runner  # noqa: E402
from config_loader import validate_config  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading import universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
STEP = 0.05  # seconds; every wait below is a few of these


class _Client:
    rpc_endpoint = ""

    async def get_health(self) -> str:
        return "ok"

    async def close(self) -> None:
        return None


class _Listener:
    """Plays launches a few ms apart, then stays connected until `stop` is set."""

    def __init__(self, tokens: list[TokenInfo]) -> None:
        self.tokens = tokens
        self.stop = asyncio.Event()

    async def listen_for_tokens(
        self, token_callback: Callable[[TokenInfo], Awaitable[None]], *_args: object
    ) -> None:
        for token in self.tokens:
            await token_callback(token)
            await asyncio.sleep(0.005)
        await self.stop.wait()


class _Run:
    """One trader on recorders, and the order things happened in."""

    def __init__(self, symbols: str, **settings: object) -> None:
        self.events: list[str] = []
        self.release = {s: asyncio.Event() for s in symbols}
        self.raises: set[str] = set()
        # Seconds a cancelled position takes to unwind, as a sell in flight would.
        self.unwind = 0.0
        self.active = 0
        self.peak = 0
        self.open_at_cleanup: int | None = None
        self.tokens = [_token(s) for s in symbols]
        self.listener = _Listener(self.tokens)
        self.trader = _trader(self.listener, **settings)
        self.trader.buyer.execute = self._buy
        if self.trader.positions.exit_strategy != "manual":
            self.trader.positions.manage = self._manage
        self.trader._cleanup_resources = self._cleanup  # noqa: SLF001

    async def _buy(self, token_info: TokenInfo) -> TradeResult:
        self.events.append(f"buy {token_info.symbol}")
        return TradeResult(
            success=True, price=1.0, amount=1.0, amount_raw=1, tx_signature="sig"
        )

    async def _manage(
        self, token_info: TokenInfo, _buy: TradeResult, **_kwargs: object
    ) -> None:
        symbol = token_info.symbol
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.events.append(f"hold {symbol}")
        try:
            await self.release[symbol].wait()
            if symbol in self.raises:
                self.events.append(f"raise {symbol}")
                raise RuntimeError(f"exit of {symbol} failed")  # noqa: TRY003
            self.events.append(f"exit {symbol}")
        except asyncio.CancelledError:
            if self.unwind:
                await asyncio.sleep(self.unwind)
            self.events.append(f"cancelled {symbol}")
            raise
        finally:
            self.active -= 1

    async def _cleanup(self) -> None:
        self.open_at_cleanup = self.trader.positions.open_count
        self.events.append("cleanup")

    def bought(self) -> list[str]:
        return [e.split()[1] for e in self.events if e.startswith("buy ")]

    def index(self, event: str) -> int:
        return self.events.index(event) if event in self.events else -1


def _token(symbol: str) -> TokenInfo:
    return TokenInfo(
        name=symbol,
        symbol=symbol,
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.new_unique(),
        quote_mint=WSOL_MINT,
    )


def _trader(listener: _Listener, **settings: object) -> object:
    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "buy_amount": 0.01,
        "buy_slippage": 0.1,
        "sell_slippage": 0.1,
        "yolo_mode": True,
        "max_token_age": 30,
        "token_wait_timeout": 5,
        "wait_time_after_creation": 0,
        "wait_time_before_new_token": 0,
        "exit_strategy": "time_based",
    }
    originals = (
        universal_trader.SolanaClient,
        universal_trader.ListenerFactory.create_listener,
    )
    universal_trader.SolanaClient = lambda *_a, **_k: _Client()
    universal_trader.ListenerFactory.create_listener = staticmethod(
        lambda **_k: listener
    )
    try:
        trader = universal_trader.UniversalTrader(TraderSettings(**(fields | settings)))
    finally:
        universal_trader.SolanaClient = originals[0]
        universal_trader.ListenerFactory.create_listener = staticmethod(originals[1])

    async def no_quotes() -> None:
        return None

    trader._resolve_quote_token_programs = no_quotes  # noqa: SLF001
    return trader


def check_a_config() -> bool:
    problems = []
    with (PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml").open() as f:
        base = yaml.safe_load(f)
    for value, accepted in (
        (1, True),
        (3, True),
        (100, True),
        (0, False),
        (101, False),
        (-1, False),
        (2.5, False),
        ("3", False),
    ):
        config = {**base, "trade": {**base["trade"], "max_open_positions": value}}
        try:
            validate_config(config)
        except ValueError as e:
            if accepted:
                problems.append(f"max_open_positions {value!r} refused: {e}")
            elif "trade.max_open_positions" not in str(e):
                problems.append(f"{value!r} refused without naming the key: {e}")
        else:
            if not accepted:
                problems.append(f"max_open_positions {value!r} accepted")
    minimal = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "trade": {"buy_amount": 0.01, "buy_slippage": 0.1, "sell_slippage": 0.1},
        "filters": {"listener_type": "logs"},
    }
    settings = bot_runner.build_trader_settings(minimal, Platform.PUMP_FUN)
    if settings.max_open_positions != 1:
        problems.append(f"default max_open_positions {settings.max_open_positions}")
    return _report(problems)


async def _check_b() -> list[str]:
    run = _Run("ABC", wait_time_before_new_token=STEP * 2)
    loop = asyncio.get_running_loop()
    start = asyncio.create_task(run.trader.start())
    times: dict[str, float] = {}
    for symbol in "ABC":
        if not await _until(lambda s=symbol: f"hold {s}" in run.events):
            break
        await asyncio.sleep(STEP * 2)
        times[f"exit {symbol}"] = loop.time()
        run.release[symbol].set()
        await _until(lambda s=symbol: f"exit {s}" in run.events)
    await asyncio.sleep(STEP)
    run.listener.stop.set()
    await asyncio.wait_for(start, 5)
    problems = []
    want = [f"{e} {s}" for s in "ABC" for e in ("buy", "hold", "exit")] + ["cleanup"]
    if run.events != want:
        problems.append(f"order {run.events}, expected {want}")
    if run.peak != 1:
        problems.append(f"{run.peak} positions held at once")
    return problems


def check_b_default_is_one_at_a_time() -> bool:
    problems = asyncio.run(_check_b())
    # The wait before the next coin is counted from the exit, as before.
    run_gap = asyncio.run(_gap_after_exit())
    if run_gap < STEP * 2 * 0.9:
        problems.append(f"next coin taken {run_gap:.3f}s after the exit")
    return _report(problems)


async def _gap_after_exit() -> float:
    run = _Run("AB", wait_time_before_new_token=STEP * 2)
    loop = asyncio.get_running_loop()
    start = asyncio.create_task(run.trader.start())
    await _until(lambda: "hold A" in run.events)
    released = loop.time()
    run.release["A"].set()
    await _until(lambda: "buy B" in run.events)
    gap = loop.time() - released
    run.release["B"].set()
    run.listener.stop.set()
    await asyncio.wait_for(start, 5)
    return gap


async def _check_c() -> list[str]:
    run = _Run("ABCD", max_open_positions=3)
    start = asyncio.create_task(run.trader.start())
    problems = []
    if not await _until(lambda: run.active == 3):  # noqa: PLR2004
        problems.append(f"three positions never open together: {run.events}")
    await asyncio.sleep(STEP * 2)
    if run.bought() != ["A", "B", "C"]:
        problems.append(f"with three open, bought {run.bought()}")
    run.release["B"].set()
    if not await _until(lambda: "hold D" in run.events):
        problems.append(f"fourth coin not bought after a slot freed: {run.events}")
    elif run.index("exit B") > run.index("buy D"):
        problems.append(f"fourth coin bought before a slot freed: {run.events}")
    if "exit A" in run.events or "exit C" in run.events:
        problems.append(f"another position closed with B: {run.events}")
    for symbol in "CAD":
        run.release[symbol].set()
        await asyncio.sleep(STEP / 5)
    run.listener.stop.set()
    await asyncio.wait_for(start, 5)
    exits = [e.split()[1] for e in run.events if e.startswith("exit ")]
    if exits != ["B", "C", "A", "D"]:
        problems.append(f"exit order {exits}, expected each on its own")
    if run.peak != 3:  # noqa: PLR2004
        problems.append(f"peak of {run.peak} open positions, limit 3")
    return problems


def check_c_three_at_once() -> bool:
    return _report(asyncio.run(_check_c()))


async def _check_d() -> list[str]:
    run = _Run("A", max_open_positions=3, yolo_mode=False)
    start = asyncio.create_task(run.trader.start())
    await _until(lambda: "hold A" in run.events)
    await asyncio.sleep(STEP * 2)
    problems = []
    if start.done() or "cleanup" in run.events:
        problems.append(f"single-token mode ended with its position open: {run.events}")
    run.release["A"].set()
    await asyncio.wait_for(start, 5)
    if run.events != ["buy A", "hold A", "exit A", "cleanup"]:
        problems.append(f"single-token order {run.events}")
    return problems


def check_d_single_token_waits() -> bool:
    return _report(asyncio.run(_check_d()))


async def _check_e() -> list[str]:
    run = _Run("AB", max_open_positions=3)
    start = asyncio.create_task(run.trader.start())
    await _until(lambda: run.active == 2)  # noqa: PLR2004
    run.listener.stop.set()
    await asyncio.sleep(STEP * 2)
    problems = []
    if "cleanup" in run.events:
        problems.append(f"cleanup ran with positions open: {run.events}")
    run.release["A"].set()
    run.release["B"].set()
    await asyncio.wait_for(start, 5)
    if run.events[-1:] != ["cleanup"] or run.open_at_cleanup != 0:
        problems.append(f"order {run.events}, {run.open_at_cleanup} open at cleanup")
    if any(e.startswith("cancelled") for e in run.events):
        problems.append(f"a position was cancelled: {run.events}")
    return problems


def check_e_listener_stop_waits() -> bool:
    return _report(asyncio.run(_check_e()))


async def _check_f(cancellations: int) -> list[str]:
    run = _Run("ABC", max_open_positions=3)
    start = asyncio.create_task(run.trader.start())
    await _until(lambda: run.active == 3)  # noqa: PLR2004
    if cancellations > 1:
        # The positions take a while to unwind, so the second cancellation
        # lands while shutdown waits for them.
        run.unwind = STEP * 2
    run.trader.positions._STOP_BUDGET = 1.0  # noqa: SLF001
    for _ in range(cancellations):
        start.cancel()
        await asyncio.sleep(STEP / 2)
    problems = []
    try:
        await asyncio.wait_for(start, 5)
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        problems.append("shutdown did not finish")
    return problems + _shutdown_problems(run)


def _shutdown_problems(run: _Run) -> list[str]:
    """What a finished shutdown left undone: a position, cleanup, or a task."""
    problems = []
    cancelled = {e.split()[1] for e in run.events if e.startswith("cancelled ")}
    if cancelled != set("ABC"):
        problems.append(f"cancelled {sorted(cancelled)}, expected A, B and C")
    if "cleanup" not in run.events:
        problems.append(f"cleanup never ran: {run.events}")
    elif run.events.index("cleanup") < max(run.index(f"cancelled {s}") for s in "ABC"):
        problems.append(f"cleanup ran before every position ended: {run.events}")
    if run.open_at_cleanup != 0:
        problems.append(f"{run.open_at_cleanup} position(s) running at cleanup")
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if left:
        problems.append(f"tasks left running: {[t.get_name() for t in left]}")
        for task in left:
            task.cancel()
    return problems


def check_f_shutdown_stops_each_position() -> bool:
    problems = asyncio.run(_check_f(1))
    problems += [f"second cancellation: {p}" for p in asyncio.run(_check_f(2))]
    return _report(problems)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _check_g() -> list[str]:
    run = _Run("ABCD", max_open_positions=3)
    run.raises.add("B")
    capture = _Capture()
    logging.getLogger("trading.position_manager").addHandler(capture)
    try:
        start = asyncio.create_task(run.trader.start())
        await _until(lambda: run.active == 3)  # noqa: PLR2004
        run.release["B"].set()
        await _until(lambda: "hold D" in run.events)
        for symbol in "ACD":
            run.release[symbol].set()
        run.listener.stop.set()
        await asyncio.wait_for(start, 5)
    finally:
        logging.getLogger("trading.position_manager").removeHandler(capture)
    problems = []
    exits = sorted(e.split()[1] for e in run.events if e.startswith("exit "))
    if exits != ["A", "C", "D"]:
        problems.append(f"after B raised, exits {exits}")
    if not any("Error managing position in B" in m for m in capture.messages):
        problems.append("the failed exit was not logged")
    if run.events[-1:] != ["cleanup"]:
        problems.append(f"order {run.events}")
    return problems


def check_g_failing_position_ends_alone() -> bool:
    return _report(asyncio.run(_check_g()))


async def _check_h() -> list[str]:
    run = _Run("ABC", exit_strategy="manual")
    start = asyncio.create_task(run.trader.start())
    await _until(lambda: len(run.bought()) == 3)  # noqa: PLR2004
    run.listener.stop.set()
    await asyncio.wait_for(start, 5)
    problems = []
    if run.bought() != ["A", "B", "C"] or run.events[-1:] != ["cleanup"]:
        problems.append(f"manual positions: {run.events}")
    return problems


def check_h_manual_takes_no_slot() -> bool:
    return _report(asyncio.run(_check_h()))


def main() -> int:
    """Run every check and report."""
    universal_trader.trade_log.log_trade = lambda *_a, **_k: None
    universal_trader.trade_log.save_token_info = lambda _t: None
    checks = [
        ("A: max_open_positions validates, default 1", check_a_config),
        ("B: the default holds one coin at a time", check_b_default_is_one_at_a_time),
        ("C: three open at once, the fourth waits", check_c_three_at_once),
        ("D: single-token mode waits for its position", check_d_single_token_waits),
        ("E: a stopped listener lets positions exit", check_e_listener_stop_waits),
        (
            "F: shutdown stops every position first",
            check_f_shutdown_stops_each_position,
        ),
        ("G: a failing position ends alone", check_g_failing_position_ends_alone),
        ("H: manual positions take no slot", check_h_manual_takes_no_slot),
    ]
    passed = 0
    for label, check in checks:
        ok = check()
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

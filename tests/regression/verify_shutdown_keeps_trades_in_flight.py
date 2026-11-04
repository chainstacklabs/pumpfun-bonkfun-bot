"""Verify a shutdown never loses a buy in flight or a sold coin's account close.

Two trades were cut short by Ctrl+C:

- A buy already sent when the shutdown came was lost. The cancellation reached
  the buyer while it waited for the confirmation, nothing was written to
  `positions/<bot name>.json`, and the next start knew nothing of the coin: the
  tokens sat in the wallet unmanaged. A buy is now kept in the file as
  `pending`, with its signature, from the moment it may have reached the chain
  until it is settled, and the next start resolves a pending record from the
  wallet's balance.
- A position sold just before the shutdown was waiting for the RPC node to
  settle before closing its empty account. Shutdown cancelled it as if it were
  still open (`Stopping 1 open position(s)`), and the account kept its rent.
  The close now runs to the end, bounded, and is not counted as open.

  A. A cancellation while the buy waits for its confirmation (continuous mode,
     the shutdown path SIGINT takes) leaves the coin in the file as pending,
     with the signature sent, already on disk before the confirmation
     resolves; post-session cleanup neither burns nor closes it.
  B. A cancellation in the middle of the send still leaves it, with the
     signature of the transaction being sent.
  C. A cancellation before anything was signed leaves nothing.
  D. In single-token mode a cancellation during the confirmation keeps it too.
  E. Without a shutdown: a buy that lands replaces the pending record with the
     amounts read back; one that reverts, or whose send fails (SUBMIT_FAILED),
     leaves no record and is cleaned up as a failed buy; one whose outcome is
     unknown stays pending and is not cleaned up, since that could burn it.
  F. The next start resolves a pending record from the wallet's balance: a coin
     held is resumed with the amount held and rewritten as a landed position;
     one the wallet holds none of is cleared, never sold; an unreadable balance
     keeps it pending. A record written before records could be pending
     resumes as before.
  G. A cancellation while a sold position waits to close its empty account
     (`cleanup.mode: after_sell`) lets the close finish before the trader
     exits, and the shutdown log does not call that position open.
  H. With one position held and one sold, only the held one is stopped and
     counted, and the sold one still closes its account.
  I. A close that hangs is cancelled once the stop budget runs out, with a
     warning, so shutdown still ends.

Offline: the RPC client, the seller and the account close are stubs; the
trader, intake, buyer, position manager and store are the real ones. Runs in a
temporary directory.

Usage:
    uv run tests/regression/verify_shutdown_keeps_trades_in_flight.py
"""

import asyncio
import contextlib
import json
import logging
import os
import struct
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402

from core.pubkeys import TOKEN_PROGRAM, WSOL_MINT  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from trading import platform_aware, position_manager, universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore, encode_token_info  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
WALLET = Wallet(KEY)
CURVE = Pubkey.new_unique()
TOKENS = 20  # what an extreme_fast_mode buy asks for
RECEIVED_RAW = 19_500_000  # what the buy delivers, read back from its transaction
HELD_RAW = 18_000_000  # what the wallet holds at the next start
SHUTDOWN_LIMIT = 2.0  # seconds; check I's stop budget is 0.3


async def _no_fee(*_args: object) -> None:
    return None


async def _empty(*_args: object, **_kwargs: object) -> list:
    return []


def _implementations() -> PlatformImplementations:
    """A curve that builds an empty buy, so the buyer reaches the send."""
    return PlatformImplementations(
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _mint: CURVE,
            derive_creator_vault=lambda _creator: CURVE,
        ),
        instruction_builder=SimpleNamespace(
            build_buy_instruction=_empty,
            get_required_accounts_for_buy=lambda *_a, **_k: [],
            get_buy_compute_unit_limit=lambda _override: 100_000,
        ),
        curve_manager=SimpleNamespace(),
        event_parser=None,
    )


def _token(symbol: str) -> TokenInfo:
    return TokenInfo(
        name=symbol,
        symbol=symbol,
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=CURVE,
        associated_bonding_curve=CURVE,
        creator=CURVE,
        creator_vault=CURVE,
        token_program_id=TOKEN_PROGRAM,
        quote_mint=WSOL_MINT,
        state_from_event=True,  # extreme_fast_mode reads no curve
    )


class _Client:
    """The RPC: a send and a confirmation that wait when told to.

    `send_gate` holds a send after the transaction is signed; `confirm_gate`
    holds the confirmation, which then answers `status`. `fee_gate` holds the
    buyer before it signs anything.
    """

    rpc_endpoint = ""

    def __init__(self) -> None:
        self.fee_gate: asyncio.Event | None = None
        self.send_gate: asyncio.Event | None = None
        self.confirm_gate: asyncio.Event | None = None
        self.send_raises = False
        self.status = ConfirmationStatus.SUCCESS
        self.signed: list[str] = []
        self.sent: list[str] = []
        self.at: set[str] = set()
        # Token accounts by address, for the balance reads on resume.
        self.accounts: dict[Pubkey, SimpleNamespace] = {}
        self.down = False

    async def calculate_priority_fee(self, *_args: object) -> None:
        self.at.add("fee")
        if self.fee_gate is not None:
            await self.fee_gate.wait()

    async def build_and_send_transaction(
        self, *_args: object, on_signed: Callable | None = None, **_kwargs: object
    ) -> Signature:
        signature = Signature.new_unique()
        self.signed.append(str(signature))
        if on_signed is not None:
            on_signed(signature)
        self.at.add("send")
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_raises:
            raise ConnectionError("node unreachable")  # noqa: TRY003
        self.sent.append(str(signature))
        return signature

    async def confirm_transaction_detailed(
        self, *_args: object, **_kwargs: object
    ) -> ConfirmationStatus:
        self.at.add("confirm")
        if self.confirm_gate is not None:
            await self.confirm_gate.wait()
        return self.status

    async def confirm_transaction(self, *args: object, **kwargs: object) -> bool:
        status = await self.confirm_transaction_detailed(*args, **kwargs)
        return status is ConfirmationStatus.SUCCESS

    async def get_buy_transaction_details(
        self, *_args: object, **_kwargs: object
    ) -> tuple[int, int]:
        return RECEIVED_RAW, 3_000_000

    async def get_token_account_balance(self, *_args: object) -> int:
        return RECEIVED_RAW

    def hold(self, token: TokenInfo, raw: int) -> None:
        account = WALLET.get_associated_token_address(token.mint, TOKEN_PROGRAM)
        data = bytes(token.mint) + bytes(WALLET.pubkey) + struct.pack("<Q", raw)
        self.accounts[account] = SimpleNamespace(
            data=data + bytes(93), owner=TOKEN_PROGRAM
        )

    async def get_account_info(self, pubkey: Pubkey) -> SimpleNamespace:
        if self.down:
            raise RuntimeError("rpc down")  # noqa: TRY003
        if pubkey not in self.accounts:
            raise ValueError(f"Account {pubkey} not found")  # noqa: TRY003
        return self.accounts[pubkey]

    async def get_health(self) -> str:
        return "ok"

    async def close(self) -> None:
        return None


class _Listener:
    def __init__(self, tokens: list[TokenInfo]) -> None:
        self.tokens = tokens

    async def listen_for_tokens(
        self, token_callback: Callable[[TokenInfo], Awaitable[None]], *_args: object
    ) -> None:
        for token in self.tokens:
            await token_callback(token)
        await asyncio.Event().wait()


class _Seller:
    async def execute(
        self, _token_info: TokenInfo, token_amount: float, token_price: float,
        token_amount_raw: int | None = None,
    ) -> TradeResult:  # fmt: skip
        return TradeResult(
            success=True,
            price=token_price,
            amount=token_amount,
            amount_raw=token_amount_raw,
            tx_signature="sell",
        )


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def has(self, text: str) -> bool:
        return any(text in line for line in self.lines)


class _Run:
    """One trader on the stub RPC; a real buyer, intake, positions and store."""

    def __init__(self, path: Path, tokens: list[TokenInfo], **settings: object) -> None:
        self.path = path
        self.client = _Client()
        self.events: list[str] = []
        fields = {
            "rpc_endpoint": "",
            "wss_endpoint": "",
            "private_key": KEY,
            "buy_amount": 0.003,
            "buy_slippage": 0.3,
            "sell_slippage": 0.3,
            "extreme_fast_mode": True,
            "extreme_fast_token_amount": TOKENS,
            "yolo_mode": True,
            "max_token_age": 30,
            "token_wait_timeout": 5,
            "wait_time_after_creation": 0,
            "wait_time_before_new_token": 0,
            "wait_time_after_buy": 0,
            "exit_strategy": "time_based",
            "positions_file": str(path),
        }
        originals = (
            universal_trader.SolanaClient,
            universal_trader.ListenerFactory.create_listener,
        )
        universal_trader.SolanaClient = lambda *_a, **_k: self.client
        universal_trader.ListenerFactory.create_listener = staticmethod(
            lambda **_k: _Listener(tokens)
        )
        try:
            self.trader = universal_trader.UniversalTrader(
                TraderSettings(**(fields | settings))
            )
        finally:
            universal_trader.SolanaClient = originals[0]
            universal_trader.ListenerFactory.create_listener = staticmethod(
                originals[1]
            )
        self.trader._resolve_quote_token_programs = self._nothing  # noqa: SLF001
        self.trader.buyer.priority_fee_manager = self.client
        self.trader.positions.seller = _Seller()

    async def _nothing(self) -> None:
        return None

    def hold_positions(self) -> None:
        """Positions wait in their exit until cancelled; nothing is sold."""

        async def manage(token_info: TokenInfo, *_a: object, **_k: object) -> None:
            self.events.append(f"hold {token_info.symbol}")
            await asyncio.Event().wait()

        self.trader.positions.manage = manage

    def records(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return {r["mint"]: r for r in json.loads(self.path.read_text())["positions"]}


@contextlib.contextmanager
def _patched(module: object, name: str, value: object):  # noqa: ANN202
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


@contextlib.contextmanager
def _stubbed_market():  # noqa: ANN202
    with _patched(
        platform_aware,
        "get_platform_implementations",
        lambda *_a, **_k: _implementations(),
    ):
        yield


@contextlib.contextmanager
def _logged():  # noqa: ANN202
    handler = _Logs()
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.005)
    return True


async def _shut_down(task: asyncio.Task, timeout: float = 5.0) -> bool:
    """Cancel the trader as SIGINT does under asyncio.run; whether it ended."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout)
    return task.done()


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _check_pending(run: _Run, token: TokenInfo, signature: str) -> list[str]:
    record = run.records().get(str(token.mint))
    if record is None:
        return [f"{token.symbol} is not in the positions file: {run.records()}"]
    problems = []
    if record.get("pending") is not True:
        problems.append(f"record is not pending: {record}")
    if record["buy"]["tx_signature"] != signature:
        problems.append(
            f"record names {record['buy']['tx_signature']}, the buy was {signature}"
        )
    return problems


# A, B, C: continuous mode, cancelled at three points of the buy.


async def _check_a(tmp: Path) -> list[str]:
    problems = []
    token = _token("INFLIGHT")
    run = _Run(
        tmp / "a.json",
        [token],
        cleanup_mode="post_session",
        cleanup_force_close_with_burn=True,
    )
    run.hold_positions()
    run.client.confirm_gate = asyncio.Event()
    closed: list[list[Pubkey]] = []

    async def post_session(_c: object, _w: object, mints: list, *_a: object) -> None:
        closed.append(list(mints))

    with (
        _stubbed_market(),
        _patched(universal_trader, "handle_cleanup_post_session", post_session),
    ):
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: "confirm" in run.client.at):
            return ["the buy never reached its confirmation"]
        signature = run.client.sent[0]
        if str(token.mint) not in run.records():
            problems.append("the buy is not on disk while it waits to confirm")
        if not await _shut_down(task):
            problems.append("the trader did not end after the cancellation")
    problems += _check_pending(run, token, signature)
    if any(token.mint in mints for mints in closed):
        problems.append(f"post-session cleanup was handed the kept coin: {closed}")
    return problems


def check_a_cancelled_during_confirmation(tmp: Path) -> bool:
    return _report(asyncio.run(_check_a(tmp)))


async def _check_b(tmp: Path) -> list[str]:
    token = _token("MIDSEND")
    run = _Run(tmp / "b.json", [token])
    run.hold_positions()
    run.client.send_gate = asyncio.Event()
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: "send" in run.client.at):
            return ["the buy never reached its send"]
        if not await _shut_down(task):
            return ["the trader did not end after the cancellation"]
    return _check_pending(run, token, run.client.signed[0])


def check_b_cancelled_mid_send(tmp: Path) -> bool:
    return _report(asyncio.run(_check_b(tmp)))


async def _check_c(tmp: Path) -> list[str]:
    run = _Run(tmp / "c.json", [_token("UNSENT")])
    run.hold_positions()
    run.client.fee_gate = asyncio.Event()
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: "fee" in run.client.at):
            return ["the buy never started"]
        if not await _shut_down(task):
            return ["the trader did not end after the cancellation"]
    problems = []
    if run.client.signed:
        problems.append(f"signed a buy after the cancellation: {run.client.signed}")
    if run.records():
        problems.append(f"a buy never signed left a record: {run.records()}")
    return problems


def check_c_cancelled_before_signing(tmp: Path) -> bool:
    return _report(asyncio.run(_check_c(tmp)))


# D: single-token mode.


async def _check_d(tmp: Path) -> list[str]:
    token = _token("SINGLE")
    run = _Run(tmp / "d.json", [token], yolo_mode=False)
    run.hold_positions()
    run.client.confirm_gate = asyncio.Event()
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: "confirm" in run.client.at):
            return ["the buy never reached its confirmation"]
        if not await _shut_down(task):
            return ["the trader did not end after the cancellation"]
    return _check_pending(run, token, run.client.sent[0])


def check_d_single_token_mode(tmp: Path) -> bool:
    return _report(asyncio.run(_check_d(tmp)))


# E: each outcome of a buy without a shutdown.


async def _outcome(tmp: Path, name: str, **client: object) -> tuple[_Run, list, dict]:
    """Run one buy to its outcome; the run, failure cleanups and the record."""
    token = _token(name)
    run = _Run(
        tmp / f"e-{name}.json",
        [token],
        cleanup_mode="on_fail",
        cleanup_force_close_with_burn=True,
    )
    run.hold_positions()
    for key, value in client.items():
        setattr(run.client, key, value)
    failed: list[Pubkey] = []

    async def after_failure(_c: object, _w: object, mint: Pubkey, *_a: object) -> None:
        failed.append(mint)

    with (
        _stubbed_market(),
        _patched(universal_trader, "handle_cleanup_after_failure", after_failure),
    ):
        task = asyncio.create_task(run.trader.start())
        await _until(
            lambda: f"hold {name}" in run.events or bool(failed) or bool(run.records())
        )
        await asyncio.sleep(0.05)
        await _shut_down(task)
    return run, failed, run.records().get(str(token.mint), {})


async def _check_e(tmp: Path) -> list[str]:
    problems = []
    run, failed, record = await _outcome(tmp, "LANDED")
    if (
        not record
        or record.get("pending")
        or record["buy"]["amount_raw"] != RECEIVED_RAW
    ):
        problems.append(f"landed: not rewritten with the amount read back: {record}")
    run, failed, record = await _outcome(
        tmp, "REVERTED", status=ConfirmationStatus.REVERTED
    )
    if record:
        problems.append(f"reverted: left a record: {record}")
    if len(failed) != 1:
        problems.append("reverted: not cleaned up as a failed buy")
    run, failed, record = await _outcome(tmp, "NOTSENT", send_raises=True)
    if record or run.client.sent:
        problems.append(f"send failed: left a record: {record}")
    run, failed, record = await _outcome(
        tmp, "UNKNOWN", status=ConfirmationStatus.UNCONFIRMED
    )
    if record.get("pending") is not True:
        problems.append(f"unconfirmed: not kept pending: {record}")
    if failed:
        problems.append("unconfirmed: cleaned up as a failed buy, which can burn it")
    return problems


def check_e_outcomes_without_shutdown(tmp: Path) -> bool:
    return _report(asyncio.run(_check_e(tmp)))


# F: the next start.


def _manager(path: Path, client: _Client) -> tuple[PositionManager, list]:
    settings = TraderSettings(
        rpc_endpoint="",
        wss_endpoint="",
        private_key=KEY,
        buy_amount=0.003,
        buy_slippage=0.3,
        sell_slippage=0.3,
        exit_strategy="time_based",
    )
    manager = PositionManager(
        settings,
        seller=_Seller(),
        solana_client=client,
        wallet=WALLET,
        priority_fee_manager=None,
        platform_implementations=SimpleNamespace(),
        store=PositionStore(path),
    )
    held: list[tuple[str, int | None]] = []

    async def manage(token_info: TokenInfo, buy: TradeResult, **_k: object) -> None:
        held.append((token_info.symbol, buy.amount_raw))

    manager.manage = manage
    return manager, held


def _pending_record(token: TokenInfo) -> dict:
    """A record as a buy sent but never confirmed leaves it."""
    return {
        "mint": str(token.mint),
        "symbol": token.symbol,
        "wallet": str(WALLET.pubkey),
        "entry_time": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "buy": {
            "price": 1.5e-7,
            "amount": float(TOKENS),
            "amount_raw": None,
            "tx_signature": f"sig-{token.symbol}",
        },
        "token_info": encode_token_info(token),
        "pending": True,
    }


async def _check_f(tmp: Path) -> list[str]:
    landed, lost, unknown, old = (
        _token(s) for s in ("LANDED", "LOST", "UNKNOWN", "OLD")
    )
    path = tmp / "f.json"
    # A record as a landed buy has always been written, never pending.
    store = PositionStore(path)
    store.add(
        old,
        TradeResult(success=True, amount=1.0, amount_raw=1_000_000, price=1.0),
        datetime.now(UTC).replace(tzinfo=None),
        WALLET.pubkey,
    )
    data = json.loads(path.read_text())
    data["positions"] += [_pending_record(t) for t in (landed, lost, unknown)]
    path.write_text(json.dumps(data))

    client = _Client()
    client.hold(landed, HELD_RAW)
    client.hold(old, 1_000_000)
    original = asyncio.sleep

    async def quick(delay: float, *args: object) -> object:
        return await original(min(delay, 0.01), *args)

    async def resolve_quote(*_a: object) -> Pubkey:
        return TOKEN_PROGRAM

    after, held = _manager(path, client)
    with (
        _patched(position_manager.asyncio, "sleep", quick),
        _patched(position_manager, "resolve_quote_token_program", resolve_quote),
    ):
        # The UNKNOWN coin's balance cannot be read: down for its reads only.
        unknown_account = WALLET.get_associated_token_address(
            unknown.mint, TOKEN_PROGRAM
        )
        real_read = client.get_account_info

        async def read(pubkey: Pubkey) -> SimpleNamespace:
            if pubkey == unknown_account:
                raise RuntimeError("rpc down")  # noqa: TRY003
            return await real_read(pubkey)

        client.get_account_info = read
        resumed = {t.symbol for t in await after.resume()}
        await after.wait_all()

    return _resolved(
        resumed,
        held,
        {r["symbol"]: r for r in json.loads(path.read_text())["positions"]},
    )


def _resolved(resumed: set[str], held: list, records: dict[str, dict]) -> list[str]:
    """What check F found wrong with the resume and the file it left."""
    problems = []
    if resumed != {"LANDED", "OLD"}:
        problems.append(f"resumed {sorted(resumed)}, expected LANDED and OLD")
    if ("LANDED", HELD_RAW) not in held:
        problems.append(
            f"LANDED not held at the {HELD_RAW} raw the wallet holds: {held}"
        )
    if ("OLD", 1_000_000) not in held:
        problems.append(f"a record without `pending` did not resume as before: {held}")
    landed_record = records.get("LANDED", {})
    if (
        landed_record.get("pending")
        or landed_record.get("buy", {}).get("amount_raw") != HELD_RAW
    ):
        problems.append(f"LANDED not rewritten as a landed position: {landed_record}")
    if "LOST" in records:
        problems.append("a pending buy the wallet holds none of was not cleared")
    if any(symbol == "LOST" for symbol, _ in held):
        problems.append("a pending buy the wallet holds none of was sold")
    if records.get("UNKNOWN", {}).get("pending") is not True:
        problems.append(f"an unreadable balance did not keep it pending: {records}")
    return problems


def check_f_next_start_resolves_pending(tmp: Path) -> bool:
    return _report(asyncio.run(_check_f(tmp)))


# G, H, I: a sold position closing its account at shutdown.


class _Close:
    """Stands in for the after-sell account close: a settle wait, then the close."""

    def __init__(self, settle: float = 0.3) -> None:
        self.settle = settle
        self.waiting: list[str] = []
        self.closed: list[str] = []
        self.cancelled: list[str] = []

    async def __call__(
        self, _c: object, _w: object, mint: Pubkey, *_args: object
    ) -> None:
        self.waiting.append(str(mint))
        try:
            await asyncio.sleep(self.settle)
        except asyncio.CancelledError:
            self.cancelled.append(str(mint))
            raise
        self.closed.append(str(mint))


async def _check_g(tmp: Path) -> list[str]:
    problems = []
    token = _token("SOLD")
    run = _Run(tmp / "g.json", [token], cleanup_mode="after_sell")
    close = _Close()
    with (
        _stubbed_market(),
        _patched(position_manager, "handle_cleanup_after_sell", close),
        _logged() as logs,
    ):
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: bool(close.waiting)):
            return ["the position never reached its after-sell close"]
        if not await _shut_down(task):
            problems.append("the trader did not end after the cancellation")
    if str(token.mint) not in close.closed:
        problems.append("the sold coin's account close was cut short")
    if logs.has("Stopping 1 open position"):
        problems.append("the shutdown log calls the sold position open")
    if not logs.has("Waiting for 1 sold position(s)"):
        problems.append("the shutdown log does not say it waits for the close")
    if run.records():
        problems.append(f"the sold position is still in the file: {run.records()}")
    return problems


def check_g_close_after_sell_finishes(tmp: Path) -> bool:
    return _report(asyncio.run(_check_g(tmp)))


async def _check_h(tmp: Path) -> list[str]:
    problems = []
    sold, held = _token("SOLD"), _token("HELD")
    path = tmp / "h.json"
    client = _Client()
    manager, _ = _manager(path, client)
    manager.manage = PositionManager.manage.__get__(manager)
    manager.cleanup_mode = "after_sell"
    manager.wait_time_after_buy = 0
    close = _Close()
    bought = TradeResult(success=True, amount=1.0, amount_raw=1_000_000, price=1.0)
    with (
        _patched(position_manager, "handle_cleanup_after_sell", close),
        _logged() as logs,
    ):
        manager.store.load()
        manager.max_open_positions = 2
        manager.open(sold, bought)
        if not await _until(lambda: bool(close.waiting)):
            return ["the sold position never reached its close"]
        manager.wait_time_after_buy = 3600
        manager.open(held, bought)
        await asyncio.sleep(0.02)
        await manager.stop()
    if str(sold.mint) not in close.closed:
        problems.append("the sold coin's account close was cut short")
    if not logs.has("Stopping 1 open position(s)"):
        problems.append(f"expected one open position stopped: {logs.lines}")
    if str(held.mint) not in {
        r["mint"] for r in json.loads(path.read_text())["positions"]
    }:
        problems.append("the held position left the file")
    if manager.open_count:
        problems.append(f"{manager.open_count} position task(s) still running")
    return problems


def check_h_only_held_positions_are_stopped(tmp: Path) -> bool:
    return _report(asyncio.run(_check_h(tmp)))


async def _check_i(tmp: Path) -> list[str]:
    problems = []
    token = _token("HANG")
    run = _Run(tmp / "i.json", [token], cleanup_mode="after_sell")
    run.trader.positions._STOP_BUDGET = 0.3  # noqa: SLF001
    close = _Close(settle=3600)
    with (
        _stubbed_market(),
        _patched(position_manager, "handle_cleanup_after_sell", close),
        _logged() as logs,
    ):
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: bool(close.waiting)):
            return ["the position never reached its after-sell close"]
        loop = asyncio.get_running_loop()
        started = loop.time()
        if not await _shut_down(task, timeout=3.0):
            problems.append("a hanging close held the shutdown")
        elapsed = loop.time() - started
        if not await _until(lambda: close.cancelled):
            problems.append("the hanging close is still running after shutdown")
    if elapsed > SHUTDOWN_LIMIT:
        problems.append(f"shutdown took {elapsed:.1f}s, past its budget")
    if not logs.has("did not finish within"):
        problems.append("no warning names the close that did not finish")
    return problems


def check_i_hanging_close_is_bounded(tmp: Path) -> bool:
    return _report(asyncio.run(_check_i(tmp)))


def main() -> None:
    print("=" * 72)
    print("Verifying a shutdown keeps buys in flight and finishes account closes")
    print("=" * 72)
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        cwd = Path.cwd()
        os.chdir(tmp)  # the trade log writes under trades/ in the working dir
        try:
            checks = [
                ("A: cancelled while the buy confirms, kept pending", lambda: check_a_cancelled_during_confirmation(tmp)),
                ("B: cancelled mid-send, kept with its signature", lambda: check_b_cancelled_mid_send(tmp)),
                ("C: cancelled before signing, nothing kept", lambda: check_c_cancelled_before_signing(tmp)),
                ("D: single-token mode keeps it too", lambda: check_d_single_token_mode(tmp)),
                ("E: landed, reverted, unsent and unknown buys", lambda: check_e_outcomes_without_shutdown(tmp)),
                ("F: the next start resolves pending buys", lambda: check_f_next_start_resolves_pending(tmp)),
                ("G: a sold coin's close finishes at shutdown", lambda: check_g_close_after_sell_finishes(tmp)),
                ("H: only held positions are stopped", lambda: check_h_only_held_positions_are_stopped(tmp)),
                ("I: a hanging close is bounded", lambda: check_i_hanging_close_is_bounded(tmp)),
            ]  # fmt: skip
            results = []
            for name, check in checks:
                try:
                    ok = check()
                except Exception as e:  # noqa: BLE001 - a crash is a failed check
                    print(f"    raised {type(e).__name__}: {e}")
                    ok = False
                results.append(ok)
                print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)
        finally:
            os.chdir(cwd)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

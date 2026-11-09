"""Verify a buy in flight is never lost, and a sold coin's close survives shutdown.

Two trades were cut short by Ctrl+C:

- A buy already sent when the shutdown came was lost. The cancellation reached
  the buyer while it waited for the confirmation, nothing was written to
  `positions/<bot name>.json`, and the next start knew nothing of the coin: the
  tokens sat in the wallet unmanaged. A buy is now kept in the file as pending,
  with its signature and what resolving it needs, from the moment it may have
  reached the chain until it is resolved, and a resolver settles it by its
  signature: on the next start, and in the same run when its confirmation never
  came.
- A position sold just before the shutdown was waiting for the RPC node to
  settle before closing its empty account. Shutdown cancelled it as if it were
  still open (`Stopping 1 open position(s)`), and the account kept its rent.
  The close now runs to the end, bounded, and is not counted as open.

  A. A cancellation while the buy waits for its confirmation (continuous mode,
     the shutdown path SIGINT takes) leaves the coin in the file as pending,
     with the signature sent and its blockhash's last valid block height,
     already on disk before the confirmation resolves; post-session cleanup
     neither burns nor closes it.
  B. A cancellation in the middle of the send still leaves it, with the
     signature of the transaction being sent.
  C. A cancellation before anything was signed leaves nothing.
  D. In single-token mode a cancellation during the confirmation keeps it too.
  E. Without a shutdown: a buy that lands replaces the pending record with the
     amounts read back; one that reverts, or whose send provably never left
     (SUBMIT_FAILED), leaves no record and is cleaned up as a failed buy; one
     whose send failed ambiguously after signing, or whose confirmation never
     came, stays pending and is not cleaned up, since that could burn it;
     under `manual` nothing is written and an unconfirmed buy is not cleaned
     up either.
  F. The next start resolves a pending buy by its signature: one that landed
     is held at the amount and fill price its transaction shows and rewritten
     as landed, with its trade-log line, unless the wallet no longer holds it
     (sold by hand while the bot was down: cleared) or holds less (held at
     what is left, still at its fill price); a reverted one is cleared; only
     the first lookup searches the whole history; one no
     node knows yet is looked at again until it shows up. Once its blockhash
     has expired (by block height, or two minutes after the send when none
     was recorded) the balance decides: none held clears it, a balance holds
     at most what the buy can have delivered, with no price, and an
     unreadable balance keeps it pending. A record written before records
     could be pending resumes as before.
  G. Balances on restart are read at "confirmed": a buy that landed seconds
     ago, not yet "finalized", is resumed, not cleared.
  H. A position whose fill price is unknown runs no exit until a real price
     is read, says so once, and then counts profit and loss from that price.
  I. In a running session an unconfirmed buy is resolved by the same
     resolver: once it shows up it is held at the transaction's amounts and
     logged, and once sold its account is closed by post-session cleanup; one
     that expired with nothing held is removed.
  J. A sold position waiting to close its empty account (`cleanup.mode:
     after_sell`) when the trader is cancelled closes it before the trader
     exits, and the shutdown log does not call it open.
  K. With one position held and one sold, only the held one is stopped and
     counted, and the sold one still closes its account.
  L. A close that hangs is cancelled once the stop budget runs out, with a
     warning, so shutdown still ends. The sold coin stays in the positions
     file, marked sold, and the next start closes its account without
     selling and drops the record, also under `manual`, which resumes nothing.
  M. A send is SUBMIT_FAILED only when no attempt can have reached the
     network (no connection, a node refusal, a 4xx); a read timeout or a 5xx
     on any attempt leaves it possibly sent. The signer reports the signature
     and its last valid block height once, before the first attempt.
  N. Shutdown names the buys still pending, and only those.
  O. The client reads a signature's status as REVERTED or SUCCESS only at
     "confirmed" or above (an error at "processed" is not final), returns the
     slot it landed in, searches the history only when asked, and reads an
     account at a minimum slot: a node behind it fails the read rather than
     answering "not found".
  P. A buy still being resolved holds its position slot: no other coin is
     bought meanwhile.
  Q. A node behind the slot a decision rests on (the slot the buy landed in,
     or the one its expiry was seen at) keeps the record pending; it never
     clears it on its "not found". A record with a block height whose height
     cannot be read stays pending too: the clock does not stand in for it, and
     the warning names the coin, at once and then once a minute.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import httpx2  # noqa: E402
from _fakes import _deltas, _report, _until  # noqa: E402
from solana.rpc.core import RPCException  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402

from core.client import SolanaClient, TransactionNotSent  # noqa: E402
from core.pubkeys import TOKEN_PROGRAM, WSOL_MINT  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from trading import platform_aware, position_manager, universal_trader  # noqa: E402
from trading.base import PendingBuy, TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
WALLET = Wallet(KEY)
CURVE = Pubkey.new_unique()
TOKENS = 20  # what an extreme_fast_mode buy asks for
RECEIVED_RAW = 19_500_000  # what the buy delivers, read back from its transaction
SPENT_LAMPORTS = 3_000_000  # what it paid, read back from its transaction
FILL_PRICE = SPENT_LAMPORTS / 1e9 / (RECEIVED_RAW / 1e6)
LAST_VALID = 1_000  # the block height past which a buy's blockhash is dead
LANDED_SLOT = 500  # the slot a looked-up signature landed in
EXPIRY_SLOT = 600  # the slot the expiry was seen at
CAP_RAW = 20_000_000  # the most a pending buy can have delivered
SHARED_RAW = 50_000_000  # a wallet balance that also holds another bot's coins
PARTIAL_RAW = 5_000_000  # what is left of a landed buy partly sold by hand
OLD_RAW = 1_000_000  # a position bought before records could be pending
SHUTDOWN_LIMIT = 2.0  # seconds; check L's stop budget is 0.3


async def _empty(*_args: object, **_kwargs: object) -> list:
    return []


def _implementations() -> PlatformImplementations:
    """A curve that builds an empty buy, so the buyer reaches the send."""
    return PlatformImplementations(
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _mint: CURVE,
            derive_creator_vault=lambda _creator: CURVE,
            program_id=None,
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


def _account(token: TokenInfo) -> Pubkey:
    return WALLET.get_associated_token_address(token.mint, TOKEN_PROGRAM)


class _Client:
    """The RPC: a send and a confirmation that wait when told to.

    `send_gate` holds a send after the transaction is signed; `confirm_gate`
    holds the confirmation, which then answers `status`. `fee_gate` holds the
    buyer before it signs anything. `send_error` fails the send after signing.
    Signature lookups answer from `outcomes` (by signature, else `outcome`).
    Accounts in `fresh` landed too recently to show at "finalized".
    """

    rpc_endpoint = ""

    def __init__(self) -> None:
        self.fee_gate: asyncio.Event | None = None
        self.send_gate: asyncio.Event | None = None
        self.confirm_gate: asyncio.Event | None = None
        self.send_error: Exception | None = None
        self.status = ConfirmationStatus.SUCCESS
        self.signed: list[str] = []
        self.sent: list[str] = []
        self.at: set[str] = set()
        self.outcomes: dict[str, list[ConfirmationStatus | None]] = {}
        self.outcome: ConfirmationStatus | None = None
        self.lookups: dict[str, int] = {}
        self.block_height = 0
        self.height_error: Exception | None = None
        self.details: dict[str, tuple[int | None, int | None]] = {}
        self.accounts: dict[Pubkey, SimpleNamespace] = {}
        self.fresh: set[Pubkey] = set()
        self.unreadable: set[Pubkey] = set()
        # The slot the answering node has reached; accounts in `behind` are
        # newer than it, so it reports them missing unless asked for a later
        # slot, which it refuses.
        self.node_slot = 10**9
        self.behind: set[Pubkey] = set()
        self.history: dict[str, list[bool]] = {}

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
            on_signed(signature, LAST_VALID)
        self.at.add("send")
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(str(signature))
        return signature

    async def confirm_transaction_detailed(
        self, *_args: object, **_kwargs: object
    ) -> ConfirmationStatus:
        self.at.add("confirm")
        if self.confirm_gate is not None:
            await self.confirm_gate.wait()
        return self.status

    async def get_signature_outcome(
        self, signature: object, *, search_history: bool = True, **_kwargs: object
    ) -> tuple[object, int | None]:
        key = str(signature)
        self.lookups[key] = self.lookups.get(key, 0) + 1
        self.history.setdefault(key, []).append(search_history)
        script = self.outcomes.get(key)
        if script:
            status = script.pop(0) if len(script) > 1 else script[0]
        else:
            status = self.outcome
        return status, (LANDED_SLOT if status is not None else None)

    async def get_block_height_and_slot(self) -> tuple[int, int]:
        if self.height_error is not None:
            raise self.height_error
        return self.block_height, EXPIRY_SLOT

    async def get_trade_deltas(
        self, signature: object, *_args: object, **_kwargs: object
    ) -> TradeDeltas | None:
        return _deltas(
            *self.details.get(str(signature), (RECEIVED_RAW, SPENT_LAMPORTS))
        )

    def hold(self, token: TokenInfo, raw: int, *, fresh: bool = False) -> None:
        data = bytes(token.mint) + bytes(WALLET.pubkey) + struct.pack("<Q", raw)
        self.accounts[_account(token)] = SimpleNamespace(
            data=data + bytes(93), owner=TOKEN_PROGRAM
        )
        if fresh:
            self.fresh.add(_account(token))

    async def get_account_info(
        self,
        pubkey: Pubkey,
        commitment: str | None = None,
        min_context_slot: int | None = None,
    ) -> SimpleNamespace:
        if pubkey in self.unreadable:
            raise RuntimeError("rpc down")  # noqa: TRY003
        if min_context_slot is not None and min_context_slot > self.node_slot:
            raise RuntimeError("Minimum context slot has not been reached")  # noqa: TRY003
        lags = commitment not in ("confirmed", "processed")  # None is finalized
        unseen = pubkey in self.behind  # landed after the node's own slot
        if pubkey not in self.accounts or (lags and pubkey in self.fresh) or unseen:
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
    def __init__(self) -> None:
        self.sells: list[tuple[str, float]] = []

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float,
        token_amount_raw: int | None = None,
    ) -> TradeResult:  # fmt: skip
        self.sells.append((token_info.symbol, token_price))
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

    def count(self, text: str) -> int:
        return sum(text in line for line in self.lines)


class _Run:
    """One trader on the stub RPC; a real buyer, intake, positions and store."""

    def __init__(self, path: Path, tokens: list[TokenInfo], **settings: object) -> None:
        self.path = path
        self.client = _Client()
        self.events: list[str] = []
        self.handled = asyncio.Event()
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
        # Set once the trader has handled the buy's outcome, landed or not.
        for name in ("_handle_successful_buy", "_handle_failed_buy"):
            handler = getattr(self.trader, name)

            async def handled(*args: object, _handler: Callable = handler) -> None:
                try:
                    await _handler(*args)
                finally:
                    self.handled.set()

            setattr(self.trader, name, handled)

    async def _nothing(self) -> None:
        return None

    def hold_positions(self) -> None:
        """Positions wait in their exit until cancelled; nothing is sold."""

        async def manage(token_info: TokenInfo, buy: TradeResult, **_k: object) -> None:
            self.events.append(f"hold {token_info.symbol} {buy.amount_raw}")
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


async def _shut_down(task: asyncio.Task, timeout: float = 5.0) -> bool:
    """Cancel the trader as SIGINT does under asyncio.run; whether it ended."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout)
    return task.done()


def _trade_lines() -> list[dict]:
    path = Path("trades/trades.log")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _check_pending(run: _Run, token: TokenInfo, signature: str) -> list[str]:
    record = run.records().get(str(token.mint))
    if record is None:
        return [f"{token.symbol} is not in the positions file: {run.records()}"]
    problems = []
    pending = record.get("pending")
    if not isinstance(pending, dict):
        problems.append(f"record is not pending: {record}")
    elif pending.get("last_valid_block_height") != LAST_VALID:
        problems.append(f"record lacks its blockhash's last valid height: {pending}")
    if record["buy"]["tx_signature"] != signature:
        problems.append(
            f"record names {record['buy']['tx_signature']}, the buy was {signature}"
        )
    return problems


# A, B, C, D: cancelled at each point of the buy.


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

    async def post_session(
        _c: object, _w: object, mints: list, *_a: object, **_k: object
    ) -> None:
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


async def _outcome(
    tmp: Path, name: str, settings: dict | None = None, **client: object
) -> tuple[_Run, list, dict]:
    """Run one buy to its outcome; the run, failure cleanups and the record."""
    token = _token(name)
    run = _Run(
        tmp / f"e-{name}.json",
        [token],
        cleanup_mode="on_fail",
        cleanup_force_close_with_burn=True,
        **(settings or {}),
    )
    if run.trader.positions.monitors:
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
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run.handled.wait(), 5.0)
        await _shut_down(task)
    return run, failed, run.records().get(str(token.mint), {})


async def _check_e(tmp: Path) -> list[str]:  # noqa: C901
    problems = []
    run, failed, record = await _outcome(tmp, "LANDED")
    if not record or "pending" in record or record["buy"]["amount_raw"] != RECEIVED_RAW:
        problems.append(f"landed: not rewritten with the amount read back: {record}")
    run, failed, record = await _outcome(
        tmp, "REVERTED", status=ConfirmationStatus.REVERTED
    )
    if record:
        problems.append(f"reverted: left a record: {record}")
    if len(failed) != 1:
        problems.append("reverted: not cleaned up as a failed buy")
    run, failed, record = await _outcome(
        tmp, "NOTSENT", send_error=TransactionNotSent("refused")
    )
    if record or run.client.sent:
        problems.append(f"never sent: left a record: {record}")
    if len(failed) != 1:
        problems.append("never sent: not cleaned up as a failed buy")
    run, failed, record = await _outcome(
        tmp, "AMBIGUOUS", send_error=TimeoutError("read timed out")
    )
    if not isinstance(record.get("pending"), dict):
        problems.append(f"send failed after signing: not kept pending: {record}")
    elif record["buy"]["tx_signature"] != run.client.signed[0]:
        problems.append(f"send failed after signing: wrong signature: {record}")
    if failed:
        problems.append("send failed after signing: cleaned up, which can burn it")
    run, failed, record = await _outcome(
        tmp, "UNKNOWN", status=ConfirmationStatus.UNCONFIRMED
    )
    if not isinstance(record.get("pending"), dict):
        problems.append(f"unconfirmed: not kept pending: {record}")
    if failed:
        problems.append("unconfirmed: cleaned up as a failed buy, which can burn it")
    run, failed, record = await _outcome(
        tmp,
        "MANUAL",
        {"exit_strategy": "manual"},
        status=ConfirmationStatus.UNCONFIRMED,
    )
    if run.records():
        problems.append(f"manual: a pending record was written: {run.records()}")
    if failed:
        problems.append("manual: an unconfirmed buy was cleaned up, which can burn it")
    return problems


def check_e_outcomes_without_shutdown(tmp: Path) -> bool:
    return _report(asyncio.run(_check_e(tmp)))


# F, G, H: the next start.


def _manager(path: Path, client: _Client, **settings: object) -> PositionManager:
    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "buy_amount": 0.003,
        "buy_slippage": 0.3,
        "sell_slippage": 0.3,
        "exit_strategy": "time_based",
    }
    manager = PositionManager(
        TraderSettings(**(fields | settings)),
        seller=_Seller(),
        solana_client=client,
        wallet=WALLET,
        priority_fee_manager=None,
        platform_implementations=SimpleNamespace(
            curve_market=None, graduated_market=None
        ),
        store=PositionStore(path),
    )
    manager._PENDING_RECHECK = 0.01  # noqa: SLF001
    return manager


def _holding(manager: PositionManager) -> dict[str, TradeResult]:
    """Replace the exit strategy with a recorder of what each position holds."""
    held: dict[str, TradeResult] = {}

    async def manage(token_info: TokenInfo, buy: TradeResult, **_k: object) -> None:
        held[token_info.symbol] = buy
        await asyncio.Event().wait()

    manager.manage = manage
    return held


def _store_pending(
    store: PositionStore,
    token: TokenInfo,
    *,
    last_valid: int | None = LAST_VALID,
    sent_ago: float = 0.0,
) -> str:
    signature = f"sig-{token.symbol}"
    store.add(
        token,
        TradeResult(
            success=False, amount=float(TOKENS), price=1.5e-4, tx_signature=signature
        ),
        datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=sent_ago),
        WALLET.pubkey,
        pending=PendingBuy(
            max_amount_raw=CAP_RAW,
            quote_destination=str(CURVE),
            last_valid_block_height=last_valid,
        ),
    )
    return signature


async def _resolve_quote(*_a: object) -> Pubkey:
    return TOKEN_PROGRAM


def _records(path: Path) -> dict[str, dict]:
    return {r["symbol"]: r for r in json.loads(path.read_text())["positions"]}


async def _check_f(tmp: Path) -> list[str]:
    names = (
        "LANDED REVERTED LATE LOST SHARED UNREAD WAITING NOHEIGHT NOHEIGHTNEW OLD "
        "SOLDHAND PARTIAL"
    )
    tokens = {name: _token(name) for name in names.split()}
    path = tmp / "f.json"
    store = PositionStore(path)
    store.add(
        tokens["OLD"],
        TradeResult(success=True, amount=1.0, amount_raw=OLD_RAW, price=1.0),
        datetime.now(UTC).replace(tzinfo=None),
        WALLET.pubkey,
    )
    client = _Client()
    client.block_height = LAST_VALID + 1  # every recorded blockhash has expired
    sigs = {}
    for name in (
        "LANDED",
        "REVERTED",
        "LOST",
        "SHARED",
        "UNREAD",
        "SOLDHAND",
        "PARTIAL",
    ):
        sigs[name] = _store_pending(store, tokens[name])
    for name in ("LATE", "WAITING"):  # blockhashes still alive
        sigs[name] = _store_pending(store, tokens[name], last_valid=10**9)
    sigs["NOHEIGHT"] = _store_pending(
        store, tokens["NOHEIGHT"], last_valid=None, sent_ago=200
    )
    sigs["NOHEIGHTNEW"] = _store_pending(store, tokens["NOHEIGHTNEW"], last_valid=None)
    client.outcomes = {
        sigs["LANDED"]: [ConfirmationStatus.SUCCESS],
        sigs["REVERTED"]: [ConfirmationStatus.REVERTED],
        # Not known on the first look, landed on a later one.
        sigs["LATE"]: [None, None, ConfirmationStatus.SUCCESS],
        sigs["SOLDHAND"]: [ConfirmationStatus.SUCCESS],
        sigs["PARTIAL"]: [ConfirmationStatus.SUCCESS],
    }
    for name in ("LANDED", "LATE"):  # landed seconds ago: not finalized yet
        client.hold(tokens[name], RECEIVED_RAW, fresh=True)
    client.hold(tokens["PARTIAL"], PARTIAL_RAW)
    client.hold(tokens["OLD"], OLD_RAW)
    client.hold(tokens["SHARED"], SHARED_RAW)
    client.unreadable.add(_account(tokens["UNREAD"]))

    manager = _manager(path, client)
    held = _holding(manager)
    with _patched(position_manager, "resolve_quote_token_program", _resolve_quote):
        await manager.resume()
        settled = {"LANDED", "LATE", "SHARED", "OLD", "PARTIAL"}
        cleared = {"REVERTED", "LOST", "NOHEIGHT", "SOLDHAND"}
        await _until(
            lambda: settled <= set(held) and not cleared & set(_records(path)),
            timeout=8.0,
        )
        await _until(lambda: client.lookups.get(sigs["WAITING"], 0) >= 3)  # noqa: PLR2004
        await manager.stop()
    return _resolved(held, _records(path), client, sigs)


def _landed(
    name: str, buy: TradeResult | None, record: dict, signature: str
) -> list[str]:
    """What check F found wrong with a pending buy that landed."""
    problems = []
    if buy is None or buy.amount_raw != RECEIVED_RAW:
        problems.append(f"{name}: not held at the transaction's amount: {buy}")
    elif buy.price is None or abs(buy.price - FILL_PRICE) > FILL_PRICE * 1e-9:
        problems.append(f"{name}: not priced at its fill {FILL_PRICE}: {buy.price}")
    if "pending" in record or record.get("buy", {}).get("amount_raw") != RECEIVED_RAW:
        problems.append(f"{name}: not rewritten as a landed position: {record}")
    lines = [t for t in _trade_lines() if t.get("tx_hash") == signature]
    if [t["action"] for t in lines] != ["buy"]:
        problems.append(f"{name}: no single trade-log buy line: {lines}")
    return problems


def _partial(partial: TradeResult | None) -> list[str]:
    """What check F found wrong with a landed buy partly sold by hand."""
    if partial is None or partial.amount_raw != PARTIAL_RAW:
        return [f"PARTIAL: not held at the {PARTIAL_RAW} raw left: {partial}"]
    if partial.price is None or abs(partial.price - FILL_PRICE) > FILL_PRICE * 1e-9:
        return [f"PARTIAL: priced at {partial.price}, its fill was {FILL_PRICE}"]
    return []


def _shared(shared: TradeResult | None) -> list[str]:
    """What check F found wrong with a buy known only by a shared balance."""
    if shared is None or shared.amount_raw != CAP_RAW:
        return [f"SHARED: not capped at what the buy can hold: {shared}"]
    if shared.price is not None:
        return [f"SHARED: priced off the estimate {shared.price}, not unknown"]
    return []


def _resolved(
    held: dict[str, TradeResult],
    records: dict[str, dict],
    client: _Client,
    sigs: dict[str, str],
) -> list[str]:
    """What check F found wrong with the positions held and the file left."""
    problems = []
    for name in ("LANDED", "LATE"):
        problems += _landed(name, held.get(name), records.get(name, {}), sigs[name])
    for name in ("REVERTED", "LOST", "NOHEIGHT", "SOLDHAND"):
        if name in records or name in held:
            problems.append(f"{name}: not cleared")
    problems += _partial(held.get("PARTIAL"))
    flags = client.history.get(sigs["LATE"], [])
    if flags[:1] != [True] or any(flags[1:]):
        problems.append(f"LATE: history searched {flags}, want the first lookup only")
    problems += _shared(held.get("SHARED"))
    for name in ("UNREAD", "WAITING", "NOHEIGHTNEW"):
        if not isinstance(records.get(name, {}).get("pending"), dict) or name in held:
            problems.append(
                f"{name}: did not stay pending, unheld: {records.get(name)}"
            )
    if client.lookups.get(sigs["WAITING"], 0) < 3:  # noqa: PLR2004
        problems.append("WAITING: its signature was not looked at again")
    if held.get("OLD") is None or held["OLD"].amount_raw != OLD_RAW:
        problems.append(f"OLD: a record without `pending` did not resume: {held}")
    return problems


def check_f_next_start_resolves_by_signature(tmp: Path) -> bool:
    return _report(asyncio.run(_check_f(tmp)))


async def _check_g(tmp: Path) -> list[str]:
    token = _token("FRESH")
    path = tmp / "g.json"
    PositionStore(path).add(
        token,
        TradeResult(success=True, amount=1.0, amount_raw=1_000_000, price=1.0),
        datetime.now(UTC).replace(tzinfo=None),
        WALLET.pubkey,
    )
    client = _Client()
    client.hold(token, 1_000_000, fresh=True)  # landed, not finalized yet
    manager = _manager(path, client)
    held = _holding(manager)
    with _patched(position_manager, "resolve_quote_token_program", _resolve_quote):
        resumed = await manager.resume()
        await asyncio.sleep(0.05)
        await manager.stop()
    problems = []
    if [t.symbol for t in resumed] != ["FRESH"] or "FRESH" not in held:
        problems.append("a coin held at confirmed, not yet finalized, was not resumed")
    if "FRESH" not in _records(path):
        problems.append("a coin held at confirmed was cleared")
    return problems


def check_g_balances_read_at_confirmed(tmp: Path) -> bool:
    return _report(asyncio.run(_check_g(tmp)))


async def _check_h(tmp: Path) -> list[str]:
    problems = []
    token = _token("NOFILL")
    path = tmp / "h.json"
    manager = _manager(
        path,
        _Client(),
        exit_strategy="tp_sl",
        take_profit_percentage=0.5,
        stop_loss_percentage=0.2,
        price_check_interval=0.01,
    )
    manager._PRICE_WARN_EVERY = 0.05  # noqa: SLF001
    feed = {"state": "down", "price": 1.0}
    reads: list[str] = []

    async def get_price(_token_info: TokenInfo) -> float:
        reads.append(feed["state"])
        if feed["state"] == "down":
            raise RuntimeError("feed down")  # noqa: TRY003
        return 0.0 if feed["state"] == "zero" else feed["price"]

    manager.platform_implementations = SimpleNamespace(
        curve_market=SimpleNamespace(get_price=get_price), graduated_market=None
    )
    manager.store.load()
    bought = TradeResult(success=True, amount=1.0, amount_raw=OLD_RAW, price=None)
    now = datetime.now(UTC).replace(tzinfo=None)
    manager.store.add(token, bought, now, WALLET.pubkey)
    with _logged() as logs:
        task = asyncio.create_task(manager.manage(token, bought, entry_time=now))
        await asyncio.sleep(0.3)
        feed["state"] = "zero"  # a curve with nothing in it prices at 0.0
        await asyncio.sleep(0.1)
        if manager.seller.sells:
            problems.append(
                f"sold before a real price was known: {manager.seller.sells}"
            )
        feed["state"] = "up"
        await _until(lambda: _records(path)["NOFILL"]["buy"]["price"] is not None)
        kept = _records(path)["NOFILL"]["buy"]["price"]
        feed["price"] = 0.7  # 30 % under the first real price
        await _until(lambda: bool(manager.seller.sells))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if manager.seller.sells != [("NOFILL", 0.7)]:
        problems.append(
            f"stop loss did not fire off the first real price: {manager.seller.sells}"
        )
    if kept != 1.0:
        problems.append(f"the first real price was not kept in the file: {kept}")
    if logs.count("fill price of NOFILL is unknown") != 1:
        problems.append("the unknown fill price was not said exactly once")
    down = reads.count("down")
    still = logs.count("Still no price for NOFILL")
    if not 1 <= still < down // 2:
        problems.append(f"{still} repeat warnings over {down} failed reads")
    return problems


def check_h_unknown_fill_waits_for_a_price(tmp: Path) -> bool:
    return _report(asyncio.run(_check_h(tmp)))


# I: an unconfirmed buy resolved in the same run.


async def _check_i(tmp: Path) -> list[str]:
    problems = []
    late, gone = _token("LATE"), _token("GONE")
    run = _Run(tmp / "i.json", [late])
    run.client.hold(late, RECEIVED_RAW)
    run.hold_positions()
    run.trader.positions._PENDING_RECHECK = 0.01  # noqa: SLF001
    run.client.status = ConfirmationStatus.UNCONFIRMED
    run.client.outcome = None
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: bool(run.client.sent)):
            return ["the buy was never sent"]
        signature = run.client.sent[0]
        await _until(lambda: run.client.lookups.get(signature, 0) >= 2)  # noqa: PLR2004
        run.client.outcomes[signature] = [ConfirmationStatus.SUCCESS]
        held = await _until(lambda: f"hold LATE {RECEIVED_RAW}" in run.events)
        await _shut_down(task)
    if not held:
        problems.append(f"a buy that showed up later was not held: {run.events}")
    record = run.records().get(str(late.mint), {})
    if "pending" in record or record.get("buy", {}).get("amount_raw") != RECEIVED_RAW:
        problems.append(f"it was not rewritten as a landed position: {record}")
    if not any(t.get("tx_hash") == signature for t in _trade_lines()):
        problems.append("no trade-log buy line for it")

    run = _Run(tmp / "i2.json", [gone])
    run.hold_positions()
    run.trader.positions._PENDING_RECHECK = 0.01  # noqa: SLF001
    run.client.status = ConfirmationStatus.UNCONFIRMED
    run.client.outcome = None
    run.client.block_height = LAST_VALID + 1
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        cleared = await _until(
            lambda: bool(run.client.sent) and not run.records(), timeout=6.0
        )
        await _shut_down(task)
    if not cleared:
        problems.append(f"an expired buy with nothing held stayed: {run.records()}")
    return problems + await _check_i_cleanup(tmp)


async def _check_i_cleanup(tmp: Path) -> list[str]:
    """A buy resolved in the run, then sold, is cleaned up after the session."""
    token = _token("TRACKED")
    run = _Run(tmp / "i3.json", [token], cleanup_mode="post_session")
    run.client.hold(token, RECEIVED_RAW)
    run.trader.positions._PENDING_RECHECK = 0.01  # noqa: SLF001
    run.client.status = ConfirmationStatus.UNCONFIRMED
    run.client.outcome = ConfirmationStatus.SUCCESS
    handed: list[Pubkey] = []

    async def post_session(
        _c: object, _w: object, mints: list, *_a: object, **_k: object
    ) -> None:
        handed.extend(mints)

    with (
        _stubbed_market(),
        _patched(universal_trader, "handle_cleanup_post_session", post_session),
    ):
        task = asyncio.create_task(run.trader.start())
        sold = await _until(lambda: bool(run.trader.positions.seller.sells))
        await _shut_down(task)
    if not sold:
        return ["a buy resolved in the run was never sold"]
    if token.mint not in handed:
        return [f"post-session cleanup was not handed its account: {handed}"]
    return []


def check_i_resolved_in_the_same_run(tmp: Path) -> bool:
    return _report(asyncio.run(_check_i(tmp)))


async def _check_p(tmp: Path) -> list[str]:
    """A buy still being resolved holds its position slot."""
    first, second = _token("FIRST"), _token("SECOND")
    run = _Run(tmp / "p.json", [first, second])
    run.hold_positions()
    run.trader.positions._PENDING_RECHECK = 0.01  # noqa: SLF001
    run.client.status = ConfirmationStatus.UNCONFIRMED
    run.client.outcome = None  # blockhash alive: it stays unresolved
    with _stubbed_market():
        task = asyncio.create_task(run.trader.start())
        if not await _until(lambda: bool(run.client.sent)):
            return ["the first buy was never sent"]
        await asyncio.sleep(0.3)
        sent = list(run.client.sent)
        await _shut_down(task)
    problems = []
    if len(sent) != 1:
        problems.append(f"bought a second coin while the first was unresolved: {sent}")
    if not isinstance(run.records().get(str(first.mint), {}).get("pending"), dict):
        problems.append(f"the unresolved buy did not stay pending: {run.records()}")
    return problems


def check_p_pending_buy_holds_a_slot(tmp: Path) -> bool:
    return _report(asyncio.run(_check_p(tmp)))


async def _check_q(tmp: Path) -> list[str]:
    """A node behind the slot a decision rests on keeps the record."""
    landed, expired = _token("LANDEDLAG"), _token("EXPIREDLAG")
    path = tmp / "q.json"
    store = PositionStore(path)
    sig_landed = _store_pending(store, landed)
    _store_pending(store, expired)
    client = _Client()
    client.block_height = LAST_VALID + 1
    client.outcomes = {sig_landed: [ConfirmationStatus.SUCCESS]}
    for token in (landed, expired):
        client.hold(token, RECEIVED_RAW)
        client.behind.add(_account(token))  # the node has not seen them yet
    client.node_slot = LANDED_SLOT - 1
    manager = _manager(path, client)
    held = _holding(manager)
    with _patched(position_manager, "resolve_quote_token_program", _resolve_quote):
        await manager.resume()
        await _until(lambda: manager.open_count == 0, timeout=8.0)
        await manager.stop()
    records = _records(path)
    problems = []
    for name in ("LANDEDLAG", "EXPIREDLAG"):
        if not isinstance(records.get(name, {}).get("pending"), dict):
            problems.append(f"{name}: a node behind cleared it: {records.get(name)}")
        if name in held:
            problems.append(f"{name}: held off a node behind")
    return problems


async def _check_q_height_unreadable(tmp: Path) -> list[str]:
    """A stored height that cannot be read is retried, not replaced by the clock.

    Its warning names the coin, once, then once every `_PRICE_WARN_EVERY`.
    """
    problems = []
    warning = "Could not read the block height to settle the buy of NOHEIGHT"
    for every, least, most in ((60.0, 1, 1), (0.2, 3, 7)):
        token = _token("NOHEIGHT")
        path = tmp / f"q-height-{every}.json"
        _store_pending(PositionStore(path), token, sent_ago=600)  # clock: long gone
        client = _Client()
        client.height_error = RuntimeError("getEpochInfo failed")
        client.hold(token, RECEIVED_RAW)  # the clock path would hold it off this
        manager = _manager(path, client)
        manager._PRICE_WARN_EVERY = every  # noqa: SLF001
        held = _holding(manager)
        with (
            _patched(position_manager, "resolve_quote_token_program", _resolve_quote),
            _logged() as logs,
        ):
            await manager.resume()
            await asyncio.sleep(1.0)  # about a hundred failed height reads
            await manager.stop()
        record = _records(path).get("NOHEIGHT", {})
        if not isinstance(record.get("pending"), dict) or "NOHEIGHT" in held:
            problems.append(f"NOHEIGHT: settled without a block height: {record}")
        if not least <= logs.count(warning) <= most:
            problems.append(
                f"NOHEIGHT, a warning every {every:g}s: {logs.count(warning)} "
                f"warnings naming the coin in 1s"
            )
    return problems


def check_q_lagging_node_keeps_the_record(tmp: Path) -> bool:
    async def both() -> list[str]:
        return await _check_q(tmp) + await _check_q_height_unreadable(tmp)

    return _report(asyncio.run(both()))


async def _check_o(_tmp: Path) -> list[str]:
    """How the client reads a signature's status and an account at a slot."""
    problems = []
    client = object.__new__(SolanaClient)
    answers: list[dict] = []
    bodies: list[dict] = []

    async def post_rpc(body: dict, **_k: object) -> dict:
        bodies.append(body)
        return answers.pop(0)

    client.post_rpc = post_rpc

    def status(confirmation: str, err: object = None) -> dict:
        value = {"slot": 77, "err": err, "confirmationStatus": confirmation}
        return {"result": {"value": [value]}}

    cases = (
        ("processed", status("processed"), ConfirmationStatus.UNCONFIRMED),
        (
            "processed, err",
            status("processed", {"x": 1}),
            ConfirmationStatus.UNCONFIRMED,
        ),
        ("confirmed, err", status("confirmed", {"x": 1}), ConfirmationStatus.REVERTED),
        ("confirmed", status("confirmed"), ConfirmationStatus.SUCCESS),
        ("finalized", status("finalized"), ConfirmationStatus.SUCCESS),
        ("unknown", {"result": {"value": [None]}}, None),
    )
    for label, answer, want in cases:
        answers.append(answer)
        got, slot = await client.get_signature_outcome("sig", search_history=False)
        if got is not want:
            problems.append(f"status {label}: {got}, want {want}")
        if want is not None and slot != 77:  # noqa: PLR2004
            problems.append(f"status {label}: slot {slot}, want 77")
    flags = [b["params"][1]["searchTransactionHistory"] for b in bodies]
    if any(flags):
        problems.append(f"searched the history when told not to: {flags}")
    return problems + await _account_at_slot(client, answers, bodies)


async def _account_at_slot(
    client: SolanaClient, answers: list[dict], bodies: list[dict]
) -> list[str]:
    """Check O's account reads at a minimum slot, on the same stub RPC."""
    problems = []
    account = Pubkey.new_unique()
    answers.append({"error": {"code": -32016, "message": "Minimum context slot"}})
    try:
        await client.get_account_info(account, min_context_slot=500)
        problems.append("a node behind the slot answered instead of failing")
    except ValueError:
        problems.append("a node behind the slot read as 'not found'")
    except RuntimeError:
        pass
    if bodies[-1]["params"][1].get("minContextSlot") != 500:  # noqa: PLR2004
        problems.append(f"minContextSlot not sent: {bodies[-1]}")
    answers.append({"result": {"context": {"slot": 501}, "value": None}})
    try:
        await client.get_account_info(account, min_context_slot=500)
        problems.append("a missing account did not raise")
    except ValueError:
        pass
    return problems


def check_o_client_reads_status_and_slot(tmp: Path) -> bool:
    return _report(asyncio.run(_check_o(tmp)))


# J, K, L: a sold position closing its account at shutdown.


class _Close:
    """Stands in for the after-sell account close: a settle wait, then the close."""

    def __init__(self, settle: float = 0.3) -> None:
        self.settle = settle
        self.waiting: list[str] = []
        self.closed: list[str] = []
        self.cancelled: list[str] = []

    async def __call__(
        self, _c: object, _w: object, mint: Pubkey, *_args: object, **_k: object
    ) -> None:
        self.waiting.append(str(mint))
        try:
            await asyncio.sleep(self.settle)
        except asyncio.CancelledError:
            self.cancelled.append(str(mint))
            raise
        self.closed.append(str(mint))


async def _check_j(tmp: Path) -> list[str]:
    problems = []
    token = _token("SOLD")
    run = _Run(tmp / "j.json", [token], cleanup_mode="after_sell")
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
    if not logs.has("Waiting for 1 position(s) to close their token account"):
        problems.append("the shutdown log does not say it waits for the close")
    if run.records():
        problems.append(f"the sold position is still in the file: {run.records()}")
    return problems


def check_j_close_after_sell_finishes(tmp: Path) -> bool:
    return _report(asyncio.run(_check_j(tmp)))


async def _check_k(tmp: Path) -> list[str]:
    problems = []
    sold, held = _token("SOLD"), _token("HELD")
    path = tmp / "k.json"
    manager = _manager(path, _Client(), cleanup_mode="after_sell")
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


def check_k_only_held_positions_are_stopped(tmp: Path) -> bool:
    return _report(asyncio.run(_check_k(tmp)))


async def _check_l(tmp: Path) -> list[str]:
    problems = []
    token = _token("HANG")
    run = _Run(tmp / "l.json", [token], cleanup_mode="after_sell")
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
    record = run.records().get(str(token.mint))
    if record is None or record.get("sold") is not True:
        problems.append(f"the cut-short close left no sold record: {record}")
        return problems
    saved = run.path.read_text()
    problems += await _next_start_closes(run.path, token)
    run.path.write_text(saved)
    return problems + await _next_start_closes(run.path, token, "manual")


async def _next_start_closes(
    path: Path, token: TokenInfo, exit_strategy: str = "time_based"
) -> list[str]:
    """A fresh start closes a sold record's account, sells nothing, drops it."""
    closed: list[Pubkey] = []

    async def cleared(_c: object, _w: object, mint: Pubkey, *_a: object) -> None:
        closed.append(mint)

    manager = _manager(
        path, _Client(), cleanup_mode="after_sell", exit_strategy=exit_strategy
    )
    with _patched(position_manager, "handle_cleanup_cleared", cleared):
        resumed = await manager.resume()
        await manager.wait_all()
    problems = []
    if resumed or manager.seller.sells:
        problems.append(f"{exit_strategy}: resumed {resumed} / sold again")
    if closed != [token.mint]:
        problems.append(f"{exit_strategy}: the next start closed {closed}")
    if PositionStore(path).load():
        problems.append(f"{exit_strategy}: the sold record is still in the file")
    return problems


def check_l_hanging_close_is_bounded(tmp: Path) -> bool:
    return _report(asyncio.run(_check_l(tmp)))


# M: what counts as never sent.


def _wrapped(cause: Exception) -> Exception:
    """An error as solana-py raises it: a wrapper whose cause is the HTTP error."""
    error = RuntimeError(f"{type(cause).__name__} raised in SendTransaction")
    error.__cause__ = cause
    return error


def _status_error(code: int) -> Exception:
    request = httpx2.Request("POST", "https://node.invalid")
    return _wrapped(
        httpx2.HTTPStatusError(
            "status", request=request, response=httpx2.Response(code, request=request)
        )
    )


async def _send_raises(errors: list[Exception]) -> tuple[type, list[tuple]]:
    """Run the real send with every attempt failing; what it raised and signed."""
    client = object.__new__(SolanaClient)
    client._cached_last_valid_block_height = 4242  # noqa: SLF001
    client._rate_limiter = SimpleNamespace(acquire=_empty)  # noqa: SLF001
    script = list(errors)

    async def send_transaction(*_a: object, **_k: object) -> None:
        raise script.pop(0)

    async def get_client() -> SimpleNamespace:
        return SimpleNamespace(send_transaction=send_transaction)

    async def blockhash() -> Hash:
        return Hash.default()

    client.get_client = get_client
    client.get_cached_blockhash = blockhash
    signed: list[tuple] = []
    with _patched(asyncio, "sleep", _empty):
        try:
            await client.build_and_send_transaction(
                [],
                Keypair(),
                max_retries=len(errors),
                on_signed=lambda sig, height: signed.append((sig, height)),
            )
        except Exception as e:  # noqa: BLE001 - the type is the answer
            return type(e), signed
    return type(None), signed


async def _check_m(_tmp: Path) -> list[str]:
    problems = []
    refused = RPCException("Blockhash not found")
    connect = _wrapped(httpx2.ConnectError("refused"))
    read = _wrapped(httpx2.ReadTimeout("timed out"))
    cases = (
        ("node refusals", [refused, refused], True),
        ("no connection", [connect, connect], True),
        ("HTTP 429", [_status_error(429), refused], True),
        ("a read timeout, then a refusal", [read, refused], False),
        ("a refusal, then a read timeout", [refused, read], False),
        ("HTTP 502", [_status_error(502), connect], False),
    )
    with _logged():
        for label, errors, never in cases:
            raised, signed = await _send_raises(errors)
            if (raised is TransactionNotSent) is not never:
                want = "TransactionNotSent" if never else "the attempt's own error"
                problems.append(f"{label}: raised {raised.__name__}, want {want}")
            if len(signed) != 1 or signed[0][1] != 4242:  # noqa: PLR2004
                problems.append(f"{label}: signer reported {signed}, want one, height")
    return problems


def check_m_never_sent_means_never_sent(tmp: Path) -> bool:
    return _report(asyncio.run(_check_m(tmp)))


# N: shutdown names what is still pending.


async def _check_n(tmp: Path) -> list[str]:
    problems = []
    landed, pending = _token("LANDED"), _token("PENDING")
    path = tmp / "n.json"
    manager = _manager(path, _Client())
    manager.store.load()
    manager.store.add(
        landed,
        TradeResult(success=True, amount=1.0, amount_raw=1, price=1.0),
        datetime.now(UTC).replace(tzinfo=None),
        WALLET.pubkey,
    )
    _store_pending(manager.store, pending)
    if manager.store.pending_mints() != {str(pending.mint)}:
        problems.append(f"pending_mints: {manager.store.pending_mints()}")
    with _logged() as logs:
        await manager.stop()
    lines = [line for line in logs.lines if "sent but not confirmed" in line]
    if len(lines) != 1 or str(pending.mint) not in lines[0]:
        problems.append(f"shutdown did not name the pending buy: {logs.lines}")
    elif str(landed.mint) in lines[0]:
        problems.append(f"shutdown called a landed position pending: {lines[0]}")
    return problems


def check_n_shutdown_names_pending_buys(tmp: Path) -> bool:
    return _report(asyncio.run(_check_n(tmp)))


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
                ("A: cancelled while the buy confirms, kept pending", check_a_cancelled_during_confirmation),
                ("B: cancelled mid-send, kept with its signature", check_b_cancelled_mid_send),
                ("C: cancelled before signing, nothing kept", check_c_cancelled_before_signing),
                ("D: single-token mode keeps it too", check_d_single_token_mode),
                ("E: each outcome of a buy, and manual", check_e_outcomes_without_shutdown),
                ("F: the next start resolves pending buys by signature", check_f_next_start_resolves_by_signature),
                ("G: restart reads balances at confirmed", check_g_balances_read_at_confirmed),
                ("H: an unknown fill waits for a real price", check_h_unknown_fill_waits_for_a_price),
                ("I: an unconfirmed buy is resolved in the same run", check_i_resolved_in_the_same_run),
                ("J: a sold coin's close finishes at shutdown", check_j_close_after_sell_finishes),
                ("K: only held positions are stopped", check_k_only_held_positions_are_stopped),
                ("L: a hanging close is bounded", check_l_hanging_close_is_bounded),
                ("M: SUBMIT_FAILED only when nothing can have left", check_m_never_sent_means_never_sent),
                ("N: shutdown names the pending buys, only those", check_n_shutdown_names_pending_buys),
                ("O: the client reads status, slot and accounts at a slot", check_o_client_reads_status_and_slot),
                ("P: a buy being resolved holds its slot", check_p_pending_buy_holds_a_slot),
                ("Q: a node behind cannot clear a buy", check_q_lagging_node_keeps_the_record),
            ]  # fmt: skip
            results = []
            for name, check in checks:
                try:
                    ok = check(tmp)
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

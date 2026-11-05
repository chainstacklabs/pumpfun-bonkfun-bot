"""Verify the logs name each coin and its venue, and say what happened to a trade.

What a trader reading the log could not tell:

- A LaunchLab detection was `New token detected: <name> (<sym>) on launchlab`,
  with no mint and no platform config, and two pump.fun coins with one name
  read as one coin detected twice.
- The pump.fun event parser wrote about ten INFO lines for every coin on the
  stream, burying the trading lines.
- A buy in a coin's PumpSwap pool was logged as `... on pump_fun`, and nothing
  in `trades.log` said where a trade went.
- A buy that landed and reverted was reported as `Transaction failed to
  confirm`, with its error on another line.
- A pending buy found reverted after a restart was cleared without its error,
  and a revert whose transaction a lagging node could not serve yet was
  reported as `failed to confirm` although its signature status said it
  reverted.
- The shutdown line said pending buys are resolved from the wallet's balance;
  they are settled by signature.
- The blocks listener reported only the first create in a block.
- The README and the shipped YAML left out `launchlab`, `shreds` and
  `timing.token_wait_timeout`.

Fixtures: pump.fun creates (`cookbook/pumpfun/decode/`: two getTransaction
results, two SubscribeDeshred frames) and LaunchLab launches under letsbonk,
StonkFun and other launchpads (`raw_launchlab_launches_*`,
`raw_stonkfun_launches_*`, `raw_launchlab_creates_from_deshred.json`, and
`recorded_letsbonk_launches_from_gettransaction.json`, two letsbonk launches
as `getTransaction` returns them), each
played as its stream delivers it through the listener's real loop. The revert
is buy `3BP2RDMc…` (`meta.err` Custom 6002), as `getTransaction` returns it
(`recorded_reverts_from_gettransaction.json`).

Offline machine checks, no network and no funds moved:

  A. Every listener on every platform it supports (pump.fun: logs, blocks,
     geyser, shreds; launchlab, lets_bonk, stonk_fun: blocks, geyser, shreds)
     logs each coin it reports in one INFO line naming the mint, the platform
     and the creator; for pump.fun also the curve, for LaunchLab the platform
     config and the quote.
  B. On every stream, pump.fun's and LaunchLab's alike, no INFO line but that
     one names the coin and no parser logs at INFO: the pump.fun parser's
     per-coin detail is at DEBUG.
  C. A coin is reported once per mint: every frame delivered again, and the
     same create in a transaction with another signature, gives no second line
     and no second callback, on every listener, while every coin with its own
     mint is still reported, also when one block holds several creates.
  D. The real buyer and seller on a bonding curve, in PumpSwap and in Raydium
     CPMM name the venue in their log lines (the tp_sl exit line included),
     results and the pending-buy record;
     the trader's buy row, the position manager's sell row and the row of a
     pending buy resolved after a restart carry it as `venue` in `trades.log`,
     with `platform` unchanged.
  E. A buy and a sell that landed and reverted are reported as reverted with the
     transaction's error, in one line, not as "failed to confirm", also when the
     node cannot serve the transaction yet but the confirmed signature status
     shows the error (an error at `processed` is not taken), its code then named,
     hedged, from the IDL of its venue's program (none for CPMM); the client's own
     line says the same; one never confirmed still says it failed to confirm; a
     pending buy found reverted after a restart is cleared naming its error.
  F. The shutdown line for pending buys says the next start settles them by
     signature.
  G. The README documents `timing.token_wait_timeout` with the default the
     runner uses and `venue` with each venue's name; each shipped YAML lists
     every platform in its `platform` options and, at `listener_type`, every
     listener its platform supports and no other.
  H. On every listener, a create the name or creator filter refused does not
     hide a later attempt at the same mint that passes: that one reaches the
     callback, a third does not, and the coin's detection line is logged once.

Usage:
    uv run tests/regression/verify_logs_name_coin_and_venue.py
"""

# The listeners' entry points open sockets and gRPC streams; these checks swap
# those for scripted ones and drive the real loops. The trader and position
# manager are driven through their own methods with stub collaborators.
# ruff: noqa: SLF001

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
import traceback
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import websockets  # noqa: E402
from _fakes import _FakeChannel, _FakeSocket, _FeedEnd  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.rpc.responses import GetSignatureStatusesResp  # noqa: E402
from solders.signature import Signature  # noqa: E402

from config_loader import PLATFORM_LISTENER_COMPATIBILITY  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from platforms.curve_market import CurveMarket  # noqa: E402
from platforms.launchlab.cpmm_market import CpmmMarket  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.pumpswap_market import PumpSwapMarket  # noqa: E402
from trading import base, platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

HERE = Path(__file__).resolve().parent
DECODE = PROJECT_ROOT / "cookbook/pumpfun/decode"
PUMP_TXS = (
    DECODE / "raw_create_v2_with_fee_bps_from_gettransaction.json",
    DECODE / "raw_create_v2_omitted_fee_bps_from_gettransaction.json",
)
PUMP_DESHRED = (
    DECODE / "raw_create_v2_ordinary_from_deshred.json",
    DECODE / "raw_create_v2_holder_reward_from_deshred.json",
)
LAUNCHLAB_TXS = (
    HERE / "raw_launchlab_launches_from_gettransaction.json",
    HERE / "raw_stonkfun_launches_from_gettransaction.json",
    HERE / "recorded_letsbonk_launches_from_gettransaction.json",
)
LAUNCHLAB_STREAMS = HERE / "raw_launchlab_creates_from_deshred.json"
# Buy 3BP2RDMcyNk4s4pZttVUf3jnVvV5BWphMv2eeuHeBMYgWijgxtyRJeUKFKci3EqCfWPgB8njjccSAZmgDw4PMN6R,
# a pump.fun buy_v2 that landed and reverted on slippage, as `getTransaction`
# returns it, and its `meta.err`.
REVERTED_TX = next(
    entry["transaction"]
    for entry in json.loads(
        (HERE / "recorded_reverts_from_gettransaction.json").read_text()
    )
    if entry["signature"].startswith("3BP2RDMc")
)
REVERT_ERR = REVERTED_TX["meta"]["err"]
# What each venue's revert of REVERT_ERR is named when its signature status
# shows it and the transaction cannot be read: from the IDL of the program the
# trade went to, hedged, since a program it called may have raised it; CPMM has
# no IDL here.
STATUS_NAMES = {
    "curve": "pump.fun IDL, if pump.fun raised it: 6002 TooMuchSolRequired",
    "pumpswap": "PumpSwap IDL, if PumpSwap raised it: 6002 ZeroQuoteAmount",
    "cpmm": None,
}
TOKEN = 10**6
# The venue names `trades.log` and the README use, as each market class gives it.
WANTED_VENUES = {
    "curve": "bonding curve",
    "pumpswap": "PumpSwap",
    "cpmm": "Raydium CPMM",
}


def _venue_names() -> dict[str, str | None]:
    classes = {"curve": CurveMarket, "pumpswap": PumpSwapMarket, "cpmm": CpmmMarket}
    return {label: getattr(cls, "name", None) for label, cls in classes.items()}


# --- log capture ----------------------------------------------------------------


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def info(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.INFO]

    def lines(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def _captured(run):  # noqa: ANN001, ANN202
    """Run `run()` in a fresh folder; return its result, trades.log rows and records."""
    records = _Records()
    root = logging.getLogger()
    root.addHandler(records)
    level = root.level
    root.setLevel(logging.DEBUG)
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
            root.removeHandler(records)
            root.setLevel(level)
    return out, rows, records


# --- frames ---------------------------------------------------------------------


def _load(path: Path) -> object:
    return json.loads(path.read_text())


def _logs_frame(tx: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": tx.get("slot", 1)},
                "value": {
                    "signature": tx["transaction"]["signatures"][0],
                    "err": tx["meta"]["err"],
                    "logs": tx["meta"]["logMessages"],
                },
            },
            "subscription": 1,
        },
    }


def _block_frame(tx: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "blockNotification",
        "params": {
            "result": {
                "value": {
                    "slot": tx.get("slot", 1),
                    "block": {
                        "transactions": [
                            {
                                "transaction": tx["transaction"],
                                "meta": tx["meta"],
                                "version": tx.get("version"),
                            }
                        ]
                    },
                }
            },
            "subscription": 1,
        },
    }


def _geyser_frame(tx: dict) -> geyser_pb2.SubscribeUpdate:
    """The executed transaction as geyser's Subscribe stream carries it."""
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    info.signature = base58.b58decode(tx["transaction"]["signatures"][0])
    message = info.transaction.message
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    meta = tx["meta"]
    info.meta.log_messages.extend(meta.get("logMessages") or [])
    loaded = meta.get("loadedAddresses") or {}
    info.meta.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.meta.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    for group in meta.get("innerInstructions") or []:
        inner = info.meta.inner_instructions.add()
        inner.index = group["index"]
        for ix in group["instructions"]:
            compiled = inner.instructions.add()
            compiled.program_id_index = ix["programIdIndex"]
            compiled.accounts = bytes(ix["accounts"])
            compiled.data = base58.b58decode(ix["data"])
    return update


def _parsed(message_cls, encoded: str):  # noqa: ANN001, ANN202
    update = message_cls()
    update.ParseFromString(base64.b64decode(encoded))
    return update


def _frames(platform: Platform, kind: str) -> list:
    """Every recorded create this listener kind is fed, in stream form."""
    if platform is Platform.PUMP_FUN:
        if kind == "shreds":
            return [
                _parsed(
                    geyser_pb2.SubscribeUpdateDeshred,
                    _load(path)["subscribe_update_base64"],
                )
                for path in PUMP_DESHRED
            ]
        txs = [_load(path) for path in PUMP_TXS]
    else:
        streams = _load(LAUNCHLAB_STREAMS)["launches"]
        if kind == "shreds":
            return [
                _parsed(geyser_pb2.SubscribeUpdateDeshred, s["deshred_update_base64"])
                for s in streams
            ]
        txs = [tx for path in LAUNCHLAB_TXS for tx in _load(path)]
        if kind == "geyser":
            recorded = [
                _parsed(geyser_pb2.SubscribeUpdate, s["geyser_update_base64"])
                for s in streams
            ]
            # The same launch from both sources is one coin, reported once.
            return recorded + [_geyser_frame(tx) for tx in txs]
    build = {"logs": _logs_frame, "blocks": _block_frame, "geyser": _geyser_frame}
    return [build[kind](tx) for tx in txs]


# --- scripted transports --------------------------------------------------------


class _FakeStub:
    """A geyser stub whose first stream plays frames, then ends."""

    def __init__(self, updates: list) -> None:
        self.updates = updates

    def _stream(self, _requests):  # noqa: ANN001, ANN202
        updates, self.updates = self.updates, []

        async def stream():  # noqa: ANN202
            for update in updates:
                yield update
            raise _FeedEnd

        return stream()

    Subscribe = _stream
    SubscribeDeshred = _stream


def _play(platform: Platform, kind: str, frames: list) -> SimpleNamespace:
    """Run one listener's real loop over `frames`; what it reported and logged."""
    listener = ListenerFactory.create_listener(
        listener_type=kind,
        wss_endpoint="wss://rpc.offline.invalid",
        geyser_endpoint="geyser.offline.invalid:443",
        geyser_api_token="unused",  # noqa: S106 — never sent; nothing connects
        platforms=[platform],
    )
    tokens: list[TokenInfo] = []

    async def on_token(token: TokenInfo) -> None:
        tokens.append(token)

    async def connection():  # noqa: ANN202
        return _FakeStub(frames), _FakeChannel()

    listener._create_geyser_connection = connection
    original_connect = websockets.connect
    feeds = deque([frames])
    websockets.connect = lambda *_a, **_k: _FakeSocket(feeds.popleft() if feeds else [])

    async def go() -> None:
        try:
            await listener.listen_for_tokens(on_token)
        except _FeedEnd:
            pass

    try:
        _, _, records = _captured(lambda: asyncio.run(go()))
    finally:
        websockets.connect = original_connect
    return SimpleNamespace(tokens=tokens, records=records)


def _resigned(kind: str, frame: object) -> object:
    """The same create in a transaction with a new signature."""
    signature = Signature.new_unique()
    if kind in ("logs", "blocks"):
        frame = json.loads(json.dumps(frame))
        if kind == "logs":
            frame["params"]["result"]["value"]["signature"] = str(signature)
        else:
            for tx in frame["params"]["result"]["value"]["block"]["transactions"]:
                tx["transaction"]["signatures"][0] = str(signature)
        return frame
    copy = type(frame)()
    copy.CopyFrom(frame)
    info = (
        copy.deshred_transaction.transaction
        if kind == "shreds"
        else copy.transaction.transaction
    )
    info.signature = bytes(signature)
    if info.transaction.signatures:
        info.transaction.signatures[0] = bytes(signature)
    return copy


def _streams() -> list[tuple[Platform, str]]:
    return [
        (platform, kind)
        for platform, kinds in PLATFORM_LISTENER_COMPATIBILITY.items()
        for kind in kinds
    ]


def _report(name: str, bad: list[str]) -> bool:
    for line in bad:
        print(f"    {name}: {line}")
    return not bad


# --- A, B, C: detection ---------------------------------------------------------


def _detection_problems(where: str, token: TokenInfo, line: str) -> list[str]:
    bad = []
    if not line.startswith("New token detected"):
        bad.append(f"{where}: not a detection line: {line!r}")
    wanted = [f"mint {token.mint}", f"on {token.platform.value}:"]
    creator = token.creator or token.user
    wanted.append(f"creator {creator}")
    if token.platform is Platform.PUMP_FUN:
        wanted.append(f"curve {token.bonding_curve}")
    else:
        wanted += [f"platform config {token.platform_config}", "quote "]
    bad += [
        f"{where}: {want!r} missing from {line!r}"
        for want in wanted
        if want not in line
    ]
    return bad


def check_a_one_line_names_the_coin() -> bool:
    """A: each listener logs a coin in one INFO line naming mint, platform and more."""
    bad = []
    for platform, kind in _streams():
        where = f"{platform.value}/{kind}"
        run = _play(platform, kind, _frames(platform, kind))
        if not run.tokens:
            bad.append(f"{where}: no coin detected from the fixtures")
        for token in run.tokens:
            lines = [
                r.getMessage()
                for r in run.records.info()
                if r.getMessage().startswith("New token detected")
                and str(token.mint) in r.getMessage()
            ]
            if len(lines) != 1:
                named = [
                    r.getMessage()
                    for r in run.records.info()
                    if r.getMessage().startswith("New token detected")
                ]
                bad.append(
                    f"{where}: {len(lines)} detection lines name {token.mint}; got {named[:2]}"
                )
                continue
            bad += _detection_problems(where, token, lines[0])
            if kind == "shreds" and "pre-execution" not in lines[0]:
                bad.append(f"{where}: does not say pre-execution: {lines[0]!r}")
    return _report("A", bad)


def check_b_no_other_info_line_per_coin() -> bool:
    """B: no other INFO line names the coin; the parsers' detail is at DEBUG."""
    bad = []
    for platform, kind in _streams():
        where = f"{platform.value}/{kind}"
        run = _play(platform, kind, _frames(platform, kind))
        info = run.records.info()
        parser_info = [r.getMessage() for r in info if r.name.startswith("platforms.")]
        if parser_info:
            bad.append(
                f"{where}: {len(parser_info)} INFO lines from the parsers, e.g. {parser_info[0]!r}"
            )
        if not run.tokens:
            bad.append(f"{where}: no coin detected, nothing to check")
        for token in run.tokens:
            others = [
                r.getMessage()
                for r in info
                if str(token.mint) in r.getMessage()
                and not r.getMessage().startswith("New token detected")
            ]
            if others:
                bad.append(
                    f"{where}: {len(others)} other INFO lines name {token.mint}: {others[:3]}"
                )
    return _report("B", bad)


def check_c_a_coin_is_reported_once() -> bool:
    """C: a frame delivered again reports nothing more; other coins still report.

    A block holding several creates reports each of them.
    """
    bad = []
    coins: dict[Platform, set[str]] = {}
    for platform, kind in _streams():
        where = f"{platform.value}/{kind}"
        frames = _frames(platform, kind)
        once = _play(platform, kind, frames)
        mints = [str(t.mint) for t in once.tokens]
        coins.setdefault(platform, set()).update(mints)
        if len(mints) != len(set(mints)):
            bad.append(f"{where}: one play reported a mint twice: {mints}")
        repeated = [
            f for frame in frames for f in (frame, frame, _resigned(kind, frame))
        ]
        twice = _play(platform, kind, repeated)
        again = [str(t.mint) for t in twice.tokens]
        if again != mints:
            bad.append(
                f"{where}: frames delivered again and re-signed reported "
                f"{len(again)} coins, once {len(mints)}"
            )
        if kind == "blocks":
            # One block holding every create: each is a coin of its own.
            block = json.loads(json.dumps(frames[0]))
            block["params"]["result"]["value"]["block"]["transactions"] = [
                tx
                for frame in frames
                for tx in frame["params"]["result"]["value"]["block"]["transactions"]
            ]
            together = [str(t.mint) for t in _play(platform, kind, [block]).tokens]
            if together != mints:
                bad.append(
                    f"{where}: one block holding {len(mints)} creates reported "
                    f"{len(together)} coin(s)"
                )
        for mint in set(mints):
            lines = [
                r
                for r in twice.records.info()
                if r.getMessage().startswith("New token detected")
                and mint in r.getMessage()
            ]
            if len(lines) != 1:
                bad.append(
                    f"{where}: {len(lines)} detection lines for {mint} delivered again"
                )
    for platform, mints in coins.items():
        if len(mints) < 2:  # noqa: PLR2004
            bad.append(
                f"{platform.value}: the fixtures give {len(mints)} coin(s), need two"
            )
    return _report("C", bad)


# --- D, E: buyer, seller, trader, position manager ------------------------------


async def _nothing(*_a: object, **_k: object) -> None:
    return None


class _Client(SolanaClient):
    """Sends land at once; confirmation runs the client's own status read.

    Args:
        err: `meta.err` of every transaction; None for one that succeeded
        readable: False when no transaction can be read, as for one never seen
        deltas: What each read-back returns, in order
        status: The signature status that ends the confirmation wait, as
            `getSignatureStatuses` returns it; None: the wait returns nothing
    """

    def __init__(
        self,
        *,
        err: object = None,
        readable: bool = True,
        deltas: list[TradeDeltas] | None = None,
        status: dict | None = None,
    ) -> None:
        self._rate_limiter = SimpleNamespace(acquire=_nothing)
        self.err, self.readable = err, readable
        self.deltas = list(deltas or [])
        self.status = status

    async def get_client(self) -> SimpleNamespace:
        async def confirm(*_a: object, **_k: object) -> object:
            if self.status is None:
                return None
            return GetSignatureStatusesResp.from_json(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"context": {"slot": 9}, "value": [self.status]},
                    }
                )
            )

        return SimpleNamespace(confirm_transaction=confirm)

    async def post_rpc(self, *_a: object, **_k: object) -> dict:
        # Only getSignatureStatuses is asked for here.
        return {"result": {"context": {"slot": 9}, "value": [self.status]}}

    async def build_and_send_transaction(self, *_a: object, **_k: object) -> Signature:
        return Signature.new_unique()

    async def _get_transaction_result(self, *_a: object, **_k: object) -> dict | None:
        if not self.readable:
            return None
        if self.err == REVERT_ERR:
            return REVERTED_TX
        return {"meta": {"err": self.err}}

    async def get_trade_deltas(self, *_a: object, **_k: object) -> TradeDeltas | None:
        return self.deltas.pop(0) if self.deltas else None


class _Curve:
    """The curve's builder, curve manager and address provider, priced at `price`."""

    # The curve venue traded here is pump.fun's.
    program_id = PumpFunAddresses.PROGRAM

    def __init__(self, price: float, *, graduated: bool) -> None:
        self.price, self.graduated = price, graduated

    async def get_pool_state(self, *_a: object, **_k: object) -> dict:
        state = {"price_per_token": self.price, "quote_mint": WSOL_MINT}
        if self.graduated:
            state = {"graduated": True, "quote_mint": WSOL_MINT}
        return state

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


def _pool(market_cls: type, price: float):  # noqa: ANN202
    """The real pool market class, its chain reads and instructions stubbed."""

    class _Pool(market_cls):
        def __init__(self) -> None:
            pass

        async def get_market_state(self, *_a: object) -> dict:
            return {"price_per_token": price, "fee_fraction": 0.0, "pool": "POOL"}

        def quote_destination(self, *_a: object) -> None:
            return None

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

    return _Pool()


# (label, platform, the pool market a graduated coin trades in, or None: the curve)
VENUES = (
    ("curve", Platform.PUMP_FUN, None),
    ("pumpswap", Platform.PUMP_FUN, PumpSwapMarket),
    ("cpmm", Platform.LAUNCHLAB, CpmmMarket),
)
PRICE = 3e-9


def _wire(pool_cls: type | None) -> PlatformImplementations:
    curve = _Curve(PRICE, graduated=pool_cls is not None)
    implementations = PlatformImplementations(
        address_provider=curve,
        instruction_builder=curve,
        curve_manager=curve,
        event_parser=None,
        graduated_market=_pool(pool_cls, PRICE) if pool_cls else None,
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations
    return implementations


def _token(platform: Platform) -> TokenInfo:
    pump = platform is Platform.PUMP_FUN
    curve = Pubkey.new_unique()
    return TokenInfo(
        name="Venue",
        symbol="VEN",
        uri="",
        mint=Pubkey.new_unique(),
        platform=platform,
        bonding_curve=curve if pump else None,
        pool_state=None if pump else curve,
        quote_vault=None if pump else Pubkey.new_unique(),
        platform_config=None if pump else Pubkey.new_unique(),
        creator=Pubkey.new_unique(),
        quote_mint=WSOL_MINT,
    )


WALLET = SimpleNamespace(
    pubkey=Pubkey.new_unique(),
    keypair=None,
    get_associated_token_address=lambda *_a: Pubkey.default(),
)
FEES = SimpleNamespace(calculate_priority_fee=_nothing)
BOUGHT = TradeDeltas(
    tokens_raw=1000 * TOKEN,
    quote_raw=-3_100,
    network_fee_lamports=5_000,
    venue_quote_raw=3_000,
)
SOLD = TradeDeltas(
    tokens_raw=-1000 * TOKEN, quote_raw=2_900, network_fee_lamports=5_000
)


def _buyer(client: _Client) -> PlatformAwareBuyer:
    return PlatformAwareBuyer(
        client, WALLET, FEES, amount=0.003, slippage=0.3, max_retries=1
    )


def _seller(client: _Client) -> PlatformAwareSeller:
    return PlatformAwareSeller(client, WALLET, FEES, slippage=0.5, max_retries=1)


def _manager(seller: PlatformAwareSeller, client: _Client) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.seller = seller
    manager.solana_client = client
    manager.wallet = WALLET
    manager.priority_fee_manager = None
    manager.exit_strategy = "time_based"
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
    manager._graduated_mints = set()
    manager._open = set()
    curve = _Curve(PRICE, graduated=False)
    manager.platform_implementations = PlatformImplementations(
        address_provider=curve,
        instruction_builder=curve,
        curve_manager=curve,
        event_parser=None,
        graduated_market=_pool(PumpSwapMarket, PRICE),
    )
    return manager


def _trader() -> SimpleNamespace:
    """What the trader's buy handlers use of themselves."""
    return SimpleNamespace(
        _track_traded=lambda _t: None,
        positions=SimpleNamespace(
            open=lambda *_a, **_k: None, resolve_failed_buy=lambda *_a: False
        ),
        settings=SimpleNamespace(
            cleanup_mode="disabled",
            cleanup_with_priority_fee=False,
            cleanup_force_close_with_burn=False,
        ),
        solana_client=None,
        wallet=WALLET,
        priority_fee_manager=None,
    )


def _round_trip(
    platform: Platform, pool_cls: type | None, strategy: str
) -> SimpleNamespace:
    """Buy through the real buyer and trader, sell through the real position manager."""
    client = _Client(deltas=[BOUGHT, SOLD])
    token = _token(platform)
    sent: list = []

    def run() -> SimpleNamespace:
        implementations = _wire(pool_cls)
        buyer = _buyer(client)
        buyer.on_sent = lambda *args: sent.append(args)
        buy = asyncio.run(buyer.execute(token))
        asyncio.run(UniversalTrader._handle_successful_buy(_trader(), token, buy))
        manager = _manager(_seller(client), client)
        manager.platform_implementations = implementations
        manager.exit_strategy = strategy
        if strategy == "tp_sl":
            manager.max_hold_time = 0.01  # exits at once, on max_hold_time
        with tempfile.TemporaryDirectory() as tmp:
            manager.store = PositionStore(Path(tmp) / "positions.json")
            manager.store.add(token, buy, datetime.now(UTC).replace(tzinfo=None))

            async def go() -> None:
                await manager.manage(token, buy)
                await manager.wait_all()

            asyncio.run(asyncio.wait_for(go(), 30))
        return buy

    buy, rows, records = _captured(run)
    pending = sent[0][2] if sent else None
    return SimpleNamespace(
        buy=buy,
        rows=rows,
        lines=records.lines(),
        pending=pending,
        exit_line=EXIT_LINES[strategy],
    )


# The line each exit strategy ends a sale with.
EXIT_LINES = {
    "time_based": "Successfully sold",
    "tp_sl": "Successfully exited position",
}


def _venue_problems(
    label: str, platform: Platform, name: str, run: SimpleNamespace
) -> list[str]:
    bad = []
    if not run.buy.success:
        return [f"{label}: the buy failed: {run.buy.error_message}"]
    if getattr(run.buy, "venue", None) != name:
        bad.append(f"{label}: buy result venue {getattr(run.buy, 'venue', None)!r}")
    if getattr(run.pending, "venue", None) != name:
        bad.append(
            f"{label}: pending-buy record venue {getattr(run.pending, 'venue', None)!r}"
        )
    for prefix in ("Buying ", "Selling ", "Successfully bought", run.exit_line):
        lines = [x for x in run.lines if x.startswith(prefix)]
        if not any(f"venue: {name}" in x for x in lines):
            bad.append(
                f"{label}: no {prefix.strip()!r} line names venue {name!r}: {lines[:2]}"
            )
    for action in ("buy", "sell"):
        rows = [r for r in run.rows if r["action"] == action]
        if len(rows) != 1:
            bad.append(f"{label}: {len(rows)} {action} rows")
            continue
        if rows[0].get("venue") != name:
            bad.append(
                f"{label}: {action} row venue {rows[0].get('venue')!r}, want {name!r}"
            )
        if rows[0].get("platform") != platform.value:
            bad.append(f"{label}: {action} row platform {rows[0].get('platform')!r}")
    return bad


def _pending_resolved_row() -> list[str]:
    """A buy sent before a restart and resolved after it is logged with its venue."""
    token = _token(Platform.PUMP_FUN)
    signature = str(Signature.new_unique())

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            sent = base.TradeResult(
                success=False,
                platform=token.platform,
                tx_signature=signature,
                amount=1000.0,
                price=PRICE,
            )
            PositionStore(path).add(
                token,
                sent,
                datetime.now(UTC).replace(tzinfo=None),
                WALLET.pubkey,
                pending=base.PendingBuy(venue="PumpSwap"),
            )
            store = PositionStore(path)  # the next start reads it from disk
            store.load()
            manager = _manager(None, None)
            manager.store = store

            async def outcome(*_a: object, **_k: object) -> tuple:
                return ConfirmationStatus.SUCCESS, 1

            async def landed(token_info, sent, *_a: object) -> base.TradeResult:  # noqa: ANN001
                return base.TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=sent.tx_signature,
                    amount=1000.0,
                    amount_raw=1000 * TOKEN,
                    price=PRICE,
                )

            manager._signature_outcome = outcome
            manager._landed_buy = landed
            asyncio.run(manager._resolve_buy(store.pending(token.mint)))

    _, rows, _ = _captured(run)
    buys = [r for r in rows if r["action"] == "buy"]
    if len(buys) != 1:
        return [f"pending buy resolved after a restart: {len(buys)} buy rows"]
    if buys[0].get("venue") != "PumpSwap":
        return [
            f"pending buy resolved after a restart: row venue {buys[0].get('venue')!r}"
        ]
    return []


def check_d_trades_name_their_venue() -> bool:
    """D: buys and sells name the venue in the log, results and trades.log."""
    bad = []
    names = _venue_names()
    if names != WANTED_VENUES:
        bad.append(f"venue names: {names}")
    for label, platform, pool_cls in VENUES:
        for strategy in EXIT_LINES:
            run = _round_trip(platform, pool_cls, strategy)
            where = f"{label} {strategy}"
            bad += _venue_problems(where, platform, WANTED_VENUES[label], run)
    try:
        bad += _pending_resolved_row()
    except TypeError as e:
        bad.append(f"a pending-buy record cannot carry its venue: {e}")
    return _report("D", bad)


def _buy_and_sell(
    client: _Client, token: TokenInfo, pool_cls: type | None
) -> tuple[base.TradeResult, base.TradeResult]:
    """The real buyer, the trader's failed-buy handling and the real seller."""
    _wire(pool_cls)
    buy = asyncio.run(_buyer(client).execute(token))
    asyncio.run(UniversalTrader._handle_failed_buy(_trader(), token, buy))
    sell = asyncio.run(
        _seller(client).execute(
            token,
            token_amount=1000.0,
            token_price=PRICE,
            token_amount_raw=1000 * TOKEN,
        )
    )
    return buy, sell


def _reverted_problems(
    label: str, platform: Platform, pool_cls: type | None
) -> list[str]:
    bad = []
    token = _token(platform)
    (buy, sell), _, records = _captured(
        lambda: _buy_and_sell(_Client(err=REVERT_ERR), token, pool_cls)
    )
    for side, result in (("buy", buy), ("sell", sell)):
        text = result.error_message or ""
        if result.failure_reason is not TradeFailureReason.REVERTED:
            bad.append(f"{label} {side}: failure reason {result.failure_reason}")
        if "landed and reverted" not in text or "6002" not in text:
            bad.append(
                f"{label} {side}: {text!r} does not say it reverted with its error"
            )
        if "TooMuchSolRequired" not in text:
            bad.append(f"{label} {side}: {text!r} does not name the error")
        if "failed to confirm" in text:
            bad.append(f"{label} {side}: {text!r} says it failed to confirm")
    failed = [
        x for x in records.lines() if x.startswith(f"Failed to buy {token.symbol}")
    ]
    if not failed or "landed and reverted" not in failed[0] or "6002" not in failed[0]:
        bad.append(f"{label}: the trader's line is {failed[:1]}")
    # Only the buy is logged here (the seller's caller logs the sell).
    errors = [x for x in records.lines() if "6002" in x]
    if len(errors) != 1:
        bad.append(f"{label}: the buy's error is said in {len(errors)} lines: {errors}")

    return bad


def _unconfirmed_problems(
    label: str, platform: Platform, pool_cls: type | None
) -> list[str]:
    bad = []
    token = _token(platform)
    (buy, sell), _, _ = _captured(
        lambda: _buy_and_sell(_Client(readable=False), token, pool_cls)
    )
    for side, result in (("buy", buy), ("sell", sell)):
        if result.failure_reason is not TradeFailureReason.UNCONFIRMED:
            bad.append(
                f"{label} {side} never confirmed: reason {result.failure_reason}"
            )
        if "failed to confirm" not in (result.error_message or ""):
            bad.append(f"{label} {side} never confirmed: {result.error_message!r}")
    return bad


def _status(err: object, level: str) -> dict:
    return {
        "slot": 8,
        "confirmations": None,
        "err": err,
        "status": {"Err": err} if err else {"Ok": None},
        "confirmationStatus": level,
    }


def _lagging_node_problems(
    label: str, platform: Platform, pool_cls: type | None
) -> list[str]:
    """The confirmed status shows the error; the transaction cannot be read yet."""
    bad = []
    token = _token(platform)
    client = _Client(readable=False, status=_status(REVERT_ERR, "confirmed"))
    (buy, sell), _, _ = _captured(lambda: _buy_and_sell(client, token, pool_cls))
    name = STATUS_NAMES[label]
    for side, result in (("buy", buy), ("sell", sell)):
        text = result.error_message or ""
        if result.failure_reason is not TradeFailureReason.REVERTED:
            bad.append(f"{label} {side}, node behind: reason {result.failure_reason}")
        if "landed and reverted" not in text or "6002" not in text:
            bad.append(f"{label} {side}, node behind: {text!r}")
        if (name not in text) if name else ("IDL:" in text):
            bad.append(
                f"{label} {side}, node behind: {text!r} is not named "
                f"{name or 'by no IDL'}, its venue's program"
            )
    # An error seen at `processed` may be a fork that is dropped: not final.
    client = _Client(readable=False, status=_status(REVERT_ERR, "processed"))
    (buy, _), _, _ = _captured(lambda: _buy_and_sell(client, token, pool_cls))
    if buy.failure_reason is not TradeFailureReason.UNCONFIRMED:
        bad.append(
            f"{label}: an error at processed taken as final: {buy.error_message!r}"
        )
    return bad


def _pending_revert_line() -> list[str]:
    """A pending buy whose signature status shows it reverted is cleared naming the error."""
    token = _token(Platform.PUMP_FUN)
    client = _Client(status=_status(REVERT_ERR, "confirmed"))

    def run() -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            store = PositionStore(Path(tmp) / "positions.json")
            store.add(
                token,
                base.TradeResult(
                    success=False,
                    platform=token.platform,
                    tx_signature=str(Signature.new_unique()),
                    amount=1000.0,
                    price=PRICE,
                ),
                datetime.now(UTC).replace(tzinfo=None),
                WALLET.pubkey,
                pending=base.PendingBuy(venue=CurveMarket.name),
            )
            manager = _manager(None, client)
            manager.store = store
            try:
                asyncio.run(
                    asyncio.wait_for(manager._resolve_buy(store.pending(token.mint)), 5)
                )
            except TimeoutError:
                return ["the reverted pending buy was not settled within 5 s"]
            if str(token.mint) in store.mints():
                return ["the reverted pending buy was not cleared"]
            return []

    bad, _, records = _captured(run)
    lines = [x for x in records.lines() if "reverted" in x and str(token.mint) in x]
    if (
        len(lines) != 1
        or "6002" not in lines[0]
        or "TooMuchSolRequired" not in lines[0]
    ):
        bad.append(f"pending buy found reverted: {lines}")
    return bad


def check_e_reverted_is_reported_as_reverted() -> bool:
    """E: a landed-and-reverted buy or sell is reported with its error, once."""
    bad = []
    for label, platform, pool_cls in VENUES:
        bad += _reverted_problems(label, platform, pool_cls)
        bad += _unconfirmed_problems(label, platform, pool_cls)
        bad += _lagging_node_problems(label, platform, pool_cls)
    bad += _pending_revert_line()

    # The client's own line, for a caller that does not take the error itself.
    signature = str(Signature.new_unique())
    status, _, records = _captured(
        lambda: asyncio.run(
            _Client(err=REVERT_ERR).verify_transaction_status(signature)
        )
    )
    own = [x for x in records.lines() if "6002" in x]
    if status is not ConfirmationStatus.REVERTED:
        bad.append(f"client status {status}")
    if len(own) != 1 or "landed and reverted" not in own[0]:
        bad.append(f"the client's own line: {own}")
    return _report("E", bad)


# --- F: shutdown ------------------------------------------------------------------


def check_f_shutdown_says_resolved_by_signature() -> bool:
    """F: the shutdown line says pending buys are settled by signature."""
    token = _token(Platform.PUMP_FUN)

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PositionStore(Path(tmp) / "positions.json")
            store.add(
                token,
                base.TradeResult(
                    success=False,
                    platform=token.platform,
                    tx_signature=str(Signature.new_unique()),
                    amount=1000.0,
                    price=PRICE,
                ),
                datetime.now(UTC).replace(tzinfo=None),
                WALLET.pubkey,
                pending=base.PendingBuy(),
            )
            manager = _manager(None, None)
            manager.store = store
            manager._report_pending()

    _, _, records = _captured(run)
    lines = [x for x in records.lines() if "sent but not confirmed" in x]
    bad = []
    if len(lines) != 1 or str(token.mint) not in lines[0]:
        bad.append(f"shutdown lines: {lines}")
    elif "by its signature" not in lines[0]:
        bad.append(f"does not say settled by signature: {lines[0]!r}")
    elif "resolves each from the wallet's balance" in lines[0]:
        bad.append(f"still says resolved from the wallet's balance: {lines[0]!r}")
    return _report("F", bad)


# --- G: docs ------------------------------------------------------------------------


def _quoted(text: str, names: set[str]) -> set[str]:
    return {m for m in re.findall(r'"([a-z_]+)"', text) if m in names}


def _readme_problems() -> list[str]:
    bad = []
    readme = (PROJECT_ROOT / "README.md").read_text()
    runner = (PROJECT_ROOT / "src/bot_runner.py").read_text()
    default = re.search(r'get\("token_wait_timeout", (\d+)\)', runner)
    line = next(
        (x for x in readme.splitlines() if "`timing.token_wait_timeout`" in x), ""
    )
    if not default:
        bad.append("bot_runner reads no token_wait_timeout default")
    elif f"Default {default.group(1)} seconds" not in line:
        bad.append(
            f"README: token_wait_timeout with default {default.group(1)}: {line!r}"
        )
    venue = next((x for x in readme.splitlines() if "`venue`" in x), "")
    for name in _venue_names().values():
        if name is None or f"`{name}`" not in venue:
            bad.append(f"README: `venue` paragraph does not name `{name}`")
    return bad


def _yaml_problems(path: Path) -> list[str]:
    """The file's platform options, and the listeners it says its platform takes."""
    bad = []
    platforms = {p.value for p in Platform}
    listeners = {k for kinds in PLATFORM_LISTENER_COMPATIBILITY.values() for k in kinds}
    lines = path.read_text().splitlines()
    platform_at = next(i for i, x in enumerate(lines) if x.startswith("platform:"))
    options = next(
        (x for x in reversed(lines[:platform_at]) if x.startswith("# Options:")), ""
    )
    if _quoted(options, platforms) != platforms:
        bad.append(f"{path.name}: platform options {options!r}")
    platform = Platform(
        lines[platform_at].split(":", 1)[1].split("#")[0].strip().strip('"')
    )
    supported = set(PLATFORM_LISTENER_COMPATIBILITY[platform])
    # The listener_type line and the comment lines right under it.
    at = next(i for i, x in enumerate(lines) if x.strip().startswith("listener_type:"))
    block = [lines[at]]
    for x in lines[at + 1 :]:
        if not x.strip().startswith("#"):
            break
        block.append(x)
    said = _quoted("\n".join(block), listeners)
    if said != supported:
        bad.append(
            f"{path.name}: listener_type lists {sorted(said)} for {platform.value}"
        )
    # A comment right above `platform:` that names listeners names them all.
    header = "\n".join(lines[options and lines.index(options) : platform_at])
    named = _quoted(header, listeners)
    if named and named != supported:
        bad.append(f"{path.name}: the platform comment lists {sorted(named)}")
    return bad


def check_g_docs_match_the_code() -> bool:
    """G: README and shipped YAML say what the code does."""
    bad = _readme_problems()
    for path in sorted((PROJECT_ROOT / "bots").glob("*.yaml")):
        bad += _yaml_problems(path)
    return _report("G", bad)


# --- H: filters before the once-per-mint mark --------------------------------------


def _attempts(kind: str, tokens: list[TokenInfo], **filters: str) -> SimpleNamespace:
    """Run one listener's real loop, its decode swapped for `tokens` in order."""
    listener = ListenerFactory.create_listener(
        listener_type=kind,
        wss_endpoint="wss://rpc.offline.invalid",
        geyser_endpoint="geyser.offline.invalid:443",
        geyser_api_token="unused",  # noqa: S106 — never sent; nothing connects
        platforms=[Platform.PUMP_FUN],
    )
    queue = deque(tokens)
    taken: list[TokenInfo] = []

    def decode(*_a: object) -> TokenInfo:
        if not queue:
            raise _FeedEnd
        return queue.popleft()

    async def decode_async(*_a: object) -> TokenInfo:
        return decode()

    async def decode_block(*_a: object) -> list[TokenInfo]:
        return [decode()]

    if kind in ("logs", "blocks"):
        listener._wait_for_token_creation = (
            decode_block if kind == "blocks" else decode_async
        )
    else:
        listener._process_update = decode if kind == "shreds" else decode_async
    update = (
        geyser_pb2.SubscribeUpdateDeshred()
        if kind == "shreds"
        else geyser_pb2.SubscribeUpdate()
    )

    async def connection():  # noqa: ANN202
        return _FakeStub([update] * (len(tokens) + 1)), _FakeChannel()

    async def on_token(token: TokenInfo) -> None:
        taken.append(token)

    listener._create_geyser_connection = connection
    original_connect = websockets.connect
    websockets.connect = lambda *_a, **_k: _FakeSocket([])

    async def go() -> None:
        try:
            await listener.listen_for_tokens(
                on_token, filters.get("match"), filters.get("creator")
            )
        except _FeedEnd:
            pass

    try:
        _, _, records = _captured(lambda: asyncio.run(go()))
    finally:
        websockets.connect = original_connect
    return SimpleNamespace(taken=taken, lines=records.lines())


def check_h_a_refused_attempt_does_not_hide_the_next() -> bool:
    """H: an attempt the filters refused does not hide a later one at the same mint."""
    bad = []
    mint, creator = Pubkey.new_unique(), Pubkey.new_unique()

    def attempt(name: str, user: Pubkey) -> TokenInfo:
        return TokenInfo(
            name=name, symbol=name[:4], uri="", mint=mint,
            platform=Platform.PUMP_FUN, user=user, creator=user,
        )  # fmt: skip

    cases = {
        "name filter": (
            [attempt("Other", creator), attempt("Wanted", creator)],
            {"match": "Wanted"},
        ),
        "creator filter": (
            [attempt("Wanted", Pubkey.new_unique()), attempt("Wanted", creator)],
            {"creator": str(creator)},
        ),
    }
    for kind in ("logs", "blocks", "geyser", "shreds"):
        for case, (tokens, filters) in cases.items():
            third = attempt("Wanted", creator)
            run = _attempts(kind, [*tokens, third], **filters)
            if [id(t) for t in run.taken] != [id(tokens[1])]:
                names = [(t.name, str(t.user)[:4]) for t in run.taken]
                bad.append(f"{kind}, {case}: handed on {names}, want the second only")
            detected = [x for x in run.lines if x.startswith("New token detected")]
            if len(detected) != 1:
                bad.append(f"{kind}, {case}: {len(detected)} detection lines")
    return _report("H", bad)


CHECKS = (
    check_a_one_line_names_the_coin,
    check_b_no_other_info_line_per_coin,
    check_c_a_coin_is_reported_once,
    check_d_trades_name_their_venue,
    check_e_reverted_is_reported_as_reverted,
    check_f_shutdown_says_resolved_by_signature,
    check_g_docs_match_the_code,
    check_h_a_refused_attempt_does_not_hide_the_next,
)


def main() -> int:
    results = []
    for check in CHECKS:
        print(check.__doc__.splitlines()[0])
        try:
            ok = check()
        except Exception:  # noqa: BLE001 — a crash is a failure, not an abort
            traceback.print_exc()
            ok = False
        print("  PASS" if ok else "  FAIL")
        results.append(ok)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

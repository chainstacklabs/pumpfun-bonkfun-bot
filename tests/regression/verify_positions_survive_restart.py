"""Verify open positions survive a restart of the bot.

A position lived only in its monitoring task, so a bot stopped or killed while
holding a coin came back knowing nothing about it: the coin stayed in the wallet,
unsold. Each monitored position is now kept in `positions/<bot name>.json` from
its buy until its sell, and the next start resumes it:

  A. The file round-trips every `TokenInfo` field (pump.fun, LaunchLab and a
     migrated coin's pool), the buy and its entry time; nothing is left
     half-written.
  B. A buy writes the position before any await; a sale removes it, on the
     `tp_sl` and `time_based` paths and on a sell confirmed late.
  C. A position that is not sold stays: an exit that gives up, one whose sell
     could not be resolved, and one stopped at shutdown.
  D. `manual` and `marry_mode` positions are not written, and not resumed.
  E. A resumed `tp_sl` position keeps its entry price and counts
     `max_hold_time` from its original buy, and sells the raw amount bought.
  F. A resumed `time_based` position counts `wait_after_buy` from its buy, and
     its first sell is floored at the price now, not the stale buy price.
  G. A position whose coin the wallet no longer holds (sold while the bot was
     down, or its account closed) is cleared and never sold.
  H. A wallet holding less than was bought sells what it holds.
  I. A position whose balance cannot be read is neither sold nor cleared.
  J. A record that does not decode stays in the file; a file that cannot be
     read (not JSON, not UTF-8, no permission) is moved aside, never over an
     earlier one, and one that cannot be moved is never written over.
  K. The trader resumes before it takes a coin: resumed positions hold slots
     (all of them, also beyond `max_open_positions`), are cleaned up after the
     session, are never bought again when seen anew, and single-token mode
     waits for them also when no new coin comes.
  L. `bot_runner` keeps each bot's positions in `positions/<name>.json`, which
     git ignores, and starts no second enabled bot under a name in use.
  M. A position bought on another platform than the bot's stays in the file and
     is not resumed: this bot cannot price it.

Offline: the RPC client, seller and prices are stubs; the position manager, its
store and the trader are the real ones. Runs in a temporary directory.

Usage:
    uv run tests/regression/verify_positions_survive_restart.py
"""

import asyncio
import dataclasses
import json
import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

import bot_runner  # noqa: E402
from core.pubkeys import TOKEN_PROGRAM, WSOL_MINT  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading import universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
WALLET = Wallet(KEY)
BOUGHT_RAW = 1_000_000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _token(symbol: str, **fields: object) -> TokenInfo:
    return TokenInfo(
        name=symbol,
        symbol=symbol,
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.new_unique(),
        token_program_id=TOKEN_PROGRAM,
        quote_mint=WSOL_MINT,
        **fields,
    )


def _buy(price: float = 1.0, raw: int | None = BOUGHT_RAW) -> TradeResult:
    return TradeResult(
        success=True, price=price, amount=1.0, amount_raw=raw, tx_signature="buy"
    )


def _token_account(mint: Pubkey, raw: int) -> SimpleNamespace:
    data = bytes(mint) + bytes(WALLET.pubkey) + struct.pack("<Q", raw) + bytes(93)
    return SimpleNamespace(data=data, owner=TOKEN_PROGRAM)


class _Client:
    """Token accounts by address; `down` makes every read fail."""

    rpc_endpoint = ""

    def __init__(self) -> None:
        self.accounts: dict[Pubkey, SimpleNamespace] = {}
        self.down = False
        self.status = ConfirmationStatus.SUCCESS

    def hold(self, token: TokenInfo, raw: int) -> None:
        ata = WALLET.get_associated_token_address(token.mint, token.token_program_id)
        self.accounts[ata] = _token_account(token.mint, raw)

    async def get_account_info(self, pubkey: Pubkey) -> SimpleNamespace:
        if self.down:
            raise RuntimeError("rpc down")  # noqa: TRY003
        if pubkey not in self.accounts:
            raise ValueError(f"Account {pubkey} not found")  # noqa: TRY003
        return self.accounts[pubkey]

    async def verify_transaction_status(self, _sig: str) -> ConfirmationStatus:
        return self.status

    async def get_health(self) -> str:
        return "ok"

    async def close(self) -> None:
        return None


class _Seller:
    """Records each sell; `outcome` is "sold", "reverted" or "unconfirmed"."""

    def __init__(self) -> None:
        self.sells: list[tuple[str, int | None, float]] = []
        # The token_price each sell was handed: its slippage floor.
        self.floors: list[tuple[str, float]] = []
        self.outcome = "sold"

    async def execute(
        self,
        token_info: TokenInfo,
        token_amount: float,
        token_price: float,
        token_amount_raw: int | None = None,
    ) -> TradeResult:
        self.sells.append((token_info.symbol, token_amount_raw, token_amount))
        self.floors.append((token_info.symbol, token_price))
        if self.outcome == "sold":
            return TradeResult(
                success=True, price=token_price, amount=token_amount, tx_signature="s"
            )
        reason = (
            TradeFailureReason.REVERTED
            if self.outcome == "reverted"
            else TradeFailureReason.UNCONFIRMED
        )
        return TradeResult(
            success=False, error_message=self.outcome, failure_reason=reason,
            tx_signature="s",
        )  # fmt: skip


def _settings(**overrides: object) -> TraderSettings:
    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "buy_amount": 0.01,
        "buy_slippage": 0.1,
        "sell_slippage": 0.1,
        "exit_strategy": "tp_sl",
        "take_profit_percentage": 0.5,
        "stop_loss_percentage": 0.4,
        "max_hold_time": 60,
        "price_check_interval": 0.02,
        "wait_time_after_buy": 30,
    }
    return TraderSettings(**(fields | overrides))


class _Rig:
    """A real PositionManager and store on stub client, seller and prices."""

    def __init__(self, path: Path, **settings: object) -> None:
        self.client, self.seller = _Client(), _Seller()
        self.prices: dict[str, float] = {}

        async def get_price(token_info: TokenInfo) -> float:
            return self.prices.get(token_info.symbol, 1.0)

        self.store = PositionStore(path)
        self.pm = PositionManager(
            _settings(**settings),
            seller=self.seller,
            solana_client=self.client,
            wallet=WALLET,
            priority_fee_manager=None,
            platform_implementations=SimpleNamespace(
                curve_market=SimpleNamespace(get_price=get_price),
                graduated_market=None,
            ),
            store=self.store,
        )

    def stored(self) -> set[str]:
        return _stored(self.store.path)


def _stored(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {r["mint"] for r in json.loads(path.read_text())["positions"]}


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _full_token(platform: Platform, **fields: object) -> TokenInfo:
    keys = (
        "bonding_curve associated_bonding_curve pool_state base_vault quote_vault "
        "global_config platform_config user creator creator_vault "
        "quote_token_program_id"
    ).split()
    return TokenInfo(
        name="Full",
        symbol="FULL",
        uri="https://example/x.json",
        mint=Pubkey.new_unique(),
        platform=platform,
        token_program_id=TOKEN_PROGRAM,
        quote_mint=Pubkey.new_unique(),
        is_mayhem_mode=True,
        is_cashback_coin=True,
        is_holder_reward=True,
        creator_fee_bps=25,
        virtual_quote_reserves=123_456,
        transfer_fee_bps=100,
        state_from_event=True,
        creation_timestamp=1_700_000_000.5,
        **{key: Pubkey.new_unique() for key in keys},
        **fields,
    )


def check_a_round_trip(tmp: Path) -> bool:
    problems = []
    path = tmp / "a" / "bot.json"
    store = PositionStore(path)
    tokens = [
        _full_token(Platform.PUMP_FUN),
        _full_token(Platform.STONK_FUN),
        _full_token(
            Platform.LAUNCHLAB,
            additional_data={"entry": "migration", "pool": str(Pubkey.new_unique())},
        ),
    ]
    entry = datetime(2026, 1, 2, 3, 4, 5, 678901)
    for i, token in enumerate(tokens):
        store.add(token, _buy(price=0.5 + i, raw=BOUGHT_RAW + i), entry)
    loaded = PositionStore(path).load()
    if [p.token_info for p in loaded] != tokens:
        problems.append("a TokenInfo does not come back equal")
    for i, position in enumerate(loaded):
        buy = position.buy_result
        if (buy.success, buy.price, buy.amount, buy.amount_raw, buy.tx_signature) != (
            True, 0.5 + i, 1.0, BOUGHT_RAW + i, "buy",
        ):  # fmt: skip
            problems.append(f"buy {i} came back as {buy}")
        if position.entry_time != entry:
            problems.append(f"entry time {position.entry_time}, wrote {entry}")
        if buy.platform != tokens[i].platform:
            problems.append(f"buy platform {buy.platform}")
    store.remove(tokens[1].mint)
    if _stored(path) != {str(tokens[0].mint), str(tokens[2].mint)}:
        problems.append("remove dropped the wrong position")
    if sorted(p.name for p in path.parent.iterdir()) != ["bot.json"]:
        problems.append(f"left beside the file: {list(path.parent.iterdir())}")
    return _report(problems)


async def _check_b(tmp: Path) -> list[str]:
    problems = []
    rig = _Rig(tmp / "b.json")
    sold = _token("TP")
    rig.prices["TP"] = 1.0
    task = rig.pm.open(sold, _buy())
    if str(sold.mint) not in rig.stored():
        problems.append("tp_sl: not written when the buy landed")
    await asyncio.sleep(0.1)
    if str(sold.mint) not in rig.stored():
        problems.append("tp_sl: removed before it was sold")
    rig.prices["TP"] = 2.0
    await asyncio.wait_for(task, 2)
    if str(sold.mint) in rig.stored():
        problems.append("tp_sl: still in the file after its sale")

    rig = _Rig(tmp / "b2.json", exit_strategy="time_based", wait_time_after_buy=0)
    timed = _token("TIME")
    task = rig.pm.open(timed, _buy())
    if str(timed.mint) not in rig.stored():
        problems.append("time_based: not written when the buy landed")
    await asyncio.wait_for(task, 2)
    if rig.stored() or len(rig.seller.sells) != 1:
        problems.append(f"time_based: {rig.stored()} after {rig.seller.sells}")

    rig = _Rig(tmp / "b3.json")
    late = _token("LATE")
    rig.prices["LATE"] = 2.0
    rig.seller.outcome = "unconfirmed"
    await asyncio.wait_for(rig.pm.open(late, _buy()), 2)
    if rig.stored():
        problems.append("a sell confirmed late left the position in the file")
    return problems


def check_b_written_on_buy_removed_on_sale(tmp: Path) -> bool:
    return _report(asyncio.run(_check_b(tmp)))


async def _check_c(tmp: Path) -> list[str]:
    problems = []
    for outcome, status in (
        ("reverted", ConfirmationStatus.SUCCESS),
        ("unconfirmed", ConfirmationStatus.UNCONFIRMED),
    ):
        for strategy in ("tp_sl", "time_based"):
            rig = _Rig(
                tmp / f"c-{outcome}-{strategy}.json",
                exit_strategy=strategy,
                wait_time_after_buy=0,
                max_exit_sell_attempts=2,
            )
            rig.seller.outcome, rig.client.status = outcome, status
            coin = _token("KEEP")
            rig.prices["KEEP"] = 2.0
            await asyncio.wait_for(rig.pm.open(coin, _buy()), 3)
            if not rig.seller.sells:
                problems.append(f"{strategy}/{outcome}: never tried to sell")
            if str(coin.mint) not in rig.stored():
                problems.append(f"{strategy}/{outcome}: unsold position removed")

    rig = _Rig(tmp / "c-stop.json")
    coin = _token("HELD")
    rig.pm.open(coin, _buy())
    await asyncio.sleep(0.05)
    await rig.pm.stop()
    if str(coin.mint) not in rig.stored():
        problems.append("a position stopped at shutdown was removed")
    return problems


def check_c_unsold_positions_stay(tmp: Path) -> bool:
    return _report(asyncio.run(_check_c(tmp)))


async def _check_d(tmp: Path) -> list[str]:
    problems = []
    for name, settings in (
        ("manual", {"exit_strategy": "manual"}),
        ("marry_mode", {"marry_mode": True}),
    ):
        rig = _Rig(tmp / f"d-{name}.json", **settings)
        await asyncio.wait_for(rig.pm.open(_token("M"), _buy()), 2)
        if rig.store.path.exists():
            problems.append(f"{name}: position written")
        old = _token("OLD")
        PositionStore(rig.store.path).add(old, _buy(), _now())
        rig.client.hold(old, BOUGHT_RAW)
        if await rig.pm.resume() or rig.pm.open_count or rig.seller.sells:
            problems.append(f"{name}: a stored position was resumed")
        if len(_stored(rig.store.path)) != 1:
            problems.append(f"{name}: the stored position was dropped")
    return problems


def check_d_manual_and_marry_not_kept(tmp: Path) -> bool:
    return _report(asyncio.run(_check_d(tmp)))


async def _check_e(tmp: Path) -> list[str]:
    problems = []
    path = tmp / "e.json"
    old, falling = _token("OLD"), _token("FALL")
    store = PositionStore(path)
    store.add(old, _buy(raw=777_000), _now() - timedelta(seconds=120))
    store.add(falling, _buy(price=2.0), _now())
    rig = _Rig(path)
    rig.client.hold(old, 777_000)
    rig.client.hold(falling, BOUGHT_RAW)
    rig.prices["FALL"] = 1.0  # half the entry price: below the 40% stop loss
    resumed = await rig.pm.resume()
    if {t.mint for t in resumed} != {old.mint, falling.mint}:
        problems.append(f"resumed {[t.symbol for t in resumed]}")
    if not await _until(lambda: rig.pm.open_count == 0, 2):
        problems.append(
            f"not sold right away: max_hold_time or the stop loss did not count "
            f"from the stored buy ({rig.seller.sells})"
        )
        await rig.pm.stop()
    if sorted(rig.seller.sells) != [("FALL", BOUGHT_RAW, 1.0), ("OLD", 777_000, 1.0)]:
        problems.append(f"sells {rig.seller.sells}")
    if rig.stored():
        problems.append("sold positions left in the file")
    return problems


def check_e_tp_sl_resumes_from_its_buy(tmp: Path) -> bool:
    return _report(asyncio.run(_check_e(tmp)))


async def _check_f(tmp: Path) -> list[str]:
    problems = []
    path = tmp / "f.json"
    due, fresh = _token("DUE"), _token("FRESH")
    store = PositionStore(path)
    store.add(due, _buy(), _now() - timedelta(seconds=100))
    store.add(fresh, _buy(), _now())
    rig = _Rig(path, exit_strategy="time_based")
    rig.client.hold(due, BOUGHT_RAW)
    rig.client.hold(fresh, BOUGHT_RAW)
    rig.prices["DUE"] = 0.25  # fell since the buy at 1.0
    await rig.pm.resume()
    if not await _until(lambda: rig.pm.open_count == 1, 2):
        problems.append("a position past its wait_after_buy was not sold at once")
    await asyncio.sleep(0.1)
    if [s[0] for s in rig.seller.sells] != ["DUE"]:
        problems.append(f"sells {rig.seller.sells}; FRESH has 30s still to wait")
    if rig.seller.floors != [("DUE", 0.25)]:
        problems.append(
            f"floors {rig.seller.floors}: the first sell of a resumed position "
            f"must price off the market now, not the buy before the restart"
        )
    await rig.pm.stop()
    if rig.stored() != {str(fresh.mint)}:
        problems.append(f"file holds {rig.stored()}")
    return problems


def check_f_time_based_resumes_from_its_buy(tmp: Path) -> bool:
    return _report(asyncio.run(_check_f(tmp)))


async def _check_g(tmp: Path) -> list[str]:
    problems = []
    path = tmp / "g.json"
    emptied, closed = _token("EMPTY"), _token("CLOSED")
    store = PositionStore(path)
    for token in (emptied, closed):
        store.add(token, _buy(), _now() - timedelta(seconds=120))
    rig = _Rig(path)
    rig.client.hold(emptied, 0)  # CLOSED has no account at all
    if await rig.pm.resume():
        problems.append("a position the wallet no longer holds was resumed")
    await asyncio.sleep(0.1)
    if rig.seller.sells or rig.pm.open_count:
        problems.append(f"sold again: {rig.seller.sells}")
    if rig.stored():
        problems.append(f"not cleared: {rig.stored()}")
    return problems


def check_g_sold_while_down_is_cleared(tmp: Path) -> bool:
    return _report(asyncio.run(_check_g(tmp)))


async def _check_h(tmp: Path) -> list[str]:
    path = tmp / "h.json"
    coin = _token("PART")
    PositionStore(path).add(coin, _buy(), _now() - timedelta(seconds=120))
    rig = _Rig(path)
    rig.client.hold(coin, 400_000)
    await rig.pm.resume()
    await _until(lambda: rig.pm.open_count == 0, 2)
    if rig.seller.sells != [("PART", 400_000, 0.4)]:
        return [f"sells {rig.seller.sells}, expected the 400000 raw held"]
    return []


def check_h_sells_what_is_held(tmp: Path) -> bool:
    return _report(asyncio.run(_check_h(tmp)))


async def _check_i(tmp: Path) -> list[str]:
    path = tmp / "i.json"
    coin = _token("DARK")
    PositionStore(path).add(coin, _buy(), _now())
    rig = _Rig(path)
    rig.client.down = True
    problems = []
    if await rig.pm.resume() or rig.pm.open_count or rig.seller.sells:
        problems.append("a position with an unreadable balance was resumed")
    if rig.stored() != {str(coin.mint)}:
        problems.append("a position with an unreadable balance was cleared")
    return problems


def check_i_unreadable_balance_is_kept(tmp: Path) -> bool:
    return _report(asyncio.run(_check_i(tmp)))


def _text(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


def check_j_bad_records_are_kept(tmp: Path) -> bool:
    problems = []
    path = tmp / "j.json"
    good = _token("GOOD")
    store = PositionStore(path)
    store.add(good, _buy(), _now())
    data = json.loads(path.read_text())
    data["positions"].append({"mint": "bad", "token_info": {"mint": "not base58"}})
    path.write_text(json.dumps(data))
    store = PositionStore(path)
    loaded = store.load()
    if [p.token_info.mint for p in loaded] != [good.mint]:
        problems.append(f"loaded {loaded}")
    store.remove(good.mint)
    if _stored(path) != {"bad"}:
        problems.append(f"after a write the file holds {_stored(path)}")

    path.write_text("{not json")
    if PositionStore(path).load():
        problems.append("an unreadable file produced positions")
    aside = path.with_name(path.name + ".unreadable")
    if path.exists() or _text(aside) != "{not json":
        problems.append("an unreadable file was not moved aside intact")
    path.write_text("{still not json")
    PositionStore(path).load()
    second = path.with_name(path.name + ".unreadable.1")
    if _text(aside) != "{not json" or _text(second) != "{still not json":
        problems.append("a second unreadable file overwrote the first one aside")
    problems += _check_j_unreadable_bytes(tmp)
    return _report(problems)


def _check_j_unreadable_bytes(tmp: Path) -> list[str]:
    """Files that fail before JSON parsing: bad UTF-8, no read permission."""
    problems = []
    cases = [("utf8", b"\xff\xfe{", None)]
    if os.geteuid() != 0:  # root reads a mode-000 file anyway
        cases.append(("perm", b'{"version": 1, "positions": []}', 0))
    for name, content, mode in cases:
        path = tmp / f"j-{name}.json"
        path.write_bytes(content)
        if mode is not None:
            path.chmod(mode)
        try:
            loaded = PositionStore(path).load()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}: load raised {type(e).__name__}: {e}")
            continue
        aside = path.with_name(path.name + ".unreadable")
        if loaded or path.exists() or not aside.exists():
            problems.append(f"{name}: an unreadable file was not moved aside")
            continue
        aside.chmod(0o600)
        if aside.read_bytes() != content:
            problems.append(f"{name}: the file moved aside changed")

    # Neither readable nor movable: the store must not write over it.
    path = tmp / "j-stuck.json"
    path.write_bytes(b"\xff stuck")
    store = PositionStore(path)
    store._free_aside_path = lambda: tmp / "no-such-dir" / "x"  # noqa: SLF001
    try:
        store.load()
        store.add(_token("NEW"), _buy(), _now())
    except Exception as e:  # noqa: BLE001
        problems.append(f"stuck: raised {type(e).__name__}: {e}")
    if _text_bytes(path) != b"\xff stuck":
        problems.append("a file that could not be moved aside was written over")
    return problems


def _text_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


class _Listener:
    def __init__(self, tokens: list[TokenInfo]) -> None:
        self.tokens = tokens

    async def listen_for_tokens(self, token_callback, *_args: object) -> None:  # noqa: ANN001
        for token in self.tokens:
            await token_callback(token)
        await asyncio.Event().wait()


async def _trader_run(
    path: Path, tokens: list[TokenInfo], **settings: object
) -> tuple[object, list[str], dict[str, asyncio.Event]]:
    client = _Client()
    originals = (
        universal_trader.SolanaClient,
        universal_trader.ListenerFactory.create_listener,
    )
    universal_trader.SolanaClient = lambda *_a, **_k: client
    universal_trader.ListenerFactory.create_listener = staticmethod(
        lambda **_k: _Listener(tokens)
    )
    try:
        trader = universal_trader.UniversalTrader(
            _settings(
                yolo_mode=False,
                max_token_age=30,
                token_wait_timeout=0.3,
                wait_time_after_creation=0,
                positions_file=str(path),
                **settings,
            )
        )
    finally:
        universal_trader.SolanaClient = originals[0]
        universal_trader.ListenerFactory.create_listener = staticmethod(originals[1])
    events: list[str] = []
    release = {t.symbol: asyncio.Event() for t in tokens}

    async def no_quotes() -> None:
        return None

    async def buy(token_info: TokenInfo) -> TradeResult:
        events.append(f"buy {token_info.symbol}")
        return _buy()

    async def manage(token_info: TokenInfo, _b: TradeResult, entry_time=None) -> None:  # noqa: ANN001
        kind = "fresh" if entry_time is None else "resumed"
        events.append(f"hold {token_info.symbol} {kind}")
        release.setdefault(token_info.symbol, asyncio.Event())
        await release[token_info.symbol].wait()
        events.append(f"exit {token_info.symbol}")

    async def cleanup() -> None:
        events.append(f"cleanup {sorted(t.symbol for t in tokens_by(trader))}")

    def tokens_by(t: object) -> list[TokenInfo]:
        known = {tok.mint: tok for tok in [*tokens, *stored_tokens]}
        return [known[m] for m in t.traded_mints if m in known]

    stored_tokens: list[TokenInfo] = []
    trader._resolve_quote_token_programs = no_quotes  # noqa: SLF001
    trader.buyer.execute = buy
    trader.positions.manage = manage
    trader._cleanup_resources = cleanup  # noqa: SLF001
    trader._stored_tokens = stored_tokens  # noqa: SLF001
    return trader, events, release


async def _check_k_over_limit(tmp: Path) -> list[str]:
    problems = []
    # More stored than the limit allows: every one resumes, and the new coin
    # waits until the count is below the limit again.
    path = tmp / "k3.json"
    old, old2, new = _token("OLD"), _token("OLD2"), _token("NEW")
    store = PositionStore(path)
    for token in (old, old2):
        store.add(token, _buy(), _now())
    trader, events, release = await _trader_run(path, [new])
    trader._stored_tokens.extend([old, old2])  # noqa: SLF001
    for token in (old, old2):
        trader.solana_client.hold(token, BOUGHT_RAW)
        release[token.symbol] = asyncio.Event()
    start = asyncio.create_task(trader.start())
    await _until(lambda: "hold OLD2 resumed" in events)
    release["OLD"].set()
    await asyncio.sleep(0.1)
    if "buy NEW" in events:
        problems.append(f"bought while still over the limit: {events}")
    release["OLD2"].set()
    await _until(lambda: "hold NEW fresh" in events)
    release["NEW"].set()
    await asyncio.wait_for(start, 5)
    want = [
        "hold OLD resumed",
        "hold OLD2 resumed",
        "exit OLD",
        "exit OLD2",
        "buy NEW",
        "hold NEW fresh",
        "exit NEW",
        "cleanup ['NEW', 'OLD', 'OLD2']",
    ]
    if events != want:
        problems.append(f"over the limit: {events}, expected {want}")
    return problems


async def _check_k_held_not_rebought(tmp: Path) -> list[str]:
    problems = []
    # The listener reports a coin the bot already holds from before the restart
    # (as a migration would under `entry: migration`), with a slot free: it is
    # not bought a second time.
    path = tmp / "k4.json"
    old = _token("OLD")
    PositionStore(path).add(old, _buy(), _now())
    trader, events, release = await _trader_run(path, [old], max_open_positions=2)
    trader.solana_client.hold(old, BOUGHT_RAW)
    start = asyncio.create_task(trader.start())
    await asyncio.sleep(0.6)  # past token_wait_timeout
    if str(old.mint) not in trader.intake.processed_tokens:
        problems.append("a resumed coin is not marked processed in the intake")
    release["OLD"].set()
    await asyncio.wait_for(start, 5)
    if events != ["hold OLD resumed", "exit OLD", "cleanup ['OLD']"]:
        problems.append(f"a held coin seen again: {events}")
    return problems


async def _check_k(tmp: Path) -> list[str]:
    problems = []
    # Limit 1: the resumed position holds the only slot, so the new coin waits.
    path = tmp / "k1.json"
    old, new = _token("OLD"), _token("NEW")
    PositionStore(path).add(old, _buy(), _now())
    trader, events, release = await _trader_run(path, [new])
    trader._stored_tokens.append(old)  # noqa: SLF001
    trader.solana_client.hold(old, BOUGHT_RAW)
    release["OLD"] = asyncio.Event()
    start = asyncio.create_task(trader.start())
    await _until(lambda: "hold OLD resumed" in events)
    await asyncio.sleep(0.1)
    if "buy NEW" in events:
        problems.append(f"bought with the slot held by a resumed position: {events}")
    release["OLD"].set()
    await _until(lambda: "hold NEW fresh" in events)
    release["NEW"].set()
    await asyncio.wait_for(start, 5)
    want = [
        "hold OLD resumed",
        "exit OLD",
        "buy NEW",
        "hold NEW fresh",
        "exit NEW",
        "cleanup ['NEW', 'OLD']",
    ]
    if events != want:
        problems.append(f"limit 1: {events}, expected {want}")
    problems += await _check_k_over_limit(tmp)
    problems += await _check_k_held_not_rebought(tmp)

    # Single-token mode with no new coin still waits for the resumed position.
    path = tmp / "k2.json"
    old = _token("OLD")
    PositionStore(path).add(old, _buy(), _now())
    trader, events, release = await _trader_run(path, [], max_open_positions=2)
    trader._stored_tokens.append(old)  # noqa: SLF001
    trader.solana_client.hold(old, BOUGHT_RAW)
    release["OLD"] = asyncio.Event()
    start = asyncio.create_task(trader.start())
    await asyncio.sleep(0.6)  # past token_wait_timeout
    if any(e.startswith("cleanup") for e in events):
        problems.append(
            f"no coin came and cleanup ran under a resumed position: {events}"
        )
    release["OLD"].set()
    await asyncio.wait_for(start, 5)
    if events != ["hold OLD resumed", "exit OLD", "cleanup ['OLD']"]:
        problems.append(f"no new coin: {events}")
    return problems


def check_k_trader_resumes_first(tmp: Path) -> bool:
    return _report(asyncio.run(_check_k(tmp)))


async def _check_m(tmp: Path) -> list[str]:
    problems = []
    path = tmp / "m.json"
    same = _token("SAME")
    other = dataclasses.replace(_token("BONK"), platform=Platform.LETS_BONK)
    store = PositionStore(path)
    for token in (same, other):
        store.add(token, _buy(), _now())
    rig = _Rig(path)  # a pump_fun bot
    for token in (same, other):
        rig.client.hold(token, BOUGHT_RAW)
    resumed = await rig.pm.resume()
    if [t.symbol for t in resumed] != ["SAME"]:
        problems.append(f"resumed {[t.symbol for t in resumed]}, expected SAME only")
    await asyncio.sleep(0.1)
    await rig.pm.stop()
    if any(sell[0] == "BONK" for sell in rig.seller.sells):
        problems.append("a position from another platform was sold")
    if str(other.mint) not in rig.stored():
        problems.append("a position from another platform was dropped from the file")
    return problems


def check_m_other_platform_is_kept(tmp: Path) -> bool:
    return _report(asyncio.run(_check_m(tmp)))


def _run_all_bots_started(tmp: Path, configs: dict[str, dict]) -> list[str]:
    """Which of `configs` (file stem -> config) `run_all_bots` starts."""
    started: list[str] = []
    bots = tmp / "bots"
    bots.mkdir(parents=True)
    for stem in configs:
        (bots / f"{stem}.yaml").write_text("")

    async def start_bot(config_path: str) -> None:
        started.append(Path(config_path).stem)

    originals = (bot_runner.load_bot_config, bot_runner.start_bot)
    bot_runner.load_bot_config = lambda config_path: configs[Path(config_path).stem]
    bot_runner.start_bot = start_bot
    cwd = Path.cwd()
    os.chdir(tmp)
    try:
        bot_runner.run_all_bots()
    finally:
        os.chdir(cwd)
        bot_runner.load_bot_config, bot_runner.start_bot = originals
    return started


def check_l_runner_and_gitignore(tmp: Path) -> bool:
    problems = []
    config = {
        "name": "sniper-x",
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "trade": {"buy_amount": 0.01, "buy_slippage": 0.1, "sell_slippage": 0.1},
        "filters": {"listener_type": "logs"},
    }
    settings = bot_runner.build_trader_settings(config, Platform.PUMP_FUN)
    if settings.positions_file != "positions/sniper-x.json":
        problems.append(f"positions_file {settings.positions_file!r}")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "positions/sniper-x.json"],  # noqa: S607
        cwd=PROJECT_ROOT,
        check=False,
    )
    if ignored.returncode != 0:
        problems.append("git does not ignore positions/")

    def bot(name: str, enabled: bool = True) -> dict:  # noqa: FBT001, FBT002
        return {"name": name, "enabled": enabled, "filters": {"listener_type": "logs"}}

    started = _run_all_bots_started(
        tmp / "l",
        {"a": bot("x", enabled=False), "b": bot("x"), "c": bot("x"), "d": bot("y")},
    )
    if started != ["b", "d"]:
        problems.append(f"started {started}: two enabled bots shared a name")
    return _report(problems)


def main() -> None:
    print("=" * 72)
    print("Verifying open positions survive a restart")
    print("=" * 72)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        cwd = Path.cwd()
        os.chdir(tmp)  # the trade log writes under trades/ in the working dir
        try:
            checks = [
                ("A: the file round-trips a position", lambda: check_a_round_trip(tmp)),
                ("B: written on buy, removed on sale", lambda: check_b_written_on_buy_removed_on_sale(tmp)),
                ("C: unsold positions stay", lambda: check_c_unsold_positions_stay(tmp)),
                ("D: manual and marry positions not kept", lambda: check_d_manual_and_marry_not_kept(tmp)),
                ("E: tp_sl resumes from its buy", lambda: check_e_tp_sl_resumes_from_its_buy(tmp)),
                ("F: time_based resumes from its buy", lambda: check_f_time_based_resumes_from_its_buy(tmp)),
                ("G: sold while down is cleared, not resold", lambda: check_g_sold_while_down_is_cleared(tmp)),
                ("H: sells what the wallet holds", lambda: check_h_sells_what_is_held(tmp)),
                ("I: unreadable balance: kept, not sold", lambda: check_i_unreadable_balance_is_kept(tmp)),
                ("J: bad records and files are kept", lambda: check_j_bad_records_are_kept(tmp)),
                ("K: the trader resumes before taking a coin", lambda: check_k_trader_resumes_first(tmp)),
                ("L: positions/<name>.json, git-ignored, names unique", lambda: check_l_runner_and_gitignore(tmp)),
                ("M: a position from another platform is kept", lambda: check_m_other_platform_is_kept(tmp)),
            ]  # fmt: skip
            results = []
            for name, check in checks:
                ok = check()
                results.append(ok)
                print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)
        finally:
            os.chdir(cwd)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

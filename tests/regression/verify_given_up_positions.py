"""Verify positions the bot gives up on are marked, reported and retried sensibly.

When an exit runs out of attempts or a sell can't be resolved, the position
stays in the wallet and the positions file but is no longer monitored. That
used to show only as a log line: the file looked like any other position and
the session summary only counted it. A sale the curve could pay nothing for
was also retried back to back.

Offline machine checks, no network and no funds moved:

  1. Each way an exit gives up (time-based attempts spent, time-based sell
     unresolved, tp_sl attempts spent, tp_sl sell unresolved) writes
     `given_up` with a reason to the position's record, and the session lists
     the coin.
  2. A resumed position loses its `given_up` mark: the bot is trying again.
  3. A time-based sale the curve can pay nothing for waits
     `price_check_interval` before the next attempt.
  4. `PositionStore.peek` reads the file without writing, renaming or
     logging; an unreadable file raises and stays where it is.
  5. The session summary gives one line per coin given up on, with its rent
     once read, and nothing extra when there are none.
  6. At shutdown the manager reads each given-up coin's token account in one
     call at `confirmed` and records its rent; a failed read leaves it unknown.

Usage:
    uv run tests/regression/verify_given_up_positions.py
"""

import asyncio
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.account import Account  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core import session  # noqa: E402
from core.pubkeys import SystemAddresses  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo, TradeFailureReason  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from trading import trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402

PRICE = 1.0e-6
AMOUNT = 1_000.0
AMOUNT_RAW = 1_000_000_000
INTERVAL = 0.3
EXIT_TIMEOUT = 10


@dataclass
class _Curve:
    async def calculate_price(self, _pool: Pubkey) -> float:
        return PRICE


@dataclass
class _Seller:
    """Fails every sale the way `result` says, and records when."""

    result: TradeResult
    calls: list[float] = field(default_factory=list)

    async def execute(self, *_args: object, **_kwargs: object) -> TradeResult:
        self.calls.append(time.monotonic())
        return self.result


def _coin() -> TokenInfo:
    return TokenInfo(
        name="GiveUp",
        symbol="GVUP",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
    )


def _reverted(token: TokenInfo) -> TradeResult:
    return TradeResult(
        success=False,
        platform=token.platform,
        tx_signature="reverted",
        error_message="custom program error",
        failure_reason=TradeFailureReason.REVERTED,
    )


def _unresolved(token: TokenInfo) -> TradeResult:
    # No reason and no signature: the manager can't tell, so it stops.
    return TradeResult(success=False, platform=token.platform, error_message="?")


def _curve_short(token: TokenInfo) -> TradeResult:
    return TradeResult(
        success=False,
        platform=token.platform,
        error_message="the curve can pay nothing",
        failure_reason=TradeFailureReason.SUBMIT_FAILED,
        curve_short=True,
    )


def _buy(token: TokenInfo) -> TradeResult:
    return TradeResult(
        success=True,
        platform=token.platform,
        tx_signature="buy",
        amount=AMOUNT,
        amount_raw=AMOUNT_RAW,
        price=PRICE,
    )


def _manager(store: PositionStore, seller: _Seller, attempts: int) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.wait_time_after_buy = 0
    manager.max_exit_sell_attempts = attempts
    manager.price_check_interval = INTERVAL
    manager.platform_implementations = PlatformImplementations(
        address_provider=None,
        instruction_builder=None,
        curve_manager=_Curve(),
        event_parser=None,
    )
    manager.seller = seller
    manager.solana_client = None
    manager.wallet = None
    manager.priority_fee_manager = None
    manager.cleanup_mode = "disabled"
    manager.cleanup_with_priority_fee = False
    manager.cleanup_force_close_with_burn = False
    manager._graduated_mints = set()  # noqa: SLF001
    manager.store = store
    return manager


def _store(tmp: str, token: TokenInfo) -> PositionStore:
    store = PositionStore(Path(tmp) / "bot.json")
    store.add(token, _buy(token), datetime.now(UTC).replace(tzinfo=None))
    return store


async def _time_based(token: TokenInfo, seller: _Seller, store: PositionStore) -> None:
    manager = _manager(store, seller, attempts=2)
    await asyncio.wait_for(
        manager._handle_time_based_exit(token, _buy(token)),  # noqa: SLF001
        timeout=EXIT_TIMEOUT,
    )


async def _tp_sl(token: TokenInfo, seller: _Seller, store: PositionStore) -> None:
    manager = _manager(store, seller, attempts=1)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=PRICE,
        quantity=AMOUNT,
        quantity_raw=AMOUNT_RAW,
    )
    await manager._run_exit_attempt(  # noqa: SLF001
        token, position, ExitReason.STOP_LOSS, PRICE, attempt=1
    )


async def check_give_up_marks() -> bool:
    trade_log.log_trade = lambda *_a, **_k: None
    results = []
    paths = {
        "time-based attempts spent": (_time_based, _reverted),
        "time-based sell unresolved": (_time_based, _unresolved),
        "tp_sl attempts spent": (_tp_sl, _reverted),
        "tp_sl sell unresolved": (_tp_sl, _unresolved),
    }
    for label, (run, failure) in paths.items():
        session.start("verify")
        token = _coin()
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp, token)
            await run(token, _Seller(failure(token)), store)
            [stored] = PositionStore.peek(store.path)
        listed = [g.mint for g in session.current().given_up]
        ok = bool(stored.given_up) and listed == [token.mint]
        results.append(_check(f"gave up: {label}", ok, f"{stored.given_up!r}"))
    return all(results)


async def check_resume_clears_mark() -> bool:
    token = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, token)
        store.mark_given_up(token.mint, "attempts spent")
        manager = _manager(store, _Seller(_reverted(token)), attempts=1)
        manager.platform = Platform.PUMP_FUN
        manager.wallet = None
        opened = []

        async def held(_token: TokenInfo) -> int:
            return AMOUNT_RAW

        async def resolved(_token: TokenInfo, _label: str) -> bool:
            return True

        manager._held_raw = held  # noqa: SLF001
        manager._quote_resolved = resolved  # noqa: SLF001
        manager.open = lambda t, _b, **_k: opened.append(t.mint)
        [stored_before] = PositionStore.peek(store.path)
        await manager._resume_one(stored_before)  # noqa: SLF001
        [stored_after] = PositionStore.peek(store.path)
    ok = (
        stored_before.given_up == "attempts spent"
        and stored_after.given_up is None
        and opened == [token.mint]
    )
    return _check(
        "a resumed position loses its mark",
        ok,
        f"{stored_before.given_up!r} -> {stored_after.given_up!r}",
    )


async def check_curve_short_retries_wait() -> bool:
    token = _coin()
    seller = _Seller(_curve_short(token))
    with tempfile.TemporaryDirectory() as tmp:
        await _time_based(token, seller, _store(tmp, token))
    gaps = [b - a for a, b in zip(seller.calls, seller.calls[1:], strict=False)]
    ok = len(seller.calls) == 2 and all(g >= INTERVAL for g in gaps)  # noqa: PLR2004
    return _check(
        "a sale the curve can't pay waits before the retry",
        ok,
        f"{len(seller.calls)} attempts, gaps {[round(g, 2) for g in gaps]}",
    )


def check_peek() -> bool:
    results = []
    token = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, token)
        before = store.path.read_bytes()
        peeked = PositionStore.peek(store.path)
        after = sorted(p.name for p in Path(tmp).iterdir())
        ok = (
            [p.token_info.mint for p in peeked] == [token.mint]
            and store.path.read_bytes() == before
            and after == ["bot.json"]
        )
        results.append(_check("peek reads without writing", ok, str(after)))
        store.path.write_text("{not json")
        try:
            PositionStore.peek(store.path)
        except (OSError, ValueError):
            left = sorted(p.name for p in Path(tmp).iterdir())
            results.append(
                _check(
                    "peek on an unreadable file raises, file stays",
                    left == ["bot.json"],
                    str(left),
                )
            )
        else:
            results.append(
                _check(
                    "peek on an unreadable file raises", passed=False, detail="returned"
                )
            )
        missing = PositionStore.peek(Path(tmp) / "none.json")
        results.append(_check("peek on no file is empty", missing == [], str(missing)))
    return all(results)


def check_session_lines() -> bool:
    session.start("verify")
    plain = session.current().lines(held=0, pending=0)
    token = _coin()
    session.current().add_given_up(token, AMOUNT_RAW, "attempts spent")
    without_rent = session.current().lines(held=1, pending=0)
    session.current().set_given_up_rent(token.mint, 2_039_280)
    with_rent = session.current().lines(held=1, pending=0)
    line = next((x for x in with_rent if x.startswith("Gave up on")), "")
    ok = (
        not any(x.startswith("Gave up on") for x in plain)
        and str(token.mint) in line
        and "attempts spent" in line
        and "1000" in line
        and "0.002039" in line
        and any("unknown" in x for x in without_rent if x.startswith("Gave up on"))
    )
    return _check("session lists each coin given up on", ok, line)


class _AccountsClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[list[Pubkey], str | None]] = []

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Account | None]:
        self.calls.append((pubkeys, commitment))
        if self.fail:
            raise ConnectionError("node unavailable")
        return [
            Account(
                lamports=2_039_280,
                data=bytes(165),
                owner=SystemAddresses.TOKEN_PROGRAM,
                executable=False,
                rent_epoch=0,
            )
            for _ in pubkeys
        ]


async def check_rent_read() -> bool:
    results = []
    for fail in (False, True):
        session.start("verify")
        token = _coin()
        token.token_program_id = SystemAddresses.TOKEN_PROGRAM
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp, token)
            client = _AccountsClient(fail=fail)
            manager = _manager(store, _Seller(_reverted(token)), attempts=1)
            manager.solana_client = client
            manager.wallet = Wallet(str(Keypair()))
            manager._give_up(token, "attempts spent", AMOUNT_RAW)  # noqa: SLF001
            await manager.read_given_up_rent()
        [given_up] = session.current().given_up
        want = None if fail else 2_039_280
        ok = (
            given_up.rent_lamports == want
            and len(client.calls) == 1
            and client.calls[0][1] == "confirmed"
        )
        label = "a failed read leaves rent unknown" if fail else "rent read once"
        results.append(_check(label, ok, f"{given_up.rent_lamports} {client.calls}"))
    return all(results)


async def main() -> int:
    print("Given-up positions")
    results = [
        await check_give_up_marks(),
        await check_resume_clears_mark(),
        await check_curve_short_retries_wait(),
        check_peek(),
        check_session_lines(),
        await check_rent_read(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

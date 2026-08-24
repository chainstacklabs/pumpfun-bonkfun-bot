"""Verify the tp/sl exit sells against the fresh price and retries a failed sell.

Two bugs in `UniversalTrader._monitor_position_until_exit` (issue #189):

  1. The sell was handed `position.entry_price` while the `current_price` that
     had just triggered the exit sat in the same scope, fetched one RPC call
     earlier. The seller turns that price into the slippage floor
     (`min_quote_output`), so on a stop-loss the floor was computed from the
     higher entry price and demanded more quote asset than the curve could pay
     — the sell reverts with pump.fun 6003 TooLittleSolReceived exactly during
     the drop the stop-loss exists to escape. On a take-profit the error runs
     the other way: the floor lands far below market and protects nothing.

  2. `break` sat outside both branches of `if sell_result.success:`, so the loop
     exited whether the sell landed or not, contradicting the
     "Keep monitoring in case sell can be retried" comment right above it. The
     seller's own `max_retries` covers transaction *submission* only, so an
     on-chain revert was never retried: the position was abandoned mid-crash.

Offline machine checks, no network and no funds moved. The real monitor loop is
driven with a stub curve manager serving a scripted price series and a stub
seller that records the price it is handed:

  1. A stop-loss exit passes the triggering price, not the entry price.
  2. A take-profit exit passes the triggering price too.
  3. The floor built from the entry price is unpayable on a stop-loss, while
     the floor from the triggering price is payable (why check 1 matters).
  4. A failed sell is retried, and a retry that succeeds closes the position.
  5. Retries are bounded, so a token that keeps reverting cannot pin the bot.
  6. A price that recovers before the retry resets the attempt counter.
  7. A successful sell still closes the position on the first attempt.
  8. The cap comes from trade.max_exit_sell_attempts and is wired end to end.

Usage:
    uv run learning-examples/verify_tp_sl_exit_price.py
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, quote_units_per_token  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import Position  # noqa: E402
from trading.universal_trader import (  # noqa: E402
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    UniversalTrader,
)

ENTRY_PRICE = 1.0e-6  # SOL per token
QUANTITY = 1_000_000.0  # tokens, so entry notional is 1.0 SOL
SELL_SLIPPAGE = 0.3  # bots/*.yaml default
STOP_LOSS_PCT = 0.4
TAKE_PROFIT_PCT = 0.4

SL_TRIGGER_PRICE = ENTRY_PRICE * 0.55  # 45% down, past the stop loss
TP_TRIGGER_PRICE = ENTRY_PRICE * 1.5  # 50% up, past the take profit

REVERT_6003 = "custom program error: 0x1773 (6003 TooLittleSolReceived)"


@dataclass
class StubCurveManager:
    """Serves a scripted price series; the last value repeats forever."""

    prices: list[float]
    calls: int = 0

    async def calculate_price(self, _pool_address: Pubkey) -> float:
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        return price


@dataclass
class StubSeller:
    """Records the price it is handed. Fails the first `fail_first` calls."""

    fail_first: int = 0
    prices_seen: list[float] = field(default_factory=list)

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        self.prices_seen.append(token_price)
        if len(self.prices_seen) <= self.fail_first:
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=REVERT_6003,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub-signature",
            amount=token_amount,
            price=token_price,
        )


def _make_trader(
    curve_manager: StubCurveManager,
    seller: StubSeller,
    max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
) -> UniversalTrader:
    """Build a trader carrying only what the monitor loop touches."""
    trader = object.__new__(UniversalTrader)
    trader.price_check_interval = 0  # no real waiting between iterations
    trader.max_exit_sell_attempts = max_exit_sell_attempts
    trader.platform_implementations = SimpleNamespace(
        curve_manager=curve_manager, address_provider=None
    )
    trader.seller = seller
    trader.solana_client = None
    trader.wallet = None
    trader.priority_fee_manager = None
    trader.cleanup_mode = "disabled"  # keeps handle_cleanup_after_sell a no-op
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    # Keep a verification run from writing to ./trades.
    trader._log_trade = lambda *_args, **_kwargs: None  # noqa: SLF001
    return trader


def _make_token_info() -> TokenInfo:
    return TokenInfo(
        name="Verify189",
        symbol="V189",
        uri="",
        mint=Pubkey.default(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.default(),
    )


def _make_position() -> Position:
    return Position.create_from_buy_result(
        mint=Pubkey.default(),
        symbol="V189",
        entry_price=ENTRY_PRICE,
        quantity=QUANTITY,
        take_profit_percentage=TAKE_PROFIT_PCT,
        stop_loss_percentage=STOP_LOSS_PCT,
        max_hold_time=None,
    )


def _slippage_floor(reference_price: float) -> int:
    """Reproduce the seller's min_quote_output for the fixture position.

    Mirrors PlatformAwareSeller.execute: expected output is amount * price,
    then the slippage tolerance comes off it, in the quote mint's raw units.
    """
    expected_quote_output = QUANTITY * reference_price
    return max(
        1,
        int(
            (expected_quote_output * (1 - SELL_SLIPPAGE))
            * quote_units_per_token(WSOL_MINT)
        ),
    )


def _payable(price: float) -> int:
    """Raw quote units the pool would return at `price`, ignoring curve impact.

    Optimistic on purpose: a real sell moves the curve down and pays a fee, so
    anything unpayable against this number is unpayable on chain too.
    """
    return int(QUANTITY * price * quote_units_per_token(WSOL_MINT))


MONITOR_TIMEOUT = 10  # a bounded loop finishes in milliseconds here


async def _run_monitor(
    prices: list[float],
    fail_first: int = 0,
    max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
) -> tuple[Position, StubSeller, StubCurveManager]:
    """Drive the real monitor loop to completion over a scripted price series.

    Raises:
        TimeoutError: If the loop never exits, i.e. retries are unbounded.
    """
    curve_manager = StubCurveManager(prices=list(prices))
    seller = StubSeller(fail_first=fail_first)
    position = _make_position()
    trader = _make_trader(curve_manager, seller, max_exit_sell_attempts)
    await asyncio.wait_for(
        trader._monitor_position_until_exit(_make_token_info(), position),  # noqa: SLF001
        timeout=MONITOR_TIMEOUT,
    )
    return position, seller, curve_manager


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def check_stop_loss_uses_trigger_price() -> bool:
    print("\n1. Stop-loss exit sells against the triggering price")
    _, seller, _ = await _run_monitor([ENTRY_PRICE, SL_TRIGGER_PRICE])
    price = seller.prices_seen[0]
    return _check(
        "price handed to seller",
        price == SL_TRIGGER_PRICE,
        f"{price:.8f} SOL (trigger {SL_TRIGGER_PRICE:.8f}, entry {ENTRY_PRICE:.8f})",
    )


async def check_take_profit_uses_trigger_price() -> bool:
    print("\n2. Take-profit exit sells against the triggering price")
    _, seller, _ = await _run_monitor([ENTRY_PRICE, TP_TRIGGER_PRICE])
    price = seller.prices_seen[0]
    return _check(
        "price handed to seller",
        price == TP_TRIGGER_PRICE,
        f"{price:.8f} SOL (trigger {TP_TRIGGER_PRICE:.8f}, entry {ENTRY_PRICE:.8f})",
    )


def check_stale_floor_is_unpayable() -> bool:
    print("\n3. Why it matters: the entry-price floor is unpayable on a drop")
    from_entry = _slippage_floor(ENTRY_PRICE)
    from_trigger = _slippage_floor(SL_TRIGGER_PRICE)
    payable = _payable(SL_TRIGGER_PRICE)
    print(
        f"       floor from entry price   : {from_entry:>14,} raw quote units\n"
        f"       floor from trigger price : {from_trigger:>14,}\n"
        f"       pool can pay (optimistic): {payable:>14,}"
    )
    ok = _check(
        "entry-price floor exceeds what the pool can pay",
        from_entry > payable,
        f"{from_entry:,} > {payable:,}, so the sell reverts 6003",
    )
    return ok and _check(
        "trigger-price floor is payable",
        from_trigger <= payable,
        f"{from_trigger:,} <= {payable:,}",
    )


async def check_failed_sell_is_retried() -> bool:
    print("\n4. A failed sell is retried on the next price check")
    fail_first = 1
    position, seller, _ = await _run_monitor(
        [ENTRY_PRICE, SL_TRIGGER_PRICE], fail_first=fail_first
    )
    expected = fail_first + 1  # the failure, then the retry that lands
    ok = _check(
        "seller called again after the failure",
        len(seller.prices_seen) == expected,
        f"{len(seller.prices_seen)} attempts, expected {expected}",
    )
    ok = (
        _check(
            "position closed after the retry landed",
            not position.is_active,
            f"is_active={position.is_active}, "
            f"exit_reason={position.exit_reason.value if position.exit_reason else None}",
        )
        and ok
    )
    return ok


async def check_retries_are_bounded() -> bool:
    print("\n5. Retries are bounded, so a reverting token cannot pin the bot")
    # fail_first far above the cap: the loop must give up on its own, so the
    # timeout firing is itself a failure - it means the retry never terminates
    # and the bot would sit on this position forever.
    try:
        position, seller, _ = await _run_monitor(
            [ENTRY_PRICE, SL_TRIGGER_PRICE], fail_first=99
        )
    except TimeoutError:
        return _check(
            "monitor loop terminates on repeated failures",
            False,  # noqa: FBT003
            f"still retrying after {MONITOR_TIMEOUT}s - retries are unbounded",
        )
    ok = _check(
        "attempts capped at the configured maximum",
        len(seller.prices_seen) == DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        f"{len(seller.prices_seen)} attempts, cap {DEFAULT_MAX_EXIT_SELL_ATTEMPTS}",
    )
    ok = (
        _check(
            "position not falsely marked closed",
            position.is_active and position.exit_price is None,
            f"is_active={position.is_active}, exit_price={position.exit_price}",
        )
        and ok
    )
    # Every retry must re-read the price rather than reuse the first one.
    ok = (
        _check(
            "every attempt used the freshly read price",
            all(p == SL_TRIGGER_PRICE for p in seller.prices_seen),
            f"prices seen: {[f'{p:.8f}' for p in seller.prices_seen]}",
        )
        and ok
    )
    return ok


async def check_recovery_resets_attempts() -> bool:
    print("\n6. A price recovery between attempts resets the attempt counter")
    # Fail every sell. The price dips below the stop loss, recovers to flat,
    # then dips again. With the counter reset on recovery, the cap applies to
    # each burst separately, so the total is one full cap plus the earlier dip.
    prices = [ENTRY_PRICE, SL_TRIGGER_PRICE, ENTRY_PRICE, SL_TRIGGER_PRICE]
    _, seller, _ = await _run_monitor(prices, fail_first=99)
    expected = 1 + DEFAULT_MAX_EXIT_SELL_ATTEMPTS
    return _check(
        "attempts counted per burst, not per position",
        len(seller.prices_seen) == expected,
        f"{len(seller.prices_seen)} attempts (1 before recovery + "
        f"{DEFAULT_MAX_EXIT_SELL_ATTEMPTS} after), expected {expected}",
    )


async def check_successful_sell_closes_once() -> bool:
    print("\n7. A successful sell still closes the position on the first attempt")
    position, seller, _ = await _run_monitor([ENTRY_PRICE, TP_TRIGGER_PRICE])
    ok = _check(
        "single sell attempt",
        len(seller.prices_seen) == 1,
        f"{len(seller.prices_seen)} attempt",
    )
    return (
        _check(
            "position closed with the exit recorded",
            not position.is_active
            and position.exit_reason is not None
            and position.exit_price == TP_TRIGGER_PRICE,
            f"is_active={position.is_active}, "
            f"reason={position.exit_reason.value if position.exit_reason else None}, "
            f"exit_price={position.exit_price}",
        )
        and ok
    )


async def check_config_knob_is_honoured() -> bool:
    """The cap comes from trade.max_exit_sell_attempts, not a hardcoded value."""
    print("\n8. trade.max_exit_sell_attempts drives the cap")
    configured = 2  # deliberately different from the default
    _, seller, _ = await _run_monitor(
        [ENTRY_PRICE, SL_TRIGGER_PRICE],
        fail_first=99,
        max_exit_sell_attempts=configured,
    )
    ok = _check(
        "configured value overrides the default",
        len(seller.prices_seen) == configured != DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        f"{len(seller.prices_seen)} attempts with the knob set to {configured} "
        f"(default {DEFAULT_MAX_EXIT_SELL_ATTEMPTS})",
    )

    # Static wiring: a knob the runner never reads is a knob that does nothing.
    runner = (PROJECT_ROOT / "src" / "bot_runner.py").read_text()
    ok = (
        _check(
            "bot_runner reads it from the trade config",
            'cfg["trade"].get(' in runner
            and "max_exit_sell_attempts" in runner
            and "DEFAULT_MAX_EXIT_SELL_ATTEMPTS" in runner,
            "passed to UniversalTrader with the module default as fallback",
        )
        and ok
    )
    loader = (PROJECT_ROOT / "src" / "config_loader.py").read_text()
    ok = (
        _check(
            "config_loader validates its range",
            "trade.max_exit_sell_attempts" in loader,
            "a 0 or a string in the YAML is rejected at startup",
        )
        and ok
    )
    documented = sorted(
        path.name
        for path in (PROJECT_ROOT / "bots").glob("*.yaml")
        if "max_exit_sell_attempts" in path.read_text()
    )
    bots = sorted(path.name for path in (PROJECT_ROOT / "bots").glob("*.yaml"))
    return (
        _check(
            "every bot config documents it",
            documented == bots,
            f"{len(documented)}/{len(bots)} configs mention it",
        )
        and ok
    )


async def main() -> int:
    print("Verifying tp/sl exit pricing and retry behaviour (issue #189)")
    print(
        f"fixture: entry {ENTRY_PRICE:.8f} SOL, {QUANTITY:,.0f} tokens, "
        f"SL -{STOP_LOSS_PCT:.0%}, TP +{TAKE_PROFIT_PCT:.0%}, "
        f"sell slippage {SELL_SLIPPAGE:.0%}"
    )

    results = [
        await check_stop_loss_uses_trigger_price(),
        await check_take_profit_uses_trigger_price(),
        check_stale_floor_is_unpayable(),
        await check_failed_sell_is_retried(),
        await check_retries_are_bounded(),
        await check_recovery_resets_attempts(),
        await check_successful_sell_closes_once(),
        await check_config_knob_is_honoured(),
    ]

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    if passed != len(results):
        print("FAILED")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

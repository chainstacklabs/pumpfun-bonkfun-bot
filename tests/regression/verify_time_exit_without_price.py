"""Verify max_hold_time still fires when the price read keeps failing.

`UniversalTrader._monitor_position_until_exit` read the price at the top of each
iteration and evaluated every exit condition after it. `position.should_exit()`
takes `current_price`, so a failed read skipped the whole check - including
`max_hold_time`, which needs no price at all.

If the read kept failing the loop span forever: `position.is_active` never
changed, the position was never sold, and the bot never moved on. The only
signal was a repeating `Error monitoring position`.

That is not hypothetical. During live testing of the previous fix,
`calculate_price` returned `Invalid bonding curve state: Account ... not found`
three times in a row for a curve that provably existed - a load-balanced
endpoint serving nodes behind the one that had just confirmed the buy. A longer
outage or a rate-limit storm would hold it open indefinitely.

Offline machine checks, no network and no funds moved. The real monitor loop
runs against a curve manager that fails on demand:

  1. max_hold_time fires with the price feed down - the loop is not infinite.
  2. The blind exit is floored against the last price actually read.
  3. With no successful read ever, it falls back to the entry price.
  4. Before the deadline, a failing read keeps monitoring and sells nothing.
  5. The blind exit is still bounded by trade.max_exit_sell_attempts.
  6. should_exit_on_time stays False without a deadline or an open position.
  7. A curve that prices at 0.0 never becomes the slippage floor, on either
     the blind path or the ordinary one.

Usage:
    uv run tests/regression/verify_time_exit_without_price.py
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from interfaces.core import Platform, TokenInfo, TradeFailureReason  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

BUY_PRICE = 1.0e-6
QUANTITY = 1_000_000.0
READABLE_PRICE = BUY_PRICE * 0.8  # read once, before the feed goes down
MAX_HOLD_TIME = 30  # seconds
MAX_EXIT_SELL_ATTEMPTS = 3

# A loop that cannot terminate is the bug under test, so every run is bounded.
EXIT_TIMEOUT = 5
STILL_MONITORING_TIMEOUT = 0.5

# For the "feed dies mid-hold" case: short enough to cross during a run, long
# enough that the first read lands before it.
SHORT_HOLD_TIME = 1
CALM_CHECK_INTERVAL = 0.05

RPC_DOWN = "Invalid bonding curve state: Account not found"
NON_POSITIVE_PRICE = "token_price is required for sell operation and must be positive."

# What calculate_price returns for a curve with no virtual token reserves left.
# It does not raise, so a naive `last_known_price = current_price` stores it,
# and the seller rejects a non-positive price with a ValueError raised before
# its own try block - escaping the bounded exit handling entirely.
CURVE_PRICED_AT_ZERO = 0.0


class FlakyCurveManager:
    """Serves a few prices, then fails every read from then on."""

    def __init__(self, prices: list[float] | None = None) -> None:
        self.prices = list(prices or [])
        self.calls = 0

    async def calculate_price(self, _pool_address: Pubkey) -> float:
        """Serve the next price, or fail once the script runs dry.

        Returns:
            The next scripted price

        Raises:
            ValueError: Once no scripted prices remain
        """
        self.calls += 1
        if self.prices:
            return self.prices.pop(0)
        raise ValueError(RPC_DOWN)


class RecordingSeller:
    """Records the floor each sell was priced against."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.prices_seen: list[float] = []

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        """Record the attempt and return the scripted outcome.

        Args:
            token_info: Token being sold
            token_amount: Amount asked for
            token_price: Price the sell is floored against

        Returns:
            The scripted TradeResult

        Raises:
            ValueError: On a non-positive price, exactly as the real seller
                does - and, like the real one, before any try block.
        """
        self.prices_seen.append(token_price)
        if token_price is None or token_price <= 0:
            raise ValueError(NON_POSITIVE_PRICE)
        if len(self.prices_seen) <= self.fail_first:
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message="scripted revert",
                failure_reason=TradeFailureReason.REVERTED,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub-sell",
            amount=token_amount,
            price=token_price,
        )


def _make_token_info() -> TokenInfo:
    """Build the minimum TokenInfo the monitor loop touches.

    Returns:
        A pump.fun TokenInfo
    """
    mint = Pubkey.from_string("11111111111111111111111111111112")
    return TokenInfo(
        name="Stub",
        symbol="STUB",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=mint,
        user=mint,
        creator=mint,
    )


def _make_trader(
    curve_manager: FlakyCurveManager,
    seller: RecordingSeller,
    price_check_interval: float = 0,
) -> UniversalTrader:
    """Build a trader carrying only what the monitor loop touches.

    Args:
        curve_manager: Stub price source
        seller: Stub seller
        price_check_interval: Delay between iterations

    Returns:
        A UniversalTrader wired to the stubs
    """
    trader = object.__new__(UniversalTrader)
    trader.price_check_interval = price_check_interval
    trader.max_exit_sell_attempts = MAX_EXIT_SELL_ATTEMPTS
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
    trader._log_trade = lambda *_a, **_kw: None  # noqa: SLF001
    return trader


def _make_position(
    *, past_deadline: bool, max_hold_time: int = MAX_HOLD_TIME
) -> Position:
    """Build an open position with max_hold_time already met or not.

    Args:
        past_deadline: Whether the hold deadline has already passed
        max_hold_time: Hold deadline in seconds

    Returns:
        An active Position with no tp/sl set
    """
    entry_time = datetime.utcnow()
    if past_deadline:
        entry_time -= timedelta(seconds=max_hold_time + 1)
    return Position(
        mint=Pubkey.from_string("11111111111111111111111111111112"),
        symbol="STUB",
        entry_price=BUY_PRICE,
        quantity=QUANTITY,
        entry_time=entry_time,
        max_hold_time=max_hold_time,
    )


async def _monitor(
    prices: list[float] | None,
    position: Position,
    *,
    fail_first: int = 0,
    timeout: float = EXIT_TIMEOUT,
    price_check_interval: float = 0,
) -> tuple[RecordingSeller, Position, FlakyCurveManager, bool]:
    """Drive the real monitor loop with a price feed that goes down.

    Args:
        prices: Prices served before the feed fails
        position: The open position to monitor
        fail_first: How many sells revert before one succeeds
        timeout: Wall-clock bound, since the bug under test is an endless loop
        price_check_interval: Delay between iterations

    Returns:
        (seller, position, curve_manager, timed_out)
    """
    curve_manager = FlakyCurveManager(prices)
    seller = RecordingSeller(fail_first=fail_first)
    trader = _make_trader(curve_manager, seller, price_check_interval)
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            await trader._monitor_position_until_exit(  # noqa: SLF001
                _make_token_info(), position
            )
    except TimeoutError:
        timed_out = True
    return seller, position, curve_manager, timed_out


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    """Print one check result.

    Args:
        label: What was checked
        passed: Whether it held
        detail: Evidence behind the verdict

    Returns:
        The value of `passed`
    """
    print(f"{'PASS' if passed else 'FAIL'}  {label}\n      {detail}")
    return passed


async def check_time_exit_fires_without_price() -> bool:
    """The deadline is enforced even with every price read failing.

    Returns:
        Whether the check passed
    """
    seller, position, _curve, timed_out = await _monitor(
        [], _make_position(past_deadline=True)
    )
    return _check(
        "max_hold_time fires with the price feed down - the loop terminates",
        not timed_out and len(seller.prices_seen) >= 1 and not position.is_active,
        f"timed out: {timed_out}, {len(seller.prices_seen)} sell(s), "
        f"position active: {position.is_active}, "
        f"exit reason: {position.exit_reason.value if position.exit_reason else None}",
    )


async def check_uses_last_known_price() -> bool:
    """The blind exit is floored against the last price actually read.

    The feed has to die *after* a successful read and *before* the deadline,
    or the exit goes out through the ordinary priced path and proves nothing.

    Returns:
        Whether the check passed
    """
    seller, _position, curve, timed_out = await _monitor(
        [READABLE_PRICE],
        _make_position(past_deadline=False, max_hold_time=SHORT_HOLD_TIME),
        price_check_interval=CALM_CHECK_INTERVAL,
    )
    floored_at = seller.prices_seen[0] if seller.prices_seen else None
    went_blind = curve.calls > 1  # the exit came after reads started failing
    return _check(
        "the blind exit prices off the last price actually read",
        not timed_out and went_blind and floored_at == READABLE_PRICE,
        f"sold against {floored_at} after {curve.calls} read(s), "
        f"last good read {READABLE_PRICE}, entry {BUY_PRICE}",
    )


async def check_falls_back_to_entry_price() -> bool:
    """With no read ever succeeding, the entry price is the floor.

    Returns:
        Whether the check passed
    """
    seller, _position, _curve, timed_out = await _monitor(
        [], _make_position(past_deadline=True)
    )
    floored_at = seller.prices_seen[0] if seller.prices_seen else None
    return _check(
        "with no successful read ever, it falls back to the entry price",
        not timed_out and floored_at == BUY_PRICE,
        f"sold against {floored_at}, entry {BUY_PRICE}",
    )


async def check_no_early_exit() -> bool:
    """Before the deadline, a failing read sells nothing.

    Returns:
        Whether the check passed
    """
    seller, position, _curve, timed_out = await _monitor(
        [],
        _make_position(past_deadline=False),
        timeout=STILL_MONITORING_TIMEOUT,
    )
    return _check(
        "before the deadline, a failing read keeps monitoring and sells nothing",
        timed_out and not seller.prices_seen and position.is_active,
        f"{len(seller.prices_seen)} sell(s), still monitoring: {timed_out}, "
        f"position active: {position.is_active}",
    )


async def check_blind_exit_is_bounded() -> bool:
    """A blind exit that keeps reverting gives up rather than looping.

    Returns:
        Whether the check passed
    """
    seller, position, _curve, timed_out = await _monitor(
        [],
        _make_position(past_deadline=True),
        fail_first=MAX_EXIT_SELL_ATTEMPTS + 5,
    )
    return _check(
        "a blind exit that keeps reverting is bounded by max_exit_sell_attempts",
        not timed_out and len(seller.prices_seen) == MAX_EXIT_SELL_ATTEMPTS,
        f"{len(seller.prices_seen)} attempt(s), cap {MAX_EXIT_SELL_ATTEMPTS}, "
        f"position left open: {position.is_active}",
    )


async def check_zero_price_never_becomes_the_floor() -> bool:
    """A curve priced at 0.0 is never handed to the seller as a floor.

    Two ways it could be. The blind path stores the last read, so a 0.0 would
    be replayed once the feed went down. The ordinary path is worse: 0.0
    satisfies `current_price <= stop_loss_price`, so the stop loss fires and
    the exit is priced off the same 0.0 in the same iteration.

    Returns:
        Whether the check passed
    """
    # Ordinary path: one readable 0.0, which trips the stop loss immediately.
    curve = FlakyCurveManager([CURVE_PRICED_AT_ZERO])
    seller = RecordingSeller()
    trader = _make_trader(curve, seller)
    position = _make_position(past_deadline=False)
    position.stop_loss_price = BUY_PRICE * 0.5  # 0.0 is comfortably below this
    timed_out = False
    try:
        async with asyncio.timeout(EXIT_TIMEOUT):
            await trader._monitor_position_until_exit(  # noqa: SLF001
                _make_token_info(), position
            )
    except TimeoutError:
        timed_out = True
    except ValueError:
        escaped = False  # the ValueError got out; that is the bug
        return _check(
            "a curve priced at 0.0 never becomes the slippage floor",
            escaped,
            "the seller's non-positive price ValueError escaped the monitor loop",
        )

    floored_at = seller.prices_seen[0] if seller.prices_seen else None
    return _check(
        "a curve priced at 0.0 never becomes the slippage floor",
        not timed_out
        and floored_at == BUY_PRICE
        and all(p > 0 for p in seller.prices_seen),
        f"stop loss fired on a 0.0 read; sold against {floored_at} "
        f"(entry {BUY_PRICE}), all floors positive: "
        f"{all(p > 0 for p in seller.prices_seen)}",
    )


def check_should_exit_on_time_is_narrow() -> bool:
    """should_exit_on_time answers only its own question.

    Returns:
        Whether the check passed
    """
    no_deadline = _make_position(past_deadline=True)
    no_deadline.max_hold_time = None
    closed = _make_position(past_deadline=True)
    closed.close_position(BUY_PRICE, ExitReason.MANUAL)
    due = _make_position(past_deadline=True)
    early = _make_position(past_deadline=False)

    return _check(
        "should_exit_on_time is False without a deadline or an open position",
        not no_deadline.should_exit_on_time()
        and not closed.should_exit_on_time()
        and due.should_exit_on_time()
        and not early.should_exit_on_time(),
        f"no max_hold_time: {no_deadline.should_exit_on_time()}, "
        f"already closed: {closed.should_exit_on_time()}, "
        f"past deadline: {due.should_exit_on_time()}, "
        f"before deadline: {early.should_exit_on_time()}",
    )


async def main() -> None:
    """Run every check and exit non-zero if any failed."""
    print("=" * 72)
    print("Verifying max_hold_time survives a dead price feed")
    print("=" * 72)

    results = [
        await check_time_exit_fires_without_price(),
        await check_uses_last_known_price(),
        await check_falls_back_to_entry_price(),
        await check_no_early_exit(),
        await check_blind_exit_is_bounded(),
        check_should_exit_on_time_is_narrow(),
        await check_zero_price_never_becomes_the_floor(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""Verify the time-based exit retries a reverted sell instead of abandoning it.

`exit_strategy: "time_based"` is what every config in `bots/` ships with, and
`UniversalTrader._handle_time_based_exit` used to sell exactly once: a revert
was logged at ERROR and the position was left open, unmonitored and unsold.

Issue #189 added a bounded retry with a re-read price, but only to
`_monitor_position_until_exit`, which is the `tp_sl` path. The default path kept
the single-shot behaviour, so the same 6003 `TooLittleSolReceived` revert that
#189 exists to survive still stranded the tokens.

The seller's own `max_retries` does not cover this: it retries transaction
*submission*, while an on-chain revert comes back as `success=False`. The retry
has to happen here, where the price can be read again first — a floor built from
the buy price is exactly what the revert was complaining about.

Offline machine checks, no network and no funds moved. The real
`_handle_time_based_exit` runs against a stub seller that reverts on demand and
a stub curve manager serving a scripted price series:

  1. A reverted sell is retried rather than abandoned after one attempt.
  2. The first attempt prices off the buy — nothing fresher exists yet.
  3. The retry prices off a freshly read price, not the stale buy price.
  4. Retries are bounded by trade.max_exit_sell_attempts.
  5. A sell that succeeds first time is not retried, and cleanup still runs.
  6. A price re-read that fails does not abort the retry.

Usage:
    uv run learning-examples/verify_time_based_exit_retry.py
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from interfaces.core import (  # noqa: E402
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading.base import TradeResult  # noqa: E402
from trading.universal_trader import (  # noqa: E402
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    UniversalTrader,
)

BUY_PRICE = 1.0e-6  # SOL per token, the price the buy came back with
QUANTITY = 1_000_000.0
FRESH_PRICE = BUY_PRICE * 0.6  # the market has moved down since the buy

REVERT_6003 = "custom program error: 0x1773 (6003 TooLittleSolReceived)"
EXIT_TIMEOUT = 10  # a bounded retry loop finishes in milliseconds here

# One revert followed by one retry: the smallest series that tells a retry
# apart from the single-shot sell this verifier exists to rule out.
REVERT_THEN_RETRY = 2
RPC_UNAVAILABLE = "RPC unavailable"


@dataclass
class StubCurveManager:
    """Serves a scripted price series; the last value repeats forever."""

    prices: list[float]
    calls: int = 0
    raises: bool = False

    async def calculate_price(self, _pool_address: Pubkey) -> float:
        self.calls += 1
        if self.raises:
            raise ConnectionError(RPC_UNAVAILABLE)
        return self.prices[min(self.calls - 1, len(self.prices) - 1)]


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
                # A revert is what this stub simulates, so it has to say so:
                # an exit sell is only retried when retrying is provably safe,
                # and a failure with no reason is treated as unresolved.
                tx_signature="stub-reverted-signature",
                error_message=REVERT_6003,
                failure_reason=TradeFailureReason.REVERTED,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub-signature",
            amount=token_amount,
            price=token_price,
        )


def _make_token_info() -> TokenInfo:
    return TokenInfo(
        name="VerifyExit",
        symbol="VEXIT",
        uri="",
        mint=Pubkey.default(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.default(),
    )


def _make_trader(
    curve_manager: StubCurveManager,
    seller: StubSeller,
    max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
) -> UniversalTrader:
    """Build a trader carrying only what the time-based exit touches."""
    trader = object.__new__(UniversalTrader)
    trader.wait_time_after_buy = 0  # no real waiting before the sell
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
    trader.cleanups = []
    # Keep a verification run from writing to ./trades.
    trader._log_trade = lambda *_args, **_kwargs: None  # noqa: SLF001
    return trader


async def _run_exit(
    fail_first: int = 0,
    prices: list[float] | None = None,
    max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    *,
    price_read_raises: bool = False,
) -> tuple[StubSeller, StubCurveManager]:
    """Drive the real time-based exit to completion.

    Raises:
        TimeoutError: If the exit never returns, i.e. retries are unbounded.
    """
    curve_manager = StubCurveManager(
        prices=list(prices or [FRESH_PRICE]), raises=price_read_raises
    )
    seller = StubSeller(fail_first=fail_first)
    trader = _make_trader(curve_manager, seller, max_exit_sell_attempts)
    buy_result = TradeResult(
        success=True,
        platform=Platform.PUMP_FUN,
        tx_signature="stub-buy",
        amount=QUANTITY,
        price=BUY_PRICE,
    )
    await asyncio.wait_for(
        trader._handle_time_based_exit(_make_token_info(), buy_result),  # noqa: SLF001
        timeout=EXIT_TIMEOUT,
    )
    return seller, curve_manager


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def check_reverted_sell_is_retried() -> bool:
    print("\n1. A reverted time-based sell is retried")
    seller, _ = await _run_exit(fail_first=1)
    return _check(
        "sell attempts",
        len(seller.prices_seen) == REVERT_THEN_RETRY,
        f"{len(seller.prices_seen)} attempt(s) — the revert, then the retry",
    )


async def check_first_attempt_uses_buy_price() -> bool:
    print("\n2. The first attempt still prices off the buy")
    seller, curve = await _run_exit(fail_first=0)
    return _check(
        "price handed to seller",
        seller.prices_seen[0] == BUY_PRICE,
        f"{seller.prices_seen[0]:.8f} SOL, {curve.calls} price read(s) — "
        f"no extra RPC call on the happy path",
    )


async def check_retry_uses_fresh_price() -> bool:
    print("\n3. The retry prices off a freshly read price")
    seller, curve = await _run_exit(fail_first=1)
    if len(seller.prices_seen) < REVERT_THEN_RETRY:
        return _check(
            "price handed to the retry", passed=False, detail="there was no retry"
        )
    retry_price = seller.prices_seen[1]
    return _check(
        "price handed to the retry",
        retry_price == FRESH_PRICE,
        f"{retry_price:.8f} SOL (fresh {FRESH_PRICE:.8f}, buy {BUY_PRICE:.8f}), "
        f"{curve.calls} price read(s)",
    )


async def check_retries_are_bounded() -> bool:
    print("\n4. Retries are bounded by trade.max_exit_sell_attempts")
    cap = 2
    seller, _ = await _run_exit(fail_first=99, max_exit_sell_attempts=cap)
    return _check(
        "sell attempts",
        len(seller.prices_seen) == cap,
        f"{len(seller.prices_seen)} attempt(s) against a cap of {cap} — "
        f"a token that keeps reverting cannot pin the bot",
    )


async def check_successful_sell_is_not_retried() -> bool:
    print("\n5. A sell that lands first time is not retried")
    seller, _ = await _run_exit(fail_first=0)
    return _check(
        "sell attempts", len(seller.prices_seen) == 1, f"{len(seller.prices_seen)}"
    )


async def check_price_read_failure_still_retries() -> bool:
    print("\n6. A failed price re-read does not abort the retry")
    seller, curve = await _run_exit(fail_first=1, price_read_raises=True)
    return _check(
        "sell attempts",
        len(seller.prices_seen) == REVERT_THEN_RETRY,
        f"{len(seller.prices_seen)} attempt(s) after {curve.calls} failed "
        f"price read(s) — retried with the last known price",
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying the time-based exit retries a reverted sell")
    print("=" * 72)

    results = [
        await check_reverted_sell_is_retried(),
        await check_first_attempt_uses_buy_price(),
        await check_retry_uses_fresh_price(),
        await check_retries_are_bounded(),
        await check_successful_sell_is_not_retried(),
        await check_price_read_failure_still_retries(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

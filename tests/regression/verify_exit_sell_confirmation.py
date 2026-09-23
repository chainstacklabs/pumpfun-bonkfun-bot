"""Verify an exit sell is only retried when retrying is provably safe.

`SolanaClient.confirm_transaction` answered with a bare bool, so both exit paths
saw the same `TradeResult(success=False)` for two very different outcomes:

1. the sell landed and **reverted** - nothing happened, retrying is the point;
2. the sell landed but `getTransaction` was still unavailable when the retry
   budget ran out - the tokens may already be gone, and another sell spends a
   fee to act on a balance that no longer represents the position.

An exit sell is not idempotent, so case 2 mattered: on the `tp_sl` path it could
also burn one of the bounded `max_exit_sell_attempts` on a position that was
already closed. The trader could not even re-check the previous attempt, because
the seller's failure branch never populated `TradeResult.tx_signature` - the
signature existed only as text inside `error_message`.

Offline machine checks, no network and no funds moved. Both real exit loops run
against a stub seller and a stub client serving scripted confirmations:

  1. A confirmed revert is still retried - the #189/#206 behaviour is intact.
  2. An unconfirmed sell is not blindly resold; the signature is re-checked.
  3. A re-check that lands SUCCESS closes the position instead of reselling.
  4. A re-check that lands REVERTED retries.
  5. A re-check that is still unconfirmed stops rather than reselling blind.
  6. A failure carrying neither a reason nor a signature stops.
  7. A submit failure, which never reached the chain, is retried.
  8. The seller populates tx_signature and failure_reason on its failure branch.
  9. confirm_transaction stays a bool, so no `if await ...` silently inverts.
 10. A throw after submission is UNCONFIRMED with its signature, not
     SUBMIT_FAILED - only the latter is safe to resend unchecked.
 11. A re-check that itself throws stops, rather than escaping into a retry.

Usage:
    uv run tests/regression/verify_exit_sell_confirmation.py
"""

import asyncio
import inspect
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
from trading.position import Position  # noqa: E402
from trading.universal_trader import (  # noqa: E402
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    ExitSellVerdict,
    UniversalTrader,
    _exit_sell_verdict_for,
)

BUY_PRICE = 1.0e-6
QUANTITY = 1_000_000.0
STOP_LOSS_PRICE = BUY_PRICE * 0.5
CRASHED_PRICE = BUY_PRICE * 0.2  # below the stop loss, so tp/sl fires at once
SELL_SIGNATURE = "5" * 88
EXIT_TIMEOUT = 10

ONE_ATTEMPT = 1
TWO_ATTEMPTS = 2

# Stands in for an RPC error post_rpc does not contain, e.g. a malformed JSON
# body raising json.JSONDecodeError out of response.json().
RECHECK_RPC_FAILURE = "scripted RPC failure during the re-check"


class StubSeller:
    """Fails on demand with a scripted reason, then succeeds."""

    def __init__(
        self,
        fail_first: int,
        reason: TradeFailureReason | None,
        signature: str | None = SELL_SIGNATURE,
    ) -> None:
        self.fail_first = fail_first
        self.reason = reason
        self.signature = signature
        self.attempts = 0

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        """Return the next scripted sell outcome.

        Args:
            token_info: Token being sold
            token_amount: Amount asked for
            token_price: Price the sell is floored against

        Returns:
            The scripted TradeResult
        """
        self.attempts += 1
        if self.attempts <= self.fail_first:
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=self.signature,
                error_message="scripted failure",
                failure_reason=self.reason,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature=SELL_SIGNATURE,
            amount=token_amount,
            price=token_price,
        )


class StubClient:
    """Answers verify_transaction_status from a script."""

    def __init__(
        self, statuses: list[ConfirmationStatus], *, raises: bool = False
    ) -> None:
        self.statuses = list(statuses)
        self.raises = raises
        self.checks = 0

    async def verify_transaction_status(self, _signature: str) -> ConfirmationStatus:
        """Report the next scripted status.

        Returns:
            The scripted ConfirmationStatus

        Raises:
            RuntimeError: When scripted to fail, standing in for an RPC error
                post_rpc does not contain.
        """
        self.checks += 1
        if self.raises:
            raise RuntimeError(RECHECK_RPC_FAILURE)
        return self.statuses.pop(0) if self.statuses else ConfirmationStatus.UNCONFIRMED


class StubCurveManager:
    """Serves a price that keeps the tp/sl exit condition satisfied."""

    def __init__(self, price: float = CRASHED_PRICE) -> None:
        self.price = price
        self.calls = 0

    async def calculate_price(self, _pool_address: Pubkey) -> float:
        """Return the scripted price.

        Returns:
            Current price in SOL per token
        """
        self.calls += 1
        return self.price


def _make_token_info() -> TokenInfo:
    """Build the minimum TokenInfo the exit paths touch.

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


def _make_trader(seller: StubSeller, client: StubClient) -> UniversalTrader:
    """Build a trader carrying only what the exit paths touch.

    Args:
        seller: Stub seller
        client: Stub client for signature re-checks

    Returns:
        A UniversalTrader wired to the stubs
    """
    trader = object.__new__(UniversalTrader)
    trader.wait_time_after_buy = 0
    trader.max_exit_sell_attempts = DEFAULT_MAX_EXIT_SELL_ATTEMPTS
    trader.price_check_interval = 0
    trader.platform_implementations = SimpleNamespace(
        curve_manager=StubCurveManager(), address_provider=None
    )
    trader.seller = seller
    trader.solana_client = client
    trader.wallet = None
    trader.priority_fee_manager = None
    trader.cleanup_mode = "disabled"  # keeps handle_cleanup_after_sell a no-op
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._log_trade = lambda *_a, **_kw: None  # noqa: SLF001
    return trader


def _make_position() -> Position:
    """Build an open position already below its stop loss.

    Returns:
        An active Position
    """
    return Position(
        mint=Pubkey.from_string("11111111111111111111111111111112"),
        symbol="STUB",
        entry_price=BUY_PRICE,
        quantity=QUANTITY,
        entry_time=datetime.utcnow(),
        stop_loss_price=STOP_LOSS_PRICE,
    )


async def _run_both_paths(
    fail_first: int,
    reason: TradeFailureReason | None,
    statuses: list[ConfirmationStatus],
    signature: str | None = SELL_SIGNATURE,
    *,
    recheck_raises: bool = False,
) -> list[tuple[str, StubSeller, StubClient]]:
    """Drive the time-based and tp/sl exits over the same script.

    Args:
        fail_first: How many sells fail before one succeeds
        reason: Failure reason the seller reports
        statuses: Scripted re-check statuses
        signature: Signature the failure carries, if any
        recheck_raises: Whether the status re-check throws instead of answering

    Returns:
        One (label, seller, client) triple per exit path

    Raises:
        TimeoutError: If an exit loop never returns
    """
    runs = []

    seller = StubSeller(fail_first, reason, signature)
    client = StubClient(list(statuses), raises=recheck_raises)
    trader = _make_trader(seller, client)
    buy_result = TradeResult(
        success=True,
        platform=Platform.PUMP_FUN,
        tx_signature="stub-buy",
        amount=QUANTITY,
        price=BUY_PRICE,
    )
    async with asyncio.timeout(EXIT_TIMEOUT):
        await trader._handle_time_based_exit(  # noqa: SLF001
            _make_token_info(), buy_result
        )
    runs.append(("time_based", seller, client))

    seller = StubSeller(fail_first, reason, signature)
    client = StubClient(list(statuses), raises=recheck_raises)
    trader = _make_trader(seller, client)
    async with asyncio.timeout(EXIT_TIMEOUT):
        await trader._monitor_position_until_exit(  # noqa: SLF001
            _make_token_info(), _make_position()
        )
    runs.append(("tp_sl", seller, client))

    return runs


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


async def check_revert_is_retried() -> bool:
    """A confirmed revert is retried without a re-check.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(1, TradeFailureReason.REVERTED, [])
    passed = all(
        seller.attempts == TWO_ATTEMPTS and client.checks == 0
        for _label, seller, client in runs
    )
    return _check(
        "a confirmed revert is retried, with no re-check needed",
        passed,
        ", ".join(
            f"{label}: {s.attempts} sell(s), {c.checks} re-check(s)"
            for label, s, c in runs
        ),
    )


async def check_unconfirmed_is_rechecked() -> bool:
    """An unconfirmed sell triggers a re-check rather than a blind resell.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.UNCONFIRMED, [ConfirmationStatus.REVERTED]
    )
    passed = all(c.checks == ONE_ATTEMPT for _label, _s, c in runs)
    return _check(
        "an unconfirmed sell is re-checked before any second sell",
        passed,
        ", ".join(f"{label}: {c.checks} re-check(s)" for label, _s, c in runs),
    )


async def check_late_success_is_not_resold() -> bool:
    """A sell that turns out to have landed is not sold again.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.UNCONFIRMED, [ConfirmationStatus.SUCCESS]
    )
    passed = all(seller.attempts == ONE_ATTEMPT for _label, seller, _c in runs)
    return _check(
        "a late-confirmed sell closes the position instead of reselling",
        passed,
        ", ".join(f"{label}: {s.attempts} sell(s)" for label, s, _c in runs),
    )


async def check_recheck_revert_is_retried() -> bool:
    """A re-check that finds a revert retries.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.UNCONFIRMED, [ConfirmationStatus.REVERTED]
    )
    passed = all(seller.attempts == TWO_ATTEMPTS for _label, seller, _c in runs)
    return _check(
        "a re-check that finds a revert retries the sell",
        passed,
        ", ".join(f"{label}: {s.attempts} sell(s)" for label, s, _c in runs),
    )


async def check_still_unconfirmed_stops() -> bool:
    """A sell that stays unresolved is not sold again.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.UNCONFIRMED, [ConfirmationStatus.UNCONFIRMED]
    )
    passed = all(seller.attempts == ONE_ATTEMPT for _label, seller, _c in runs)
    return _check(
        "a still-unconfirmed sell stops rather than reselling blind",
        passed,
        ", ".join(f"{label}: {s.attempts} sell(s)" for label, s, _c in runs),
    )


async def check_no_signature_stops() -> bool:
    """A failure with neither reason nor signature stops.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(1, None, [], signature=None)
    passed = all(
        seller.attempts == ONE_ATTEMPT and client.checks == 0
        for _label, seller, client in runs
    )
    return _check(
        "a failure with no reason and no signature stops, it does not guess",
        passed,
        ", ".join(
            f"{label}: {s.attempts} sell(s), {c.checks} re-check(s)"
            for label, s, c in runs
        ),
    )


async def check_submit_failure_is_retried() -> bool:
    """A sell that never reached the chain is retried.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.SUBMIT_FAILED, [], signature=None
    )
    passed = all(seller.attempts == TWO_ATTEMPTS for _label, seller, _c in runs)
    return _check(
        "a submit failure, which never reached the chain, is retried",
        passed,
        ", ".join(f"{label}: {s.attempts} sell(s)" for label, s, _c in runs),
    )


def check_seller_reports_failure_detail() -> bool:
    """The seller's failure branch carries a signature and a reason.

    Returns:
        Whether the check passed
    """
    source = inspect.getsource(PlatformAwareSeller.execute)
    has_signature = "tx_signature=str(tx_signature)" in source
    has_reason = "failure_reason=_FAILURE_REASON_FOR[status]" in source
    has_submit = "failure_reason=TradeFailureReason.SUBMIT_FAILED" in source
    uses_detailed = "confirm_transaction_detailed" in source
    return _check(
        "the seller populates tx_signature and failure_reason when a sell fails",
        has_signature and has_reason and has_submit and uses_detailed,
        f"detailed confirm: {uses_detailed}, signature: {has_signature}, "
        f"reason: {has_reason}, submit failure: {has_submit}",
    )


async def check_confirm_transaction_stays_bool() -> bool:
    """confirm_transaction returns a bool, never a truthy enum.

    Returns:
        Whether the check passed
    """
    solana_client = object.__new__(SolanaClient)
    seen = []
    for status in ConfirmationStatus:

        async def _detailed(
            _sig: str,
            _commitment: str = "confirmed",
            bound: ConfirmationStatus = status,
        ) -> ConfirmationStatus:
            return bound

        solana_client.confirm_transaction_detailed = _detailed
        seen.append(await solana_client.confirm_transaction("sig"))

    expected = [status is ConfirmationStatus.SUCCESS for status in ConfirmationStatus]
    return _check(
        "confirm_transaction stays a bool, so no `if await ...` silently inverts",
        all(isinstance(v, bool) for v in seen) and seen == expected,
        f"{[s.value for s in ConfirmationStatus]} -> {seen}",
    )


async def check_recheck_failure_stops() -> bool:
    """A status re-check that throws stops instead of escaping into a retry.

    The re-check is the only thing between an unresolved sell and a second one.
    `post_rpc` contains the RPC errors it knows about, but not every reachable
    one - a malformed JSON body raises `json.JSONDecodeError`, which it does not
    catch. In the monitor loop an escape lands in the outer handler, which can
    call straight back into another exit attempt.

    Returns:
        Whether the check passed
    """
    runs = await _run_both_paths(
        1, TradeFailureReason.UNCONFIRMED, [], recheck_raises=True
    )
    passed = all(seller.attempts == ONE_ATTEMPT for _label, seller, _c in runs)
    return _check(
        "a re-check that throws stops rather than selling again",
        passed,
        ", ".join(f"{label}: {s.attempts} sell(s)" for label, s, _c in runs),
    )


def check_post_submission_throw_is_unconfirmed() -> bool:
    """A throw after submission keeps the signature and reports UNCONFIRMED.

    Confirmation and status reads both run after `build_and_send_transaction`
    has returned a signature, and both can raise. Reporting that as
    SUBMIT_FAILED would drop the signature and map straight to RETRY, so the
    loop could send a second sell for one that had already landed.

    Returns:
        Whether the check passed
    """
    source = inspect.getsource(PlatformAwareSeller.execute)
    declares_first = source.index("tx_signature = None") < source.index(
        "tx_signature = await self.client.build_and_send_transaction"
    )
    branches_on_signature = "if tx_signature is None:" in source
    unconfirmed_after_submit = "failure_reason=TradeFailureReason.UNCONFIRMED" in source
    return _check(
        "a throw after submission is UNCONFIRMED with its signature kept",
        declares_first and branches_on_signature and unconfirmed_after_submit,
        f"signature declared before submission: {declares_first}, "
        f"handler branches on it: {branches_on_signature}, "
        f"reports UNCONFIRMED: {unconfirmed_after_submit}",
    )


def check_submit_failed_only_before_a_signature() -> bool:
    """SUBMIT_FAILED is only reachable while no signature exists.

    _exit_sell_verdict_for maps SUBMIT_FAILED straight to RETRY without asking
    the chain anything, so it has to mean "nothing was ever sent".

    Returns:
        Whether the check passed
    """
    verdicts = {
        reason: _exit_sell_verdict_for(reason) for reason in (None, *TradeFailureReason)
    }
    return _check(
        "only SUBMIT_FAILED and REVERTED retry without asking the chain",
        verdicts[TradeFailureReason.SUBMIT_FAILED] is ExitSellVerdict.RETRY
        and verdicts[TradeFailureReason.REVERTED] is ExitSellVerdict.RETRY
        and verdicts[TradeFailureReason.UNCONFIRMED] is None
        and verdicts[None] is None,
        ", ".join(
            f"{r.value if r else 'unset'}={v.value if v else 're-check'}"
            for r, v in verdicts.items()
        ),
    )


async def main() -> None:
    """Run every check and exit non-zero if any failed."""
    print("=" * 72)
    print("Verifying exit sells retry only when retrying is safe")
    print("=" * 72)

    results = [
        await check_revert_is_retried(),
        await check_unconfirmed_is_rechecked(),
        await check_late_success_is_not_resold(),
        await check_recheck_revert_is_retried(),
        await check_still_unconfirmed_stops(),
        await check_no_signature_stops(),
        await check_submit_failure_is_retried(),
        check_seller_reports_failure_detail(),
        await check_confirm_transaction_stays_bool(),
        await check_recheck_failure_stops(),
        check_post_submission_throw_is_unconfirmed(),
        check_submit_failed_only_before_a_signature(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

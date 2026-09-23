"""Verify post_rpc bounds wall time, not just attempts.

`SolanaClient.post_rpc` bounded how many times it retried but never how long it
could take. With the defaults a stalled or rate-limiting endpoint could keep one
call going for minutes: three error retries backing off 1, 2, 4 ... 16s, or ten
429 retries waiting up to 30s each and honouring a `Retry-After` header of any
size. Every caller inherits that, and on the trade path a buy confirmation that
blocks for minutes holds up the whole bot.

`_get_transaction_result` showed it: it takes a `budget_seconds` and checks the
deadline *between* attempts, so the check only runs once `post_rpc` has returned
and the real worst case is `budget_seconds + one post_rpc worst case`.

The fix belongs in `post_rpc` itself, as an overall deadline separate from the
attempt count. Wrapping the lookup in `asyncio.timeout` would instead cut off an
in-flight `getTransaction` and report None, which is the "can't see it, so call
it failed" conflation.

Offline machine checks, no network and no funds moved. The real `post_rpc` runs
against a stub session on a virtual clock, so a 30s backoff costs no real time:

  1. Without a deadline, behaviour is unchanged - attempts still bound the call.
  2. A deadline stops the retries early instead of running the full schedule.
  3. A backoff that would overshoot the deadline is not slept at all.
  4. 429 retries honour the deadline, including an oversized Retry-After.
  5. A deadline already spent returns without sending anything.
  6. A successful response is unaffected by the deadline.
  7. _get_transaction_result hands each lookup only the budget that is left.

Usage:
    uv run tests/regression/verify_rpc_deadline.py
"""

import asyncio
import sys
from itertools import pairwise
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import core.client as client_module  # noqa: E402
from core.client import SolanaClient  # noqa: E402

BODY = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": []}

# post_rpc's own defaults, restated so a change to them fails a check here
# rather than silently weakening one.
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_429_RETRIES = 10

# 1 + 2 + 4s of backoff sits well inside this, so a deadline this size must not
# change the unbounded outcome; 2.5s cuts the schedule after the first retry.
GENEROUS_DEADLINE = 120.0
TIGHT_DEADLINE = 2.5
ATTEMPTS_BEFORE_TIGHT_DEADLINE = 2

HUGE_RETRY_AFTER = "600"  # a provider asking for ten minutes
TX_BUDGET = 5.0


class VirtualClock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        """Read the clock.

        Returns:
            Current virtual time in seconds
        """
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting.

        Args:
            seconds: How far to move the clock forward
        """
        self.slept.append(seconds)
        self.now += seconds


class StubResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: dict, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def __aenter__(self) -> "StubResponse":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        """No-op: any non-200 case here is expressed through status directly."""

    async def json(self) -> dict:
        """Return the scripted payload.

        Returns:
            The response body
        """
        return self._payload


class StubSession:
    """Serves a scripted sequence of outcomes and counts the requests."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.requests = 0

    def post(self, _url: str, json: dict) -> StubResponse:  # noqa: ARG002
        """Answer one request.

        Args:
            _url: Ignored
            json: Ignored request body

        Returns:
            The next scripted response

        Raises:
            TimeoutError: When the script says this attempt times out
        """
        self.requests += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_default()
        if outcome == "timeout":
            raise TimeoutError
        return outcome

    def outcomes_default(self) -> str:
        """Keep timing out once the script runs dry.

        Returns:
            The repeating outcome
        """
        return "timeout"


def _make_client(session: StubSession) -> SolanaClient:
    """Build a client carrying only what post_rpc touches.

    Args:
        session: Stub session to serve requests

    Returns:
        A SolanaClient wired to the stub
    """

    class _NoRateLimit:
        async def acquire(self) -> None:
            pass

    solana_client = object.__new__(SolanaClient)
    solana_client.rpc_endpoint = "https://stub.invalid"
    solana_client._rate_limiter = _NoRateLimit()  # noqa: SLF001

    async def _get_session() -> StubSession:
        return session

    solana_client._get_session = _get_session  # noqa: SLF001
    return solana_client


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


async def _run(outcomes: list, clock: VirtualClock, **kwargs: float) -> tuple:
    """Drive the real post_rpc against the stub on a virtual clock.

    Args:
        outcomes: Scripted responses
        clock: Virtual clock to install
        **kwargs: Passed through to post_rpc

    Returns:
        (result, session, elapsed virtual seconds)
    """
    session = StubSession(outcomes)
    solana_client = _make_client(session)
    started = clock.now
    real_sleep, real_monotonic = asyncio.sleep, client_module.monotonic
    asyncio.sleep = clock.sleep
    client_module.monotonic = clock.monotonic
    try:
        result = await solana_client.post_rpc(BODY, **kwargs)
    finally:
        asyncio.sleep = real_sleep
        client_module.monotonic = real_monotonic
    return result, session, clock.now - started


async def check_no_deadline_is_unchanged() -> bool:
    """Without a deadline the call still runs its full attempt budget.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    result, session, elapsed = await _run(["timeout"] * 10, clock)
    return _check(
        "no deadline: attempts still bound the call, wall time does not",
        result is None and session.requests == DEFAULT_MAX_RETRIES,
        f"{session.requests} request(s) over {elapsed:.1f}s virtual "
        f"(max_retries={DEFAULT_MAX_RETRIES})",
    )


async def check_deadline_stops_retrying() -> bool:
    """A deadline cuts the retry schedule short.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    result, session, elapsed = await _run(
        ["timeout"] * 10, clock, deadline_seconds=TIGHT_DEADLINE
    )
    return _check(
        "deadline: retries stop early instead of running the full schedule",
        result is None
        and session.requests == ATTEMPTS_BEFORE_TIGHT_DEADLINE
        and elapsed <= TIGHT_DEADLINE,
        f"{session.requests} request(s) in {elapsed:.1f}s virtual, "
        f"deadline {TIGHT_DEADLINE}s",
    )


async def check_backoff_never_overshoots() -> bool:
    """No sleep is taken that would run past the deadline.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    _result, _session, elapsed = await _run(
        ["timeout"] * 10, clock, deadline_seconds=TIGHT_DEADLINE
    )
    return _check(
        "deadline: a backoff that would overshoot is not slept at all",
        elapsed <= TIGHT_DEADLINE and all(s <= TIGHT_DEADLINE for s in clock.slept),
        f"slept {clock.slept} for {elapsed:.1f}s total, deadline {TIGHT_DEADLINE}s",
    )


async def check_429_honours_deadline() -> bool:
    """An oversized Retry-After cannot push the call past its deadline.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    rate_limited = [
        StubResponse(429, {}, {"Retry-After": HUGE_RETRY_AFTER}) for _ in range(10)
    ]
    result, session, elapsed = await _run(
        rate_limited, clock, deadline_seconds=TIGHT_DEADLINE
    )
    return _check(
        "deadline: 429 retries honour it, Retry-After included",
        result is None and elapsed <= TIGHT_DEADLINE and session.requests == 1,
        f"Retry-After {HUGE_RETRY_AFTER}s, gave up after {session.requests} "
        f"request(s) in {elapsed:.1f}s virtual",
    )


async def check_spent_deadline_sends_nothing() -> bool:
    """A deadline of zero returns before any request goes out.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    result, session, _elapsed = await _run(["timeout"], clock, deadline_seconds=0.0)
    return _check(
        "deadline already spent: returns without sending a request",
        result is None and session.requests == 0,
        f"{session.requests} request(s) sent",
    )


async def check_success_is_unaffected() -> bool:
    """A deadline does not disturb the happy path.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    payload = {"result": {"meta": {"err": None}}}
    result, session, elapsed = await _run(
        [StubResponse(200, payload)], clock, deadline_seconds=TIGHT_DEADLINE
    )
    return _check(
        "success: a deadline does not disturb a first-try answer",
        result == payload and session.requests == 1 and elapsed == 0,
        f"{session.requests} request(s), {elapsed:.1f}s virtual, result returned",
    )


async def check_tx_result_passes_remaining_budget() -> bool:
    """_get_transaction_result gives each lookup only the budget that is left.

    Returns:
        Whether the check passed
    """
    clock = VirtualClock()
    solana_client = _make_client(StubSession([]))
    deadlines: list[float] = []

    async def fake_post_rpc(_body: dict, **kwargs: float) -> None:
        deadlines.append(kwargs.get("deadline_seconds"))
        await clock.sleep(1.0)  # each lookup burns a second of the budget

    solana_client.post_rpc = fake_post_rpc
    real_sleep, real_monotonic = asyncio.sleep, client_module.monotonic
    asyncio.sleep = clock.sleep
    client_module.monotonic = clock.monotonic
    try:
        result = await solana_client._get_transaction_result(  # noqa: SLF001
            "sig", budget_seconds=TX_BUDGET
        )
    finally:
        asyncio.sleep = real_sleep
        client_module.monotonic = real_monotonic

    passed_any = bool(deadlines)
    all_bounded = all(d is not None and d <= TX_BUDGET for d in deadlines)
    shrinking = all(later <= earlier for earlier, later in pairwise(deadlines))
    return _check(
        "_get_transaction_result hands each lookup the time it has left",
        result is None and passed_any and all_bounded and shrinking,
        f"deadlines passed: {[round(d, 1) for d in deadlines]} (budget {TX_BUDGET}s)",
    )


async def main() -> None:
    """Run every check and exit non-zero if any failed."""
    print("=" * 72)
    print("Verifying post_rpc bounds wall time, not just attempts")
    print("=" * 72)

    results = [
        await check_no_deadline_is_unchanged(),
        await check_deadline_stops_retrying(),
        await check_backoff_never_overshoots(),
        await check_429_honours_deadline(),
        await check_spent_deadline_sends_nothing(),
        await check_success_is_unaffected(),
        await check_tx_result_passes_remaining_budget(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

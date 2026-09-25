"""Verify the compare_listeners PumpPortal lane reports pump.fun coins only.

`tools/compare_listeners.py` races five lanes for coverage. Four of them
subscribe to the pump.fun program; the fifth reads PumpPortal's
`subscribeNewToken`, which aggregates launchpads and names each coin's one in
the payload's `pool` field. The lane ignored that field, so every letsbonk
create entered the race as though it were a pump.fun coin — detected by
PumpPortal and by nobody else, because nobody else was listening for it, and so
scored as a simultaneous miss by all four on-chain lanes.

Offline machine checks, no network and no funds moved. A stub websocket replays
scripted payloads into the real `listen_pumpportal`:

  1. A `pool: "pump"` payload is reported.
  2. A `pool: "bonk"` payload is not. This is the check that fails before the
     fix.
  3. A payload naming no pool is not reported either — an unknown launchpad is
     not assumed to be pump.fun.
  4. Every payload still counts towards the lane's frame total, so filtering
     them out does not read as a starved lane.
  5. The ignored launchpads are tallied on the way out, so a run says what it
     dropped rather than dropping it silently.

Usage:
    uv run tests/regression/verify_pumpportal_pool_filter.py
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PUMP_PAYLOAD = {
    "signature": "1" * 88,
    "mint": "PumpCoin1111111111111111111111111111111111",
    "traderPublicKey": "Trader11111111111111111111111111111111111",
    "txType": "create",
    "name": "Pump Coin",
    "symbol": "PUMP",
    "pool": "pump",
}

BONK_PAYLOAD = {
    "signature": "2" * 88,
    "mint": "BonkCoin1111111111111111111111111111111111",
    "traderPublicKey": "Trader11111111111111111111111111111111111",
    "txType": "create",
    "pool": "bonk",
}

# A bonk create carries no name, symbol or uri; a payload naming no pool at all
# is the shape a launchpad added to the feed later would arrive in.
POOLLESS_PAYLOAD = {
    "signature": "3" * 88,
    "mint": "Unknown11111111111111111111111111111111111",
    "traderPublicKey": "Trader11111111111111111111111111111111111",
    "txType": "create",
}

PAYLOADS = (PUMP_PAYLOAD, BONK_PAYLOAD, POOLLESS_PAYLOAD)


def _load_compare_listeners() -> ModuleType:
    """Import tools/compare_listeners.py by path; it is not an installed module."""
    path = PROJECT_ROOT / "tools" / "compare_listeners.py"
    spec = importlib.util.spec_from_file_location("compare_listeners", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubWebSocket:
    """Serves the scripted payloads, then cancels to end the lane."""

    def __init__(self, payloads: Iterable[dict]) -> None:
        self.payloads = list(payloads)
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if not self.payloads:
            raise asyncio.CancelledError
        return json.dumps(self.payloads.pop(0))

    async def __aenter__(self) -> "StubWebSocket":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class RecordingTracker:
    """Stands in for QueueTracker, keeping what the lane reported."""

    def __init__(self) -> None:
        self.tokens: list[tuple] = []
        self.frames = 0

    def add_token(
        self,
        mint: str,
        name: str,
        symbol: str,
        lane: str,
        timestamp: float,
    ) -> None:
        self.tokens.append((mint, name, symbol, lane, timestamp))

    def increment_messages(self, _lane: str) -> None:
        self.frames += 1


def _run_lane() -> tuple[RecordingTracker, str]:
    """Drive the real lane over the scripted payloads.

    Returns:
        Tuple of (tracker holding what was reported, the lane's stdout)
    """
    module = _load_compare_listeners()
    tracker = RecordingTracker()
    module.websockets.connect = lambda *_a, **_kw: StubWebSocket(PAYLOADS)

    output = io.StringIO()

    async def drive() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await module.listen_pumpportal("pumpportal", tracker, set())

    with contextlib.redirect_stdout(output):
        asyncio.run(drive())
    return tracker, output.getvalue()


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def main() -> None:
    """Run every check and exit non-zero if any failed."""
    print("Verifying the PumpPortal lane's launchpad filter")
    tracker, output = _run_lane()
    reported = {token[0] for token in tracker.tokens}

    print("\n1. A pump.fun coin is reported")
    results = [
        _check(
            PUMP_PAYLOAD["mint"],
            PUMP_PAYLOAD["mint"] in reported,
            "reported" if PUMP_PAYLOAD["mint"] in reported else "missing",
        )
    ]

    print("\n2. A letsbonk coin is not")
    results.append(
        _check(
            BONK_PAYLOAD["mint"],
            BONK_PAYLOAD["mint"] not in reported,
            "ignored"
            if BONK_PAYLOAD["mint"] not in reported
            else "counted against the on-chain lanes",
        )
    )

    print("\n3. A payload naming no pool is not reported")
    results.append(
        _check(
            POOLLESS_PAYLOAD["mint"],
            POOLLESS_PAYLOAD["mint"] not in reported,
            "ignored" if POOLLESS_PAYLOAD["mint"] not in reported else "reported",
        )
    )

    print("\n4. Every payload still counts as a frame")
    results.append(
        _check(
            "frames",
            tracker.frames == len(PAYLOADS),
            f"{tracker.frames} of {len(PAYLOADS)}",
        )
    )

    print("\n5. The ignored launchpads are tallied")
    tallied = "ignored coins from other launchpads" in output and "bonk=1" in output
    results.append(
        _check(
            "shutdown tally",
            tallied,
            next(
                (line for line in output.splitlines() if "launchpads" in line),
                "not printed",
            ),
        )
    )

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

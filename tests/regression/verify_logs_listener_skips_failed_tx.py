"""Verify the logs listener ignores creates whose transaction reverted.

`UniversalLogsListener._wait_for_token_creation` read `logs` and `signature` out
of a logsNotification's `value` and never looked at `value.err`. logsSubscribe
delivers the logs a transaction emitted *before* it reverted, so a CreateEvent
decoded from one describes a coin that does not exist, and the bot bought it.

The buy fails at the ATA-create instruction with `IncorrectProgramId`: the mint
account was never created, so the address is owned by the system program rather
than by a token program. That reads as a token-program bug and sends the reader
into `_correct_token_program` and `state_from_event`, neither of which is at
fault. `extreme_fast_mode` compounds it without causing it — the CreateEvent
carried the trailing fields, so `state_from_event` was set and the pre-buy curve
read, which would have found no curve and skipped the buy, never ran. The cost
is one full buy fee per occurrence.

The blocks listener already guarded on `meta.err`; the geyser listener excludes
failed transactions server-side with `SubscribeRequestFilterTransactions.failed
= False`. The shreds listener sees transactions pre-execution and has no `meta`
at all, so it cannot know the outcome — a design limit, not this bug.

Offline machine checks, no network and no funds moved. A stub websocket replays
scripted frames into the real `_wait_for_token_creation`, with real create logs
taken from the captured fixtures under `cookbook/pumpfun/decode/`:

  1. A create frame carrying an `err` yields no token. This is the check that
     fails before the fix.
  2. The same logs with `err: null` still yield a token, so the guard did not
     swallow genuine creates.
  3. A frame with no `err` key at all still yields a token.
  4. The geyser subscription request still sets `failed = False`, which is what
     keeps the same hole closed there.

Usage:
    uv run tests/regression/verify_logs_listener_skips_failed_tx.py
"""

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.universal_geyser_listener import UniversalGeyserListener  # noqa: E402
from monitoring.universal_logs_listener import UniversalLogsListener  # noqa: E402

DECODE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"

# The create transaction that prompted this verifier reverted this way.
INSTRUCTION_ERROR = {"InstructionError": [4, {"Custom": 1}]}

SIGNATURE = "3TpvJ71gxpfpCWEiJ9XzADK732aePo61yNCnuUFPo2aMLaT3c9qdXc28EyaFKs1kRGEZBFhB2NyRwvEhZk1fUKvZ"


def _logs_from_fixture(path: Path) -> list[str] | None:
    """Pull `meta.logMessages` out of a captured transaction fixture.

    Returns:
        The log lines, or None if the fixture holds no transaction meta.
    """

    def walk(node: object) -> Iterator[list[str]]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("logMessages", "log_messages") and value:
                    yield value
                else:
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return next(walk(json.loads(path.read_text())), None)


def _create_logs() -> list[str]:
    """Real pump.fun create logs, from the first fixture that carries them."""
    for path in sorted(DECODE_DIR.glob("raw_create*.json")):
        logs = _logs_from_fixture(path)
        if logs and any("Instruction: Create" in line for line in logs):
            return logs
    raise SystemExit("No create fixture with logs under cookbook/pumpfun/decode")  # noqa: TRY003


def _frame(logs: list[str], err: object, *, include_err: bool = True) -> dict:
    """Build a logsNotification frame around one transaction's logs."""
    value = {"signature": SIGNATURE, "logs": logs}
    if include_err:
        value["err"] = err
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {"context": {"slot": 372_000_000}, "value": value},
            "subscription": 1,
        },
    }


class StubWebSocket:
    """Serves one scripted frame, then blocks so recv() never returns twice."""

    def __init__(self, frame: dict) -> None:
        self.frame = frame
        self.sent = False

    async def recv(self) -> str:
        if self.sent:
            await asyncio.sleep(3600)
        self.sent = True
        return json.dumps(self.frame)


async def _feed(frame: dict) -> TokenInfo | None:
    """Run one frame through the real handler and return what it yielded."""
    listener = UniversalLogsListener(
        wss_endpoint="wss://stub.invalid", platforms=[Platform.PUMP_FUN]
    )
    return await listener._wait_for_token_creation(  # noqa: SLF001
        SimpleNamespace(recv=StubWebSocket(frame).recv)
    )


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def check_reverted_create_yields_no_token() -> bool:
    print("\n1. A create whose transaction reverted yields no token")
    token_info = await _feed(_frame(_create_logs(), INSTRUCTION_ERROR))
    return _check(
        "handler return value",
        token_info is None,
        "None"
        if token_info is None
        else f"{token_info.symbol} ({token_info.mint}) — a coin that does not exist",
    )


async def check_successful_create_is_still_detected() -> bool:
    print("\n2. The same logs with err: null still yield a token")
    token_info = await _feed(_frame(_create_logs(), None))
    return _check(
        "handler return value",
        token_info is not None,
        f"{token_info.symbol}" if token_info else "not detected",
    )


async def check_missing_err_key_is_still_detected() -> bool:
    print("\n3. A frame carrying no err key at all still yields a token")
    token_info = await _feed(_frame(_create_logs(), None, include_err=False))
    return _check(
        "handler return value",
        token_info is not None,
        f"{token_info.symbol}" if token_info else "not detected",
    )


def check_geyser_excludes_failed_transactions() -> bool:
    print("\n4. The geyser subscription still asks the server to drop failed txs")
    listener = UniversalGeyserListener(
        geyser_endpoint="stub.invalid:443",
        geyser_api_token="stub",  # noqa: S106 — offline stub, never connects
        geyser_auth_type="x-token",
        platforms=[Platform.PUMP_FUN],
    )
    request = listener._create_subscription_request()  # noqa: SLF001
    filters = list(request.transactions.values())
    ok = _check("transaction filters", bool(filters), f"{len(filters)} filter(s)")
    for name, tx_filter in request.transactions.items():
        ok &= _check(
            name,
            tx_filter.HasField("failed") and tx_filter.failed is False,
            f"failed={tx_filter.failed if tx_filter.HasField('failed') else 'unset'}",
        )
    return ok


async def main() -> None:
    print("=" * 72)
    print("Verifying the logs listener skips reverted token creates")
    print("=" * 72)

    results = [
        await check_reverted_create_yields_no_token(),
        await check_successful_create_is_still_detected(),
        await check_missing_err_key_is_still_detected(),
        check_geyser_excludes_failed_transactions(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

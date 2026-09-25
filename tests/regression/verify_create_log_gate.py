"""Verify the logs create gate matches pump.fun's own marker and nothing else.

`parse_token_creation_from_logs` opened with a substring test:

    if not any("Program log: Instruction: Create" in log for log in logs):

Anchor writes `Instruction: <Name>` for every program, so that line accepted any
instruction whose name merely starts with "Create" — CreateTokenAccount,
CreateFeeSharingConfig, CreatePool, CreateConsume — from any program sharing the
transaction. (The associated token account program is not among them: it writes
`Program log: Create`, without Anchor's `Instruction: ` prefix.)

No false coin reached the bot, because the CreateEvent discriminator check
downstream rejects every one of them. The cost was the opposite: a
`CreateTokenAccount` veto had been added on top to suppress the loudest of those
false hits, and being a whole-transaction test it also dropped any genuine
create whose transaction opened a token account — a real coin, silently not
detected. Matching the two marker lines whole removes the need for the veto.

Offline machine checks, no network and no funds moved:

  1. Every create fixture is still detected from its logs alone.
  2. A genuine create whose transaction also carries `CreateTokenAccount` is
     detected. This is the check that fails before the fix: the veto returned
     None for it.
  3. Log sets carrying only a foreign `Create*` instruction are rejected.
  4. No listener, tool or cookbook script tests the marker as a substring.

Usage:
    uv run tests/regression/verify_create_log_gate.py
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from interfaces.core import Platform  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

DECODE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"

# Instruction markers the substring test accepted and the whole-line test
# does not. None of them creates a coin.
FOREIGN_CREATE_MARKERS = (
    "Program log: Instruction: CreateTokenAccount",
    "Program log: Instruction: CreateFeeSharingConfig",
    "Program log: Instruction: CreatePool",
    "Program log: Instruction: CreateConsume",
    "Program log: Instruction: CreateIdempotent",
)

# Every site that decides "this transaction created a coin" from the logs.
GATE_SITES = (
    Path("src/platforms/pumpfun/event_parser.py"),
    Path("tools/compare_listeners.py"),
    Path("cookbook/pumpfun/listen/pumpfun_listen_tokens_logsubscribe.py"),
    Path("cookbook/pumpfun/listen/pumpfun_listen_tokens_blocksubscribe.py"),
    Path("cookbook/pumpfun/trade/pumpfun_snipe_token_blocksubscribe.py"),
)

# A substring test against the marker, in either argument order.
SUBSTRING_GATE = re.compile(
    r'"Program log: Instruction: Create\w*"\s+in\s+log\b'
    r'|\bin\s+"Program log: Instruction: Create\w*"'
)


def _parser() -> PumpFunEventParser:
    """Real pump.fun event parser with the vendored IDL, no RPC client needed."""
    return PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )


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


def _create_fixtures() -> list[tuple[Path, list[str]]]:
    """Every decode fixture whose logs carry a create marker."""
    found = []
    for path in sorted(DECODE_DIR.glob("raw_create*.json")):
        logs = _logs_from_fixture(path)
        if logs and any("Instruction: Create" in line for line in logs):
            found.append((path, logs))
    return found


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def check_real_creates_are_still_detected() -> bool:
    print("\n1. Every create fixture is detected from its logs alone")
    fixtures = _create_fixtures()
    if not fixtures:
        return _check(
            "fixtures found", passed=False, detail="none under cookbook/pumpfun/decode"
        )
    parser = _parser()
    ok = True
    for path, logs in fixtures:
        token_info = parser.parse_token_creation_from_logs(logs, path.stem)
        ok &= _check(
            path.name,
            token_info is not None,
            f"{token_info.symbol}" if token_info else "not detected",
        )
    return ok


def check_create_alongside_token_account_is_detected() -> bool:
    print("\n2. A create sharing its transaction with CreateTokenAccount is detected")
    fixtures = _create_fixtures()
    parser = _parser()
    ok = True
    for path, logs in fixtures:
        polluted = [*logs, "Program log: Instruction: CreateTokenAccount"]
        token_info = parser.parse_token_creation_from_logs(polluted, path.stem)
        ok &= _check(
            path.name,
            token_info is not None,
            f"{token_info.symbol}" if token_info else "dropped by the veto",
        )
    return ok


def check_foreign_create_markers_are_rejected() -> bool:
    print("\n3. A foreign Create* instruction alone is not a coin creation")
    parser = _parser()
    # The event blob is a real one from a fixture, so the rejection is the
    # gate's doing and not a decode failure further down.
    _, sample = _create_fixtures()[0]
    program_data = [line for line in sample if "Program data:" in line]
    ok = True
    for marker in FOREIGN_CREATE_MARKERS:
        logs = [
            "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
            marker,
            *program_data,
        ]
        token_info = parser.parse_token_creation_from_logs(logs, marker)
        ok &= _check(
            marker.split(": ")[-1],
            token_info is None,
            "rejected" if token_info is None else f"accepted as {token_info.symbol}",
        )
    return ok


def check_no_site_matches_the_marker_as_a_substring() -> bool:
    print("\n4. No site tests the create marker as a substring")
    ok = True
    for relative in GATE_SITES:
        path = PROJECT_ROOT / relative
        hits = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if SUBSTRING_GATE.search(line)
        ]
        ok &= _check(str(relative), not hits, "clean" if not hits else "; ".join(hits))
    return ok


def main() -> None:
    """Run every check and exit non-zero if any failed."""
    print("Verifying the logs create gate")
    results = [
        check_real_creates_are_still_detected(),
        check_create_alongside_token_account_is_detected(),
        check_foreign_create_markers_are_rejected(),
        check_no_site_matches_the_marker_as_a_substring(),
    ]
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

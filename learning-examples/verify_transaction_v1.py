"""Verify the bot still detects coins after the Solana transaction v1 cutover.

Transaction v1 (SIMD-0296 size, SIMD-0385 format) activated at epoch 1035 on
2026-09-15. It is not an opt-in: other people's creates and trades arrive in
whatever version they chose, and both pump.fun and letsbonk.fun already carry
v1 traffic. Two things break a reader that ignores it.

`maxSupportedTransactionVersion` is a whole-frame setting, not a per-transaction
one. Asking `blockSubscribe` for 0 does not skip the v1 transactions in a block,
it nulls out `value.block` for the entire notification. Measured against mainnet
on 2026-09-16, 60s per run: `0` delivered 1 block and 177 nulls, `1` delivered
78 blocks and no nulls — the blocks listener was roughly 99% blind, silently,
because a null frame looks exactly like a skipped slot.

The installed solders cannot deserialize a v1 transaction (they begin with byte
129 and put the signatures at the tail). That is survivable, because the RPC has
already decoded the envelope by the time it emits `meta.logMessages`, and the
platform parsers prefer the CreateEvent in those logs anyway — for the canonical
creator. It is only fatal if the listener insists on decoding the bytes *before*
it will hand a transaction to a parser, which is what
`_process_block_transactions` used to do.

Offline machine checks, no network and no funds moved:

  1. Every blockSubscribe/getBlock/getTransaction call site in `src/` and
     `learning-examples/` asks for maxSupportedTransactionVersion >= 1 —
     both the raw JSON key and solana-py's snake_case keyword argument.
  2. The committed fixture really is a v1 transaction (version 1, byte 129)
     carrying a successful create_v2.
  3. The pump.fun event parser reads it into a TokenInfo — mint, creator and
     state_from_event, so extreme_fast_mode keeps its zero-RPC contract.
  4. The block listener returns that TokenInfo for the fixture, which is the
     regression that matters: it must not depend on the envelope decoding.
  5. The listener still reads a pre-v1 create, so nothing regressed for the
     coins that already worked.
  6. With the logs removed, the v1 create is NOT detected — which is what makes
     the log route load-bearing rather than decorative. If this check starts
     failing, solders has learned to decode a v1 envelope and the routing could
     be simplified.

Usage:
    uv run learning-examples/verify_transaction_v1.py
"""

import base64
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.client import SolanaClient  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402

V1_FIXTURE = (
    PROJECT_ROOT / "learning-examples" / "raw_create_v2_v1_from_gettransaction.json"
)
# A pre-v1 create, already committed for the optional-args verifier. Reused
# rather than capturing another one: it only has to be a create the listener
# still detects.
V0_FIXTURE = (
    PROJECT_ROOT
    / "learning-examples"
    / "raw_create_v2_with_fee_bps_from_gettransaction.json"
)

# Solana tags a v1 transaction with this first byte (SIMD-0385 VersionByte).
V1_VERSION_BYTE = 129

MIN_SUPPORTED_VERSION = 1

SCAN_ROOTS = ("src", "learning-examples")
# Two spellings reach the same RPC field: the raw JSON key used by hand-built
# request bodies, and solana-py's snake_case keyword argument. A scan that knows
# only the first one misses tx_status.py, which every example confirms through.
MSV_PATTERN = re.compile(
    r"[\"']maxSupportedTransactionVersion[\"']\s*:\s*(\d+)"
    r"|max_supported_transaction_version\s*=\s*(\d+)"
)


class _OfflineClient(SolanaClient):
    """A SolanaClient that never opens a connection.

    The parsers only need address derivation here, and SolanaClient.__init__
    starts a blockhash updater task.
    """

    def __init__(self) -> None:
        self.rpc_endpoint = "http://offline.invalid"
        self._client = None
        self._cached_blockhash = None
        self._blockhash_lock = None
        self._blockhash_updater_task = None


def _load(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def _as_block_transaction(result: dict) -> dict:
    """Shape a getTransaction result like one blockSubscribe transaction."""
    return {"transaction": result["transaction"], "meta": result["meta"]}


def check_every_call_site_accepts_v1() -> bool:
    """No reader may ask for a version below v1.

    A call site left at 0 does not degrade gracefully — it loses whole blocks.
    """
    stale: list[str] = []
    for root in SCAN_ROOTS:
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            if "generated" in path.parts:
                continue
            text = path.read_text()
            for match in MSV_PATTERN.finditer(text):
                value = match.group(1) or match.group(2)
                if int(value) < MIN_SUPPORTED_VERSION:
                    line = text[: match.start()].count("\n") + 1
                    stale.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")
    if stale:
        print("     call sites still below v1: " + ", ".join(stale))
    return not stale


def check_fixture_is_a_v1_create() -> bool:
    """The fixture has to be the real thing or the rest proves nothing."""
    result = _load(V1_FIXTURE)
    if result.get("version") != 1:
        print(f"     fixture version is {result.get('version')!r}, expected 1")
        return False
    if result["meta"].get("err") is not None:
        print("     fixture create failed on chain; a failed create has no mint")
        return False

    raw = base64.b64decode(result["transaction"][0])
    if raw[0] != V1_VERSION_BYTE:
        print(f"     first byte is {raw[0]}, expected {V1_VERSION_BYTE}")
        return False

    logs = result["meta"].get("logMessages") or []
    if not any("Instruction: CreateV2" in line for line in logs):
        print("     fixture carries no CreateV2 instruction")
        return False
    return True


def check_parser_reads_the_v1_create() -> bool:
    """The platform parser works off logs, so the version is irrelevant to it."""
    parser = get_platform_implementations(
        Platform.PUMP_FUN, _OfflineClient()
    ).event_parser
    token_info = parser.parse_token_creation_from_block(
        {"transactions": [_as_block_transaction(_load(V1_FIXTURE))]}
    )
    if token_info is None:
        print("     parser returned None for a v1 create_v2")
        return False
    if not token_info.state_from_event:
        print("     state_from_event is False — extreme_fast_mode would refresh")
        return False
    if token_info.mint is None or token_info.creator is None:
        print("     parsed TokenInfo is missing mint or creator")
        return False
    return True


def check_listener_detects_the_v1_create() -> bool:
    """The regression under test: dispatch must not require a byte decode.

    solders cannot deserialize a v1 envelope, so a listener that decodes first
    and dispatches second drops the coin without raising anything.
    """
    listener = UniversalBlockListener("wss://offline.invalid", [Platform.PUMP_FUN])
    result = _load(V1_FIXTURE)
    token_info = listener._process_block_transactions(  # noqa: SLF001
        [_as_block_transaction(result)]
    )
    if token_info is None:
        print("     block listener returned None for a v1 create_v2")
        return False
    return True


def check_log_route_is_what_detects_v1() -> bool:
    """Strip the logs and the v1 create must vanish.

    Proves the log route is doing the work, rather than passing because some
    other path happens to cope. solders cannot decode a v1 envelope, so with
    no logs there is nothing left to parse.
    """
    listener = UniversalBlockListener("wss://offline.invalid", [Platform.PUMP_FUN])
    result = _load(V1_FIXTURE)
    tx = _as_block_transaction(result)
    tx["meta"] = {**tx["meta"], "logMessages": []}
    token_info = listener._process_block_transactions([tx])  # noqa: SLF001
    if token_info is not None:
        print("     a v1 create parsed without its logs — routing may have changed")
        return False
    return True


def check_listener_still_detects_a_pre_v1_create() -> bool:
    """The log route is an addition, not a replacement."""
    listener = UniversalBlockListener("wss://offline.invalid", [Platform.PUMP_FUN])
    result = _load(V0_FIXTURE)
    if result.get("version") == 1:
        print("     the legacy/v0 fixture is itself v1; the check proves nothing")
        return False
    token_info = listener._process_block_transactions(  # noqa: SLF001
        [_as_block_transaction(result)]
    )
    if token_info is None:
        print("     block listener returned None for a pre-v1 create")
        return False
    return True


def main() -> int:
    checks = [
        ("every call site accepts v1", check_every_call_site_accepts_v1),
        ("fixture is a successful v1 create_v2", check_fixture_is_a_v1_create),
        ("event parser reads the v1 create", check_parser_reads_the_v1_create),
        ("block listener detects the v1 create", check_listener_detects_the_v1_create),
        (
            "block listener still detects a pre-v1 create",
            check_listener_still_detects_a_pre_v1_create,
        ),
        ("the log route is what detects v1", check_log_route_is_what_detects_v1),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

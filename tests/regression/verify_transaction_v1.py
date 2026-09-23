"""Verify the bot still detects coins after the Solana transaction v1 cutover.

Transaction v1 (SIMD-0296 size, SIMD-0385 format) is not an opt-in: other
people's creates and trades arrive in whatever version they chose, and both
pump.fun and letsbonk.fun carry v1 traffic. Two things break a reader that
ignores it.

`maxSupportedTransactionVersion` is a whole-frame setting, not a per-transaction
one. Asking `blockSubscribe` for 0 does not skip a block's v1 transactions, it
nulls `value.block` for the entire notification — which looks exactly like a
skipped slot, so the blocks listener goes almost entirely blind, silently.

solders could not deserialize a v1 envelope (they begin with byte 129 and put
the signatures at the tail) until 0.29. That was survivable, because the RPC has
already decoded the envelope by the time it emits `meta.logMessages` and the
parsers prefer the CreateEvent in those logs anyway; it is fatal only for a
listener that insists on decoding the bytes *before* handing a transaction to a
parser.

solders 0.29 reads a v1 envelope, but the byte decode stays the fallback, not
the route: the next version byte will be unreadable in its turn, and a listener
that has come to depend on the decode goes blind on the day it lands. Both
routes are pinned below, separately.

Offline machine checks, no network and no funds moved:

  1. Every blockSubscribe/getBlock/getTransaction call site in `src/` and
     `cookbook/` asks for maxSupportedTransactionVersion >= 1 —
     both the raw JSON key and solana-py's snake_case keyword argument.
  2. The committed fixture really is a v1 transaction (version 1, byte 129)
     carrying a successful create_v2.
  3. The pump.fun event parser reads it into a TokenInfo — mint, creator and
     state_from_event, so extreme_fast_mode keeps its zero-RPC contract.
  4. The block listener returns that TokenInfo for the fixture, which is the
     regression that matters: it must not depend on the envelope decoding.
  5. The listener still reads a pre-v1 create, so nothing regressed for the
     coins that already worked.
  6. With the envelope replaced by bytes solders cannot read, the v1 create is
     still detected — which is what makes the log route load-bearing rather
     than decorative.
  7. With the logs removed, the installed solders reads the v1 create straight
     off the envelope. This is the fallback, and it only works from solders
     0.29; a failure here means the decode has regressed and the log route is
     carrying the whole load again.

The geyser route decodes protobuf rather than transaction bytes, so it goes
blind differently and is pinned on its own frame:

  8. The committed geyser frame really is a v1 create_v2. Over geyser there is
     no version number, so `Message.config` is the test; it exists only in the
     stubs from yellowstone-grpc-proto 13.0.0-rc4, so a rollback fails here.
  9. The pump.fun parser reads that frame into a usable TokenInfo. A missing
     proto field is skipped rather than raised on, so a stale proto degrades in
     silence — which is why it is pinned.
 10. With the logs stripped from the frame, the instruction decode still finds
     the coin, and still leaves state_from_event False: `args.creator` is
     user-supplied, so the curve must be read.
 11. The inline v1 budget is readable as a known field. A v1 coin carries no
     ComputeBudget instructions at all, so anything reading a budget from the
     instruction list reads nothing for it.

Usage:
    uv run tests/regression/verify_transaction_v1.py
"""

import base64
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.client import SolanaClient  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402

V1_FIXTURE = (
    PROJECT_ROOT
    / "cookbook"
    / "pumpfun"
    / "decode"
    / "raw_create_v2_v1_from_gettransaction.json"
)
# A pre-v1 create, already committed for the optional-args verifier. Reused
# rather than capturing another one: it only has to be a create the listener
# still detects.
V0_FIXTURE = (
    PROJECT_ROOT
    / "cookbook"
    / "pumpfun"
    / "decode"
    / "raw_create_v2_with_fee_bps_from_gettransaction.json"
)
# One geyser SubscribeUpdate carrying a v1 create_v2. The
# geyser route has its own decode path and its own stubs, so it needs its own
# fixture: the getTransaction JSON above cannot exercise a protobuf frame.
GEYSER_V1_FIXTURE = (
    PROJECT_ROOT
    / "cookbook"
    / "pumpfun"
    / "decode"
    / "raw_create_v2_v1_from_geyser.json"
)

# Solana tags a v1 transaction with this first byte (SIMD-0385 VersionByte).
V1_VERSION_BYTE = 129

MIN_SUPPORTED_VERSION = 1

SCAN_ROOTS = ("src", "cookbook", "tools")
# Two spellings reach the same RPC field: the raw JSON key used by hand-built
# request bodies, and solana-py's snake_case keyword argument. A scan that knows
# only the first one misses solana_transaction_status.py, which every example confirms through.
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


def check_log_route_detects_without_the_envelope() -> bool:
    """Make the envelope unreadable and the v1 create must still arrive.

    Proves the log route is doing the work on its own, rather than passing
    because the byte decode happens to cope. Until solders 0.29 that was proved
    the other way round — strip the logs and nothing was left to parse — but a
    solders that reads v1 makes the absence of a result prove nothing. Breaking
    the envelope instead pins the invariant whether or not the installed solders
    can decode the version in hand.
    """
    listener = UniversalBlockListener("wss://offline.invalid", [Platform.PUMP_FUN])
    result = _load(V1_FIXTURE)
    tx = _as_block_transaction(result)
    # Valid base64, not a transaction: the decode raises and the fallback has
    # nothing to offer, so only the logs can answer.
    tx["transaction"] = [base64.b64encode(b"not a transaction").decode(), "base64"]
    token_info = listener._process_block_transactions([tx])  # noqa: SLF001
    if token_info is None:
        print(
            "     a v1 create with unreadable bytes was missed — the logs are not the route"
        )
        return False
    return True


def check_envelope_fallback_reads_v1() -> bool:
    """Strip the logs and the installed solders must read the v1 envelope.

    The fallback, not the route. It only works from solders 0.29 — 0.26, 0.27.1
    and 0.28 all raise `ValueError: io error: unexpected end of file` on this
    same fixture. A failure here is a decode regression, not a routing bug: the
    bot keeps detecting v1 coins through the logs either way.
    """
    listener = UniversalBlockListener("wss://offline.invalid", [Platform.PUMP_FUN])
    result = _load(V1_FIXTURE)
    tx = _as_block_transaction(result)
    tx["meta"] = {**tx["meta"], "logMessages": []}
    token_info = listener._process_block_transactions([tx])  # noqa: SLF001
    if token_info is None:
        print("     the installed solders cannot decode a v1 envelope")
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


def check_examples_route_on_logs() -> bool:
    """No cookbook or tools listener may detect by opening the envelope.

    The envelope is the one part of a transaction whose format changes
    underneath you, and the installed solders cannot read a v1 one. A script
    that deserializes it is free to do so for enrichment, but it must also read
    `logMessages`, or it goes blind the moment a version it cannot parse
    arrives. The blocks example printed 30,080 "skipping a v1 transaction"
    lines in 150 seconds before this rule existed, and compare_listeners
    reported 601 decode errors as if they were a transport difference.
    """
    offenders = []
    for directory in ("cookbook", "tools"):
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
            # The decoders are handed one saved payload and asked to take it
            # apart. Opening the envelope is the exercise, and there is no live
            # stream for them to go blind on.
            if path.parent.name == "decode":
                continue
            source = path.read_text()
            if "VersionedTransaction.from_bytes" not in source:
                continue
            if "logMessages" not in source:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    if offenders:
        print(
            "     these decode the envelope without a log route: "
            + ", ".join(offenders)
        )
        return False
    return True


def _geyser_update() -> "geyser_pb2.SubscribeUpdate":
    """Rebuild the captured geyser frame from its committed bytes.

    Returns:
        The `SubscribeUpdate` exactly as it came off the wire
    """
    update = geyser_pb2.SubscribeUpdate()
    update.ParseFromString(
        base64.b64decode(_load(GEYSER_V1_FIXTURE)["subscribe_update_base64"])
    )
    return update


def check_geyser_fixture_is_a_v1_create() -> bool:
    """The geyser fixture has to be a v1 create or the rest proves nothing.

    Over geyser there is no version number to read: `Message.config` is set for
    v1 and absent otherwise, and `versioned` is true for v0 and v1 alike. The
    vendored proto carried no `config` field until the 13.0.0-rc4 regeneration,
    so this check also fails if the stubs are ever rolled back.
    """
    update = _geyser_update()
    message = update.transaction.transaction.transaction.message
    if not message.HasField("config"):
        print("     fixture message has no config field — not a v1, or stale stubs")
        return False
    if update.transaction.transaction.meta.err.ByteSize():
        print("     fixture create failed on chain; a failed create has no mint")
        return False
    logs = list(update.transaction.transaction.meta.log_messages)
    if not any("Instruction: CreateV2" in line for line in logs):
        print("     fixture carries no CreateV2 instruction")
        return False
    return True


def check_geyser_parser_reads_the_v1_create() -> bool:
    """The geyser route must reach a usable TokenInfo on a v1 coin.

    An unknown field is skipped by protobuf rather than raised on, so a stale
    proto degrades silently here — the frame still parses and only the budget
    goes missing. That is precisely why this is pinned.
    """
    parser = get_platform_implementations(
        Platform.PUMP_FUN, _OfflineClient()
    ).event_parser
    token_info = parser.parse_token_creation_from_geyser(_geyser_update())
    if token_info is None:
        print("     geyser parser returned None for a v1 create_v2")
        return False
    if not token_info.state_from_event:
        print("     state_from_event is False — extreme_fast_mode would refresh")
        return False
    if token_info.mint is None or token_info.creator is None:
        print("     parsed TokenInfo is missing mint or creator")
        return False
    return True


def check_geyser_instruction_fallback_reads_v1() -> bool:
    """With the logs gone, the instruction decode still has to find the coin.

    Same split as the block listener: logs are the route, instruction decoding
    is the fallback, and each is pinned separately so neither can quietly start
    carrying the other's load. The fallback must *not* set state_from_event —
    `args.creator` is user-supplied and may differ from the
    canonical `BondingCurve.creator`, so the curve still has to be read.
    """
    update = _geyser_update()
    del update.transaction.transaction.meta.log_messages[:]

    parser = get_platform_implementations(
        Platform.PUMP_FUN, _OfflineClient()
    ).event_parser
    token_info = parser.parse_token_creation_from_geyser(update)
    if token_info is None:
        print("     instruction fallback returned None for a v1 create_v2")
        return False
    if token_info.mint is None:
        print("     instruction fallback produced no mint")
        return False
    if token_info.state_from_event:
        print("     fallback set state_from_event off an instruction-parsed creator")
        return False
    return True


def check_geyser_exposes_the_v1_budget() -> bool:
    """The inline v1 budget must be readable as a known field, not guesswork.

    Transaction v1 moved the compute budget off ComputeBudget instructions and
    onto the message, so anything reading a coin's budget from the instruction
    list now reads nothing at all for a v1 coin. The listener reports this at
    debug; the point of the check is that the field survives regeneration.
    """
    config = _geyser_update().transaction.transaction.transaction.message.config
    if not config.HasField("compute_unit_limit"):
        print("     v1 fixture carries no compute unit limit")
        return False
    if config.compute_unit_limit <= 0:
        print(f"     compute unit limit is {config.compute_unit_limit}")
        return False
    # Unset means zero in v1, never a runtime default, so a real coin that
    # reaches execution has to have set this explicitly.
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
        (
            "the log route detects v1 without the envelope",
            check_log_route_detects_without_the_envelope,
        ),
        ("the envelope fallback reads v1 too", check_envelope_fallback_reads_v1),
        (
            "geyser fixture is a successful v1 create_v2",
            check_geyser_fixture_is_a_v1_create,
        ),
        (
            "geyser parser reads the v1 create",
            check_geyser_parser_reads_the_v1_create,
        ),
        (
            "geyser instruction fallback reads v1 without logs",
            check_geyser_instruction_fallback_reads_v1,
        ),
        ("geyser exposes the inline v1 budget", check_geyser_exposes_the_v1_budget),
        ("examples route on logs too", check_examples_route_on_logs),
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

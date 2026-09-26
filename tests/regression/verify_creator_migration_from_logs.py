"""Verify a fee-sharing coin's buy is built from the migrated bonding-curve creator.

`CreateEvent.creator` is the wallet that submitted the create. For an ordinary
coin that is also what `BondingCurve.creator` ends up holding, so the parser
treated it as canonical and marked the TokenInfo `state_from_event`, letting
`extreme_fast_mode` buy with no RPC call.

A coin with **creator-fee sharing** breaks that. The same transaction goes on to
call `migrate_bonding_curve_creator`, which overwrites the curve's creator with
the sharing-config PDA — `PDA(["sharing-config", mint], FEE_PROGRAM)`. Despite
the name this is not graduation: `migrate`/`migrate_v2` move a completed curve
to PumpSwap, while this rewrites one field on a curve that has just been
created, and the bot still only trades launches. `buy_v2` derives account 16
`creator_vault` from `bonding_curve.creator`, so a buy built from the
CreateEvent value passes the wrong vault and the transaction reverts with
`ConstraintSeeds` (2006), costing one buy fee.

Only a rewrite inside the launch transaction is covered. A coin whose creator
is rewritten a few slots later still reverts: the launch logs cannot carry an
event that has not happened yet, and seeing it would cost the RPC read that
extreme_fast_mode exists to avoid.

The fix reads `MigrateBondingCurveCreatorEvent` out of the same log set the
CreateEvent came from, so the correct creator costs no RPC call and the zero-RPC
path is kept.

Offline machine checks, no network and no funds moved. The fixture is the real
create transaction of mint `FdCYwtez…`, whose buy reverted 2006 live:

  1. The fee-sharing fixture parses to the migrated creator, not the event's.
     This is the check that fails before the fix.
  2. That creator equals the independently derived sharing-config PDA, so the
     parser is not just echoing a number out of the log.
  3. `creator_vault` is derived from the migrated creator.
  4. An ordinary create fixture is unaffected: creator stays the event's.
  5. `state_from_event` is still set, so the coin keeps the zero-RPC path.

Usage:
    uv run tests/regression/verify_creator_migration_from_logs.py
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from interfaces.core import Platform  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

DECODE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"
SHARING_FIXTURE = DECODE_DIR / "raw_create_fee_sharing_from_gettransaction.json"

# The coin whose live buy reverted 2006, and the two creators involved.
SHARING_MINT = Pubkey.from_string("FdCYwtezFn1vhzsSPXbLZiAJungb8LETKSnV1iVdK5Xi")
EVENT_CREATOR = Pubkey.from_string("B6ZrojFMviC3grvpEGCrtuK85B55YU9y6t3aNT7NM7wz")
MIGRATED_CREATOR = Pubkey.from_string("6HcRAKoJodAYWV6rJFWAzHU4shsudmCCjpc4i5DyShWN")


def _parser() -> PumpFunEventParser:
    """Real pump.fun event parser with the vendored IDL, no RPC client needed."""
    return PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )


def _logs_from_fixture(path: Path) -> list[str] | None:
    """Pull `meta.logMessages` out of a captured transaction fixture."""

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


def _ordinary_fixture() -> tuple[Path, list[str]]:
    """A create fixture that carries no creator migration."""
    parser = _parser()
    for path in sorted(DECODE_DIR.glob("raw_create*.json")):
        if path == SHARING_FIXTURE:
            continue
        logs = _logs_from_fixture(path)
        if not logs:
            continue
        token_info = parser.parse_token_creation_from_logs(logs, path.stem)
        if token_info:
            return path, logs
    raise SystemExit("No ordinary create fixture found")  # noqa: TRY003


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def check_migrated_creator_is_used() -> bool:
    print("\n1. A fee-sharing create parses to the migrated creator")
    logs = _logs_from_fixture(SHARING_FIXTURE)
    token_info = _parser().parse_token_creation_from_logs(logs, "fee_sharing")
    if token_info is None:
        return _check("token detected", passed=False, detail="not detected")
    return _check(
        "TokenInfo.creator",
        token_info.creator == MIGRATED_CREATOR,
        f"{token_info.creator}"
        + (
            "" if token_info.creator == MIGRATED_CREATOR else " (the CreateEvent value)"
        ),
    )


def check_creator_matches_derived_sharing_config() -> bool:
    print("\n2. That creator is the independently derived sharing-config PDA")
    derived = PumpFunAddresses.find_sharing_config(SHARING_MINT)
    logs = _logs_from_fixture(SHARING_FIXTURE)
    token_info = _parser().parse_token_creation_from_logs(logs, "fee_sharing")
    return _check(
        "creator == PDA(['sharing-config', mint], FEE_PROGRAM)",
        token_info is not None and token_info.creator == derived,
        f"derived {derived}",
    )


def check_creator_vault_follows() -> bool:
    print("\n3. creator_vault is derived from the migrated creator")
    expected, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(MIGRATED_CREATOR)], PumpFunAddresses.PROGRAM
    )
    stale, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(EVENT_CREATOR)], PumpFunAddresses.PROGRAM
    )
    logs = _logs_from_fixture(SHARING_FIXTURE)
    token_info = _parser().parse_token_creation_from_logs(logs, "fee_sharing")
    got = token_info.creator_vault if token_info else None
    return _check(
        "TokenInfo.creator_vault",
        got == expected,
        f"{got} (expected {expected}; the pre-fix value was {stale})",
    )


def check_ordinary_create_unchanged() -> bool:
    print("\n4. An ordinary create keeps the CreateEvent creator")
    path, logs = _ordinary_fixture()
    token_info = _parser().parse_token_creation_from_logs(logs, path.stem)
    derived_vault, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(token_info.creator)], PumpFunAddresses.PROGRAM
    )
    return _check(
        path.name,
        token_info.creator_vault == derived_vault,
        f"creator {token_info.creator}, vault consistent",
    )


def check_zero_rpc_path_kept() -> bool:
    print("\n5. The fee-sharing coin still carries state_from_event")
    logs = _logs_from_fixture(SHARING_FIXTURE)
    token_info = _parser().parse_token_creation_from_logs(logs, "fee_sharing")
    return _check(
        "TokenInfo.state_from_event",
        bool(token_info and token_info.state_from_event),
        f"{token_info.state_from_event if token_info else None} — no curve read needed",
    )


def main() -> None:
    print("=" * 72)
    print("Verifying the bonding-curve creator migration is read from the logs")
    print("=" * 72)

    results = [
        check_migrated_creator_is_used(),
        check_creator_matches_derived_sharing_config(),
        check_creator_vault_follows(),
        check_ordinary_create_unchanged(),
        check_zero_rpc_path_kept(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()

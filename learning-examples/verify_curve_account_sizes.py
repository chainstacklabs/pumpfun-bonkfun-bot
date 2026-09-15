"""Offline + mainnet: the bonding curve is 125 bytes, or 151 once extended.

The 2026-09-15 program upgrade appended creator_fee_bps (u64),
can_edit_creator_fee (bool) and is_holder_reward (bool) to BondingCurve and
dropped the 36 reserved padding bytes. create_v2 now allocates exactly 125
bytes; an account only reaches 151 after extend_account runs. Anything that
filters curves by dataSize must accept both, or it sees no new coins at all.

Moves no funds.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

CURVE_LEN_CREATED = 125
CURVE_LEN_EXTENDED = 151
_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000


def _synthetic_curve(length: int) -> bytes:
    """Build a valid BondingCurve account image at the given total length.

    `length` must be 125 (as `create_v2` allocates it) or 151 (after
    `extend_account` has run on it) — every other length is not a shape the
    program ever writes.

    Args:
        length: Total account length in bytes, including the discriminator

    Returns:
        Account bytes matching the IDL's field order and discriminator

    Raises:
        ValueError: If length is not 125 or 151
    """
    if length not in (CURVE_LEN_CREATED, CURVE_LEN_EXTENDED):
        raise ValueError(f"unexpected curve length: {length}")  # noqa: TRY003

    idl = json.loads((PROJECT_ROOT / "idl" / "pump_fun_idl.json").read_text())
    disc = next(
        bytes(a["discriminator"])
        for a in idl["accounts"]
        if a["name"] == "BondingCurve"
    )
    reserves = struct.pack(
        "<QQQQQ",
        _VIRTUAL_TOKEN_RESERVES,  # virtual_token_reserves
        30_000_000_000,  # virtual_quote_reserves
        793_100_000_000_000,  # real_token_reserves
        0,  # real_quote_reserves
        1_000_000_000_000_000,  # token_total_supply
    )
    body = (
        reserves
        + b"\x00"  # complete
        + bytes(32)  # creator
        + b"\x00"  # is_mayhem_mode
        + b"\x00"  # is_cashback_coin
        + bytes(32)  # quote_mint = Pubkey::default() (SOL-paired)
        + struct.pack("<Q", 0)  # creator_fee_bps
        + b"\x00"  # can_edit_creator_fee
        + b"\x00"  # is_holder_reward
    )
    account = disc + body
    return account + bytes(length - len(account))


def check_both_lengths_decode() -> bool:
    """A synthetic 125-byte and 151-byte curve both decode identically."""
    from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: PLC0415
    from utils.idl_parser import IDLParser  # noqa: PLC0415

    parser = IDLParser("idl/pump_fun_idl.json")
    manager = PumpFunCurveManager(client=None, idl_parser=parser)

    base = _synthetic_curve(CURVE_LEN_CREATED)
    extended = base + b"\x00" * (CURVE_LEN_EXTENDED - CURVE_LEN_CREATED)

    ok = True
    for length, data in ((CURVE_LEN_CREATED, base), (CURVE_LEN_EXTENDED, extended)):
        try:
            state = manager._decode_curve_state_with_idl(data)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - verifier reports, doesn't raise
            print(f"  FAIL {length}B curve failed to decode: {exc}")
            ok = False
            continue
        if state["virtual_token_reserves"] != _VIRTUAL_TOKEN_RESERVES:
            print(f"  FAIL {length}B curve decoded wrong reserves")
            ok = False
        else:
            print(f"  OK  {length}B curve decodes")
    return ok


def check_filters_accept_both() -> bool:
    """Neither graduating-token example may filter on a single dataSize."""
    ok = True
    for rel in (
        "learning-examples/bonding-curve-progress/get_graduating_tokens.py",
        "learning-examples/bonding-curve-progress/get_graduating_tokens_geyser.py",
    ):
        text = Path(rel).read_text()
        if "CURVE_ACCOUNT_LEN:" in text or "CURVE_ACCOUNT_LEN " in text:
            print(f"  FAIL {rel} still filters on one dataSize")
            ok = False
        elif "CURVE_ACCOUNT_LENS" not in text:
            print(f"  FAIL {rel} has no CURVE_ACCOUNT_LENS")
            ok = False
        else:
            print(f"  OK  {rel} accepts both lengths")
    return ok


def main() -> int:
    """Run every check and report pass/fail.

    Returns:
        0 if every check passed, 1 otherwise
    """
    checks = [
        ("both curve lengths decode via the IDL parser", check_both_lengths_decode),
        ("graduating-token filters accept both lengths", check_filters_accept_both),
    ]
    failed = 0
    for label, check in checks:
        print(f"{label}:")
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"  FAIL {type(error).__name__}: {error}")
            failed += 1
            continue
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Offline + mainnet: the bonding curve is 125 bytes as created, and grows.

The 2026-09-15 program upgrade appended creator_fee_bps (u64),
can_edit_creator_fee (bool) and is_holder_reward (bool) to BondingCurve and
dropped the 36 reserved padding bytes. create_v2 now allocates exactly 125
bytes; extend_account can grow an account past that to any length the
program allows — 151 and 256 are both confirmed live. A dataSize allowlist
is whack-a-mole against that: the next length silently drops curves again.
Anything that watches for curves must not filter on dataSize at all, and must
decode correctly regardless of which length turns up.

Moves no funds.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

CURVE_LEN_CREATED = 125
CURVE_LEN_EXTENDED = 151
CURVE_LEN_RESIZED = 256  # confirmed live 2026-09-15, see the two graduating-
# token scripts' module docstrings for the getAccountInfo + decode evidence
_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000


def _synthetic_curve(length: int) -> bytes:
    """Build a valid BondingCurve account image at the given total length.

    `length` must be at least `CURVE_LEN_CREATED` — the smallest shape the
    program ever writes. Anything at or above that is a legal `extend_account`
    target: this appends zero-byte padding to reach it, matching what live
    125/151/256-byte curves actually look like past their documented fields.

    Args:
        length: Total account length in bytes, including the discriminator

    Returns:
        Account bytes matching the IDL's field order and discriminator

    Raises:
        ValueError: If length is shorter than CURVE_LEN_CREATED
    """
    if length < CURVE_LEN_CREATED:
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


def check_all_lengths_decode() -> bool:
    """A synthetic 125-, 151- and 256-byte curve all decode identically."""
    from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: PLC0415
    from utils.idl_parser import IDLParser  # noqa: PLC0415

    parser = IDLParser("idl/pump_fun_idl.json")
    manager = PumpFunCurveManager(client=None, idl_parser=parser)

    ok = True
    for length in (CURVE_LEN_CREATED, CURVE_LEN_EXTENDED, CURVE_LEN_RESIZED):
        data = _synthetic_curve(length)
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


def check_no_datasize_filter() -> bool:
    """Neither graduating-token example may pre-filter by dataSize/datasize.

    `extend_account` can grow a curve to any length the program allows, so
    enumerating lengths is whack-a-mole — the fix is to not filter on length
    at all, not to enumerate one more. Checked by looking for the actual
    filter-construction syntax (a quoted `"dataSize":` dict key in the
    WebSocket script, a `.datasize =` protobuf field assignment in the
    Geyser one) rather than a bare substring match, since both scripts'
    docstrings legitimately discuss `dataSize`/`datasize` in prose.
    """
    ok = True
    needles = {
        "cookbook/pumpfun/graduation/get_graduating_tokens.py": ('"dataSize":'),
        "cookbook/pumpfun/graduation/get_graduating_tokens_geyser.py": (".datasize ="),
    }
    for rel, needle in needles.items():
        text = Path(rel).read_text()
        if needle in text:
            print(f"  FAIL {rel} still constructs a dataSize filter")
            ok = False
        else:
            print(f"  OK  {rel} has no dataSize filter")
    return ok


def main() -> int:
    """Run every check and report pass/fail.

    Returns:
        0 if every check passed, 1 otherwise
    """
    checks = [
        (
            "125/151/256-byte curves all decode via the IDL parser",
            check_all_lengths_decode,
        ),
        ("graduating-token scripts don't filter on dataSize", check_no_datasize_filter),
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

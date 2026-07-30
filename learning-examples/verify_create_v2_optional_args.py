"""Verify the IDL instruction decoder accepts omitted trailing optional args.

create_v2's trailing `is_cashback_enabled` OptionBool can legally be absent
from the wire (issue #184): the committed blocksubscribe fixture carries a
145-byte create_v2 whose args end right after `is_mayhem_mode`. A decoder
that insists on the byte silently drops roughly half of all coins for every
consumer of `parse_token_creation_from_instruction`.

Offline machine checks, no network and no funds moved:

  1. The raw fixture create_v2 (trailing OptionBool absent) decodes, with
     `is_cashback_enabled` reported as unset (None).
  2. The same data with the byte present still decodes to the OptionBool
     struct form ({"field_0": bool}) consumers already handle.
  3. Mandatory args stay enforced: truncating `is_mayhem_mode` as well must
     fail the decode, not fabricate a default.
  4. Native `option` types decode (update_buyback_config's Option<u64>):
     None tag, Some tag, and omitted-trailing forms.
  5. The pump.fun event parser turns the raw fixture instruction into a
     TokenInfo with is_cashback_coin=False and state_from_event=False.

Usage:
    uv run learning-examples/verify_create_v2_optional_args.py
"""

import base64
import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.transaction import VersionedTransaction  # noqa: E402

from interfaces.core import Platform  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402
from utils.idl_parser import IDLParser  # noqa: E402

FIXTURE = (
    PROJECT_ROOT
    / "learning-examples"
    / "blocksubscribe-transactions"
    / "raw_create_tx_from_blocksubscribe.json"
)
IDL_PATH = PROJECT_ROOT / "idl" / "pump_fun_idl.json"

CREATE_V2_DISCRIMINATOR = bytes.fromhex("d6904cec5f8b31b4")
UPDATE_BUYBACK_CONFIG_DISCRIMINATOR = bytes([251, 224, 171, 146, 160, 26, 113, 233])


def _parser() -> IDLParser:
    return IDLParser(str(IDL_PATH))


def _fixture_create_v2() -> tuple[bytes, list[int], list[bytes]]:
    """Raw create_v2 data, account indices and keys from the committed fixture."""
    fixture = json.loads(FIXTURE.read_text())
    raw = base64.b64decode(fixture["transaction"][0])
    message = VersionedTransaction.from_bytes(raw).message
    account_keys = [bytes(k) for k in message.account_keys]
    for ix in message.instructions:
        data = bytes(ix.data)
        if data.startswith(CREATE_V2_DISCRIMINATOR):
            return data, list(ix.accounts), account_keys
    raise ValueError("fixture has no create_v2 instruction")  # noqa: TRY003


def check_omitted_trailing_option_decodes() -> bool:
    """The 145-byte wire form (no is_cashback_enabled byte) must decode."""
    data, accounts, keys = _fixture_create_v2()
    decoded = _parser().decode_instruction(data, keys, accounts)
    if decoded is None:
        print(f"    decode_instruction returned None for {len(data)}-byte create_v2")
        return False
    args = decoded["args"]
    ok = (
        decoded["instruction_name"] == "create_v2"
        and args.get("is_cashback_enabled") is None
        and args.get("is_mayhem_mode") is False
        and bool(args.get("name"))
        and bool(args.get("creator"))
    )
    if not ok:
        print(f"    unexpected args: {args}")
    return ok


def check_present_trailing_option_unchanged() -> bool:
    """With the byte on the wire, the OptionBool struct form must survive."""
    data, accounts, keys = _fixture_create_v2()
    parser = _parser()
    for byte, expected in ((b"\x00", False), (b"\x01", True)):
        decoded = parser.decode_instruction(data + byte, keys, accounts)
        if decoded is None:
            print(f"    decode failed with trailing byte {byte.hex()}")
            return False
        value = decoded["args"].get("is_cashback_enabled")
        if not (isinstance(value, dict) and value.get("field_0") is expected):
            print(f"    trailing byte {byte.hex()} decoded to {value}")
            return False
    return True


def check_mandatory_args_still_enforced() -> bool:
    """Dropping is_mayhem_mode (a plain bool) must fail, not default."""
    data, accounts, keys = _fixture_create_v2()
    decoded = _parser().decode_instruction(data[:-1], keys, accounts)
    if decoded is not None:
        print(f"    truncated create_v2 decoded anyway: {decoded['args']}")
        return False
    return True


def check_native_option_decodes() -> bool:
    """update_buyback_config carries Option<u64>: None, Some and omitted forms."""
    parser = _parser()
    cases = (
        (UPDATE_BUYBACK_CONFIG_DISCRIMINATOR + b"\x00", None),
        (UPDATE_BUYBACK_CONFIG_DISCRIMINATOR + b"\x01" + struct.pack("<Q", 250), 250),
        (UPDATE_BUYBACK_CONFIG_DISCRIMINATOR, None),
    )
    for data, expected in cases:
        decoded = parser.decode_instruction(data, [], [])
        if decoded is None:
            print(f"    decode returned None for {len(data)}-byte data")
            return False
        value = decoded["args"].get("buyback_basis_points")
        if value != expected:
            print(f"    expected {expected}, got {value}")
            return False
    return True


def check_event_parser_reads_raw_fixture() -> bool:
    """parse_token_creation_from_instruction must not need the appended byte."""
    data, accounts, keys = _fixture_create_v2()
    parser = PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    token_info = parser.parse_token_creation_from_instruction(data, accounts, keys)
    if token_info is None:
        print("    parse_token_creation_from_instruction returned None")
        return False
    ok = (
        token_info.is_cashback_coin is False
        and getattr(token_info, "state_from_event", False) is False
    )
    if not ok:
        print(
            f"    is_cashback_coin={token_info.is_cashback_coin} "
            f"state_from_event={getattr(token_info, 'state_from_event', None)}"
        )
    return ok


def main() -> int:
    checks = [
        (
            "omitted trailing OptionBool decodes as unset",
            check_omitted_trailing_option_decodes,
        ),
        (
            "present trailing OptionBool keeps struct form",
            check_present_trailing_option_unchanged,
        ),
        ("mandatory args still enforced", check_mandatory_args_still_enforced),
        ("native Option<u64> decodes", check_native_option_decodes),
        ("event parser reads the raw fixture", check_event_parser_reads_raw_fixture),
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

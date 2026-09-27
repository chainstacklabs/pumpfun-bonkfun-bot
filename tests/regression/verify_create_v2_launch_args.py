"""Verify the launch scripts build create_v2 from the coin's real parameters.

The cookbook launch path used to hardcode what it could have derived or read.
Three of those were wrong or became wrong the moment a flag was added:

`is_holder_reward` coins do not carry their creator in `BondingCurve.creator` --
the program substitutes `PDA(["holder-rewards", mint])` so the fee accrues to
holders. The buy that follows the create derived `creator_vault` from the paying
wallet regardless, which cannot match the curve's seeds (2006). The address is
deterministic, so it is derived rather than read back from the curve -- a read
also only works while the create and the buy are separate transactions.

`create_v2`'s three trailing args are positional with no presence tag. Emitting
them all-or-nothing made two of the four wire forms on chain unreachable,
including the ones that send an explicit default.

`create_v2` takes four trailing remaining accounts, not the three recorded
previously, and the fourth is `QuoteControl`. A create that omits them is
accepted and produces a SOL-paired coin -- which is also why a creator fee sent
with them missing is stored as 0: pump.fun applies a creator fee only to a coin
priced in something other than SOL.

The opening buy was sized from literal reserves and a literal 1% fee. Both were
right only by coincidence: they match `Global` today, and the fee stops matching
the moment a coin sets a creator fee.

Offline machine checks, no network and no funds moved:

  1. A holder-reward coin's curve creator is the holder-rewards PDA, not the
     wallet that signed the create, and not the --creator argument.
  2. A coin that is not holder-reward keeps the creator it was given.
  3. Every create_v2 in the committed fixtures round-trips through the trailing
     argument encoder byte for byte, across all four wire lengths.
  4. All four wire lengths are reachable, and an omitted arg is distinguishable
     from one explicitly sent as its default.
  5. `--creator` reaches the wire: the creator in the instruction data is the
     argument, not the payer.
  6. The opening buy is sized from what it is given, not from constants --
     doubling the virtual token reserve or halving the opening quote reserve
     doubles the tokens requested, and raising the creator fee lowers them.
  7. The Global decoder stops cleanly on a buffer shorter than the layout
     rather than raising or fabricating a field.
  8. create_v2 carries all four trailing remaining accounts, in order, with
     the quote ATA derived under the quote mint's own token program. Sending
     16 accounts is what makes pump.fun silently store a creator fee of 0.

Usage:
    uv run tests/regression/verify_create_v2_launch_args.py
"""

import base64
import json
import struct
import sys
from pathlib import Path

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "solana"))

import pumpfun_instructions_v2 as pump_v2  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))
import pumpfun_create_and_buy_token_v2 as launch  # noqa: E402

DECODE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"

# A Global with the values mainnet carried when this verifier was written. Used
# as a baseline to perturb, never as an assertion about what mainnet says now.
BASELINE_GLOBAL = {
    "initial_virtual_token_reserves": 1_073_000_000_000_000,
    "initial_virtual_sol_reserves": 30_000_000_000,
    "fee_basis_points": 95,
    "creator_fee_basis_points": 5,
    "creator_fee_configurable": True,
    "max_configurable_creator_fee_bps": 300,
}


def _decode_any(value: str) -> bytes | None:
    """Decode a base58 or base64 instruction blob, whichever it is."""
    for decoder in (base58.b58decode, base64.b64decode):
        try:
            return decoder(value)
        except Exception:  # noqa: BLE001,S112 - not this encoding, try the next
            continue
    return None


def _from_envelope(value: object) -> list[bytes]:
    """create_v2 payloads inside a base64 transaction envelope."""
    if not (isinstance(value, list) and value and isinstance(value[0], str)):
        return []
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(value[0]))
    except Exception:  # noqa: BLE001 - not an envelope
        return []
    return [
        bytes(ix.data)
        for ix in tx.message.instructions
        if bytes(ix.data)[:8] == pump_v2.CREATE_V2_DISCRIMINATOR
    ]


def _walk(node: object, found: list[bytes]) -> None:
    """Collect every create_v2 payload anywhere in a decoded fixture."""
    if isinstance(node, list):
        for value in node:
            _walk(value, found)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "data" and isinstance(value, str):
            raw = _decode_any(value)
            if raw and raw[:8] == pump_v2.CREATE_V2_DISCRIMINATOR:
                found.append(raw)
        elif key == "transaction":
            found.extend(_from_envelope(value))
        _walk(value, found)


def create_v2_instruction_datas() -> list[bytes]:
    """Every create_v2 instruction data blob in the committed fixtures."""
    found: list[bytes] = []
    for path in sorted(DECODE_DIR.glob("raw_create*.json")):
        _walk(json.loads(path.read_text()), found)
    return found


def split_after_mayhem(data: bytes) -> tuple[Pubkey, bytes]:
    """Split a create_v2 payload into its creator and its trailing args."""
    offset = 8
    for _ in range(3):  # name, symbol, uri
        (length,) = struct.unpack_from("<I", data, offset)
        offset += 4 + length
    creator = Pubkey.from_bytes(data[offset : offset + 32])
    offset += 32 + 1  # creator, then is_mayhem_mode
    return creator, data[offset:]


def read_trailing(trailing: bytes) -> dict:
    """Read back whatever the trailing bytes carry, as the decoder would."""
    values = {}
    if len(trailing) >= 1:
        values["is_cashback_enabled"] = bool(trailing[0])
    if len(trailing) >= 9:  # noqa: PLR2004 - the 9-byte wire form
        values["creator_fee_bps"] = struct.unpack_from("<Q", trailing, 1)[0]
    if len(trailing) >= 10:  # noqa: PLR2004 - the 10-byte wire form
        values["is_holder_reward"] = bool(trailing[9])
    return values


def check_holder_reward_creator_is_substituted() -> bool:
    mint = Keypair().pubkey()
    payer = Keypair().pubkey()
    argument = Keypair().pubkey()
    on_curve = pump_v2.curve_creator(mint, argument, is_holder_reward=True)
    expected = Pubkey.find_program_address(
        [b"holder-rewards", bytes(mint)], pump_v2.PUMP_PROGRAM
    )[0]
    if on_curve != expected:
        print(f"  expected holder-rewards PDA {expected}, got {on_curve}")
        return False
    if on_curve in (payer, argument):
        print("  substituted creator collided with the wallet or the argument")
        return False
    # The vault the buy derives must follow the curve, not the signer.
    if pump_v2.find_creator_vault(on_curve) == pump_v2.find_creator_vault(argument):
        print("  creator_vault did not move with the substituted creator")
        return False
    return True


def check_plain_coin_keeps_its_creator() -> bool:
    mint = Keypair().pubkey()
    argument = Keypair().pubkey()
    return pump_v2.curve_creator(mint, argument, is_holder_reward=False) == argument


def check_fixtures_round_trip() -> bool:
    datas = create_v2_instruction_datas()
    if not datas:
        print("  no create_v2 instructions found in the fixtures")
        return False
    ok = True
    for data in datas:
        _, trailing = split_after_mayhem(data)
        rebuilt = pump_v2.encode_create_v2_trailing_args(**read_trailing(trailing))
        if rebuilt != trailing:
            print(f"  {len(trailing)}-byte form: {trailing.hex()} -> {rebuilt.hex()}")
            ok = False
    lengths = {len(split_after_mayhem(d)[1]) for d in datas}
    print(f"  {len(datas)} instructions, wire lengths {sorted(lengths)}")
    return ok


def check_all_four_wire_forms_reachable() -> bool:
    encode = pump_v2.encode_create_v2_trailing_args
    cases = {
        0: encode(),
        1: encode(is_cashback_enabled=False),
        9: encode(creator_fee_bps=0),
        10: encode(is_holder_reward=False),
    }
    for expected, produced in cases.items():
        if len(produced) != expected:
            print(f"  expected a {expected}-byte form, got {len(produced)}")
            return False
    # An omitted arg and an explicit default must not encode the same.
    if encode() == encode(is_cashback_enabled=False):
        print("  omitted and explicitly-false cashback encode identically")
        return False
    if encode(creator_fee_bps=300) == encode(creator_fee_bps=0):
        print("  the fee value does not reach the wire")
        return False
    return True


def check_creator_argument_reaches_the_wire() -> bool:
    mint = Keypair().pubkey()
    payer = Keypair().pubkey()
    creator = Keypair().pubkey()
    ix = pump_v2.build_create_v2_instruction(
        mint=mint,
        user=payer,
        creator=creator,
        name="n",
        symbol="s",
        uri="u",
    )
    on_wire, trailing = split_after_mayhem(bytes(ix.data))
    if on_wire != creator:
        print(f"  creator on the wire is {on_wire}, expected {creator}")
        return False
    if on_wire == payer:
        print("  creator defaulted to the payer despite an explicit argument")
        return False
    if trailing:
        print(f"  expected no trailing args by default, got {len(trailing)} bytes")
        return False
    return True


def check_buy_is_sized_from_global() -> bool:
    # One SOL at the opening SOL reserve, so the figures stay readable.
    spend = 1_000_000_000
    sol_reserve = BASELINE_GLOBAL["initial_virtual_sol_reserves"]
    base = launch.size_opening_buy(BASELINE_GLOBAL, spend, sol_reserve, None)

    doubled_reserves = BASELINE_GLOBAL | {
        "initial_virtual_token_reserves": BASELINE_GLOBAL[
            "initial_virtual_token_reserves"
        ]
        * 2
    }
    doubled = launch.size_opening_buy(doubled_reserves, spend, sol_reserve, None)
    if doubled != base * 2:
        print(f"  doubling virtual tokens gave {doubled}, expected {base * 2}")
        return False

    # The opening quote reserve is an argument, not a constant: a coin priced in
    # an asset whose reserve is half as large buys twice the tokens.
    halved = launch.size_opening_buy(BASELINE_GLOBAL, spend, sol_reserve // 2, None)
    if halved != base * 2:
        print(f"  halving the quote reserve gave {halved}, expected {base * 2}")
        return False

    pricier = launch.size_opening_buy(BASELINE_GLOBAL, spend, sol_reserve, 300)
    if pricier >= base:
        print(f"  a 300 bps creator fee gave {pricier}, not fewer than {base}")
        return False

    # The fee actually applied is Global's, not a literal 1%.
    free = launch.size_opening_buy(
        BASELINE_GLOBAL | {"fee_basis_points": 0, "creator_fee_basis_points": 0},
        spend,
        sol_reserve,
        None,
    )
    if free <= base:
        print(f"  zero fees gave {free}, not more than {base}")
        return False
    return True


def check_quote_accounts_are_present() -> bool:
    mint = Keypair().pubkey()
    payer = Keypair().pubkey()
    quote = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    ix = pump_v2.build_create_v2_instruction(
        mint=mint,
        user=payer,
        creator=payer,
        name="n",
        symbol="s",
        uri="u",
        quote_mint=quote,
        quote_token_program=pump_v2.TOKEN_PROGRAM,
    )
    expected_accounts = 20
    if len(ix.accounts) != expected_accounts:
        print(f"  expected {expected_accounts} accounts, got {len(ix.accounts)}")
        return False
    tail = [meta.pubkey for meta in ix.accounts[-4:]]
    curve = pump_v2.find_bonding_curve(mint)
    want = [
        quote,
        pump_v2.find_associated_token_account(curve, quote, pump_v2.TOKEN_PROGRAM),
        pump_v2.TOKEN_PROGRAM,
        pump_v2.find_quote_control(),
    ]
    if tail != want:
        print(f"  trailing accounts are {tail}, expected {want}")
        return False
    # The quote ATA follows the quote mint's program, not the base mint's.
    if tail[1] == pump_v2.find_associated_token_account(
        curve, quote, pump_v2.TOKEN_2022_PROGRAM
    ):
        print("  quote ATA derived under the base token program")
        return False
    return True


def check_global_decoder_tolerates_a_short_buffer() -> bool:
    full = bytes(8) + bytes(2000)
    decoded = pump_v2.decode_global(full)
    if "is_holder_reward_enabled" not in decoded:
        print("  a long buffer did not decode the whole layout")
        return False
    # 200 bytes reaches creator_fee_basis_points (ends at 162) but stops well
    # short of creator_fee_configurable (1046).
    truncated = pump_v2.decode_global(bytes(200))
    if "fee_basis_points" not in truncated:
        print("  a short buffer dropped a field it does hold")
        return False
    if "creator_fee_configurable" in truncated:
        print("  a short buffer fabricated a field past its end")
        return False
    return True


def main() -> int:
    checks = [
        (
            "holder-reward creator is substituted",
            check_holder_reward_creator_is_substituted,
        ),
        ("a plain coin keeps its creator", check_plain_coin_keeps_its_creator),
        ("fixtures round-trip through the encoder", check_fixtures_round_trip),
        ("all four wire forms reachable", check_all_four_wire_forms_reachable),
        ("--creator reaches the wire", check_creator_argument_reaches_the_wire),
        ("opening buy is sized from Global", check_buy_is_sized_from_global),
        (
            "Global decoder tolerates a short buffer",
            check_global_decoder_tolerates_a_short_buffer,
        ),
        ("create_v2 carries the four quote accounts", check_quote_accounts_are_present),
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

"""Offline verifier for the shreds (pre-execution) listener.

Guards the shreds listener and the instruction-decode path it depends on. No
network, no funds; both fixtures are committed geyser `SubscribeDeshred` frames.

What it pins:

1. **`user` is account 5 on `create_v2`, not 7.** `create_v2` dropped the four
   metaplex accounts legacy `create` carries, so the launcher's wallet sits four
   places earlier; index 7 yields the Token-2022 program and every address
   derived from the wallet is wrong. Dormant while the instruction path was only
   a fallback behind `log_messages`; shreds has no logs, so it runs on every coin.

2. **A holder-reward coin's creator comes from the mint, not the args.** The
   program ignores `args.creator` on those coins and writes
   `PDA(["holder-rewards", mint])` into `BondingCurve.creator`, so
   `creator_vault` must derive from the PDA or the buy is rejected on a seeds
   constraint. There is no curve to read before execution.

3. **The trailing-arg form cannot lie about holder rewards.** `create_v2`'s
   trailing args are positional, so an instruction that stops short cannot have
   set a later one: a truncated form decodes as *not* holder-reward rather than
   raising or guessing.

4. **Lookup-table accounts are resolved before indexing.** The stream reports
   them as `loaded_writable_addresses` then `loaded_readonly_addresses`. Both
   fixtures use a lookup table, so a listener that ignores them fails here rather
   than on the first live coin.

5. **Nothing on this path reads `meta`.** A pre-execution stream has no
   `TransactionStatusMeta`, so reaching for `meta.log_messages` gets silence, not
   an error — the failure would be a listener that quietly detects nothing.

6. **The listener marks its tokens `state_from_event`**, which is what lets
   `extreme_fast_mode` submit without a curve read. Waiting is not a safer
   fallback: the account does not exist yet.

Usage:
    uv run tests/regression/verify_shreds_listener.py
"""

# The listener's public entry point opens a gRPC stream, so these checks drive
# the decode helpers directly. Reaching past the public API is the point here.
# ruff: noqa: SLF001

import ast
import base64
import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import SystemAddresses  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.universal_shreds_listener import UniversalShredsListener  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402

FIXTURE_DIR = PROJECT_ROOT / "cookbook" / "pumpfun" / "decode"
HOLDER_REWARD_FIXTURE = FIXTURE_DIR / "raw_create_v2_holder_reward_from_deshred.json"
ORDINARY_FIXTURE = FIXTURE_DIR / "raw_create_v2_ordinary_from_deshred.json"

LISTENER_SOURCE = PROJECT_ROOT / "src" / "monitoring" / "universal_shreds_listener.py"

# create_v2 account indices, from idl/pump_fun_idl.json.
CREATE_V2_MINT_INDEX = 0
CREATE_V2_BONDING_CURVE_INDEX = 2
CREATE_V2_USER_INDEX = 5
# The index the code used to read, kept so the check states what it rules out.
LEGACY_CREATE_USER_INDEX = 7

CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])


def _load(path: Path) -> dict:
    """Read one committed fixture.

    Args:
        path: Fixture file path

    Returns:
        The parsed fixture document
    """
    with path.open() as handle:
        return json.load(handle)


def _update(fixture: dict) -> geyser_pb2.SubscribeUpdateDeshred:
    """Rebuild the geyser update from a fixture.

    Args:
        fixture: Parsed fixture document

    Returns:
        The decoded SubscribeUpdateDeshred protobuf
    """
    update = geyser_pb2.SubscribeUpdateDeshred()
    update.ParseFromString(base64.b64decode(fixture["subscribe_update_base64"]))
    return update


def _listener() -> UniversalShredsListener:
    """Build the listener without connecting to anything.

    Returns:
        A pump.fun-only shreds listener
    """
    return UniversalShredsListener(
        geyser_endpoint="unused.invalid:443",
        geyser_api_token="unused",  # noqa: S106 — never sent; nothing connects
        geyser_auth_type="x-token",
        platforms=[Platform.PUMP_FUN],
    )


def _account_keys(update: geyser_pb2.SubscribeUpdateDeshred) -> list[bytes]:
    """Resolve the full account table for a deshred update.

    Args:
        update: A deshred SubscribeUpdateDeshred

    Returns:
        Static keys followed by writable then read-only loaded addresses
    """
    transaction = update.deshred_transaction.transaction
    keys = list(transaction.transaction.message.account_keys)
    keys.extend(transaction.loaded_writable_addresses)
    keys.extend(transaction.loaded_readonly_addresses)
    return keys


def _create_instruction(update: geyser_pb2.SubscribeUpdateDeshred) -> object | None:
    """Find the create_v2 instruction in a deshred update.

    Args:
        update: A deshred SubscribeUpdateDeshred

    Returns:
        The instruction, or None if the fixture carries no create_v2
    """
    message = update.deshred_transaction.transaction.transaction.message
    for instruction in message.instructions:
        if bytes(instruction.data).startswith(CREATE_V2_DISCRIMINATOR):
            return instruction
    return None


def check_fixtures_are_pre_execution() -> bool:
    """Both fixtures must be real deshred frames with no execution metadata.

    A fixture carrying logs would make every other check here meaningless: the
    parser would take the CreateEvent route that shreds mode does not have.

    Returns:
        True if both fixtures are pre-execution frames
    """
    ok = True
    for path in (HOLDER_REWARD_FIXTURE, ORDINARY_FIXTURE):
        update = _update(_load(path))
        if not update.HasField("deshred_transaction"):
            print(f"     {path.name} is not a deshred update")
            ok = False
            continue
        transaction = update.deshred_transaction.transaction
        # Not "is meta unset" but "there is no meta field to set": the deshred
        # message has no TransactionStatusMeta, which is what makes the
        # CreateEvent route structurally unavailable rather than merely absent
        # from these two captures.
        fields = {field.name for field in transaction.DESCRIPTOR.fields}
        if "meta" in fields:
            print(
                f"     {path.name}: the deshred message gained a meta field; "
                "the CreateEvent route may now be available and this listener's "
                "instruction-only design should be revisited"
            )
            ok = False
        if _create_instruction(update) is None:
            print(f"     {path.name} carries no top-level create_v2")
            ok = False
    return ok


def check_fixtures_use_lookup_tables() -> bool:
    """The fixtures must actually need lookup-table resolution.

    Otherwise check_lookup_table_accounts_resolved proves nothing: a listener
    that ignored loaded addresses would pass it by accident.

    Returns:
        True if both fixtures load accounts from a lookup table
    """
    ok = True
    for path in (HOLDER_REWARD_FIXTURE, ORDINARY_FIXTURE):
        update = _update(_load(path))
        transaction = update.deshred_transaction.transaction
        loaded = len(transaction.loaded_writable_addresses) + len(
            transaction.loaded_readonly_addresses
        )
        if loaded == 0:
            print(f"     {path.name} loads no lookup-table accounts")
            ok = False
    return ok


def check_lookup_table_accounts_resolved() -> bool:
    """Account indices must resolve past the static keys, in the reported order.

    Args:
        None

    Returns:
        True if the resolved table is static + writable + readonly
    """
    update = _update(_load(ORDINARY_FIXTURE))
    transaction = update.deshred_transaction.transaction
    static = list(transaction.transaction.message.account_keys)
    resolved = UniversalShredsListener._resolve_account_keys(transaction)

    expected = (
        static
        + list(transaction.loaded_writable_addresses)
        + list(transaction.loaded_readonly_addresses)
    )
    if [bytes(k) for k in resolved] != [bytes(k) for k in expected]:
        print("     resolved account table does not match static+writable+readonly")
        return False

    instruction = _create_instruction(update)
    highest = max(instruction.accounts)
    if highest < len(static):
        print("     create_v2 indexes no loaded account; the check is vacuous")
        return False
    return True


def check_user_account_index() -> bool:
    """`user` must come from account 5, not the legacy index 7.

    Returns:
        True if the parsed user is the wallet at index 5
    """
    ok = True
    for path in (HOLDER_REWARD_FIXTURE, ORDINARY_FIXTURE):
        update = _update(_load(path))
        keys = _account_keys(update)
        instruction = _create_instruction(update)
        accounts = list(instruction.accounts)

        expected_user = Pubkey.from_bytes(keys[accounts[CREATE_V2_USER_INDEX]])
        wrong_user = Pubkey.from_bytes(keys[accounts[LEGACY_CREATE_USER_INDEX]])

        token_info = _listener()._process_update(update)
        if token_info is None:
            print(f"     {path.name} produced no TokenInfo")
            ok = False
            continue

        if token_info.user != expected_user:
            print(
                f"     {path.name}: user is {token_info.user}, "
                f"expected account 5 ({expected_user})"
            )
            ok = False

        # State what the old index actually pointed at, so a regression is
        # recognisable rather than just unequal.
        if wrong_user != SystemAddresses.TOKEN_2022_PROGRAM:
            print(
                f"     {path.name}: account 7 is {wrong_user}, expected the "
                "Token-2022 program — the fixture or the IDL layout moved"
            )
            ok = False
        if token_info.user == wrong_user:
            print(f"     {path.name}: user was read from the legacy index 7")
            ok = False
    return ok


def check_holder_reward_creator_derived() -> bool:
    """A holder-reward coin's creator must be the mint's holder-rewards PDA.

    Returns:
        True if creator and creator_vault derive from the PDA, not from args
    """
    fixture = _load(HOLDER_REWARD_FIXTURE)
    if not fixture.get("is_holder_reward"):
        print("     the holder-reward fixture is not flagged holder-reward")
        return False

    update = _update(fixture)
    token_info = _listener()._process_update(update)
    if token_info is None:
        print("     holder-reward fixture produced no TokenInfo")
        return False

    expected_creator = PumpFunAddresses.find_holder_reward_creator(token_info.mint)
    expected_vault, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(expected_creator)], PumpFunAddresses.PROGRAM
    )
    # The substitution is only meaningful if it differs from what the
    # instruction claims; otherwise the fixture proves nothing.
    args_creator = _args_creator(_create_instruction(update))

    ok = True
    if not token_info.is_holder_reward:
        print("     holder-reward flag not decoded from the instruction")
        ok = False
    if token_info.creator != expected_creator:
        print(
            f"     creator is {token_info.creator}, expected the holder-rewards "
            f"PDA {expected_creator}"
        )
        ok = False
    if token_info.creator_vault != expected_vault:
        print(
            f"     creator_vault is {token_info.creator_vault}, expected "
            f"{expected_vault}"
        )
        ok = False
    if token_info.creator == args_creator:
        print(
            "     creator equals args.creator; the fixture does not exercise "
            "the substitution"
        )
        ok = False
    return ok


def _args_creator(instruction: object) -> Pubkey:
    """Read the `creator` argument out of a create_v2 instruction.

    Args:
        instruction: A create_v2 instruction

    Returns:
        The creator the launcher passed
    """
    data = bytes(instruction.data)
    offset = 8
    for _ in range(3):  # name, symbol, uri
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4 + length
    return Pubkey.from_bytes(data[offset : offset + 32])


def check_ordinary_creator_from_args() -> bool:
    """An ordinary coin's creator is the one the instruction carries.

    Returns:
        True if creator matches args.creator for a non-holder-reward coin
    """
    fixture = _load(ORDINARY_FIXTURE)
    if fixture.get("is_holder_reward"):
        print("     the ordinary fixture is flagged holder-reward")
        return False

    update = _update(fixture)
    token_info = _listener()._process_update(update)
    if token_info is None:
        print("     ordinary fixture produced no TokenInfo")
        return False

    if token_info.is_holder_reward:
        print("     ordinary coin decoded as holder-reward")
        return False

    expected = _args_creator(_create_instruction(update))
    if token_info.creator != expected:
        print(f"     creator is {token_info.creator}, expected {expected}")
        return False
    return True


def check_truncated_trailing_args_are_not_holder_reward() -> bool:
    """A create_v2 that stops short cannot be a holder-reward coin.

    The trailing arguments are positional, so reaching argument 8 means sending
    6 and 7 first. Truncating the real instruction must therefore decode as a
    plain coin — never raise, and never leave the flag set.

    Returns:
        True if every truncated form decodes as a non-holder-reward coin
    """
    update = _update(_load(HOLDER_REWARD_FIXTURE))
    instruction = _create_instruction(update)
    data = bytearray(instruction.data)

    # Trailing args are the last 10 bytes: is_cashback_enabled (1),
    # creator_fee_bps (8), is_holder_reward (1).
    listener = _listener()
    for dropped in (1, 9, 10):
        truncated = bytes(data[: len(data) - dropped])
        instruction.data = truncated
        try:
            token_info = listener._process_update(update)
        except Exception as error:  # noqa: BLE001
            print(f"     dropping {dropped} trailing bytes raised {error!r}")
            return False
        if token_info is None:
            print(f"     dropping {dropped} trailing bytes produced no TokenInfo")
            return False
        if token_info.is_holder_reward:
            print(
                f"     dropping {dropped} trailing bytes still decoded as holder-reward"
            )
            return False
        # With the flag unset the creator must fall back to the args, not the PDA.
        if token_info.creator != _args_creator(instruction):
            print(
                f"     dropping {dropped} trailing bytes did not fall back to "
                "args.creator"
            )
            return False
    return True


def check_tokens_marked_trusted() -> bool:
    """The listener must mark its tokens so extreme_fast_mode skips the curve read.

    Returns:
        True if both fixtures produce state_from_event tokens with a quote mint
    """
    ok = True
    for path in (HOLDER_REWARD_FIXTURE, ORDINARY_FIXTURE):
        token_info = _listener()._process_update(_update(_load(path)))
        if token_info is None:
            print(f"     {path.name} produced no TokenInfo")
            ok = False
            continue
        if not token_info.state_from_event:
            print(f"     {path.name}: state_from_event not set")
            ok = False
        # _can_skip_refresh also requires a resolved quote mint.
        if token_info.quote_mint is None:
            print(f"     {path.name}: quote_mint unresolved, the buy would refresh")
            ok = False
    return ok


def check_listener_never_reads_meta() -> bool:
    """The listener must not reach for execution metadata that cannot exist.

    Returns:
        True if the listener source references no meta field
    """
    tree = ast.parse(LISTENER_SOURCE.read_text())

    # Walk attribute access rather than scanning text, so a mention inside a
    # docstring or comment -- this module is full of them -- is not a finding.
    banned = {"meta", "log_messages", "logMessages"}
    offenders = [
        f"line {node.lineno}: .{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in banned
    ]

    if offenders:
        print("     listener reads execution metadata that deshred never carries:")
        for line in offenders:
            print(f"       {line}")
        return False
    return True


CHECKS = (
    ("fixtures are pre-execution deshred frames", check_fixtures_are_pre_execution),
    ("fixtures genuinely use lookup tables", check_fixtures_use_lookup_tables),
    ("lookup-table accounts resolved in order", check_lookup_table_accounts_resolved),
    ("user read from create_v2 account 5", check_user_account_index),
    ("holder-reward creator derived from mint", check_holder_reward_creator_derived),
    ("ordinary creator taken from args", check_ordinary_creator_from_args),
    (
        "truncated trailing args are not holder-reward",
        check_truncated_trailing_args_are_not_holder_reward,
    ),
    ("tokens marked state_from_event", check_tokens_marked_trusted),
    ("listener never reads meta", check_listener_never_reads_meta),
)


def main() -> int:
    """Run every check.

    Returns:
        0 if all checks pass, 1 otherwise
    """
    print("Verifying the shreds (pre-execution) listener\n")
    failures = 0
    for name, check in CHECKS:
        try:
            passed = check()
        except Exception as error:  # noqa: BLE001
            print(f"  [FAIL] {name}")
            print(f"     raised {error!r}")
            failures += 1
            continue
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        failures += not passed

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

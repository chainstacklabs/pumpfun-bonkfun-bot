"""Verify LaunchLab launches parse into the TokenInfo the pool will agree with.

letsbonk.fun and StonkFun share one program, Raydium LaunchLab; a launch belongs
to the platform whose config it names. The letsbonk parser this replaced read
the creator from account 0 — the payer, which the program does not have to
make the creator — never read the quote mint, missed launches issued by a
router as inner instructions, and in its geyser path dropped every account
behind an address lookup table.

Fixture: `raw_stonkfun_launches_from_gettransaction.json`, real StonkFun
launches sent as a legacy, a v0 and a v1 transaction.

Offline machine checks, no network and no funds moved:

  A. Each launch parses over blockSubscribe JSON into the instruction's own
     accounts: creator from the `creator` slot, payer as `user`, pool, vaults,
     both configs, the quote mint and both token programs, the transfer fee
     argument, and `state_from_event` set.
  B. The same launch parses identically from a geyser frame.
  C. A launch whose payer is not its creator reports the creator slot.
  D. A launch issued as an inner instruction is found, over both routes.
  E. Each platform keeps to its own configs: the letsbonk parser ignores every
     StonkFun launch, and claims one rewritten to name letsbonk's config.
  F. The stream listeners subscribe on the platform configs for LaunchLab and
     on the program for pump.fun, and a block listener running both LaunchLab
     platforms routes a StonkFun launch to StonkFun.

Usage:
    uv run tests/regression/verify_launchlab_launch_parsing.py
"""

import copy
import json
import sys
from pathlib import Path

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.universal_block_listener import UniversalBlockListener  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from platforms.letsbonk import PLATFORM_CONFIGS as LETSBONK_CONFIGS  # noqa: E402
from platforms.letsbonk import LetsBonkEventParser  # noqa: E402
from platforms.pumpfun import PumpFunEventParser  # noqa: E402
from platforms.stonkfun import StonkFunEventParser  # noqa: E402
from platforms.stonkfun.addresses import (
    PLATFORM_CONFIGS as STONKFUN_CONFIGS,
)
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = Path(__file__).with_name("raw_stonkfun_launches_from_gettransaction.json")

PARSER = get_idl_manager().get_parser(Platform.STONK_FUN)
STONKFUN = StonkFunEventParser(PARSER)
LETSBONK = LetsBonkEventParser(PARSER)
INITIALIZE = bytes(
    next(
        i
        for i in json.load(
            (PROJECT_ROOT / "idl" / "raydium_launchlab_idl.json").open()
        )["instructions"]
        if i["name"] == "initialize_with_token_2022"
    )["discriminator"]
)
OTHER_CREATOR = "CreatorXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX1"

# initialize_with_token_2022 account slots, from the IDL.
SLOTS = {
    "payer": 0,
    "creator": 1,
    "global_config": 2,
    "platform_config": 3,
    "pool_state": 5,
    "base_mint": 6,
    "quote_mint": 7,
    "base_vault": 8,
    "quote_vault": 9,
    "base_token_program": 10,
    "quote_token_program": 11,
}


def _launches() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _initialize(tx: dict) -> tuple[dict, dict]:
    """The launch instruction and its accounts by slot name."""
    keys = _keys(tx)
    for ix in tx["transaction"]["message"]["instructions"]:
        if keys[ix["programIdIndex"]] == str(LAUNCHLAB_PROGRAM) and base58.b58decode(
            ix["data"]
        ).startswith(INITIALIZE):
            return ix, {
                name: keys[ix["accounts"][slot]] for name, slot in SLOTS.items()
            }
    raise AssertionError("fixture launch has no top-level initialize")


def _parse_block(parser, tx: dict):
    return parser.parse_token_creation_from_block(
        {"transactions": [{"transaction": tx["transaction"], "meta": tx["meta"]}]}
    )


def _geyser_update(tx: dict) -> geyser_pb2.SubscribeUpdate:
    """A geyser frame carrying the same transaction as the JSON fixture."""
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    message = info.transaction.message
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    loaded = tx["meta"].get("loadedAddresses") or {}
    info.meta.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.meta.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    for group in tx["meta"].get("innerInstructions") or []:
        inner = info.meta.inner_instructions.add()
        inner.index = group["index"]
        for ix in group["instructions"]:
            compiled = inner.instructions.add()
            compiled.program_id_index = ix["programIdIndex"]
            compiled.accounts = bytes(ix["accounts"])
            compiled.data = base58.b58decode(ix["data"])
    return update


def _fields(token_info) -> dict:
    return {
        "creator": str(token_info.creator),
        "payer": str(token_info.user),
        "pool_state": str(token_info.pool_state),
        "base_mint": str(token_info.mint),
        "quote_mint": str(token_info.quote_mint),
        "base_vault": str(token_info.base_vault),
        "quote_vault": str(token_info.quote_vault),
        "global_config": str(token_info.global_config),
        "platform_config": str(token_info.platform_config),
        "base_token_program": str(token_info.token_program_id),
        "quote_token_program": str(token_info.quote_token_program_id),
    }


def check_a_block_route() -> bool:
    """A: block JSON -> every field from its own account slot."""
    ok = True
    for tx in _launches():
        _, accounts = _initialize(tx)
        token_info = _parse_block(STONKFUN, tx)
        if token_info is None:
            print(f"    version {tx.get('version')}: not parsed")
            ok = False
            continue
        args = PARSER.decode_instruction(
            base58.b58decode(_initialize(tx)[0]["data"]), [], []
        )["args"]
        fee = (args.get("transfer_fee_extension_param") or {}).get(
            "transfer_fee_basis_points", 0
        )
        good = (
            _fields(token_info) == accounts
            and token_info.transfer_fee_bps == fee
            and token_info.state_from_event
            and token_info.platform == Platform.STONK_FUN
        )
        if not good:
            print(
                f"    version {tx.get('version')}: {_fields(token_info)} vs {accounts}"
            )
        ok &= good
    return ok


def check_b_geyser_route() -> bool:
    """B: geyser frame -> the same TokenInfo as the block route."""
    ok = True
    for tx in _launches():
        from_geyser = STONKFUN.parse_token_creation_from_geyser(_geyser_update(tx))
        from_block = _parse_block(STONKFUN, tx)
        good = from_geyser is not None and _fields(from_geyser) == _fields(from_block)
        if not good:
            print(f"    version {tx.get('version')}: geyser gave {from_geyser}")
        ok &= good
    return ok


def check_c_payer_is_not_creator() -> bool:
    """C: a separate creator account is the creator; the payer is only the user."""
    tx = copy.deepcopy(next(t for t in _launches() if t.get("version") == "legacy"))
    message = tx["transaction"]["message"]
    message["accountKeys"].append(OTHER_CREATOR)
    ix, accounts = _initialize(tx)
    ix["accounts"][SLOTS["creator"]] = len(message["accountKeys"]) - 1
    token_info = _parse_block(STONKFUN, tx)
    ok = (
        token_info is not None
        and str(token_info.creator) == OTHER_CREATOR
        and str(token_info.user) == accounts["payer"]
    )
    if not ok:
        print(
            f"    creator={token_info and token_info.creator} user={token_info and token_info.user}"
        )
    return ok


def check_d_inner_instruction() -> bool:
    """D: a launch a router issued by CPI is found over both routes."""
    tx = copy.deepcopy(next(t for t in _launches() if t.get("version") == "legacy"))
    message = tx["transaction"]["message"]
    ix, _ = _initialize(tx)
    message["instructions"].remove(ix)
    tx["meta"].setdefault("innerInstructions", []).append(
        {"index": 0, "instructions": [ix]}
    )
    from_block = _parse_block(STONKFUN, tx)
    from_geyser = STONKFUN.parse_token_creation_from_geyser(_geyser_update(tx))
    ok = from_block is not None and from_geyser is not None
    if not ok:
        print(f"    block={from_block is not None} geyser={from_geyser is not None}")
    return ok


def check_e_platforms_keep_to_their_configs() -> bool:
    """E: letsbonk ignores StonkFun launches, and claims one under its own config."""
    ignored = all(_parse_block(LETSBONK, tx) is None for tx in _launches())
    tx = copy.deepcopy(next(t for t in _launches() if t.get("version") == "legacy"))
    message = tx["transaction"]["message"]
    message["accountKeys"].append(str(next(iter(LETSBONK_CONFIGS))))
    ix, _ = _initialize(tx)
    ix["accounts"][SLOTS["platform_config"]] = len(message["accountKeys"]) - 1
    claimed = _parse_block(LETSBONK, tx)
    ok = (
        ignored
        and claimed is not None
        and claimed.platform == Platform.LETS_BONK
        and _parse_block(STONKFUN, tx) is None
    )
    if not ok:
        print(f"    ignored={ignored} claimed={claimed}")
    return ok


def check_f_stream_filters_and_routing() -> bool:
    """F: subscriptions per platform, and shared-program routing in the block listener."""
    pump = PumpFunEventParser(get_idl_manager().get_parser(Platform.PUMP_FUN))
    filters_ok = (
        set(STONKFUN.get_stream_filter_accounts()) == set(STONKFUN_CONFIGS)
        and set(LETSBONK.get_stream_filter_accounts()) == set(LETSBONK_CONFIGS)
        and pump.get_stream_filter_accounts() == [pump.get_program_id()]
    )
    listener = UniversalBlockListener(
        "wss://offline.invalid", platforms=[Platform.LETS_BONK, Platform.STONK_FUN]
    )
    subscribed = {Pubkey.from_string(a) for a in listener.platform_program_ids}
    routed = [listener._parse_from_logs(tx) for tx in _launches()]
    ok = (
        filters_ok
        and subscribed == set(STONKFUN_CONFIGS) | set(LETSBONK_CONFIGS)
        and all(r is not None and r.platform == Platform.STONK_FUN for r in routed)
    )
    if not ok:
        print(f"    filters_ok={filters_ok} subscribed={subscribed} routed={routed}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: block route reads every account slot", check_a_block_route),
        ("B: geyser route matches the block route", check_b_geyser_route),
        ("C: creator slot, not payer", check_c_payer_is_not_creator),
        ("D: router-issued launch found", check_d_inner_instruction),
        ("E: platforms keep to their configs", check_e_platforms_keep_to_their_configs),
        (
            "F: stream filters and shared-program routing",
            check_f_stream_filters_and_routing,
        ),
    ]
    passed = 0
    for label, check in checks:
        ok = check()
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

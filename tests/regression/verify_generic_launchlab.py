"""Verify `platform: launchlab` takes a launch from any launchpad on LaunchLab.

letsbonk.fun and StonkFun are two of thousands of platform configs on Raydium
LaunchLab. `launchlab` takes a launch under any of them and records which;
`launchlab.platform_configs` narrows it, and `lets_bonk` / `stonk_fun` stay
presets fixed to their own configs.

Fixtures: `raw_launchlab_launches_from_gettransaction.json`, real launches on
two other launchpads (an `initialize_with_token_2022` quoted in RAY and an
`initialize_v2` quoted in SOL); `raw_stonkfun_launches_from_gettransaction.json`.

Offline machine checks, no network and no funds moved:

  A. The `launchlab` parser decodes each other-launchpad launch over blocks and
     geyser alike, with every field from its own account slot, the platform
     `launchlab` and the launch's platform config recorded; the presets ignore
     them. It takes the StonkFun launches too.
  B. Narrowing: a `launchlab` parser narrowed to one config takes launches
     under it and ignores the rest, and a preset cannot be narrowed to a config
     it does not cover.
  C. Subscriptions: `launchlab` subscribes on the program, a narrowed one on
     its configs, and the block and geyser listeners the listener factory
     builds subscribe on what the bot's `platform_configs` names.
  D. A launch on a fixed-price curve is ignored: the trade math is for the
     constant-product curve only.
  E. Config: `launchlab` pairs with `blocks` and `geyser` only;
     `launchlab.platform_configs` is refused on pump.fun, on a preset naming a
     config outside it, empty or malformed.
  F. The registry keeps one set of implementations per narrowing, so two bots
     in one process listen on their own configs.
  G. The trade log and the token file record the coin's platform config.

Usage:
    uv run tests/regression/verify_generic_launchlab.py
"""

import copy
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_config  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import EventParser, Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms import platform_factory  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM, LaunchLabEventParser  # noqa: E402
from platforms.letsbonk import PLATFORM_CONFIGS as LETSBONK_CONFIGS  # noqa: E402
from platforms.letsbonk import LetsBonkEventParser  # noqa: E402
from platforms.stonkfun import StonkFunEventParser  # noqa: E402
from platforms.stonkfun.addresses import (  # noqa: E402
    REWARD_PLATFORM_CONFIG,
    STANDARD_PLATFORM_CONFIG,
)
from trading import trade_log  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

HERE = Path(__file__).resolve().parent
PARSER = get_idl_manager().get_parser(Platform.LAUNCHLAB)
LAUNCHLAB = LaunchLabEventParser(PARSER)
INITIALIZE = {
    bytes(i["discriminator"]): i["name"]
    for i in json.loads(
        (PROJECT_ROOT / "idl" / "raydium_launchlab_idl.json").read_text()
    )["instructions"]
    if i["name"].startswith("initialize")
}
# Account slots both initialize variants share, from the IDL.
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
}


def _fixture(name: str) -> list[dict]:
    return json.loads((HERE / name).read_text())


def _other_launches() -> list[dict]:
    return _fixture("raw_launchlab_launches_from_gettransaction.json")


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _initialize(tx: dict) -> tuple[dict, dict]:
    """The top-level launch instruction and its accounts by slot name."""
    keys = _keys(tx)
    for ix in tx["transaction"]["message"]["instructions"]:
        if keys[ix["programIdIndex"]] == str(LAUNCHLAB_PROGRAM) and (
            base58.b58decode(ix["data"])[:8] in INITIALIZE
        ):
            return ix, {
                name: keys[ix["accounts"][slot]] for name, slot in SLOTS.items()
            }
    raise AssertionError("fixture launch has no top-level initialize")  # noqa: TRY003


def _parse_block(parser: EventParser, tx: dict) -> TokenInfo | None:
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


def _fields(token_info: TokenInfo) -> dict:
    return {
        "payer": str(token_info.user),
        "creator": str(token_info.creator),
        "global_config": str(token_info.global_config),
        "platform_config": str(token_info.platform_config),
        "pool_state": str(token_info.pool_state),
        "base_mint": str(token_info.mint),
        "quote_mint": str(token_info.quote_mint),
        "base_vault": str(token_info.base_vault),
        "quote_vault": str(token_info.quote_vault),
    }


def _with_platform_config(tx: dict, config: Pubkey) -> dict:
    """The launch rewritten to name another platform config."""
    tx = copy.deepcopy(tx)
    message = tx["transaction"]["message"]
    message["accountKeys"].append(str(config))
    ix, _ = _initialize(tx)
    ix["accounts"][SLOTS["platform_config"]] = len(message["accountKeys"]) - 1
    return tx


def check_a_any_launchpad() -> bool:
    """A: other launchpads decode over both routes; presets ignore them."""
    ok = True
    for tx in _other_launches():
        _, accounts = _initialize(tx)
        from_block = _parse_block(LAUNCHLAB, tx)
        from_geyser = LAUNCHLAB.parse_token_creation_from_geyser(_geyser_update(tx))
        good = (
            from_block is not None
            and from_geyser is not None
            and _fields(from_block) == accounts
            and _fields(from_geyser) == accounts
            and from_block.platform == Platform.LAUNCHLAB
            and from_block.state_from_event
        )
        presets = [
            p(PARSER).parse_token_creation_from_geyser(_geyser_update(tx))
            for p in (LetsBonkEventParser, StonkFunEventParser)
        ] + [
            _parse_block(p(PARSER), tx)
            for p in (LetsBonkEventParser, StonkFunEventParser)
        ]
        if not good or any(presets):
            print(
                f"    {accounts['platform_config']}: block={from_block} presets={presets}"
            )
            ok = False
    stonkfun = _fixture("raw_stonkfun_launches_from_gettransaction.json")
    taken = [_parse_block(LAUNCHLAB, tx) for tx in stonkfun]
    if not all(t is not None and t.platform == Platform.LAUNCHLAB for t in taken):
        print(f"    StonkFun launches: {taken}")
        ok = False
    return ok


def check_b_narrowing() -> bool:
    """B: a narrowed parser keeps to its configs; a preset can't widen."""
    tx = _other_launches()[0]
    _, accounts = _initialize(tx)
    own = Pubkey.from_string(accounts["platform_config"])
    narrowed = LaunchLabEventParser(PARSER, platform_configs=[own])
    elsewhere = LaunchLabEventParser(
        PARSER, platform_configs=[STANDARD_PLATFORM_CONFIG]
    )
    moved = _with_platform_config(tx, STANDARD_PLATFORM_CONFIG)
    reward_only = StonkFunEventParser(PARSER, platform_configs=[REWARD_PLATFORM_CONFIG])
    problems = []
    if _parse_block(narrowed, tx) is None:
        problems.append("narrowed parser missed a launch under its config")
    if _parse_block(elsewhere, tx) is not None:
        problems.append("narrowed parser took a launch under another config")
    if _parse_block(elsewhere, moved) is None:
        problems.append("narrowed parser missed the launch moved under its config")
    if _parse_block(reward_only, moved) is not None:
        problems.append("StonkFun narrowed to reward took a standard launch")
    for preset, config in (
        (StonkFunEventParser, next(iter(LETSBONK_CONFIGS))),
        (LetsBonkEventParser, REWARD_PLATFORM_CONFIG),
    ):
        try:
            preset(PARSER, platform_configs=[config])
            problems.append(f"{preset.__name__} accepted {config}")
        except ValueError:
            pass
    try:
        LaunchLabEventParser(PARSER, platform_configs=[])
        problems.append("an empty narrowing was accepted")
    except ValueError:
        pass
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_c_subscriptions() -> bool:
    """C: program for every config, the configs once narrowed, in both listeners."""
    config = Pubkey.from_string(_initialize(_other_launches()[0])[1]["platform_config"])
    problems = []
    if LAUNCHLAB.get_stream_filter_accounts() != [LAUNCHLAB_PROGRAM]:
        problems.append(
            f"launchlab subscribes on {LAUNCHLAB.get_stream_filter_accounts()}"
        )
    if LAUNCHLAB.get_creation_filter_accounts() != [LAUNCHLAB_PROGRAM]:
        problems.append("launchlab creation filter is not the program")
    for narrowing, expected in (
        (None, {LAUNCHLAB_PROGRAM}),
        (frozenset({config}), {config}),
    ):
        block, geyser = (
            ListenerFactory.create_listener(
                listener_type=kind,
                wss_endpoint="wss://offline.invalid",
                geyser_endpoint="geyser.offline.invalid:443",
                geyser_api_token="token",  # noqa: S106
                platforms=[Platform.LAUNCHLAB],
                platform_configs=narrowing,
            )
            for kind in ("blocks", "geyser")
        )
        got_block = {Pubkey.from_string(a) for a in block.platform_program_ids}
        if got_block != expected:
            problems.append(f"blocks with {narrowing}: {got_block}")
        if set(geyser.platform_program_ids) != expected:
            problems.append(f"geyser with {narrowing}: {geyser.platform_program_ids}")
        routed = block._parse_from_logs(_other_launches()[0])  # noqa: SLF001
        if routed is None or routed.platform != Platform.LAUNCHLAB:
            problems.append(f"block listener with {narrowing} did not route the launch")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _string_end(data: bytes, offset: int) -> int:
    (length,) = struct.unpack_from("<I", data, offset)
    return offset + 4 + length


def _as_fixed_curve(tx: dict) -> dict:
    """The initialize_v2 launch with its curve rewritten to CurveParams::Fixed."""
    tx = copy.deepcopy(tx)
    ix, _ = _initialize(tx)
    data = base58.b58decode(ix["data"])
    offset = 8 + 1  # discriminator, MintParams.decimals
    for _ in range(3):  # name, symbol, uri
        offset = _string_end(data, offset)
    variant, supply, _, fund, migrate = struct.unpack_from("<BQQQB", data, offset)
    if variant != 0:
        raise AssertionError("fixture launch is not on a constant curve")  # noqa: TRY003
    fixed = struct.pack("<BQQB", 1, supply, fund, migrate)
    ix["data"] = base58.b58encode(
        data[:offset] + fixed + data[offset + struct.calcsize("<BQQQB") :]
    ).decode()
    return tx


def check_d_constant_curve_only() -> bool:
    """D: a fixed-price launch decodes as an instruction but is not a coin."""
    tx = next(
        t
        for t in _other_launches()
        if INITIALIZE[base58.b58decode(_initialize(t)[0]["data"])[:8]]
        == "initialize_v2"
    )
    fixed = _as_fixed_curve(tx)
    ix, _ = _initialize(fixed)
    decoded = PARSER.decode_instruction(base58.b58decode(ix["data"]), [], [])
    variant = decoded and decoded["args"]["curve_param"]["variant"]
    taken = _parse_block(LAUNCHLAB, fixed)
    ok = (
        variant == "Fixed" and taken is None and _parse_block(LAUNCHLAB, tx) is not None
    )
    if not ok:
        print(f"    rewritten curve decodes as {variant}; parser gave {taken}")
    return ok


BASE_CONFIG = {
    "name": "verify",
    "rpc_endpoint": "https://rpc.offline.invalid",
    "wss_endpoint": "wss://rpc.offline.invalid",
    "private_key": "unused",
    "trade": {"buy_amount": 0.01, "buy_slippage": 0.3, "sell_slippage": 0.3},
    "filters": {"listener_type": "blocks", "max_token_age": 1},
}


def _config(
    platform: str, listener: str = "blocks", configs: list | str | None = None
) -> dict:
    config = copy.deepcopy(BASE_CONFIG)
    config["platform"] = platform
    config["filters"]["listener_type"] = listener
    if configs is not None:
        config["launchlab"] = {"platform_configs": configs}
    return config


def _validates(config: dict) -> bool:
    try:
        validate_config(config)
    except ValueError:
        return False
    return True


def check_e_config_validation() -> bool:
    """E: listener pairing and platform_configs are checked before startup."""
    letsbonk = str(next(iter(LETSBONK_CONFIGS)))
    reward = str(REWARD_PLATFORM_CONFIG)
    cases = [
        ("launchlab on blocks", _config("launchlab", "blocks"), True),
        ("launchlab on geyser", _config("launchlab", "geyser"), True),
        ("launchlab on logs", _config("launchlab", "logs"), False),
        ("launchlab on shreds", _config("launchlab", "shreds"), False),
        ("launchlab narrowed", _config("launchlab", configs=[letsbonk, reward]), True),
        ("stonk_fun narrowed to reward", _config("stonk_fun", configs=[reward]), True),
        ("stonk_fun naming letsbonk", _config("stonk_fun", configs=[letsbonk]), False),
        ("pump_fun with configs", _config("pump_fun", configs=[reward]), False),
        ("empty list", _config("launchlab", configs=[]), False),
        ("not a list", _config("launchlab", configs=reward), False),
        ("not an address", _config("launchlab", configs=["not-a-key"]), False),
    ]
    problems = [
        f"{label}: validated={not expected}"
        for label, config, expected in cases
        if _validates(config) != expected
    ]
    for problem in problems:
        print(f"    {problem}")
    return not problems


class _Client:
    rpc_endpoint = "http://offline.invalid"


def check_f_registry_per_narrowing() -> bool:
    """F: two narrowings on one client get their own parsers."""
    a = platform_factory.create_for_platform(
        Platform.LAUNCHLAB,
        _Client(),
        platform_configs=frozenset({REWARD_PLATFORM_CONFIG}),
    )
    b = platform_factory.create_for_platform(
        Platform.LAUNCHLAB,
        _Client(),
        platform_configs=frozenset({STANDARD_PLATFORM_CONFIG}),
    )
    every = platform_factory.create_for_platform(Platform.LAUNCHLAB, _Client())
    got = (
        a.event_parser.get_stream_filter_accounts(),
        b.event_parser.get_stream_filter_accounts(),
        every.event_parser.get_stream_filter_accounts(),
    )
    expected = (
        [REWARD_PLATFORM_CONFIG],
        [STANDARD_PLATFORM_CONFIG],
        [LAUNCHLAB_PROGRAM],
    )
    if got != expected:
        print(f"    subscriptions {got}")
    return got == expected


def check_g_trade_log_records_config() -> bool:
    """G: trades/trades.log and trades/<mint>.txt name the platform config."""
    token = _parse_block(LAUNCHLAB, _other_launches()[0])
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            trade_log.save_token_info(token)
            trade_log.log_trade("buy", token, 1.0, 2.0, "sig")
            saved = json.loads((Path("trades") / f"{token.mint}.txt").read_text())
            logged = json.loads(Path("trades/trades.log").read_text().splitlines()[0])
        finally:
            os.chdir(cwd)
    expected = str(token.platform_config)
    ok = (
        saved.get("platform_config") == expected
        and logged.get("platform_config") == expected
    )
    if not ok:
        print(f"    token file {saved.get('platform_config')} trade log {logged}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: any launchpad decodes, presets ignore it", check_a_any_launchpad),
        ("B: narrowing keeps to its configs", check_b_narrowing),
        ("C: subscriptions follow the narrowing", check_c_subscriptions),
        ("D: only constant-product launches", check_d_constant_curve_only),
        ("E: config validated before startup", check_e_config_validation),
        ("F: registry keeps one set per narrowing", check_f_registry_per_narrowing),
        ("G: trades record the platform config", check_g_trade_log_records_config),
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

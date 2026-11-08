"""Verify the shreds listener detects LaunchLab launches as the geyser one does.

The shreds listener reads `SubscribeDeshred`, which carries a transaction before
it executes: no meta, so no logs and no inner instructions. A LaunchLab launch
needs neither. Every field the buy uses is an account or an argument of the
`initialize*` instruction, so decoding that instruction alone has to give the
TokenInfo the geyser listener builds from the executed transaction.

Fixture: `raw_launchlab_creates_from_deshred.json`, mainnet launches under
letsbonk, both StonkFun configs and two other launchpads (legacy, v0 with lookup
tables, v1; SOL, ORE, RAY and xStock quotes; SPL and Token-2022 coins), each as
the deshred frame and the geyser frame of the same transaction, plus one launch
created through another program.

Offline machine checks, no network and no funds moved:

  A. For `launchlab`, `lets_bonk` and `stonk_fun`, the shreds listener and the
     geyser listener agree on every top-level launch: the same TokenInfo field
     for field, or both ignore it. Shreds tokens are `state_from_event` with a
     quote mint, so `extreme_fast_mode` buys without reading the pool.
  B. A listener built for every platform routes a LaunchLab launch to the same
     platform as the geyser listener does. LaunchLab serves several platforms
     from one program, so a program can have more than one parser.
  C. `launchlab.platform_configs` reaches the shreds listener: it subscribes on
     the configs and ignores a launch under another one; un-narrowed
     `launchlab` subscribes on the program, the presets on their configs.
  D. A launch created through another program is invisible on shreds (its
     initialize is an inner instruction, which only execution produces) and
     still found by geyser.
  E. `launchlab`, `lets_bonk` and `stonk_fun` validate with `shreds`; `logs`
     is still refused.
  F. The fixture is sound: each deshred frame carries the geyser frame's
     transaction and lookup-table addresses, and no meta.

Usage:
    uv run tests/regression/verify_launchlab_shreds.py
"""

# The listeners' public entry points open gRPC streams, so these checks drive
# their decode helpers directly.
# ruff: noqa: SLF001

import asyncio
import base64
import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_config  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from monitoring.universal_geyser_listener import UniversalGeyserListener  # noqa: E402
from monitoring.universal_shreds_listener import UniversalShredsListener  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from platforms.letsbonk import PLATFORM_CONFIGS as LETSBONK_CONFIGS  # noqa: E402
from platforms.stonkfun.addresses import (  # noqa: E402
    PLATFORM_CONFIGS as STONKFUN_CONFIGS,
)

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "raw_launchlab_creates_from_deshred.json"
LAUNCHLAB_PLATFORMS = (Platform.LAUNCHLAB, Platform.LETS_BONK, Platform.STONK_FUN)
ENDPOINT = "geyser.offline.invalid:443"
TOKEN = "unused"  # noqa: S105 — never sent; nothing connects


def _launches() -> list[dict]:
    return json.loads(FIXTURE.read_text())["launches"]


def _deshred(launch: dict) -> geyser_pb2.SubscribeUpdateDeshred:
    update = geyser_pb2.SubscribeUpdateDeshred()
    update.ParseFromString(base64.b64decode(launch["deshred_update_base64"]))
    return update


def _geyser(launch: dict) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.ParseFromString(base64.b64decode(launch["geyser_update_base64"]))
    return update


def _top_level(launch: dict) -> bool:
    return not launch["label"].startswith("router-created")


def _listeners(
    platforms: list[Platform] | None,
    platform_configs: frozenset[Pubkey] | None = None,
) -> tuple[UniversalShredsListener, UniversalGeyserListener]:
    """Shreds and geyser listeners as the bot builds them, never connected."""
    shreds, geyser = (
        ListenerFactory.create_listener(
            listener_type=kind,
            geyser_endpoint=ENDPOINT,
            geyser_api_token=TOKEN,
            platforms=platforms,
            platform_configs=platform_configs,
        )
        for kind in ("shreds", "geyser")
    )
    return shreds, geyser


def _detect(
    shreds: UniversalShredsListener, geyser: UniversalGeyserListener, launch: dict
) -> tuple[TokenInfo | None, TokenInfo | None]:
    return (
        shreds._process_update(_deshred(launch)),
        asyncio.run(geyser._process_update(_geyser(launch))),
    )


def _fields(token: TokenInfo | None) -> dict | None:
    """Every TokenInfo field but the monotonic detection clock."""
    if token is None:
        return None
    fields = asdict(token)
    fields.pop("creation_timestamp")
    return fields


def _diff(a: dict | None, b: dict | None) -> str:
    if a is None or b is None:
        return f"shreds {'none' if a is None else 'token'}, geyser {'none' if b is None else 'token'}"
    return ", ".join(f"{k}: {a[k]} != {b[k]}" for k in a if a[k] != b[k])


def check_a_same_token_as_geyser() -> bool:
    """A: per platform, shreds and geyser give the same TokenInfo or both none."""
    problems = []
    detected = dict.fromkeys(LAUNCHLAB_PLATFORMS, 0)
    for platform in LAUNCHLAB_PLATFORMS:
        shreds, geyser = _listeners([platform])
        for launch in filter(_top_level, _launches()):
            from_shreds, from_geyser = _detect(shreds, geyser, launch)
            a, b = _fields(from_shreds), _fields(from_geyser)
            if a != b:
                problems.append(f"{platform.value}, {launch['label']}: {_diff(a, b)}")
                continue
            if from_shreds is None:
                continue
            detected[platform] += 1
            if not from_shreds.state_from_event or from_shreds.quote_mint is None:
                problems.append(
                    f"{platform.value}, {launch['label']}: not trusted for "
                    f"extreme_fast_mode"
                )
    # Each platform has to detect something, or "both none" proves nothing.
    problems += [f"{p.value} detected no launch" for p, n in detected.items() if not n]
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_b_routing_with_every_platform() -> bool:
    """B: an every-platform listener picks the platform the geyser one picks."""
    shreds, geyser = _listeners(None)
    problems = []
    for launch in filter(_top_level, _launches()):
        from_shreds, from_geyser = _detect(shreds, geyser, launch)
        if from_geyser is None:
            problems.append(f"{launch['label']}: geyser found nothing")
            continue
        if _fields(from_shreds) != _fields(from_geyser):
            problems.append(
                f"{launch['label']}: {_diff(_fields(from_shreds), _fields(from_geyser))}"
            )
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _subscribed(listener: UniversalShredsListener) -> set[str]:
    request = listener._create_subscription_request()
    return {
        account
        for deshred_filter in request.deshred_transactions.values()
        for account in deshred_filter.account_include
    }


def check_c_narrowing() -> bool:
    """C: the shreds subscription and decode keep to the configured configs."""
    problems = []
    for platform, narrowing, expected in (
        (Platform.LAUNCHLAB, None, {str(LAUNCHLAB_PROGRAM)}),
        (Platform.LETS_BONK, None, {str(c) for c in LETSBONK_CONFIGS}),
        (Platform.STONK_FUN, None, {str(c) for c in STONKFUN_CONFIGS}),
    ):
        got = _subscribed(_listeners([platform], narrowing)[0])
        if got != expected:
            problems.append(f"{platform.value} subscribes on {sorted(got)}")

    launches = list(filter(_top_level, _launches()))
    kept = launches[-1]
    kept_config = (
        _listeners([Platform.LAUNCHLAB])[0]
        ._process_update(_deshred(kept))
        .platform_config
    )
    shreds, _ = _listeners([Platform.LAUNCHLAB], frozenset({kept_config}))
    got = _subscribed(shreds)
    if got != {str(kept_config)}:
        problems.append(f"narrowed launchlab subscribes on {sorted(got)}")
    for launch in launches:
        token = shreds._process_update(_deshred(launch))
        under_config = launch is kept
        if (token is not None) != under_config:
            problems.append(
                f"narrowed launchlab, {launch['label']}: "
                f"{'ignored' if token is None else 'took'} it"
            )
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_d_router_created_launch() -> bool:
    """D: an inner-instruction launch is invisible on shreds, found on geyser."""
    shreds, geyser = _listeners([Platform.LAUNCHLAB])
    routed = [launch for launch in _launches() if not _top_level(launch)]
    problems = [] if routed else ["fixture has no router-created launch"]
    for launch in routed:
        from_shreds, from_geyser = _detect(shreds, geyser, launch)
        if from_shreds is not None:
            problems.append(f"{launch['label']}: shreds decoded a launch it cannot see")
        if from_geyser is None:
            problems.append(f"{launch['label']}: geyser missed it")
    for problem in problems:
        print(f"    {problem}")
    return not problems


BASE_CONFIG = {
    "name": "verify",
    "rpc_endpoint": "https://rpc.offline.invalid",
    "wss_endpoint": "wss://rpc.offline.invalid",
    "private_key": "unused",
    "trade": {"buy_amount": 0.01, "buy_slippage": 0.3, "sell_slippage": 0.3},
    "filters": {"listener_type": "shreds", "max_token_age": 1},
}


def _validates(platform: str, listener: str) -> bool:
    config = copy.deepcopy(BASE_CONFIG)
    config["platform"] = platform
    config["filters"]["listener_type"] = listener
    try:
        validate_config(config)
    except ValueError:
        return False
    return True


def check_e_config_pairing() -> bool:
    """E: the LaunchLab platforms validate on shreds and still refuse logs."""
    problems = []
    for platform in LAUNCHLAB_PLATFORMS:
        if not _validates(platform.value, "shreds"):
            problems.append(f"{platform.value} on shreds refused")
        if _validates(platform.value, "logs"):
            problems.append(f"{platform.value} on logs accepted")
    if not _validates("pump_fun", "shreds"):
        problems.append("pump_fun on shreds refused")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_f_fixture_is_one_transaction() -> bool:
    """F: each deshred frame holds the geyser frame's transaction, no meta."""
    problems = []
    for launch in _launches():
        deshred = _deshred(launch).deshred_transaction.transaction
        executed = _geyser(launch).transaction.transaction
        same = (
            deshred.transaction.SerializeToString(deterministic=True)
            == executed.transaction.SerializeToString(deterministic=True)
            and deshred.signature == executed.signature
            and list(deshred.loaded_writable_addresses)
            == list(executed.meta.loaded_writable_addresses)
            and list(deshred.loaded_readonly_addresses)
            == list(executed.meta.loaded_readonly_addresses)
        )
        if not same:
            problems.append(f"{launch['label']}: frames differ")
        if "meta" in deshred.DESCRIPTOR.fields_by_name:
            problems.append("the deshred frame type has a meta field")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: shreds and geyser give the same TokenInfo", check_a_same_token_as_geyser),
        ("B: routing with every platform", check_b_routing_with_every_platform),
        ("C: platform_configs narrows shreds", check_c_narrowing),
        ("D: router-created launch is shreds-invisible", check_d_router_created_launch),
        ("E: LaunchLab platforms pair with shreds", check_e_config_pairing),
        ("F: fixture frames are one transaction", check_f_fixture_is_one_transaction),
    ]
    passed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001
            print(f"    raised {error!r}")
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

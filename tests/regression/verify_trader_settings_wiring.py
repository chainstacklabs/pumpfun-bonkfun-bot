"""Verify every bot setting reaches the part of the trader that acts on it.

`bot_runner` turns a bot YAML into a `TraderSettings`, and `UniversalTrader`
hands each field to its intake, buyer, seller, position manager, priority-fee
manager, RPC client or listener. A value that lands in the wrong field, or
falls back to a different default, changes what the bot trades without failing
anything else offline:

  A. Each config key lands in its own `TraderSettings` field (every number and
     string key holds a distinct value), and every field is covered here.
  B. A config that sets only the required keys gets the runner's defaults.
  C. `UniversalTrader` passes each setting to the part that uses it; the
     position manager keeps its positions in `positions/<bot name>.json`.
  D. `max_exit_sell_attempts` and `max_open_positions` below 1 are clamped to 1
     in the position manager.
  E. The settings repr leaves out the endpoints, the key and the geyser token.
  F. Each boolean key, switched on alone, turns on its own settings field and
     only the attributes of the parts that act on it (A and C can't tell two
     booleans apart, since several share a value).
  G. `trade.entry` lands in `entry` and reaches the coordinator and the intake,
     which gets a pool resolver on the trader's client for "migration" only;
     "migration" with `extreme_fast_mode` on, or an unknown entry, refuses to
     build a trader. (A and C run the full config with `extreme_fast_mode` on,
     so they see the default entry.)

Offline: the RPC client and the listener are replaced by recorders.

Usage:
    uv run tests/regression/verify_trader_settings_wiring.py
"""

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import bot_runner  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from platforms.stonkfun.addresses import REWARD_PLATFORM_CONFIG  # noqa: E402
from trading import universal_trader  # noqa: E402
from trading.migration import MigrationResolver  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEYPAIR = Keypair()
PRIVATE_KEY = base58.b58encode(bytes(KEYPAIR)).decode()
RPC = "https://rpc.example/SECRET-RPC-KEY"
WSS = "wss://rpc.example/SECRET-WSS-KEY"
GEYSER_TOKEN = "SECRET-GEYSER-TOKEN"  # noqa: S105

# Every key bot_runner reads, each with a value no default shares.
FULL_CONFIG = {
    "name": "wired-bot",
    "rpc_endpoint": RPC,
    "wss_endpoint": WSS,
    "private_key": PRIVATE_KEY,
    "launchlab": {"platform_configs": [str(REWARD_PLATFORM_CONFIG)]},
    "trade": {
        "buy_amount": 0.0123,
        "buy_slippage": 0.11,
        "sell_slippage": 0.22,
        "extreme_fast_mode": True,
        "extreme_fast_token_amount": 31,
        "curve_refresh_budget": 2.5,
        "trust_create_event": False,
        "quote_amounts": {"usdc": 5.5},
        "exit_strategy": "TP_SL",
        "take_profit_percentage": 0.4,
        "stop_loss_percentage": 0.15,
        "max_hold_time": 61,
        "price_check_interval": 7,
        "max_exit_sell_attempts": 4,
        "max_open_positions": 6,
        "min_wallet_balance": 0.75,
    },
    "filters": {
        "listener_type": "blocks",
        "allowed_quote_mints": ["sol", "usdc"],
        "max_transfer_fee_bps": 77,
        "max_token_age": 6.5,
        "match_string": "match",
        "bro_address": "bro",
        "marry_mode": True,
        "yolo_mode": True,
    },
    "geyser": {
        "endpoint": "geyser.example:443",
        "api_token": GEYSER_TOKEN,
        "auth_type": "basic",
    },
    "priority_fees": {
        "enable_dynamic": True,
        "enable_fixed": False,
        "fixed_amount": 123_456,
        "extra_percentage": 0.05,
        "hard_cap": 654_321,
    },
    "retries": {
        "max_attempts": 9,
        "wait_after_creation": 3,
        "wait_after_buy": 8,
        "wait_before_new_token": 5,
    },
    "timing": {"token_wait_timeout": 99},
    "cleanup": {
        "mode": "after_sell",
        "force_close_with_burn": True,
        "with_priority_fee": True,
    },
    "compute_units": {"buy": 111, "sell": 222},
    "node": {"max_rps": 12.5},
}

EXPECTED_FULL = {
    "rpc_endpoint": RPC,
    "wss_endpoint": WSS,
    "private_key": PRIVATE_KEY,
    "buy_amount": 0.0123,
    "buy_slippage": 0.11,
    "sell_slippage": 0.22,
    "platform": Platform.STONK_FUN,
    "platform_configs": [str(REWARD_PLATFORM_CONFIG)],
    # FULL_CONFIG leaves trade.entry out: "migration" refuses its
    # extreme_fast_mode. G covers the key.
    "entry": "new_launch",
    "listener_type": "blocks",
    "geyser_endpoint": "geyser.example:443",
    "geyser_api_token": GEYSER_TOKEN,
    "geyser_auth_type": "basic",
    "extreme_fast_mode": True,
    "extreme_fast_token_amount": 31,
    "curve_refresh_budget": 2.5,
    "trust_create_event": False,
    "quote_amounts": {"usdc": 5.5},
    "allowed_quote_mints": ["sol", "usdc"],
    "max_transfer_fee_bps": 77,
    "exit_strategy": "TP_SL",
    "take_profit_percentage": 0.4,
    "stop_loss_percentage": 0.15,
    "max_hold_time": 61,
    "price_check_interval": 7,
    "max_exit_sell_attempts": 4,
    "max_open_positions": 6,
    "min_wallet_balance": 0.75,
    "enable_dynamic_priority_fee": True,
    "enable_fixed_priority_fee": False,
    "fixed_priority_fee": 123_456,
    "extra_priority_fee": 0.05,
    "hard_cap_prior_fee": 654_321,
    "max_retries": 9,
    "wait_time_after_creation": 3,
    "wait_time_after_buy": 8,
    "wait_time_before_new_token": 5,
    "max_token_age": 6.5,
    "token_wait_timeout": 99,
    "cleanup_mode": "after_sell",
    "cleanup_force_close_with_burn": True,
    "cleanup_with_priority_fee": True,
    "match_string": "match",
    "bro_address": "bro",
    "marry_mode": True,
    "yolo_mode": True,
    "compute_units": {"buy": 111, "sell": 222},
    "max_rps": 12.5,
    "positions_file": "positions/wired-bot.json",
}

MINIMAL_CONFIG = {
    "rpc_endpoint": RPC,
    "wss_endpoint": WSS,
    "private_key": PRIVATE_KEY,
    "trade": {"buy_amount": 0.001, "buy_slippage": 0.3, "sell_slippage": 0.3},
    "filters": {"listener_type": "logs"},
}

# What a bot YAML that leaves a key out runs with.
EXPECTED_DEFAULTS = {
    "platform": Platform.PUMP_FUN,
    "entry": "new_launch",
    "platform_configs": None,
    "listener_type": "logs",
    "geyser_endpoint": None,
    "geyser_api_token": None,
    "geyser_auth_type": "x-token",
    "extreme_fast_mode": False,
    "extreme_fast_token_amount": 30,
    "curve_refresh_budget": 2.0,
    "trust_create_event": True,
    "quote_amounts": None,
    "allowed_quote_mints": None,
    "max_transfer_fee_bps": None,
    "exit_strategy": "time_based",
    "take_profit_percentage": None,
    "stop_loss_percentage": None,
    "max_hold_time": None,
    "price_check_interval": 10,
    "max_exit_sell_attempts": 3,
    "max_open_positions": 1,
    "min_wallet_balance": None,
    "enable_dynamic_priority_fee": False,
    "enable_fixed_priority_fee": True,
    "fixed_priority_fee": 500_000,
    "extra_priority_fee": 0.0,
    "hard_cap_prior_fee": 500_000,
    "max_retries": 10,
    "wait_time_after_creation": 15,
    "wait_time_after_buy": 15,
    "wait_time_before_new_token": 15,
    "max_token_age": 0.001,
    "token_wait_timeout": 120,
    "cleanup_mode": "disabled",
    "cleanup_force_close_with_burn": False,
    "cleanup_with_priority_fee": True,
    "match_string": None,
    "bro_address": None,
    "marry_mode": False,
    "yolo_mode": False,
    "compute_units": {},
    "max_rps": 25,
    # MINIMAL_CONFIG has no name; bot_runner's own configs always do.
    "positions_file": None,
}


class _RecordingClient:
    """Stands in for SolanaClient, which starts a blockhash poller on creation."""

    def __init__(self, rpc_endpoint: str, max_rps: float = 25.0) -> None:
        self.rpc_endpoint = rpc_endpoint
        self.max_rps = max_rps


def _mismatches(actual: dict, expected: dict) -> list[str]:
    return [
        f"{name}: {actual.get(name)!r} != {value!r}"
        for name, value in expected.items()
        if actual.get(name) != value
    ]


def _settings_dict(settings: TraderSettings) -> dict:
    return {f.name: getattr(settings, f.name) for f in dataclasses.fields(settings)}


def check_a_each_key_lands_in_its_field() -> bool:
    settings = bot_runner.build_trader_settings(FULL_CONFIG, Platform.STONK_FUN)
    actual = _settings_dict(settings)
    uncovered = set(actual) ^ set(EXPECTED_FULL)
    problems = _mismatches(actual, EXPECTED_FULL)
    if uncovered:
        problems.append(f"fields not covered by this check: {sorted(uncovered)}")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_b_defaults() -> bool:
    settings = bot_runner.build_trader_settings(MINIMAL_CONFIG, Platform.PUMP_FUN)
    problems = _mismatches(_settings_dict(settings), EXPECTED_DEFAULTS)
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _build_trader(settings: TraderSettings) -> tuple[object, dict]:
    listener_calls: dict = {}

    def create_listener(**kwargs: object) -> SimpleNamespace:
        listener_calls.update(kwargs)
        return SimpleNamespace(kind="listener")

    originals = (
        universal_trader.SolanaClient,
        universal_trader.ListenerFactory.create_listener,
    )
    universal_trader.SolanaClient = _RecordingClient
    universal_trader.ListenerFactory.create_listener = staticmethod(create_listener)
    try:
        trader = universal_trader.UniversalTrader(settings)
    finally:
        universal_trader.SolanaClient = originals[0]
        universal_trader.ListenerFactory.create_listener = staticmethod(originals[1])
    return trader, listener_calls


def check_c_parts_get_their_settings() -> bool:
    settings = bot_runner.build_trader_settings(FULL_CONFIG, Platform.STONK_FUN)
    trader, listener = _build_trader(settings)
    buyer, seller = trader.buyer, trader.seller
    intake, positions = trader.intake, trader.positions
    fees = trader.priority_fee_manager
    quote_amounts = {WSOL_MINT: 0.0123, USDC_MINT: 5.5}
    got = {
        "client endpoint": trader.solana_client.rpc_endpoint,
        "client max_rps": trader.solana_client.max_rps,
        "wallet": trader.wallet.pubkey,
        "platform": trader.platform,
        "listener": {
            k: listener.get(k)
            for k in (
                "listener_type",
                "wss_endpoint",
                "geyser_endpoint",
                "geyser_api_token",
                "geyser_auth_type",
                "platforms",
                "platform_configs",
            )
        },
        "fees": (
            fees.enable_dynamic_fee,
            fees.enable_fixed_fee,
            fees.fixed_fee,
            fees.extra_fee,
            fees.hard_cap,
        ),
        "buyer": (
            buyer.amount,
            buyer.slippage,
            buyer.max_retries,
            buyer.extreme_fast_token_amount,
            buyer.extreme_fast_mode,
            buyer.compute_units,
            buyer.quote_amounts,
            buyer.curve_refresh_budget,
            buyer.trust_create_event,
        ),
        "seller": (seller.slippage, seller.max_retries, seller.compute_units),
        "intake": (
            intake.token_listener.kind,
            intake.match_string,
            intake.bro_address,
            intake.max_token_age,
            intake.token_wait_timeout,
            intake.quote_amounts,
            intake.allowed_quote_mints,
            intake.max_transfer_fee_bps,
        ),
        "positions": (
            positions.seller is seller,
            positions.solana_client is trader.solana_client,
            positions.wallet is trader.wallet,
            positions.priority_fee_manager is fees,
            positions.platform_implementations is trader.platform_implementations,
            positions.exit_strategy,
            positions.take_profit_percentage,
            positions.stop_loss_percentage,
            positions.max_hold_time,
            positions.price_check_interval,
            positions.max_exit_sell_attempts,
            positions.max_open_positions,
            positions.wait_time_after_buy,
            positions.marry_mode,
            positions.cleanup_mode,
            positions.cleanup_force_close_with_burn,
            positions.cleanup_with_priority_fee,
            str(positions.store.path) if positions.store else None,
        ),
        "coordinator": (
            trader.extreme_fast_mode,
            trader.wait_time_after_creation,
            trader.wait_time_before_new_token,
            trader.yolo_mode,
            trader.quote_amounts,
            trader.settings is settings,
            trader.min_wallet_balance,
        ),
    }
    expected = {
        "client endpoint": RPC,
        "client max_rps": 12.5,
        "wallet": KEYPAIR.pubkey(),
        "platform": Platform.STONK_FUN,
        "listener": {
            "listener_type": "blocks",
            "wss_endpoint": WSS,
            "geyser_endpoint": "geyser.example:443",
            "geyser_api_token": GEYSER_TOKEN,
            "geyser_auth_type": "basic",
            "platforms": [Platform.STONK_FUN],
            "platform_configs": frozenset({REWARD_PLATFORM_CONFIG}),
        },
        "fees": (True, False, 123_456, 0.05, 654_321),
        "buyer": (
            0.0123,
            0.11,
            9,
            31,
            True,
            {"buy": 111, "sell": 222},
            quote_amounts,
            2.5,
            False,
        ),
        "seller": (0.22, 9, {"buy": 111, "sell": 222}),
        "intake": (
            "listener",
            "match",
            "bro",
            6.5,
            99,
            quote_amounts,
            {WSOL_MINT, USDC_MINT},
            77,
        ),
        "positions": (
            True,
            True,
            True,
            True,
            True,
            "tp_sl",
            0.4,
            0.15,
            61,
            7,
            4,
            6,
            8,
            True,
            "after_sell",
            True,
            True,
            "positions/wired-bot.json",
        ),
        "coordinator": (True, 3, 5, True, quote_amounts, True, 0.75),
    }
    problems = _mismatches(got, expected)
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_d_attempts_clamped() -> bool:
    config = {
        **MINIMAL_CONFIG,
        "trade": {
            **MINIMAL_CONFIG["trade"],
            "max_exit_sell_attempts": 0,
            "max_open_positions": 0,
        },
    }
    settings = bot_runner.build_trader_settings(config, Platform.PUMP_FUN)
    trader, _ = _build_trader(settings)
    ok = True
    for name in ("max_exit_sell_attempts", "max_open_positions"):
        value = getattr(trader.positions, name)
        if value != 1:
            print(f"    {name} 0 became {value}, expected 1")
            ok = False
    return ok


def check_e_repr_hides_secrets() -> bool:
    shown = repr(bot_runner.build_trader_settings(FULL_CONFIG, Platform.STONK_FUN))
    leaked = [
        name
        for name, secret in (
            ("rpc_endpoint", "SECRET-RPC-KEY"),
            ("wss_endpoint", "SECRET-WSS-KEY"),
            ("private_key", PRIVATE_KEY),
            ("geyser_api_token", GEYSER_TOKEN),
        )
        if secret in shown
    ]
    if leaked:
        print(f"    repr shows {leaked}")
    return not leaked


# Each boolean key: where it sits in the config, its settings field, and the
# (part, attribute) pairs it switches.
BOOLEANS = (
    (
        ("trade", "extreme_fast_mode"),
        "extreme_fast_mode",
        {("coordinator", "extreme_fast_mode"), ("buyer", "extreme_fast_mode")},
    ),
    (
        ("trade", "trust_create_event"),
        "trust_create_event",
        {("buyer", "trust_create_event")},
    ),
    (
        ("priority_fees", "enable_dynamic"),
        "enable_dynamic_priority_fee",
        {("fees", "enable_dynamic_fee")},
    ),
    (
        ("priority_fees", "enable_fixed"),
        "enable_fixed_priority_fee",
        {("fees", "enable_fixed_fee")},
    ),
    (
        ("cleanup", "force_close_with_burn"),
        "cleanup_force_close_with_burn",
        {("positions", "cleanup_force_close_with_burn")},
    ),
    (
        ("cleanup", "with_priority_fee"),
        "cleanup_with_priority_fee",
        {("positions", "cleanup_with_priority_fee")},
    ),
    (("filters", "marry_mode"), "marry_mode", {("positions", "marry_mode")}),
    (("filters", "yolo_mode"), "yolo_mode", {("coordinator", "yolo_mode")}),
)


def _config_with_booleans(on: tuple[str, str] | None) -> dict:
    """The minimal config with every boolean key set, all False but `on`."""
    config = {
        section: dict(values) if isinstance(values, dict) else values
        for section, values in MINIMAL_CONFIG.items()
    }
    for (section, key), _, _ in BOOLEANS:
        config.setdefault(section, {})[key] = (section, key) == on
    return config


def _boolean_attributes(trader: object) -> dict[tuple[str, str], bool]:
    parts = {
        "coordinator": trader,
        "buyer": trader.buyer,
        "seller": trader.seller,
        "intake": trader.intake,
        "positions": trader.positions,
        "fees": trader.priority_fee_manager,
    }
    return {
        (part, name): value
        for part, obj in parts.items()
        for name, value in vars(obj).items()
        if type(value) is bool
    }


def check_f_each_boolean_reaches_only_its_parts() -> bool:
    bool_fields = {field for _, field, _ in BOOLEANS}
    covered = {
        f.name
        for f in dataclasses.fields(TraderSettings)
        if isinstance(
            getattr(
                bot_runner.build_trader_settings(MINIMAL_CONFIG, Platform.PUMP_FUN),
                f.name,
            ),
            bool,
        )
    }
    problems = []
    if covered != bool_fields:
        problems.append(f"boolean fields not covered: {sorted(covered ^ bool_fields)}")
    baseline_trader, _ = _build_trader(
        bot_runner.build_trader_settings(_config_with_booleans(None), Platform.PUMP_FUN)
    )
    baseline = _boolean_attributes(baseline_trader)
    for path, field, parts in BOOLEANS:
        settings = bot_runner.build_trader_settings(
            _config_with_booleans(path), Platform.PUMP_FUN
        )
        on = {name for name in bool_fields if getattr(settings, name)}
        if on != {field}:
            problems.append(f"{'.'.join(path)}: settings fields on {sorted(on)}")
        trader, _ = _build_trader(settings)
        changed = {
            attr
            for attr, value in _boolean_attributes(trader).items()
            if baseline.get(attr) != value
        }
        if changed != parts:
            problems.append(f"{'.'.join(path)}: switched {sorted(changed)}")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_g_entry() -> bool:
    problems = []
    migration = {
        **MINIMAL_CONFIG,
        "trade": {**MINIMAL_CONFIG["trade"], "entry": "migration"},
    }
    settings = bot_runner.build_trader_settings(migration, Platform.PUMP_FUN)
    if settings.entry != "migration":
        problems.append(f"trade.entry migration became {settings.entry!r}")
    trader, _ = _build_trader(settings)
    resolver = trader.intake.pool_resolver
    if (trader.entry, trader.intake.entry) != ("migration", "migration"):
        problems.append(f"entries {trader.entry!r}, {trader.intake.entry!r}")
    if not isinstance(resolver, MigrationResolver) or (
        resolver.client is not trader.solana_client
    ):
        problems.append(f"migration intake got pool resolver {resolver!r}")
    trader, _ = _build_trader(
        bot_runner.build_trader_settings(MINIMAL_CONFIG, Platform.PUMP_FUN)
    )
    if (trader.entry, trader.intake.pool_resolver) != ("new_launch", None):
        problems.append(
            f"default entry {trader.entry!r} with {trader.intake.pool_resolver!r}"
        )
    for label, trade, needle in (
        (
            "migration + extreme_fast_mode",
            {"entry": "migration", "extreme_fast_mode": True},
            "extreme_fast_mode",
        ),
        ("unknown entry", {"entry": "graduation"}, "trade.entry"),
    ):
        config = {**MINIMAL_CONFIG, "trade": {**MINIMAL_CONFIG["trade"], **trade}}
        try:
            _build_trader(bot_runner.build_trader_settings(config, Platform.PUMP_FUN))
        except ValueError as e:
            if needle not in str(e):
                problems.append(f"{label}: refused without naming {needle}: {e}")
        else:
            problems.append(f"{label}: built a trader")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def main() -> int:
    """Run every check and report."""
    checks = [
        (
            "A: each config key lands in its own field",
            check_a_each_key_lands_in_its_field,
        ),
        ("B: a minimal config gets the runner defaults", check_b_defaults),
        (
            "C: each part of the trader gets its settings",
            check_c_parts_get_their_settings,
        ),
        (
            "D: exit sell attempts and open positions below 1 are clamped",
            check_d_attempts_clamped,
        ),
        ("E: settings repr hides endpoints and keys", check_e_repr_hides_secrets),
        (
            "F: each boolean reaches only its own parts",
            check_f_each_boolean_reaches_only_its_parts,
        ),
        ("G: trade.entry reaches the intake", check_g_entry),
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

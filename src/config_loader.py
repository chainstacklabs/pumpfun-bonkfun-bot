"""Bot configuration loading and platform/listener validation."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from core.keystore import resolve_keystore_path
from interfaces.core import Platform
from platforms.launchlab_presets import resolve_platform_configs
from trading.settings import ENTRIES, entry_error

# Existing validation rules (keeping all existing ones)
REQUIRED_FIELDS = [
    "name",
    "rpc_endpoint",
    "wss_endpoint",
    "private_key",
    "trade.buy_amount",
    "trade.buy_slippage",
    "trade.sell_slippage",
    "filters.listener_type",
    "filters.max_token_age",
]

CONFIG_VALIDATION_RULES = [
    (
        "trade.buy_amount",
        (int, float),
        0,
        float("inf"),
        "trade.buy_amount must be a positive number",
    ),
    ("trade.buy_slippage", float, 0, 1, "trade.buy_slippage must be between 0 and 1"),
    ("trade.sell_slippage", float, 0, 1, "trade.sell_slippage must be between 0 and 1"),
    (
        "trade.max_exit_sell_attempts",
        int,
        1,
        100,
        "trade.max_exit_sell_attempts must be between 1 and 100",
    ),
    (
        "trade.max_open_positions",
        int,
        1,
        100,
        "trade.max_open_positions must be between 1 and 100",
    ),
    (
        "trade.min_wallet_balance",
        (int, float),
        0,
        float("inf"),
        "trade.min_wallet_balance must be a non-negative number of SOL",
    ),
    (
        "filters.max_transfer_fee_bps",
        int,
        0,
        10_000,
        "filters.max_transfer_fee_bps must be between 0 and 10000",
    ),
    (
        "priority_fees.fixed_amount",
        int,
        0,
        float("inf"),
        "priority_fees.fixed_amount must be a non-negative integer",
    ),
    (
        "priority_fees.extra_percentage",
        float,
        0,
        1,
        "priority_fees.extra_percentage must be between 0 and 1",
    ),
    (
        "priority_fees.hard_cap",
        int,
        0,
        float("inf"),
        "priority_fees.hard_cap must be a non-negative integer",
    ),
    (
        "retries.max_attempts",
        int,
        0,
        100,
        "retries.max_attempts must be between 0 and 100",
    ),
    (
        "filters.max_token_age",
        (int, float),
        0,
        float("inf"),
        "filters.max_token_age must be a non-negative number",
    ),
]

# Valid values for enum-like fields
VALID_VALUES = {
    "filters.listener_type": ["logs", "blocks", "geyser", "shreds"],
    "filters.age_from": ["detection", "chain"],
    "cleanup.mode": ["disabled", "on_fail", "after_sell", "post_session"],
    "trade.exit_strategy": ["time_based", "tp_sl", "manual"],
    "trade.entry": list(ENTRIES),
    "platform": ["pump_fun", "launchlab", "lets_bonk", "stonk_fun"],
}

# Which listeners each platform supports. The one copy: config validation,
# bot_runner and tools/live_listener_matrix.py all read it.
PLATFORM_LISTENER_COMPATIBILITY = {
    Platform.PUMP_FUN: ["logs", "blocks", "geyser", "shreds"],
    Platform.LAUNCHLAB: ["blocks", "geyser", "shreds"],
    Platform.LETS_BONK: ["blocks", "geyser", "shreds"],
    Platform.STONK_FUN: ["blocks", "geyser", "shreds"],
}


def disabled_in_yaml(path: str) -> bool:
    """Whether the file's raw YAML says `enabled: false`.

    Read before `${VAR}` interpolation and validation, so a disabled config is
    skipped even when it could not load. False when the file cannot be read or
    `enabled` is anything else, `${VAR}` included: such a config is loaded and
    checked as usual.
    """
    try:
        with Path(path).open() as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(raw, dict) and raw.get("enabled") is False


def load_bot_config(path: str) -> dict:
    """Load and validate a bot configuration from a YAML file."""
    config_path = Path(path)
    with config_path.open() as f:
        config = yaml.safe_load(f)

    env_file = config.get("env_file")
    if env_file:
        config_path = Path(path)
        env_path = config_path.parent / env_file
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(env_file, override=True)

    resolve_env_vars(config)

    keystore = config.get("keystore")
    if isinstance(keystore, str) and keystore.strip():
        config["keystore"] = str(resolve_keystore_path(keystore, config_path.parent))

    # Set default platform if not specified (backward compatibility)
    if "platform" not in config:
        config["platform"] = "pump_fun"

    validate_config(config)
    return config


# The on/off settings. A `${VAR}` that resolves to `true` or `false`, in any
# case, becomes a bool only here; every other setting keeps the text, so
# `match_string: ${MATCH}` with `MATCH=true` filters on the word "true".
SWITCHES = frozenset(
    {
        "enabled",
        "separate_process",
        "trade.extreme_fast_mode",
        "trade.trust_create_event",
        "priority_fees.enable_dynamic",
        "priority_fees.enable_fixed",
        "filters.marry_mode",
        "filters.yolo_mode",
        "cleanup.force_close_with_burn",
        "cleanup.with_priority_fee",
    }
)


def resolve_env_vars(config: dict) -> None:
    """Replace each `${VAR}` value with the variable's value, in place.

    A setting in `SWITCHES` set to `true` or `false` becomes a bool; anything
    else stays a string.

    Raises:
        ValueError: A referenced variable is not set
    """

    def resolve_env(path: str, value):
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            env_value = os.getenv(env_var)
            if env_value is None:
                raise ValueError(f"Environment variable '{env_var}' not found")
            switch = env_value.strip().lower()
            if path in SWITCHES and switch in ("true", "false"):
                return switch == "true"
            return env_value
        return value

    def resolve_all(d: dict, prefix: str) -> None:
        for k, v in d.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict):
                resolve_all(v, f"{path}.")
            else:
                d[k] = resolve_env(path, v)

    resolve_all(config, "")


def get_nested_value(config: dict, path: str) -> Any:
    """Get a nested value from the configuration using dot notation."""
    keys = path.split(".")
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing required config key: {path}")
        value = value[key]
    return value


def validate_config(config: dict) -> None:
    """Validate the configuration against defined rules with platform support."""
    # Validate required fields
    for field in REQUIRED_FIELDS:
        if field == "private_key" and "keystore" in config:
            continue  # the keystore stands in for it, checked below
        get_nested_value(config, field)
    # The name keys the bot's positions file: without one the bot would keep
    # its open positions in memory only, and shutdown cleanup could burn them.
    if not isinstance(config["name"], str) or not config["name"].strip():
        msg = "name must be a non-empty string"
        raise ValueError(msg)
    validate_wallet_config(config)

    # Validate config rules
    for path, expected_type, min_val, max_val, error_msg in CONFIG_VALIDATION_RULES:
        try:
            value = get_nested_value(config, path)

            if not isinstance(value, expected_type):
                raise ValueError(f"Type error: {error_msg}")

            if isinstance(value, (int, float)) and not (min_val <= value <= max_val):
                raise ValueError(f"Range error: {error_msg}")

        except ValueError as e:
            if str(e).startswith(("Type error:", "Range error:")):
                raise
            continue

    # Validate enum-like fields
    for path, valid_values in VALID_VALUES.items():
        try:
            value = get_nested_value(config, path)
            if value not in valid_values:
                raise ValueError(f"{path} must be one of {valid_values}")
        except ValueError as e:
            if "Missing required config key" not in str(e):
                raise

    # Cannot enable both dynamic and fixed priority fees
    try:
        dynamic = get_nested_value(config, "priority_fees.enable_dynamic")
        fixed = get_nested_value(config, "priority_fees.enable_fixed")
        if dynamic and fixed:
            raise ValueError(
                "Cannot enable both dynamic and fixed priority fees simultaneously"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    validate_cleanup_config(config)
    validate_checks_config(config)

    trade = config.get("trade") or {}
    error = entry_error(
        trade.get("entry", "new_launch"),
        extreme_fast_mode=bool(trade.get("extreme_fast_mode", False)),
    )
    if error:
        raise ValueError(error)

    # Quote-asset configuration must resolve before the bot starts trading,
    # otherwise a bad mint alias only surfaces on the first non-SOL coin.
    validate_quote_config(config)

    # Platform-specific validation
    platform_str = config.get("platform", "pump_fun")
    try:
        platform = Platform(platform_str)
        validate_platform_config(config, platform)
    except ValueError as e:
        if "is not a valid" in str(e):
            raise ValueError(
                f"Invalid platform '{platform_str}'. Must be one of: {[p.value for p in Platform]}"
            )
        raise


CLEANUP_PRIORITY_FEE_NOT_BOOL = (
    "cleanup.with_priority_fee must be true or false (unquoted, or a ${VAR} set "
    "to true or false); leave it out for true"
)


def validate_cleanup_config(config: dict) -> None:
    """`cleanup.with_priority_fee` is a bool or left out (null counts as left out).

    A quoted "false" or a 0 would read as true, sending closes with a fee the
    config meant to turn off, so both are refused.
    """
    cleanup = config.get("cleanup")
    if not isinstance(cleanup, dict):
        return
    fee = cleanup.get("with_priority_fee")
    if fee is not None and not isinstance(fee, bool):
        raise ValueError(CLEANUP_PRIORITY_FEE_NOT_BOOL)


def validate_checks_config(config: dict) -> None:
    """`filters.checks` is a list of mappings with well-typed common fields.

    Whether each entry names a check that loads is checked when the trader is
    built, which reports a bad one the same way.
    """
    checks = (config.get("filters") or {}).get("checks")
    if checks is None:
        return
    if not isinstance(checks, list):
        raise ValueError("filters.checks must be a list of checks")  # noqa: TRY003, TRY004
    for entry in checks:
        if not isinstance(entry, dict):
            raise ValueError(f"filters.checks entry {entry!r} must be a mapping")  # noqa: TRY003, TRY004
        for key in ("enabled", "required"):
            if key in entry and not isinstance(entry[key], bool):
                raise ValueError(  # noqa: TRY003
                    f"filters.checks entry {entry!r}: {key} must be true or false"
                )
        budget = entry.get("budget")
        if budget is not None and (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or budget <= 0
        ):
            raise ValueError(  # noqa: TRY003
                f"filters.checks entry {entry!r}: budget must be a number of "
                f"seconds above 0"
            )


BOTH_WALLET_KEYS = (
    "Set either private_key or keystore, not both: remove the private_key line "
    "to trade from the keystore"
)
KEYSTORE_NOT_A_PATH = "keystore must be the path of a keystore file"


def validate_wallet_config(config: dict) -> None:
    """Check the wallet comes from exactly one of `private_key` and `keystore`.

    Raises:
        ValueError: Both are set, or `keystore` is not a non-empty path string
    """
    if "keystore" not in config:
        return
    if "private_key" in config:
        raise ValueError(BOTH_WALLET_KEYS)
    keystore = config["keystore"]
    if not isinstance(keystore, str) or not keystore.strip():
        raise ValueError(KEYSTORE_NOT_A_PATH)


def validate_quote_config(config: dict) -> None:
    """Validate trade.quote_amounts and filters.allowed_quote_mints.

    Args:
        config: Loaded bot configuration

    Raises:
        ValueError: If a quote mint alias/address or amount is invalid
    """
    from core.pubkeys import resolve_quote_amounts, resolve_quote_mint

    quote_amounts = config.get("trade", {}).get("quote_amounts")
    if quote_amounts is not None:
        if not isinstance(quote_amounts, dict):
            raise ValueError(
                "trade.quote_amounts must be a mapping of quote mint to amount"
            )
        resolve_quote_amounts(quote_amounts)

    allowed = config.get("filters", {}).get("allowed_quote_mints")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(
                "filters.allowed_quote_mints must be a non-empty list of quote mints"
            )
        for mint in allowed:
            resolve_quote_mint(mint)


def validate_platform_config(config: dict, platform: Platform) -> None:
    """Validate platform-specific configuration requirements."""
    # Check if platform is supported
    try:
        from platforms import platform_factory

        if not platform_factory.registry.is_platform_supported(platform):
            raise ValueError(
                f"Platform {platform.value} is not supported. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
            )
    except ImportError:
        # If platform factory not available, just validate enum
        pass

    # Validate listener compatibility with platform
    try:
        listener_type = get_nested_value(config, "filters.listener_type")
        compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])

        if listener_type not in compatible_listeners:
            raise ValueError(
                f"Listener type '{listener_type}' is not compatible with platform '{platform.value}'. "
                f"Compatible listeners: {compatible_listeners}"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    # launchlab.platform_configs: LaunchLab platforms only, each a valid address
    launchlab = config.get("launchlab") or {}
    if not isinstance(launchlab, dict):
        msg = "launchlab must be a mapping, such as platform_configs: [...]"
        raise ValueError(msg)  # noqa: TRY004 - config errors are ValueError here
    resolve_platform_configs(platform, launchlab.get("platform_configs"))


def get_platform_from_config(config: dict) -> Platform:
    """Extract platform enum from configuration."""
    platform_str = config.get("platform", "pump_fun")
    try:
        return Platform(platform_str)
    except ValueError:
        raise ValueError(
            f"Invalid platform '{platform_str}'. Must be one of: {[p.value for p in Platform]}"
        )


def validate_platform_listener_combination(
    platform: Platform, listener_type: str
) -> bool:
    """Check if a platform and listener type are compatible.

    Args:
        platform: Platform enum

    Returns:
        True if combination is valid
    """
    compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])
    return listener_type in compatible_listeners


def get_supported_listeners_for_platform(platform: Platform) -> list[str]:
    """Get list of supported listener types for a platform.

    Args:
        platform: Platform enum
    """
    return PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])


def print_config_summary(config: dict) -> None:
    """Print a summary of the loaded configuration with platform info."""
    platform_str = config.get("platform", "pump_fun")

    print(f"Bot name: {config.get('name', 'unnamed')}")
    print(f"Platform: {platform_str}")
    print(
        f"Listener type: {config.get('filters', {}).get('listener_type', 'not configured')}"
    )

    # Validate platform-listener combination
    try:
        platform = Platform(platform_str)
        listener_type = config.get("filters", {}).get("listener_type")
        if listener_type and not validate_platform_listener_combination(
            platform, listener_type
        ):
            print(
                f"WARNING: Listener '{listener_type}' may not be compatible with platform '{platform_str}'"
            )
    except ValueError:
        print(f"WARNING: Invalid platform '{platform_str}'")

    trade = config.get("trade", {})
    print("Trade settings:")
    print(f"  - Buy amount: {trade.get('buy_amount', 'not configured')} SOL")
    print(f"  - Buy slippage: {trade.get('buy_slippage', 'not configured') * 100}%")
    print(
        f"  - Extreme fast mode: {'enabled' if trade.get('extreme_fast_mode') else 'disabled'}"
    )

    fees = config.get("priority_fees", {})
    print("Priority fees:")
    if fees.get("enable_dynamic"):
        print("  - Dynamic fees enabled")
    elif fees.get("enable_fixed"):
        print(
            f"  - Fixed fee: {fees.get('fixed_amount', 'not configured')} microlamports"
        )

    print("Configuration loaded successfully!")


def validate_all_platform_configs(config_dir: str = "bots") -> dict[str, Any]:
    """Validate all bot configurations in a directory.

    Args:
        config_dir: Directory containing bot config files

    Returns:
        Dictionary with validation results
    """
    results = {
        "valid_configs": [],
        "invalid_configs": [],
        "platform_distribution": {},
        "listener_distribution": {},
    }

    config_files = list(Path(config_dir).glob("*.yaml"))

    for config_file in config_files:
        try:
            config = load_bot_config(config_file)
            platform = get_platform_from_config(config)
            listener_type = config.get("filters", {}).get("listener_type", "unknown")

            results["valid_configs"].append(
                {
                    "file": config_file,
                    "name": config.get("name"),
                    "platform": platform.value,
                    "listener": listener_type,
                    "enabled": config.get("enabled", True),
                }
            )

            # Track distributions
            platform_key = platform.value
            results["platform_distribution"][platform_key] = (
                results["platform_distribution"].get(platform_key, 0) + 1
            )
            results["listener_distribution"][listener_type] = (
                results["listener_distribution"].get(listener_type, 0) + 1
            )

        except Exception as e:
            results["invalid_configs"].append({"file": config_file, "error": str(e)})

    return results


if __name__ == "__main__":
    # Example usage with platform configuration validation
    import sys

    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        try:
            config = load_bot_config(config_path)
            print_config_summary(config)

            platform = get_platform_from_config(config)
            print(f"Detected platform: {platform}")
            print(
                f"Supported listeners for this platform: {get_supported_listeners_for_platform(platform)}"
            )
        except Exception as e:
            print(f"Configuration error: {e}")
    else:
        # Validate all configs in bots directory
        results = validate_all_platform_configs()
        print("Configuration validation results:")
        print(f"Valid configs: {len(results['valid_configs'])}")
        print(f"Invalid configs: {len(results['invalid_configs'])}")
        print(f"Platform distribution: {results['platform_distribution']}")
        print(f"Listener distribution: {results['listener_distribution']}")

        if results["invalid_configs"]:
            print("\nInvalid configurations:")
            for invalid in results["invalid_configs"]:
                print(f"  {invalid['file']}: {invalid['error']}")

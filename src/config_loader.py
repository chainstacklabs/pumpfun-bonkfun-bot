"""
Updated configuration validation with comprehensive platform support.
"""

import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml
from dotenv import dotenv_values

from core.execution_policy import ExecutionMode, ExecutionPolicy, validate_finite_number
from interfaces.core import Platform
from utils.logger import get_logger

logger = get_logger(__name__)

UNRESOLVED_ENV_PLACEHOLDER = re.compile(r"\$\{[^{}]*\}")


# Required fields are intentionally small: optional sections are validated when
# present, while the execution policy supplies safe defaults for omitted options.
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

# Configuration is an API. Rejecting unknown keys prevents a typo from silently
# changing the risk posture or being ignored by the runner.
ALLOWED_CONFIG_KEYS: dict[str, set[str] | None] = {
    "root": {
        "name",
        "env_file",
        "rpc_endpoint",
        "wss_endpoint",
        "private_key",
        "enabled",
        "separate_process",
        "platform",
        "geyser",
        "pumpportal",
        "trade",
        "priority_fees",
        "compute_units",
        "filters",
        "retries",
        "cleanup",
        "node",
        "timing",
        "execution",
    },
    "geyser": {"endpoint", "api_token", "auth_type"},
    "pumpportal": {"url"},
    "trade": {
        "buy_amount",
        "buy_slippage",
        "sell_slippage",
        "quote_amounts",
        "exit_strategy",
        "take_profit_percentage",
        "stop_loss_percentage",
        "max_hold_time",
        "price_check_interval",
        "max_exit_sell_attempts",
        "extreme_fast_mode",
        "extreme_fast_token_amount",
        "curve_refresh_budget",
        "trust_create_event",
    },
    "priority_fees": {
        "enable_dynamic",
        "enable_fixed",
        "fixed_amount",
        "extra_percentage",
        "hard_cap",
    },
    "compute_units": {"buy", "sell", "account_data_size"},
    "filters": {
        "match_string",
        "bro_address",
        "allowed_quote_mints",
        "listener_type",
        "max_token_age",
        "marry_mode",
        "yolo_mode",
    },
    "retries": {
        "max_attempts",
        "wait_after_creation",
        "wait_after_buy",
        "wait_before_new_token",
    },
    "cleanup": {"mode", "force_close_with_burn", "with_priority_fee"},
    "node": {"max_rps"},
    "timing": {"token_wait_timeout"},
    "execution": {
        "mode",
        "expected_wallet",
        "max_trade_quote_raw",
        "max_total_fee_lamports",
        "allow_skip_preflight",
        "allow_force_burn",
    },
}

BOOLEAN_FIELDS = {
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
    "execution.allow_skip_preflight",
    "execution.allow_force_burn",
}

STRING_FIELDS = {
    "name",
    "env_file",
    "rpc_endpoint",
    "wss_endpoint",
    "private_key",
    "geyser.endpoint",
    "geyser.api_token",
    "geyser.auth_type",
    "pumpportal.url",
    "trade.exit_strategy",
    "filters.listener_type",
    "cleanup.mode",
    "platform",
    "execution.mode",
}

NULLABLE_STRING_FIELDS = {
    "filters.match_string",
    "filters.bro_address",
    "execution.expected_wallet",
}

# (minimum, maximum, minimum inclusive, maximum inclusive)
INTEGER_RANGES: dict[str, tuple[int | None, int | None, bool, bool]] = {
    "trade.max_hold_time": (0, None, False, True),
    "retries.wait_after_buy": (0, None, True, True),
    "trade.extreme_fast_token_amount": (1, None, True, True),
    "trade.max_exit_sell_attempts": (1, 100, True, True),
    "priority_fees.fixed_amount": (0, None, True, True),
    "priority_fees.hard_cap": (0, None, True, True),
    "compute_units.buy": (1, None, True, True),
    "compute_units.sell": (1, None, True, True),
    "compute_units.account_data_size": (1, None, True, True),
    "retries.max_attempts": (1, 1, True, True),
    "execution.max_trade_quote_raw": (0, None, True, True),
    "execution.max_total_fee_lamports": (0, None, True, True),
}

NUMBER_RANGES: dict[str, tuple[float | None, float | None, bool, bool]] = {
    "trade.buy_amount": (0, None, False, True),
    "trade.buy_slippage": (0, 1, True, False),
    "trade.sell_slippage": (0, 1, True, False),
    "trade.take_profit_percentage": (0, None, False, True),
    "trade.stop_loss_percentage": (0, 1, False, False),
    "trade.price_check_interval": (0, None, False, True),
    "trade.curve_refresh_budget": (0, None, True, True),
    "priority_fees.extra_percentage": (0, 1, True, True),
    "filters.max_token_age": (0, None, True, True),
    "retries.wait_after_creation": (0, None, True, True),
    "retries.wait_before_new_token": (0, None, True, True),
    "node.max_rps": (0, None, False, True),
    "timing.token_wait_timeout": (0, None, False, True),
}

# Valid values for enum-like fields
VALID_VALUES = {
    "filters.listener_type": ["logs", "blocks", "geyser", "pumpportal"],
    "cleanup.mode": ["disabled", "on_fail", "after_sell", "post_session"],
    "trade.exit_strategy": ["time_based", "tp_sl", "manual"],
    "platform": ["pump_fun", "lets_bonk"],
    "geyser.auth_type": ["x-token", "basic"],
    "execution.mode": [mode.value for mode in ExecutionMode],
}

# Platform-specific listener compatibility
PLATFORM_LISTENER_COMPATIBILITY = {
    Platform.PUMP_FUN: ["logs", "blocks", "geyser", "pumpportal"],
    Platform.LETS_BONK: ["blocks", "geyser"],
}


def load_bot_config(path: str | Path) -> dict[str, Any]:
    """Load, resolve, default, and validate one bot configuration."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    if not isinstance(config, dict):
        raise ValueError("Bot configuration must be a mapping")

    env_values: dict[str, str] = {}
    env_file = config.get("env_file")
    if env_file is not None:
        if not isinstance(env_file, str):
            raise ValueError("env_file must be a string")
        env_file = env_file.strip()
        if not env_file:
            config.pop("env_file")
        else:
            config["env_file"] = env_file
            env_path = _resolve_env_file(config_path, env_file)
            if env_path is None:
                if config.get("enabled", False):
                    raise ValueError(
                        f"env_file does not exist for enabled bot: {env_file}"
                    )
                logger.warning(
                    "Optional env_file %r is missing; disabled bot may rely on "
                    "explicit process environment",
                    env_file,
                )
            else:
                env_values.update(
                    {
                        key: value
                        for key, value in dotenv_values(env_path).items()
                        if value is not None
                    }
                )

    # A fresh mapping per load prevents one bot's dotenv file from changing
    # another bot. Explicit process values have the conventional precedence.
    resolved_environment = {**env_values, **dict(os.environ)}
    resolve_env_vars(config, environ=resolved_environment)

    config.setdefault("enabled", False)
    config.setdefault("platform", Platform.PUMP_FUN.value)
    execution = config.setdefault("execution", {})
    if isinstance(execution, dict):
        execution.setdefault("mode", ExecutionMode.DRY_RUN.value)

    validate_config(config)
    return config


def _resolve_env_file(config_path: Path, env_file: str) -> Path | None:
    """Resolve a safe optional dotenv path, rejecting non-file targets."""
    if UNRESOLVED_ENV_PLACEHOLDER.search(env_file):
        raise ValueError("env_file must not contain environment placeholders")
    requested = Path(env_file)
    windows_path = PureWindowsPath(env_file)
    if (
        requested.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in requested.parts
        or ".." in windows_path.parts
    ):
        raise ValueError("env_file must be a safe relative path")

    roots = (config_path.parent, Path.cwd())
    checked: set[Path] = set()
    for root in roots:
        normalized_root = root.resolve(strict=False)
        candidate = root / requested
        normalized = candidate.resolve(strict=False)
        try:
            normalized.relative_to(normalized_root)
        except ValueError as exc:
            raise ValueError("env_file must not escape its configuration root") from exc
        if normalized in checked:
            continue
        checked.add(normalized)
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise ValueError(f"env_file must reference a regular file: {env_file}")
        return candidate
    return None


def resolve_env_vars(
    config: MutableMapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Resolve exact ``${NAME}`` references without mutating the environment."""
    source = os.environ if environ is None else environ

    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            if not name:
                raise ValueError("Environment variable reference must not be empty")
            try:
                return source[name]
            except KeyError as exc:
                raise ValueError(f"Environment variable '{name}' not found") from exc
        if isinstance(value, MutableMapping):
            for key, nested in value.items():
                value[key] = resolve(nested)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                value[index] = resolve(nested)
        return value

    resolve(config)


def get_nested_value(config: dict, path: str) -> Any:
    """Get a nested value from the configuration using dot notation."""
    keys = path.split(".")
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing required config key: {path}")
        value = value[key]
    return value


def _optional_nested_value(config: dict[str, Any], path: str) -> tuple[bool, Any]:
    value: Any = config
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _validate_config_shape(config: dict[str, Any]) -> None:
    unknown_root = set(config) - ALLOWED_CONFIG_KEYS["root"]
    if unknown_root:
        key = sorted(str(item) for item in unknown_root)[0]
        raise ValueError(f"Unknown configuration key: {key}")

    for section, allowed_keys in ALLOWED_CONFIG_KEYS.items():
        if section == "root" or section not in config:
            continue
        value = config[section]
        if not isinstance(value, dict):
            raise ValueError(f"{section} must be a mapping")
        unknown = set(value) - allowed_keys
        if unknown:
            key = sorted(str(item) for item in unknown)[0]
            raise ValueError(f"Unknown configuration key: {section}.{key}")


def _validate_range(
    path: str,
    value: int | float,
    limits: tuple[int | float | None, int | float | None, bool, bool],
) -> None:
    minimum, maximum, minimum_inclusive, maximum_inclusive = limits
    if minimum is not None and (
        value < minimum or (value == minimum and not minimum_inclusive)
    ):
        operator = ">=" if minimum_inclusive else ">"
        raise ValueError(f"{path} must be {operator} {minimum}")
    if maximum is not None and (
        value > maximum or (value == maximum and not maximum_inclusive)
    ):
        operator = "<=" if maximum_inclusive else "<"
        raise ValueError(f"{path} must be {operator} {maximum}")


def _validate_bot_name(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name must be a non-empty string")
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(character.isspace() and character != " " for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("name contains unsafe path or control characters")


def _validate_unresolved_placeholders(value: Any, path: str = "<root>") -> None:
    """Reject unresolved placeholders anywhere in the configuration tree."""
    if isinstance(value, str) and UNRESOLVED_ENV_PLACEHOLDER.search(value):
        raise ValueError(f"{path} contains an unresolved environment placeholder")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _validate_unresolved_placeholders(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _validate_unresolved_placeholders(nested, f"{path}[{index}]")


def validate_config(config: dict[str, Any]) -> None:
    """Strictly validate configuration shape, types, ranges, and policy."""
    _validate_config_shape(config)
    _validate_unresolved_placeholders(config)

    for field in REQUIRED_FIELDS:
        get_nested_value(config, field)

    for path in STRING_FIELDS:
        present, value = _optional_nested_value(config, path)
        if present and not isinstance(value, str):
            raise ValueError(f"{path} must be a string")

    for path in NULLABLE_STRING_FIELDS:
        present, value = _optional_nested_value(config, path)
        if present and value is not None and not isinstance(value, str):
            raise ValueError(f"{path} must be a string or null")

    _validate_bot_name(get_nested_value(config, "name"))
    for path in ("rpc_endpoint", "wss_endpoint", "private_key"):
        value = get_nested_value(config, path)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path} must be a non-empty string")

    for path in BOOLEAN_FIELDS:
        present, value = _optional_nested_value(config, path)
        if present and not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")

    for path, limits in INTEGER_RANGES.items():
        present, value = _optional_nested_value(config, path)
        if not present:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
        _validate_range(path, value, limits)

    for path, limits in NUMBER_RANGES.items():
        present, value = _optional_nested_value(config, path)
        if not present:
            continue
        validate_finite_number(value, path)
        _validate_range(path, value, limits)

    for path, valid_values in VALID_VALUES.items():
        present, value = _optional_nested_value(config, path)
        if present and value not in valid_values:
            raise ValueError(f"{path} must be one of {valid_values}")

    trade = config["trade"]
    if trade.get("exit_strategy") == "tp_sl" and all(
        trade.get(field) is None
        for field in (
            "take_profit_percentage",
            "stop_loss_percentage",
            "max_hold_time",
        )
    ):
        raise ValueError("tp_sl exit strategy requires at least one exit condition")

    priority_fees = config.get("priority_fees", {})
    dynamic = priority_fees.get("enable_dynamic", False)
    fixed = priority_fees.get("enable_fixed", True)
    if dynamic and fixed:
        raise ValueError(
            "Cannot enable both dynamic and fixed priority fees simultaneously"
        )

    validate_quote_config(config)

    policy = ExecutionPolicy.from_config(config)
    if policy.mode is ExecutionMode.LIVE:
        if policy.max_trade_quote_raw is None:
            raise ValueError(
                "execution.max_trade_quote_raw is required for live execution"
            )
        if policy.max_total_fee_lamports is None:
            raise ValueError(
                "execution.max_total_fee_lamports is required for live execution"
            )
    policy.validate_force_burn(
        config.get("cleanup", {}).get("force_close_with_burn", False)
    )

    platform_str = config.get("platform", Platform.PUMP_FUN.value)
    platform = Platform(platform_str)
    if policy.mode is ExecutionMode.LIVE and platform is Platform.PUMP_FUN:
        raise ValueError(
            "pump_fun live execution is unavailable until dynamic protocol "
            "and creator fees are sourced into executable quotes"
        )
    validate_platform_config(config, platform)


def validate_quote_config(config: dict) -> None:
    """Validate trade.quote_amounts and filters.allowed_quote_mints.

    Args:
        config: Loaded bot configuration

    Raises:
        ValueError: If a quote mint alias/address or amount is invalid
    """
    from core.pubkeys import (
        get_quote_asset,
        resolve_quote_amounts,
        resolve_quote_mint,
    )

    quote_amounts = config.get("trade", {}).get("quote_amounts")
    if quote_amounts is not None:
        if not isinstance(quote_amounts, dict):
            raise ValueError(
                "trade.quote_amounts must be a mapping of quote mint to amount"
            )
        for mint, amount in quote_amounts.items():
            if not isinstance(mint, str):
                raise ValueError("trade.quote_amounts keys must be strings")
            validate_finite_number(amount, f"trade.quote_amounts[{mint!r}]")
            if amount <= 0:
                raise ValueError(
                    f"trade.quote_amounts[{mint!r}] must be a positive number"
                )
        resolve_quote_amounts(quote_amounts)
        for mint in quote_amounts:
            get_quote_asset(resolve_quote_mint(mint))

    allowed = config.get("filters", {}).get("allowed_quote_mints")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(
                "filters.allowed_quote_mints must be a non-empty list of quote mints"
            )
        for mint in allowed:
            if not isinstance(mint, str):
                raise ValueError("filters.allowed_quote_mints entries must be strings")
            get_quote_asset(resolve_quote_mint(mint))


def validate_platform_config(config: dict, platform: Platform) -> None:
    """Validate platform-specific configuration requirements."""
    from platforms import platform_factory

    if not platform_factory.registry.is_platform_supported(platform):
        raise ValueError(
            f"Platform {platform.value} is not supported. Available platforms: "
            f"{[p.value for p in platform_factory.get_supported_platforms()]}"
        )

    listener_type = get_nested_value(config, "filters.listener_type")
    compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])
    if listener_type not in compatible_listeners:
        raise ValueError(
            f"Listener type '{listener_type}' is not compatible with platform "
            f"'{platform.value}'. Compatible listeners: {compatible_listeners}"
        )

    # Platform-specific configuration validation belongs here as support grows.
    if platform in (Platform.PUMP_FUN, Platform.LETS_BONK):
        return
    raise ValueError(f"Unsupported platform: {platform.value}")


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
        listener_type: Listener type string

    Returns:
        True if combination is valid
    """
    compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])
    return listener_type in compatible_listeners


def get_supported_listeners_for_platform(platform: Platform) -> list[str]:
    """Get list of supported listener types for a platform.

    Args:
        platform: Platform enum

    Returns:
        List of supported listener types
    """
    return PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])


def get_platform_specific_required_config(platform: Platform) -> list[str]:
    """Get platform-specific required configuration paths.

    Args:
        platform: Platform enum

    Returns:
        List of additional required config paths for the platform
    """
    if platform == Platform.PUMP_FUN:
        return []  # No additional requirements
    elif platform == Platform.LETS_BONK:
        return []  # No additional requirements yet
    else:
        return []


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
    print(f"  - Buy amount: {trade.get('buy_amount', 'not configured')} quote units")
    buy_slippage = trade.get("buy_slippage")
    buy_slippage_display = (
        f"{buy_slippage * 100}%"
        if isinstance(buy_slippage, int | float) and not isinstance(buy_slippage, bool)
        else "not configured"
    )
    print(f"  - Buy slippage: {buy_slippage_display}")
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
                    "enabled": config.get("enabled", False),
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

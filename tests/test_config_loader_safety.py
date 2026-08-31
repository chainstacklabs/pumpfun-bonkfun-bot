from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from config_loader import (
    get_supported_listeners_for_platform,
    load_bot_config,
    resolve_env_vars,
    validate_config,
    validate_platform_listener_combination,
)
from core.execution_policy import ExecutionMode, ExecutionPolicy
from interfaces.core import Platform


def minimal_config() -> dict:
    return {
        "name": "test-bot",
        "rpc_endpoint": "https://rpc.example.test",
        "wss_endpoint": "wss://rpc.example.test",
        "private_key": "not-used-in-this-test",
        "enabled": False,
        "platform": "pump_fun",
        "trade": {
            "buy_amount": 0.001,
            "buy_slippage": 0.1,
            "sell_slippage": 0.1,
            "exit_strategy": "manual",
        },
        "filters": {"listener_type": "pumpportal", "max_token_age": 10},
    }


def test_letsbonk_pumpportal_is_rejected_during_config_validation() -> None:
    config = minimal_config()
    config["platform"] = "lets_bonk"

    with pytest.raises(
        ValueError,
        match=r"pumpportal.*not compatible.*lets_bonk",
    ):
        validate_config(config)


def test_letsbonk_does_not_advertise_pumpportal_compatibility() -> None:
    assert not validate_platform_listener_combination(
        Platform.LETS_BONK,
        "pumpportal",
    )
    assert "pumpportal" not in get_supported_listeners_for_platform(Platform.LETS_BONK)


def test_unknown_keys_are_rejected() -> None:
    config = minimal_config()
    config["unexpected"] = True

    with pytest.raises(ValueError, match="Unknown configuration key"):
        validate_config(config)


def test_unknown_nested_keys_are_rejected() -> None:
    config = minimal_config()
    config["trade"]["buy_amout"] = 1

    with pytest.raises(ValueError, match=r"trade\.buy_amout"):
        validate_config(config)


def test_non_finite_and_unsafe_slippage_are_rejected() -> None:
    config = minimal_config()
    config["trade"]["buy_slippage"] = 1.0
    with pytest.raises(ValueError, match="buy_slippage"):
        validate_config(config)

    config = minimal_config()
    config["filters"]["max_token_age"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        validate_config(config)


def test_quoted_booleans_and_non_finite_quote_amounts_are_rejected() -> None:
    config = minimal_config()
    config["enabled"] = "false"
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        validate_config(config)

    config = minimal_config()
    config["trade"]["quote_amounts"] = {"usdc": float("nan")}
    with pytest.raises(ValueError, match="finite"):
        validate_config(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("take_profit_percentage", 0),
        ("stop_loss_percentage", 0),
        ("stop_loss_percentage", 1),
        ("max_hold_time", 0),
    ],
)
def test_exit_settings_reject_trader_invalid_ranges(field: str, value: int) -> None:
    config = minimal_config()
    config["trade"][field] = value

    with pytest.raises(ValueError, match=field):
        validate_config(config)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("trade", "max_hold_time", 1.5),
        ("trade", "max_hold_time", True),
        ("trade", "max_hold_time", "1"),
        ("retries", "wait_after_buy", 1.5),
        ("retries", "wait_after_buy", True),
        ("retries", "wait_after_buy", "1"),
    ],
)
def test_position_related_durations_require_integer_seconds(
    section: str, field: str, value: object
) -> None:
    config = minimal_config()
    config.setdefault(section, {})[field] = value

    with pytest.raises(ValueError, match=field):
        validate_config(config)


def test_tp_sl_requires_at_least_one_exit_condition() -> None:
    config = minimal_config()
    config["trade"]["exit_strategy"] = "tp_sl"

    with pytest.raises(ValueError, match="at least one exit condition"):
        validate_config(config)


@pytest.mark.parametrize("attempts", [0, 2])
def test_submission_attempt_count_must_be_exactly_one(attempts: int) -> None:
    config = minimal_config()
    config["retries"] = {"max_attempts": attempts}

    with pytest.raises(ValueError, match="max_attempts"):
        validate_config(config)


def test_dynamic_priority_fee_requires_fixed_mode_to_be_explicitly_disabled() -> None:
    config = minimal_config()
    config["priority_fees"] = {"enable_dynamic": True}

    with pytest.raises(ValueError, match="both dynamic and fixed"):
        validate_config(config)


def test_environment_resolution_uses_supplied_mapping() -> None:
    config = {"rpc": "${TEST_RPC_URL}"}

    resolve_env_vars(config, environ={"TEST_RPC_URL": "https://isolated.test"})

    assert config["rpc"] == "https://isolated.test"


def test_dotenv_resolution_is_isolated_and_process_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_variable = "CONFIG_LOADER_PROCESS_OVERRIDE_TEST"
    file_variable = "CONFIG_LOADER_FILE_ONLY_TEST"
    monkeypatch.setenv(process_variable, "from-process")
    monkeypatch.delenv(file_variable, raising=False)
    first_env = tmp_path / "first.env"
    first_env.write_text(f"{process_variable}=from-first-file\n{file_variable}=first\n")
    second_env = tmp_path / "second.env"
    second_env.write_text(f"{file_variable}=second\n")

    first = minimal_config()
    first["env_file"] = first_env.name
    first["rpc_endpoint"] = f"${{{process_variable}}}"
    first["wss_endpoint"] = f"${{{file_variable}}}"
    first_path = tmp_path / "first.yaml"
    first_path.write_text(yaml.safe_dump(first))

    second = minimal_config()
    second["env_file"] = second_env.name
    second["rpc_endpoint"] = f"${{{process_variable}}}"
    second["wss_endpoint"] = f"${{{file_variable}}}"
    second_path = tmp_path / "second.yaml"
    second_path.write_text(yaml.safe_dump(second))

    assert load_bot_config(first_path)["rpc_endpoint"] == "from-process"
    assert load_bot_config(first_path)["wss_endpoint"] == "first"
    assert load_bot_config(second_path)["wss_endpoint"] == "second"
    assert file_variable not in os.environ


def test_empty_env_file_is_treated_as_omitted(tmp_path: Path) -> None:
    config = minimal_config()
    config["env_file"] = "   "
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    loaded = load_bot_config(config_path)

    assert "env_file" not in loaded


def test_env_file_must_be_a_regular_file(tmp_path: Path) -> None:
    env_directory = tmp_path / "secrets.env"
    env_directory.mkdir()
    config = minimal_config()
    config["env_file"] = env_directory.name
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(ValueError, match="regular file"):
        load_bot_config(config_path)


@pytest.mark.parametrize(
    "env_file",
    ["../outside.env", "/tmp/outside.env", "${ENV_PATH}"],
)
def test_env_file_rejects_unsafe_paths(tmp_path: Path, env_file: str) -> None:
    config = minimal_config()
    config["env_file"] = env_file
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(ValueError, match="env_file"):
        load_bot_config(config_path)


def test_env_file_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.env"
    outside.write_text("TOKEN=secret\n")
    (tmp_path / "linked.env").symlink_to(outside)
    config = minimal_config()
    config["env_file"] = "linked.env"
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(ValueError, match="env_file"):
        load_bot_config(config_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rpc_endpoint", "https://${RPC_HOST}"),
        ("rpc_endpoint", "https://${}"),
        ("wss_endpoint", "wss://${WSS_HOST}"),
        ("private_key", "prefix-${PRIVATE_KEY}"),
        ("geyser.endpoint", "https://${GEYSER_HOST}"),
        ("geyser.api_token", "token-${GEYSER_TOKEN}"),
        ("pumpportal.url", "wss://${PUMPPORTAL_HOST}"),
    ],
)
def test_sensitive_fields_reject_unresolved_placeholders(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    config = minimal_config()
    target = config
    keys = field.split(".")
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    with pytest.raises(ValueError, match=rf"{field}.*unresolved"):
        load_bot_config(config_path)


def test_live_config_requires_finite_budgets() -> None:
    config = minimal_config()
    config["execution"] = {"mode": "live"}

    with pytest.raises(ValueError, match="max_trade_quote_raw"):
        validate_config(config)

    config["platform"] = "lets_bonk"
    config["filters"]["listener_type"] = "blocks"
    config["execution"] = {
        "mode": "live",
        "expected_wallet": "11111111111111111111111111111111",
        "max_trade_quote_raw": 1_000_000,
        "max_total_fee_lamports": 50_000,
    }
    validate_config(config)
    policy = ExecutionPolicy.from_config(config)
    assert policy.mode is ExecutionMode.LIVE
    assert not policy.can_submit


def test_pump_live_mode_is_rejected_until_dynamic_fees_are_sourced() -> None:
    config = minimal_config()
    config["execution"] = {
        "mode": "live",
        "expected_wallet": "11111111111111111111111111111111",
        "max_trade_quote_raw": 1_000_000,
        "max_total_fee_lamports": 50_000,
    }

    with pytest.raises(ValueError, match="dynamic protocol and creator fees"):
        validate_config(config)


def test_loaded_config_is_dry_run_without_explicit_execution_mode(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(
        """
name: test-bot
rpc_endpoint: https://rpc.example.test
wss_endpoint: wss://rpc.example.test
private_key: test-key
enabled: false
platform: pump_fun
trade:
  buy_amount: 0.001
  buy_slippage: 0.1
  sell_slippage: 0.1
  exit_strategy: manual
filters:
  listener_type: pumpportal
  max_token_age: 10
"""
    )

    loaded = load_bot_config(config_path)

    assert loaded["execution"]["mode"] == ExecutionMode.DRY_RUN.value
    assert loaded["enabled"] is False


def test_omitted_enabled_defaults_to_false(tmp_path: Path) -> None:
    config = minimal_config()
    config.pop("enabled")
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))

    assert load_bot_config(config_path)["enabled"] is False


def test_all_shipped_sample_bots_are_valid_and_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "SOLANA_NODE_RPC_ENDPOINT": "https://rpc.example.test",
        "SOLANA_NODE_WSS_ENDPOINT": "wss://rpc.example.test",
        "SOLANA_PRIVATE_KEY": "test-key",
        "GEYSER_ENDPOINT": "https://geyser.example.test",
        "GEYSER_API_TOKEN": "test-token",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    bot_dir = Path(__file__).parents[1] / "bots"
    for path in bot_dir.glob("*.yaml"):
        config = load_bot_config(path)
        assert config.get("enabled", False) is False, path.name
        assert config["execution"]["mode"] == ExecutionMode.DRY_RUN.value, path.name


@pytest.mark.parametrize("name", ["../escape", "nested/name", "line\nname"])
def test_bot_name_cannot_escape_log_directory(name: str) -> None:
    config = minimal_config()
    config["name"] = name

    with pytest.raises(ValueError, match="name"):
        validate_config(config)

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import pytest

import bot_runner
from core.execution_policy import ExecutionBlocked, ExecutionMode


def live_config() -> dict:
    return {
        "execution": {
            "mode": "live",
            "expected_wallet": "11111111111111111111111111111111",
            "max_trade_quote_raw": 1_000_000,
            "max_total_fee_lamports": 50_000,
        }
    }


def test_live_policy_requires_explicit_runtime_authorization() -> None:
    with pytest.raises(ExecutionBlocked, match="explicit runtime authorization"):
        bot_runner.build_execution_policy(live_config(), authorize_live=False)

    policy = bot_runner.build_execution_policy(live_config(), authorize_live=True)
    assert policy.mode is ExecutionMode.LIVE
    assert policy.can_submit


def test_dry_run_cannot_be_live_authorized() -> None:
    with pytest.raises(ExecutionBlocked, match="execution.mode='live'"):
        bot_runner.build_execution_policy({}, authorize_live=True)


def test_normal_cli_invocation_does_not_authorize_live() -> None:
    args = bot_runner.parse_args([])

    assert args.config is None
    assert args.authorize_live is False


def test_start_bot_rejects_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {"name": "disabled", "enabled": False},
    )

    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(bot_runner.start_bot("unused.yaml"))


def test_start_bot_propagates_fatal_trader_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading import universal_trader

    config = {
        "name": "fatal-trader",
        "enabled": True,
        "platform": "pump_fun",
        "rpc_endpoint": "https://rpc.example.test",
        "wss_endpoint": "wss://rpc.example.test",
        "private_key": "unused-test-key",
        "execution": {"mode": "dry_run"},
        "trade": {
            "buy_amount": 0.001,
            "buy_slippage": 0.1,
            "sell_slippage": 0.1,
        },
        "filters": {
            "listener_type": "pumpportal",
            "max_token_age": 10,
        },
    }

    class FatalTrader:
        def __init__(self, **_kwargs):
            return None

        async def start(self) -> None:
            raise RuntimeError("listener failed")

    monkeypatch.setattr(bot_runner, "load_bot_config", lambda _: config)
    monkeypatch.setattr(bot_runner, "setup_logging", lambda _: None)
    monkeypatch.setattr(bot_runner, "print_config_summary", lambda _: None)
    monkeypatch.setattr(universal_trader, "UniversalTrader", FatalTrader)

    with pytest.raises(RuntimeError, match="listener failed"):
        asyncio.run(bot_runner.start_bot("unused.yaml"))


def test_child_signal_cancels_bot_for_cleanup_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_handlers: dict[int, object] = {}
    cleaned_up = False

    monkeypatch.setattr(signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed_handlers.__setitem__(signum, handler),
    )

    async def fake_start_bot(
        _config_path: str | Path,
        *,
        authorize_live: bool = False,
    ) -> None:
        nonlocal cleaned_up
        try:
            handler = installed_handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)
            await asyncio.sleep(0)
        finally:
            cleaned_up = True

    monkeypatch.setattr(bot_runner, "start_bot", fake_start_bot)

    with pytest.raises(SystemExit) as exit_info:
        bot_runner.run_bot_process("unused.yaml")

    assert exit_info.value.code == 128 + signal.SIGTERM
    assert cleaned_up


def test_bulk_runner_reports_failed_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "child.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {
            "name": "child",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    )

    class FailedProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 4312
            self.exitcode = None

        def start(self) -> None:
            self.exitcode = 7

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            return None

        def terminate(self) -> None:
            raise AssertionError("an exited process must not be terminated")

        def kill(self) -> None:
            raise AssertionError("an exited process must not be killed")

    monkeypatch.setattr(bot_runner.multiprocessing, "Process", FailedProcess)

    with caplog.at_level(logging.INFO):
        exit_status = bot_runner.run_all_bots()

    assert exit_status == 1
    assert "exited with status 7" in caplog.text


def test_bulk_runner_stops_siblings_after_fatal_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "failed.yaml").write_text("{}")
    (bot_dir / "running.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)

    configs = {
        "failed.yaml": {
            "name": "failed",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
        "running.yaml": {
            "name": "running",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    }
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda path: configs[Path(path).name],
    )

    created = []

    class FakeProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 5000 + len(created)
            self.exitcode = None
            self.join_timeouts: list[float | None] = []
            self.terminated = False
            self.killed = False
            created.append(self)

        def start(self) -> None:
            if self.name == "bot-failed":
                self.exitcode = 9

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)

        def terminate(self) -> None:
            self.terminated = True
            self.exitcode = -signal.SIGTERM

        def kill(self) -> None:
            self.killed = True
            self.exitcode = -signal.SIGKILL

    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(bot_runner.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(os, "kill", lambda pid, sig: forwarded.append((pid, sig)))
    monkeypatch.setattr(bot_runner, "PROCESS_SHUTDOWN_GRACE_SECONDS", 0.0)

    assert bot_runner.run_all_bots() == 1

    failed, running = created
    assert failed.exitcode == 9
    assert not failed.terminated
    assert (running.pid, signal.SIGTERM) in forwarded
    assert running.terminated
    assert all(timeout is not None for timeout in running.join_timeouts)


def test_bulk_runner_propagates_signal_and_kills_stubborn_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "running.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {
            "name": "running",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    )

    installed_handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed_handlers.__setitem__(signum, handler),
    )
    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: forwarded.append((pid, sig)))

    created = []

    class StubbornProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 8123
            self.exitcode = None
            self.join_timeouts: list[float | None] = []
            self.terminate_calls = 0
            self.kill_calls = 0
            self.triggered_signal = False
            created.append(self)

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)
            if not self.triggered_signal:
                self.triggered_signal = True
                handler = installed_handlers[signal.SIGTERM]
                handler(signal.SIGTERM, None)

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.exitcode = -signal.SIGKILL

    monkeypatch.setattr(bot_runner.multiprocessing, "Process", StubbornProcess)
    monkeypatch.setattr(bot_runner, "PROCESS_SHUTDOWN_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(bot_runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.0)

    assert bot_runner.run_all_bots() == 128 + signal.SIGTERM

    process = created[0]
    assert (process.pid, signal.SIGTERM) in forwarded
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert all(timeout is not None for timeout in process.join_timeouts)

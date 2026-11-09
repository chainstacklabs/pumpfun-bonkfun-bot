"""Verify `--config`, the trading wallet's log line and `trade.min_wallet_balance`.

`pump_bot` always ran every `bots/*.yaml`, so testing one config meant
disabling the others. A `private_key` config never logged which wallet it
traded from. The bot kept buying however low the wallet's SOL got.

Offline machine checks, no funds moved; the runner checks start the real
runner on configs whose `rpc_endpoint` refuses connections:

  A. `--config FILE` (and `-c`) starts only the bot in that file, also one
     whose YAML says `enabled: false`, and none of the others in `bots/`. A
     file that does not exist exits 1 with one line naming it, no traceback.
  B. A `private_key` config logs its wallet's address before anything that can
     stop it starting.
  C. Below `trade.min_wallet_balance` the trader logs one warning and buys
     nothing, without reading the balance again until `_LOW_BALANCE_RECHECK`
     has passed; once a read finds it back above, it logs one line and buys
     again. The balance is read at start and after a buy is sent, never
     before one; without the setting it is never read. A failed read keeps
     the last answer. Config validation refuses a negative or non-numeric
     floor.

Usage:
    uv run tests/regression/verify_one_config_wallet_and_balance_floor.py
"""

import asyncio
import copy
import logging
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import base58
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import _report  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

import config_loader  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

RUNNER = PROJECT_ROOT / "src" / "bot_runner.py"
LOGS_CONFIG = PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml"
# Endpoint variables a developer's shell may hold; the runs must not reach one.
ENDPOINT_VARS = ("SOLANA_NODE_RPC_ENDPOINT", "SOLANA_NODE_WSS_ENDPOINT")
LAMPORTS = 1_000_000_000


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(name: str, key: Keypair, **overrides: object) -> dict:
    """The committed logs config on an endpoint that refuses connections."""
    config = yaml.safe_load(LOGS_CONFIG.read_text())
    config.pop("geyser", None)
    config.pop("env_file", None)
    config.update(
        name=name,
        enabled=True,
        separate_process=False,
        rpc_endpoint=f"http://127.0.0.1:{_closed_port()}",
        wss_endpoint=f"ws://127.0.0.1:{_closed_port()}",
        private_key=base58.b58encode(bytes(key)).decode(),
    )
    config.update(overrides)
    return config


def _run(folder: Path, *args: str) -> tuple[subprocess.CompletedProcess, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENDPOINT_VARS}
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(RUNNER), *args],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    logs = "".join(p.read_text() for p in sorted((folder / "logs").glob("*.log")))
    return result, logs + result.stdout + result.stderr


def check_a_and_b_runner(tmp: str) -> bool:
    print("A, B: --config runs one bot; a private_key bot logs its wallet.")
    problems = []
    keys = {"one": Keypair(), "two": Keypair()}
    folder = Path(tempfile.mkdtemp(dir=tmp))
    (folder / "bots").mkdir()
    (folder / "bots" / "one.yaml").write_text(
        yaml.safe_dump(_config("bot-one", keys["one"]))
    )
    (folder / "bots" / "two.yaml").write_text(
        yaml.safe_dump(_config("bot-two", keys["two"], enabled=False))
    )
    for flag in ("--config", "-c"):
        result, text = _run(folder, flag, "bots/two.yaml")
        if result.returncode != 1:
            problems.append(f"{flag}: exit {result.returncode}, want 1 (unreachable)")
        if "Bot 'bot-two' not started" not in text:
            problems.append(f"{flag}: the named bot, enabled: false, was not tried")
        if "bot-one" in text:
            problems.append(f"{flag}: the other bot in bots/ was tried too")
        if f"Wallet {keys['two'].pubkey()} from private_key" not in text:
            problems.append(f"{flag}: no line names the private_key wallet")
    for log in (folder / "logs").glob("*.log"):
        log.unlink()
    result, text = _run(folder, "--config", "missing.yaml")
    if result.returncode != 1 or "Config file missing.yaml not found" not in text:
        problems.append(f"missing file: exit {result.returncode}, output {text!r}")
    if "Traceback" in text:
        problems.append("missing file: a traceback was printed")
    return _report(problems)


class _Client:
    """Serves the wallet's balance from `balances`, one per read, the last kept."""

    def __init__(self, sol: list[float]) -> None:
        self.balances = [int(s * LAMPORTS) for s in sol]
        self.events: list[str] = []
        self.fail = False

    async def get_sol_balance(self, _pubkey: Pubkey) -> int:
        self.events.append("read")
        if self.fail:
            raise RuntimeError("node unavailable")  # noqa: TRY003
        if len(self.balances) > 1:
            return self.balances.pop(0)
        return self.balances[0]


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((record.levelno, record.getMessage()))

    def count(self, level: int, text: str) -> int:
        return sum(lv == level and text in m for lv, m in self.lines)


def _trader(client: _Client, floor: float | None) -> UniversalTrader:
    """The trader's buy path on stubs: no listener, no wait, a buyer that logs."""
    trader = object.__new__(UniversalTrader)
    trader.min_wallet_balance = floor
    trader.solana_client = client
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.platform = Platform.PUMP_FUN
    trader.intake = SimpleNamespace(skip_reason=lambda _t: None)
    trader.extreme_fast_mode = True
    trader.yolo_mode = False
    trader.quote_amounts = {WSOL_MINT: 0.01}

    async def execute(_token: TokenInfo) -> TradeResult:
        client.events.append("buy")
        return TradeResult(success=False, error_message="stub")

    async def nothing(*_a: object, **_k: object) -> None:
        return None

    trader.buyer = SimpleNamespace(execute=execute)
    trader._handle_failed_buy = nothing  # noqa: SLF001
    trader.positions = SimpleNamespace(wait_for_free_slot=nothing)
    return trader


def _coin() -> TokenInfo:
    return TokenInfo(
        name="C",
        symbol="C",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        quote_mint=WSOL_MINT,
    )


async def _settle(trader: UniversalTrader) -> None:
    if trader._balance_read is not None:  # noqa: SLF001
        await trader._balance_read  # noqa: SLF001


async def _floor_problems() -> list[str]:
    problems = []
    logs = _Logs()
    logging.getLogger().addHandler(logs)
    try:
        client = _Client([0.05, 0.04, 0.5])
        trader = _trader(client, 0.1)
        await trader._read_balance()  # noqa: SLF001 - what `start` runs first
        await trader._handle_token(_coin())  # noqa: SLF001
        await _settle(trader)
        if client.events != ["read"]:
            problems.append(f"below the floor, at once: {client.events}")
        trader._balance_read_at -= trader._LOW_BALANCE_RECHECK + 1  # noqa: SLF001
        await trader._handle_token(_coin())  # noqa: SLF001
        await _settle(trader)
        if client.events != ["read", "read"]:
            problems.append(f"below the floor, after the recheck: {client.events}")
        if logs.count(logging.WARNING, "below trade.min_wallet_balance") != 1:
            problems.append("the low balance was not warned exactly once")
        trader._balance_read_at -= trader._LOW_BALANCE_RECHECK + 1  # noqa: SLF001
        await trader._handle_token(_coin())  # noqa: SLF001
        await _settle(trader)
        if logs.count(logging.INFO, "back above trade.min_wallet_balance") != 1:
            problems.append("the balance back above was not logged once")
        client.events.clear()
        await trader._handle_token(_coin())  # noqa: SLF001
        await _settle(trader)
        if client.events != ["buy", "read"]:
            problems.append(f"above the floor: {client.events}, want a buy then a read")
        client.fail = True
        await trader._read_balance()  # noqa: SLF001
        if trader._below_min_balance:  # noqa: SLF001
            problems.append("a failed read changed the answer")

        plain = _Client([0.0])
        trader = _trader(plain, None)
        await trader._read_balance()  # noqa: SLF001
        await trader._handle_token(_coin())  # noqa: SLF001
        await _settle(trader)
        if plain.events != ["buy"]:
            problems.append(f"without a floor: {plain.events}, want only the buy")
    finally:
        logging.getLogger().removeHandler(logs)
    return problems


def _validation_problems() -> list[str]:
    base = yaml.safe_load(LOGS_CONFIG.read_text())
    base["private_key"] = base58.b58encode(bytes(Keypair())).decode()
    problems = []
    for value, ok in ((0.05, True), (0, True), (-1, False), ("0.1", False)):
        config = copy.deepcopy(base)
        config["trade"]["min_wallet_balance"] = value
        try:
            config_loader.validate_config(config)
            accepted = True
        except ValueError:
            accepted = False
        if accepted != ok:
            problems.append(f"min_wallet_balance {value!r}: accepted={accepted}")
    return problems


def check_c_balance_floor() -> bool:
    print("C: below trade.min_wallet_balance the bot buys nothing.")
    return _report(asyncio.run(_floor_problems()) + _validation_problems())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        results = [check_a_and_b_runner(tmp), check_c_balance_floor()]
    for ok in results:
        print("  PASS" if ok else "  FAIL")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

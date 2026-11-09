"""Verify `filters.age_from: chain` measures a coin's age from its create.

`max_token_age` counted from when the listener saw a coin, so a slow listener
or a backed-up queue passed coins older on chain than the setting suggests.

Offline machine checks, no network and no funds moved:

  A. `SlotClock.age` counts from the last slot it read, at `SLOT_SECONDS` a
     slot, plus the time since that read; it has no answer before a read or
     for an unknown slot, keeps the last reading when a read fails, and makes
     no RPC call itself.
  B. Under `chain` the intake skips a coin whose create is older than
     `max_token_age`, however fresh its detection, in continuous and in
     single-token mode, saying "since created on chain"; a coin whose slot is
     unknown counts from detection. Under `detection` (the default) the slot
     is ignored and no clock runs. The clock runs only while the intake
     listens.
  C. `age_from` must be `detection` or `chain`; the trader gives the
     intake a slot clock only under `chain`.

Usage:
    uv run tests/regression/verify_token_age_on_chain.py
"""

import asyncio
import copy
import logging
import sys
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
from core import slot_clock as slot_clock_module  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from core.slot_clock import SLOT_SECONDS, SlotClock  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.intake import TokenIntake  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

LOGS_CONFIG = PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml"
KEY = base58.b58encode(bytes(Keypair())).decode()
NOW_SLOT = 10_000


class _Client:
    """Answers `get_slot` from `slots`, the last one kept; counts every call."""

    def __init__(self, *slots: int) -> None:
        self.slots = list(slots)
        self.calls = 0
        self.fail = False

    async def get_slot(self) -> int:
        self.calls += 1
        if self.fail:
            raise RuntimeError("node unavailable")  # noqa: TRY003
        return self.slots.pop(0) if len(self.slots) > 1 else self.slots[0]


async def _read_once(clock: SlotClock) -> None:
    clock.start()
    for _ in range(100):
        if clock._reference is not None:  # noqa: SLF001
            break
        await asyncio.sleep(0.001)
    await clock.stop()


async def _clock_problems() -> list[str]:
    problems = []
    client = _Client(NOW_SLOT)
    clock = SlotClock(client)
    if clock.age(NOW_SLOT) is not None:
        problems.append("an age before any read")
    await _read_once(clock)
    calls = client.calls
    age = clock.age(NOW_SLOT - 25)
    want = 25 * SLOT_SECONDS
    if age is None or not want <= age < want + 0.5:
        problems.append(f"25 slots back read as {age}s, want about {want}s")
    if clock.age(None) is not None:
        problems.append("an age for an unknown slot")
    if client.calls != calls:
        problems.append("age() made an RPC call")
    client.fail = True
    await _read_once(clock)
    if clock.age(NOW_SLOT) is None:
        problems.append("a failed read dropped the last reading")
    return problems


def check_a_slot_clock() -> bool:
    print("A: the slot clock counts from its last reading, with no call per coin.")
    return _report(asyncio.run(_clock_problems()))


def _settings(age_from: str) -> TraderSettings:
    return TraderSettings(
        rpc_endpoint="",
        wss_endpoint="",
        private_key=KEY,
        buy_amount=0.01,
        buy_slippage=0.1,
        sell_slippage=0.1,
        max_token_age=5,
        age_from=age_from,
    )


def _coin(slot: int | None) -> TokenInfo:
    return TokenInfo(
        name="C",
        symbol="C",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        quote_mint=WSOL_MINT,
        creation_slot=slot,
    )


class _Listener:
    """Hands each coin to the callback, then ends."""

    def __init__(self, coins: list[TokenInfo]) -> None:
        self.coins = coins

    async def listen_for_tokens(self, callback, *_a: object) -> None:  # noqa: ANN001
        for coin in self.coins:
            await callback(coin)
            # A real stream yields between coins: single-token mode stops at
            # the first one it takes.
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _intake(age_from: str, coins: list[TokenInfo]) -> tuple[TokenIntake, _Client]:
    client = _Client(NOW_SLOT)
    intake = TokenIntake(
        _settings(age_from),
        _Listener(coins),
        {WSOL_MINT: 0.01},
        None,
        slot_clock=SlotClock(client),
    )
    return intake, client


async def _intake_problems() -> list[str]:
    problems = []
    logs = _Logs()
    logging.getLogger().addHandler(logs)
    # The clock reads at once; its reading is all the intake needs.
    slot_clock_module.SlotClock.REFRESH_SECONDS = 0.01
    try:
        old, fresh, unknown = _coin(NOW_SLOT - 50), _coin(NOW_SLOT - 2), _coin(None)
        intake, client = _intake("chain", [old, fresh, unknown])
        intake.slot_clock._reference = (NOW_SLOT, asyncio.get_running_loop().time())  # noqa: SLF001
        handled: list[TokenInfo] = []

        async def handle(token: TokenInfo) -> None:
            handled.append(token)

        await intake.run(handle)
        await asyncio.sleep(0)
        if [t.mint for t in handled] != [fresh.mint, unknown.mint]:
            names = {old.mint: "old", fresh.mint: "fresh", unknown.mint: "unknown"}
            problems.append(
                f"chain, continuous: handled {[names[t.mint] for t in handled]}"
            )
        if not any("since created on chain" in line for line in logs.lines):
            problems.append("the skip does not say the age is on chain")
        if not client.calls:
            problems.append("the clock never read the current slot while listening")
        if intake.slot_clock._task is None or not intake.slot_clock._task.done():  # noqa: SLF001
            problems.append("the clock still runs after the intake stopped")

        intake, _ = _intake("chain", [_coin(NOW_SLOT - 50), fresh])
        intake.slot_clock._reference = (NOW_SLOT, asyncio.get_running_loop().time())  # noqa: SLF001
        intake.token_wait_timeout = 1
        found = await intake.wait_for_token()
        if found is None or found.mint != fresh.mint:
            problems.append(f"chain, single token: took {found and found.symbol}")

        intake, client = _intake("detection", [old])
        handled.clear()
        await intake.run(handle)
        if [t.mint for t in handled] != [old.mint] or client.calls:
            problems.append(
                f"detection: handled {len(handled)} coin(s), {client.calls} slot reads"
            )
    finally:
        logging.getLogger().removeHandler(logs)
        slot_clock_module.SlotClock.REFRESH_SECONDS = 2.0
    return problems


def check_b_intake_counts_from_the_create() -> bool:
    print("B: under chain the intake skips coins created too long ago.")
    return _report(asyncio.run(_intake_problems()))


def _config_problems() -> list[str]:
    base = yaml.safe_load(LOGS_CONFIG.read_text())
    base["private_key"] = KEY
    problems = []
    for value, ok in (("detection", True), ("chain", True), ("slot", False)):
        config = copy.deepcopy(base)
        config["filters"]["age_from"] = value
        try:
            config_loader.validate_config(config)
            accepted = True
        except ValueError:
            accepted = False
        if accepted != ok:
            problems.append(f"age_from {value!r}: accepted={accepted}")
    from trading import universal_trader  # noqa: PLC0415 - patched below

    originals = (
        universal_trader.SolanaClient,
        universal_trader.ListenerFactory.create_listener,
    )
    universal_trader.SolanaClient = lambda *_a, **_k: SimpleNamespace(rpc_endpoint="")
    universal_trader.ListenerFactory.create_listener = staticmethod(
        lambda **_k: SimpleNamespace()
    )
    try:
        for age_from, want in (("chain", True), ("detection", False)):
            trader = universal_trader.UniversalTrader(_settings(age_from))
            if (trader.intake.slot_clock is not None) != want:
                problems.append(f"{age_from}: slot clock {trader.intake.slot_clock}")
    finally:
        universal_trader.SolanaClient = originals[0]
        universal_trader.ListenerFactory.create_listener = staticmethod(originals[1])
    return problems


def check_c_config() -> bool:
    print("C: the setting is validated and builds a clock only under chain.")
    return _report(_config_problems())


def main() -> int:
    results = [
        check_a_slot_clock(),
        check_b_intake_counts_from_the_create(),
        check_c_config(),
    ]
    for ok in results:
        print("  PASS" if ok else "  FAIL")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

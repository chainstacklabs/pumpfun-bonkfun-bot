"""Verify the pre-buy check pipeline: order, budgets, failures and loading.

A coin is refused before the buy by a list of checks the bot YAML names under
`filters.checks`, each a built-in or a class loaded from a module path. A check
that fails to decide must never stop the trader: a required one refuses the
coin, a best-effort one lets it through.

Offline machine checks, no network and no funds moved:

  1. Checks run in list order; the first skip wins and later checks don't run.
  2. Free and chain-reading checks run in separate passes.
  3. A skip reads "<check name>: <reason>".
  4. A disabled entry is not loaded.
  5. A check that raises refuses the coin when required, passes it otherwise.
  6. A check slower than its budget is cut off at the budget, as in 5.
  7. A check that returns neither None nor a str counts as in 5.
  8. A module path loads a user check; bad entries fail with one line.
  9. The config validation refuses a malformed `filters.checks`.

Usage:
    uv run tests/regression/verify_pre_buy_checks.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_checks_config  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.checks import (  # noqa: E402
    CheckContext,
    CheckPipeline,
    CheckUnavailable,
    ConfiguredCheck,
    PreBuyCheck,
    load_checks,
)

COIN = TokenInfo(
    name="Test",
    symbol="TST",
    uri="",
    mint=Pubkey.new_unique(),
    platform=Platform.PUMP_FUN,
)
CTX = CheckContext(client=None)
BUDGET = 0.2
# Slack over the budget for the event loop to cancel the check.
CANCEL_SLACK = 0.2


class Recorder:
    calls: ClassVar[list[str]] = []


class Passes(PreBuyCheck):
    name = "passes"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        Recorder.calls.append(self.name)


class Skips(PreBuyCheck):
    name = "skips"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        Recorder.calls.append(self.name)
        return "not today"


class ChainPasses(PreBuyCheck):
    name = "chain_passes"
    reads_chain = True

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        Recorder.calls.append(self.name)


class Unavailable(PreBuyCheck):
    name = "unavailable"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        raise CheckUnavailable


class Crashes(PreBuyCheck):
    name = "crashes"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        raise RuntimeError("boom")


class Slow(PreBuyCheck):
    name = "slow"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        await asyncio.sleep(5)


class ReturnsJunk(PreBuyCheck):
    name = "junk"

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        return 42  # type: ignore[return-value]


def _pipeline(*checks: ConfiguredCheck) -> CheckPipeline:
    return CheckPipeline(list(checks), CTX)


async def check_order_and_first_skip() -> bool:
    Recorder.calls = []
    reason = await _pipeline(
        ConfiguredCheck(Passes()), ConfiguredCheck(Skips()), ConfiguredCheck(Passes())
    ).reason(COIN, reads_chain=False)
    ok = reason == "skips: not today" and Recorder.calls == ["passes", "skips"]
    a = _check("first skip wins, in order", ok, f"{reason!r} {Recorder.calls}")
    b = _check("reason names its check", reason == "skips: not today", repr(reason))
    return a and b


async def check_passes_split() -> bool:
    Recorder.calls = []
    pipeline = _pipeline(ConfiguredCheck(Passes()), ConfiguredCheck(ChainPasses()))
    await pipeline.reason(COIN, reads_chain=False)
    free = list(Recorder.calls)
    Recorder.calls = []
    await pipeline.reason(COIN, reads_chain=True)
    chain = list(Recorder.calls)
    ok = free == ["passes"] and chain == ["chain_passes"]
    return _check("free and chain passes are separate", ok, f"{free} / {chain}")


async def check_failures() -> bool:
    results = []
    for cls in (Unavailable, Crashes, ReturnsJunk):
        required = await _pipeline(ConfiguredCheck(cls(), required=True)).reason(
            COIN, reads_chain=False
        )
        lenient = await _pipeline(ConfiguredCheck(cls(), required=False)).reason(
            COIN, reads_chain=False
        )
        ok = required is not None and "could not decide" in required and lenient is None
        results.append(
            _check(
                f"{cls.name}: required refuses, best effort passes",
                ok,
                f"{required!r} / {lenient!r}",
            )
        )
    return all(results)


async def check_budget() -> bool:
    started = time.monotonic()
    required = await _pipeline(
        ConfiguredCheck(Slow(), required=True, budget=BUDGET)
    ).reason(COIN, reads_chain=False)
    elapsed = time.monotonic() - started
    lenient = await _pipeline(
        ConfiguredCheck(Slow(), required=False, budget=BUDGET)
    ).reason(COIN, reads_chain=False)
    ok = (
        elapsed < BUDGET + CANCEL_SLACK
        and required is not None
        and "could not decide" in required
        and lenient is None
    )
    return _check(
        "budget cuts a slow check off", ok, f"{elapsed:.2f}s {required!r} / {lenient!r}"
    )


def check_disabled_not_loaded() -> bool:
    loaded = load_checks(
        [{"name": "passes", "enabled": False}, {"name": "skips"}],
        {"passes": Passes, "skips": Skips},
    )
    ok = [type(c.check) for c in loaded] == [Skips]
    return _check("a disabled entry is not loaded", ok, str(loaded))


def check_entry_fields() -> bool:
    loaded = load_checks(
        [{"name": "skips", "required": False, "budget": BUDGET}], {"skips": Skips}
    )
    c = loaded[0]
    ok = c.required is False and c.budget == BUDGET
    return _check("required and budget are read", ok, str(c))


MODULE_SOURCE = """
from trading.checks import PreBuyCheck


class Denylist(PreBuyCheck):
    name = "denylist"

    def __init__(self, symbols):
        self.symbols = set(symbols)

    async def check(self, coin, ctx):
        if coin.symbol in self.symbols:
            return f"{coin.symbol} is on the denylist"
        return None


class NotACheck:
    pass
"""


async def check_module_loading() -> bool:
    results = []
    old_cwd = Path.cwd()
    saved_path = list(sys.path)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "user_checks_mod.py").write_text(MODULE_SOURCE)
        os.chdir(tmp)
        try:
            loaded = load_checks(
                [{"module": "user_checks_mod:Denylist", "symbols": ["TST"]}], {}
            )
            reason = await CheckPipeline(loaded, CTX).reason(COIN, reads_chain=False)
            results.append(
                _check(
                    "a module path loads a user check",
                    reason == "denylist: TST is on the denylist",
                    repr(reason),
                )
            )
            bad_entries = {
                "missing module": {"module": "no_such_mod_xyz:Thing"},
                "missing class": {"module": "user_checks_mod:Nope"},
                "not a check": {"module": "user_checks_mod:NotACheck"},
                "no colon": {"module": "user_checks_mod"},
                "both name and module": {"name": "skips", "module": "a:B"},
                "neither": {"enabled": True},
                "unknown builtin": {"name": "nope"},
            }
            for label, entry in bad_entries.items():
                try:
                    load_checks([entry], {"skips": Skips})
                except ValueError as e:
                    one_line = "\n" not in str(e)
                    results.append(_check(f"{label} raises one line", one_line, str(e)))
                else:
                    results.append(
                        _check(f"{label} raises", passed=False, detail="no error")
                    )
        finally:
            os.chdir(old_cwd)
            sys.path[:] = saved_path
            sys.modules.pop("user_checks_mod", None)
    return all(results)


def check_config_validation() -> bool:
    results = []
    good = {"filters": {"checks": [{"name": "mayhem", "enabled": False}]}}
    try:
        validate_checks_config(good)
        validate_checks_config({"filters": {}})
        results.append(_check("valid checks config passes", passed=True, detail="ok"))
    except ValueError as e:
        results.append(
            _check("valid checks config passes", passed=False, detail=str(e))
        )
    bad = {
        "not a list": {"checks": {"name": "mayhem"}},
        "entry not a mapping": {"checks": ["mayhem"]},
        "enabled not bool": {"checks": [{"name": "mayhem", "enabled": "yes"}]},
        "required not bool": {"checks": [{"name": "mayhem", "required": 1}]},
        "budget zero": {"checks": [{"name": "mayhem", "budget": 0}]},
        "budget text": {"checks": [{"name": "mayhem", "budget": "1"}]},
    }
    for label, filters in bad.items():
        try:
            validate_checks_config({"filters": filters})
        except ValueError as e:
            results.append(
                _check(f"config: {label} refused", passed=True, detail=str(e))
            )
        else:
            results.append(
                _check(f"config: {label} refused", passed=False, detail="accepted")
            )
    return all(results)


async def main() -> int:
    print("Pre-buy check pipeline")
    results = [
        await check_order_and_first_skip(),
        await check_passes_split(),
        check_disabled_not_loaded(),
        check_entry_fields(),
        await check_failures(),
        await check_budget(),
        await check_module_loading(),
        check_config_validation(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

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
 10. With no `filters.checks` the existing filters refuse the same coins with
     the same text as before they became checks.
 11. The mayhem check skips a mayhem coin, passes others and reads nothing.
 12. No shipped bot config enables a check that reads the chain.
 13. With only free checks, the trader touches no RPC between taking a coin
     and sending its buy.
 14. A chain-reading check runs after the creation wait and its skip stops
     the buy.
 15. The top-holders check skips a coin whose top wallets hold too much, and
     leaves out accounts that programs own (the curve, a pool's vaults).
 16. A failed or empty read leaves the top-holders check unable to decide.
 17. The top-holders check reads the chain and refuses a share outside (0, 1].

Usage:
    uv run tests/regression/verify_pre_buy_checks.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # noqa: E402
from _fakes import _check  # noqa: E402
from solders.account import Account  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_checks_config  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading import trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.checks import (  # noqa: E402
    CheckContext,
    CheckPipeline,
    CheckUnavailable,
    ConfiguredCheck,
    PreBuyCheck,
    load_checks,
)
from trading.checks.builtin import BUILTINS, MayhemCheck  # noqa: E402
from trading.checks.top_holders import TopHoldersCheck  # noqa: E402
from trading.intake import TokenIntake  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

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


def _intake(**filters: object) -> TokenIntake:
    settings = TraderSettings(
        rpc_endpoint="https://rpc.invalid",
        wss_endpoint="wss://rpc.invalid",
        private_key="unused",
        buy_amount=0.01,
        buy_slippage=0.1,
        sell_slippage=0.1,
        **filters,
    )
    return TokenIntake(
        settings,
        token_listener=None,
        quote_amounts={WSOL_MINT: 0.01},
        allowed_quote_mints=None,
    )


def _coin(**fields: object) -> TokenInfo:
    values = {
        "name": "Doge Moon",
        "symbol": "DOGE",
        "uri": "",
        "mint": Pubkey.new_unique(),
        "platform": Platform.PUMP_FUN,
        "quote_mint": WSOL_MINT,
        "creator": Pubkey.new_unique(),
    }
    values.update(fields)
    return TokenInfo(**values)


async def check_existing_filters_unchanged() -> bool:
    """The reasons are copied from the filters as they were before."""
    creator = Pubkey.new_unique()
    allow_usdc = TokenIntake(
        TraderSettings(
            rpc_endpoint="x",
            wss_endpoint="x",
            private_key="x",
            buy_amount=0.01,
            buy_slippage=0.1,
            sell_slippage=0.1,
        ),
        token_listener=None,
        quote_amounts={WSOL_MINT: 0.01},
        allowed_quote_mints={USDC_MINT},
    )
    cases = [
        (
            "quote not allowed",
            allow_usdc.skip_reason(_coin()),
            f"quote mint {WSOL_MINT} not in allowed_quote_mints",
        ),
        (
            "no buy amount",
            _intake().skip_reason(_coin(quote_mint=USDC_MINT)),
            f"no buy amount configured for quote mint {USDC_MINT}",
        ),
        (
            "transfer fee",
            _intake(max_transfer_fee_bps=50).skip_reason(_coin(transfer_fee_bps=100)),
            "transfer fee 100 bps exceeds max_transfer_fee_bps 50",
        ),
        ("passes", _intake(max_transfer_fee_bps=50).skip_reason(_coin()), None),
        (
            "migration name",
            _intake(match_string="cat").migration_skip_reason(_coin()),
            "does not match filter 'cat'",
        ),
        (
            "migration name matches",
            _intake(match_string="moon").migration_skip_reason(_coin()),
            None,
        ),
        (
            "migration creator",
            _intake(bro_address=str(creator)).migration_skip_reason(_coin()),
            f"not created by {creator}",
        ),
        (
            "migration creator matches",
            _intake(bro_address=str(creator)).migration_skip_reason(
                _coin(creator=creator)
            ),
            None,
        ),
        (
            "migration then quote",
            _intake(match_string="moon").migration_skip_reason(
                _coin(quote_mint=USDC_MINT)
            ),
            f"no buy amount configured for quote mint {USDC_MINT}",
        ),
    ]
    results = []
    for label, pending, want in cases:
        got = await pending
        results.append(_check(f"filter: {label}", got == want, repr(got)))
    return all(results)


async def check_mayhem() -> bool:
    check = MayhemCheck()
    skip = await check.check(_coin(is_mayhem_mode=True), CTX)
    ok_pass = await check.check(_coin(), CTX)
    ok = skip is not None and ok_pass is None and MayhemCheck.reads_chain is False
    a = _check(
        "mayhem: skips mayhem coins, reads nothing", ok, f"{skip!r} / {ok_pass!r}"
    )
    b = _check(
        "mayhem is a listable built-in",
        BUILTINS.get("mayhem") is MayhemCheck,
        str(sorted(BUILTINS)),
    )
    return a and b


def check_shipped_configs() -> bool:
    problems = []
    for path in sorted((PROJECT_ROOT / "bots").glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text()) or {}
        for entry in (cfg.get("filters") or {}).get("checks") or []:
            cls = BUILTINS.get(entry.get("name"))
            reads = cls.reads_chain if cls else True
            if reads and entry.get("enabled", True):
                problems.append(f"{path.name}: {entry} reads the chain and is on")
    return _check(
        "no shipped config enables a chain-reading check",
        not problems,
        "; ".join(problems) or "none",
    )


class _CountingClient:
    """Records every attribute the trader reads off the RPC client."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __getattr__(self, name: str) -> object:
        self._events.append(f"rpc:{name}")
        raise AttributeError(name)


class _ChainSkips(PreBuyCheck):
    name = "chain_skips"
    reads_chain = True

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def check(self, _coin: TokenInfo, _ctx: CheckContext) -> str | None:
        self.events.append("chain check")
        return "too concentrated"


def _trader(events: list[str], checks: list[ConfiguredCheck], *, fast: bool) -> object:
    trader = object.__new__(UniversalTrader)
    trader.platform = Platform.PUMP_FUN
    trader.min_wallet_balance = None
    trader.solana_client = _CountingClient(events)
    trader.intake = _intake()
    trader.checks = CheckPipeline(checks, CheckContext(client=trader.solana_client))
    trader._has_chain_checks = trader.checks.has(reads_chain=True)  # noqa: SLF001
    trader.extreme_fast_mode = fast
    trader.wait_time_after_creation = 0
    trader.yolo_mode = False
    trader.quote_amounts = {WSOL_MINT: 0.01}

    async def execute(_token: TokenInfo) -> TradeResult:
        events.append("buy")
        return TradeResult(success=False, error_message="stub")

    async def nothing(*_a: object, **_k: object) -> None:
        return None

    trader.buyer = SimpleNamespace(execute=execute)
    trader._handle_failed_buy = nothing  # noqa: SLF001
    trader.positions = SimpleNamespace(wait_for_free_slot=nothing)
    return trader


async def check_free_checks_cost_no_rpc() -> bool:
    events: list[str] = []
    trader = _trader(events, [ConfiguredCheck(MayhemCheck())], fast=True)
    await trader._handle_token(_coin())  # noqa: SLF001
    ok = events == ["buy"]
    return _check("free checks: no RPC before the buy", ok, str(events))


async def check_chain_check_after_wait() -> bool:
    events: list[str] = []
    original = trade_log.save_token_info
    trade_log.save_token_info = lambda _t: events.append("creation wait")
    try:
        trader = _trader(events, [ConfiguredCheck(_ChainSkips(events))], fast=False)
        await trader._handle_token(_coin())  # noqa: SLF001
    finally:
        trade_log.save_token_info = original
    ok = events == ["creation wait", "chain check"]
    return _check("chain check runs after the wait and stops the buy", ok, str(events))


SUPPLY = 1_000_000


class _HoldersClient:
    """Serves the largest token accounts, the supply and each account's owner."""

    def __init__(
        self, holders: list[tuple[int, Pubkey]], *, fail: bool = False
    ) -> None:
        self.accounts = {Pubkey.new_unique(): h for h in holders}
        self.fail = fail

    async def post_rpc(self, body: dict, **_kwargs: object) -> dict | None:
        if self.fail:
            return None
        if body["method"] == "getTokenSupply":
            return {"result": {"value": {"amount": str(SUPPLY), "decimals": 6}}}
        if body["method"] == "getTokenLargestAccounts":
            value = [
                {"address": str(a), "amount": str(amount), "decimals": 6}
                for a, (amount, _owner) in self.accounts.items()
            ]
            return {"result": {"value": value}}
        return None

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Account | None]:
        self.commitment = commitment
        out = []
        for key in pubkeys:
            _amount, owner = self.accounts[key]
            data = bytes(32) + bytes(owner) + bytes(165 - 64)
            out.append(
                Account(
                    lamports=2_039_280,
                    data=data,
                    owner=Pubkey.new_unique(),
                    executable=False,
                    rent_epoch=0,
                )
            )
        return out


def _wallet() -> Pubkey:
    return Keypair().pubkey()


def _program_address() -> Pubkey:
    return Pubkey.find_program_address([b"vault"], Pubkey.new_unique())[0]


async def check_top_holders() -> bool:
    check = TopHoldersCheck(top=2, max_share=0.3)
    concentrated = _HoldersClient([(200_000, _wallet()), (150_000, _wallet())])
    vaulted = _HoldersClient([(900_000, _program_address()), (50_000, _wallet())])
    skip = await check.check(_coin(), CheckContext(client=concentrated))
    ok_pass = await check.check(_coin(), CheckContext(client=vaulted))
    a = _check("top holders: concentrated wallets skip", skip is not None, repr(skip))
    b = _check(
        "top holders: program-owned vault is left out", ok_pass is None, repr(ok_pass)
    )
    c = _check(
        "top holders: owners read at confirmed",
        concentrated.commitment == "confirmed",
        repr(concentrated.commitment),
    )
    return a and b and c


async def check_top_holders_unavailable() -> bool:
    results = []
    for label, client in (
        ("failed read", _HoldersClient([(1, _wallet())], fail=True)),
        ("no accounts", _HoldersClient([])),
    ):
        try:
            await TopHoldersCheck(max_share=0.5).check(_coin(), CheckContext(client))
        except CheckUnavailable:
            results.append(
                _check(
                    f"top holders: {label} can't decide",
                    passed=True,
                    detail="CheckUnavailable",
                )
            )
        else:
            results.append(
                _check(
                    f"top holders: {label} can't decide", passed=False, detail="decided"
                )
            )
    return all(results)


def check_top_holders_contract() -> bool:
    results = [
        _check(
            "top holders reads the chain",
            TopHoldersCheck.reads_chain is True,
            str(TopHoldersCheck.reads_chain),
        ),
        _check(
            "top holders is a listable built-in",
            BUILTINS.get("top_holders") is TopHoldersCheck,
            str(sorted(BUILTINS)),
        ),
    ]
    for share in (0, -0.1, 1.5):
        try:
            TopHoldersCheck(max_share=share)
        except ValueError:
            results.append(
                _check(f"max_share {share} refused", passed=True, detail="ok")
            )
        else:
            results.append(
                _check(f"max_share {share} refused", passed=False, detail="accepted")
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
        await check_existing_filters_unchanged(),
        await check_mayhem(),
        check_shipped_configs(),
        await check_free_checks_cost_no_rpc(),
        await check_chain_check_after_wait(),
        await check_top_holders(),
        await check_top_holders_unavailable(),
        check_top_holders_contract(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

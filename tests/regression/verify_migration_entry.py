"""Verify migration entry: the bot buys a coin in the pool its curve migrated to.

With `trade.entry: migration` the trader takes the listener's migration reports
(`PoolReady`) instead of its launches. A report names only the mint, the pool
and its quote mint, so the coin's name, creator, token program and transfer fee
are read from chain before the configured filters can judge it, and a shreds
report arrives before its pool exists. `extreme_fast_mode` sizes a buy from a
fixed token count without reading the venue, which a pool buy cannot do.

Fixture: `raw_migrated_coins_from_getmultipleaccounts.json` -- real migrated
coins (pump.fun Token-2022 coins quoted in SOL and USDC; LaunchLab SPL Token and
Token-2022 coins quoted in SOL, USDC and an xStock, one with a transfer fee):
the accounts the trader reads, the report the bot's parser gives for each
migration, and name, symbol, uri and creator from each coin's creation.

Offline machine checks, no network and no funds moved:

  A. Config: every committed bot config still validates and runs `new_launch`;
     `entry: migration` validates, with `extreme_fast_mode` on it is refused
     naming `extreme_fast_mode`, and an unknown entry is refused.
     `extreme_fast_mode` set from the environment is the bool it spells, in
     any case: `false` lets a migration bot start with it off, `TRUE` is
     refused.
  B. Metadata: the name, symbol and uri decode from a Token-2022 mint's own
     metadata extension and from a Metaplex account, equal to the creation's.
  C. A report resolves to the coin the buyer needs: its curve (pump.fun's
     bonding curve, LaunchLab's pool state), creator and creator vault, token
     program, quote mint, platform config and transfer fee. A report whose
     curve has not completed, whose quote mint is not the curve's, whose mint
     is missing, or whose LaunchLab curve is under a platform config the bot
     does not trade resolves to nothing.
  D. The pool wait returns once the venue prices the pool, and gives up when
     the budget runs out.
  E. Single-token mode: launches are ignored, the listener is asked for
     migrations, each filter (name, creator, allowed quote, quote amount,
     transfer fee) refuses a coin, and the first coin they accept is returned.
     A shreds report that leaves nothing to buy, or whose pool never appears,
     does not use the mint up: a later attempt is still bought. An executed
     one does. A mint is handed on once. A report older than `max_token_age`
     when it is taken off the queue is skipped.
  F. Continuous mode hands each fresh migrated coin to the handler once and
     skips stale reports. The trader in migration mode, single-token and
     continuous, buys what the intake hands it, and in `new_launch` mode asks
     the listener for no migrations. A LaunchLab migration trader resolves
     only curves under the platform configs it trades.
  G. A stream reports a migration before every node serves its slot, and on
     LaunchLab the buy completing the curve can share that slot. The curve is
     read at `processed` and re-read until it shows as completed, bounded by
     the pool wait; a report whose curve never showed completed does not use
     the mint up, so a later report is still bought, while one refused for
     another reason does. An executed LaunchLab report whose curve reads as
     not completed at first is bought through the real resolver.

Usage:
    uv run tests/regression/verify_migration_entry.py
"""

import asyncio
import base64
import copy
import json
import os
import struct
import sys
import tempfile
from collections.abc import Awaitable, Callable
from itertools import count
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

import bot_runner  # noqa: E402
import config_loader  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT, SystemAddresses  # noqa: E402
from core.token_metadata import decode_token_metadata, metadata_address  # noqa: E402
from interfaces.core import Platform, PoolReady, TokenInfo  # noqa: E402
from platforms.stonkfun.addresses import (  # noqa: E402
    PLATFORM_CONFIGS as STONKFUN_CONFIGS,
)
from trading import migration, universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.intake import TokenIntake  # noqa: E402
from trading.migration import MigrationResolver, PoolNotTradable  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads(
    (HERE / "raw_migrated_coins_from_getmultipleaccounts.json").read_text()
)["coins"]
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
# PoolState: discriminator, epoch u64, auth_bump u8, status u8.
_LAUNCHLAB_STATUS_OFFSET = 8 + 8 + 1
_endpoints = count()


class _FixtureClient:
    """Serves the fixture's accounts by address; anything else does not exist."""

    def __init__(self, accounts: dict) -> None:
        self.rpc_endpoint = f"fixture-{next(_endpoints)}"
        self.accounts = {
            Pubkey.from_string(k): SimpleNamespace(
                data=base64.b64decode(v["data"]), owner=Pubkey.from_string(v["owner"])
            )
            for k, v in accounts.items()
            if v is not None
        }

    async def get_account_info(
        self,
        address: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        if address not in self.accounts:
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        return self.accounts[address]

    async def get_multiple_accounts(
        self,
        addresses: list,
        commitment: str | None = None,  # noqa: ARG002
    ) -> list:
        return [self.accounts.get(a) for a in addresses]


def _pool_ready(coin: dict, **changes: object) -> PoolReady:
    p = coin["pool_ready"]
    fields = {
        "platform": Platform(p["platform"]),
        "mint": Pubkey.from_string(p["mint"]),
        "pool": Pubkey.from_string(p["pool"]),
        "quote_mint": Pubkey.from_string(p["quote_mint"]),
        "signature": p["signature"],
    }
    return PoolReady(**(fields | changes))


def _resolve(
    coin: dict,
    accounts: dict | None = None,
    platform_configs: frozenset[Pubkey] | None = None,
    **changes: object,
) -> object:
    # A short wait: a curve that is not completed is re-read until it runs out.
    resolver = MigrationResolver(
        _FixtureClient(accounts or coin["accounts"]), 0.2, platform_configs
    )
    try:
        return asyncio.run(resolver.resolve(_pool_ready(coin, **changes)))
    except PoolNotTradable as e:
        return e


# ------------------------------------------------------------------ A: config


def check_a_config() -> bool:
    problems = []
    for path in sorted((PROJECT_ROOT / "bots").glob("*.yaml")):
        config = yaml.safe_load(path.read_text())
        config.setdefault("platform", "pump_fun")
        try:
            config_loader.validate_config(config)
        except ValueError as e:
            problems.append(f"{path.name}: {e}")
            continue
        settings = bot_runner.build_trader_settings(
            {**config, "rpc_endpoint": "", "wss_endpoint": "", "private_key": ""},
            config_loader.get_platform_from_config(config),
        )
        if settings.entry != "new_launch":
            problems.append(f"{path.name}: entry {settings.entry!r}")

    base = yaml.safe_load(
        (PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml").read_text()
    )
    for label, trade, needle in (
        ("migration", {"entry": "migration", "extreme_fast_mode": False}, None),
        (
            "migration + xfm",
            {"entry": "migration", "extreme_fast_mode": True},
            "extreme_fast_mode",
        ),
        (
            "unknown entry",
            {"entry": "graduation", "extreme_fast_mode": False},
            "trade.entry",
        ),
    ):
        config = copy.deepcopy(base)
        config["trade"].update(trade)
        try:
            config_loader.validate_config(config)
        except ValueError as e:
            if needle is None or needle not in str(e):
                problems.append(f"{label}: refused: {e}")
        else:
            if needle is not None:
                problems.append(f"{label}: validated")
    problems += _interpolated_fast_mode(base)
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _interpolated_fast_mode(base: dict) -> list[str]:
    """`extreme_fast_mode: ${VAR}` on a migration bot, loaded from a file."""
    problems = []
    config = copy.deepcopy(base)
    config.pop("env_file", None)
    config.pop("geyser", None)
    config.update(
        rpc_endpoint="http://127.0.0.1:8899",
        wss_endpoint="ws://127.0.0.1:8900",
        private_key=str(Keypair()),
    )
    config["trade"].update(entry="migration", extreme_fast_mode="${VERIFY_XFM}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.yaml"
        path.write_text(yaml.safe_dump(config))
        for value, expected in (("false", False), ("False", False), ("TRUE", True)):
            os.environ["VERIFY_XFM"] = value
            try:
                loaded = config_loader.load_bot_config(str(path))
            except ValueError as e:
                if expected is not True or "extreme_fast_mode" not in str(e):
                    problems.append(f"VERIFY_XFM={value}: refused: {e}")
                continue
            finally:
                os.environ.pop("VERIFY_XFM", None)
            if expected is True:
                problems.append(f"VERIFY_XFM={value}: validated")
            elif loaded["trade"]["extreme_fast_mode"] is not False:
                problems.append(
                    f"VERIFY_XFM={value}: loaded as "
                    f"{loaded['trade']['extreme_fast_mode']!r}"
                )
    return problems


# ---------------------------------------------------------------- B: metadata


def check_b_metadata() -> bool:
    problems, shapes = [], set()
    for coin in FIXTURE:
        mint = coin["pool_ready"]["mint"]
        accounts = coin["accounts"]
        metaplex = accounts.get(str(metadata_address(Pubkey.from_string(mint))))
        shapes.add("metaplex" if metaplex else "token-2022")
        got = decode_token_metadata(
            base64.b64decode(accounts[mint]["data"]),
            base64.b64decode(metaplex["data"]) if metaplex else None,
        )
        want = tuple(coin["expected"][k] for k in ("name", "symbol", "uri"))
        if got is None or tuple(got) != want:
            problems.append(f"{coin['label']}: {got} != {want}")
    if shapes != {"metaplex", "token-2022"}:
        problems.append(f"fixture covers only {shapes}")
    if decode_token_metadata(bytes(82), None) is not None:
        problems.append("a bare mint decoded to metadata")
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ---------------------------------------------------------------- C: resolving


def _with_data(coin: dict, address: str, edit: object) -> dict:
    accounts = copy.deepcopy(coin["accounts"])
    data = bytearray(base64.b64decode(accounts[address]["data"]))
    edit(data)
    accounts[address]["data"] = base64.b64encode(bytes(data)).decode()
    return accounts


def check_c_resolve() -> bool:  # noqa: C901, PLR0912
    problems = []
    for coin in FIXTURE:
        label, p = coin["label"], coin["pool_ready"]
        token = _resolve(coin)
        if not isinstance(token, TokenInfo):
            problems.append(f"{label}: not resolved: {token}")
            continue
        mint = Pubkey.from_string(p["mint"])
        accounts = coin["accounts"]
        curve = Pubkey.from_string(coin["curve"])
        expected = coin["expected"]
        got = {
            "name": token.name,
            "symbol": token.symbol,
            "uri": token.uri,
            "mint": token.mint,
            "token_program": token.token_program_id,
            "quote_mint": token.quote_mint,
            "pool": (token.additional_data or {}).get("pool"),
        }
        want = {
            "name": expected["name"],
            "symbol": expected["symbol"],
            "uri": expected["uri"],
            "mint": mint,
            "token_program": Pubkey.from_string(accounts[p["mint"]]["owner"]),
            "quote_mint": Pubkey.from_string(p["quote_mint"]),
            "pool": p["pool"],
        }
        curve_data = base64.b64decode(accounts[coin["curve"]]["data"])
        if token.platform == Platform.PUMP_FUN:
            # BondingCurve: discriminator, five u64 reserves, complete, creator.
            creator = Pubkey.from_bytes(curve_data[49:81])
            got |= {
                "curve": token.bonding_curve,
                "creator": token.creator,
                "creator_vault": token.creator_vault,
                "associated_bonding_curve": token.associated_bonding_curve,
            }
            want |= {
                "curve": curve,
                "creator": creator,
                "creator_vault": Pubkey.find_program_address(
                    [b"creator-vault", bytes(creator)], PUMP_PROGRAM
                )[0],
                "associated_bonding_curve": Pubkey.find_program_address(
                    [bytes(curve), bytes(want["token_program"]), bytes(mint)],
                    SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
                )[0],
            }
        else:
            mint_data = base64.b64decode(accounts[p["mint"]]["data"])
            has_fee = b"\x01\x00\x6c\x00" in mint_data[166:]
            got |= {
                "curve": token.pool_state,
                "creator": str(token.creator),
                "platform_config": token.platform_config,
                "has_transfer_fee": token.transfer_fee_bps > 0,
                "base_vault": token.base_vault is not None,
            }
            want |= {
                "curve": curve,
                "creator": expected["creator"],
                "platform_config": Pubkey.from_bytes(
                    curve_data[8 + 8 + 5 + 80 + 40 + 32 : 8 + 8 + 5 + 80 + 40 + 64]
                ),
                "has_transfer_fee": has_fee,
                "base_vault": True,
            }
        for key, value in want.items():
            if got[key] != value:
                problems.append(f"{label}: {key} {got[key]} != {value}")
        if token.platform != Platform.PUMP_FUN:
            own = frozenset({want["platform_config"]})
            if not isinstance(_resolve(coin, platform_configs=own), TokenInfo):
                problems.append(f"{label}: refused under its own platform config")
            refused = _resolve(coin, platform_configs=frozenset({Pubkey.default()}))
            if not (
                isinstance(refused, PoolNotTradable)
                and str(want["platform_config"]) in str(refused)
            ):
                problems.append(
                    f"{label}: resolved under a platform config the bot does "
                    f"not trade: {refused}"
                )

    fee_coins = [c for c in FIXTURE if "reward" in c["label"]]
    if (
        not fee_coins
        or not isinstance(t := _resolve(fee_coins[0]), TokenInfo)
        or t.transfer_fee_bps not in (100, 300)
    ):
        problems.append("the reward coin's transfer fee was not read")

    for coin in FIXTURE:
        label, p = coin["label"], coin["pool_ready"]
        pump = p["platform"] == "pump_fun"

        def not_complete(data: bytearray, *, pump: bool = pump) -> None:
            if pump:
                data[48] = 0
            else:
                data[_LAUNCHLAB_STATUS_OFFSET] = 0

        other_quote = (
            USDC_MINT if Pubkey.from_string(p["quote_mint"]) != USDC_MINT else WSOL_MINT
        )
        no_mint = {k: v for k, v in coin["accounts"].items() if k != p["mint"]}
        for case, result in (
            (
                "curve not complete",
                _resolve(coin, _with_data(coin, coin["curve"], not_complete)),
            ),
            ("another quote mint", _resolve(coin, quote_mint=other_quote)),
            ("mint missing", _resolve(coin, no_mint)),
        ):
            if not isinstance(result, PoolNotTradable):
                problems.append(f"{label}: {case} resolved to {result}")
    for problem in problems:
        print(f"    {problem}")
    return not problems


# -------------------------------------------------------------- D: pool wait


class _Market:
    def __init__(self, ready_after: int) -> None:
        self.calls, self.ready_after = 0, ready_after

    async def get_market_state(self, token_info: TokenInfo) -> dict:  # noqa: ARG002
        self.calls += 1
        if self.calls <= self.ready_after:
            raise ValueError("No pool yet")  # noqa: TRY003
        return {"price_per_token": 1.0, "fee_fraction": 0.0, "pool": None}


def _token(symbol: str = "COIN", **fields: object) -> TokenInfo:
    base = {
        "name": symbol,
        "symbol": symbol,
        "uri": "",
        "mint": Pubkey.new_unique(),
        "platform": Platform.PUMP_FUN,
        "creator": Pubkey.new_unique(),
        "quote_mint": WSOL_MINT,
    }
    return TokenInfo(**(base | fields))


def check_d_pool_wait() -> bool:
    original = migration.get_platform_implementations
    original_poll = migration.POOL_POLL_SECONDS
    problems = []
    try:
        migration.POOL_POLL_SECONDS = 0.01
        for ready_after, budget, want in (
            (3, 5.0, True),
            (10**6, 0.1, False),
            (0, 0.0, True),
        ):
            market = _Market(ready_after)
            migration.get_platform_implementations = lambda *_a, m=market: (
                SimpleNamespace(graduated_market=m)
            )
            got = asyncio.run(MigrationResolver(None, budget).wait_for_pool(_token()))
            if got is not want or (want and market.calls != ready_after + 1):
                problems.append(
                    f"ready after {ready_after}, budget {budget}: {got}, {market.calls} reads"
                )
    finally:
        migration.get_platform_implementations = original
        migration.POOL_POLL_SECONDS = original_poll
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ------------------------------------------------------------- E/F: intake


class _Resolver:
    """Resolves reports from a table; `pools` says whether a coin's pool appears."""

    def __init__(self, coins: dict, pools: dict | None = None) -> None:
        self.coins, self.pools = coins, pools or {}
        self.waited: list = []

    async def resolve(self, pool: PoolReady) -> TokenInfo:
        found = self.coins.get((pool.mint, pool.signature)) or self.coins.get(pool.mint)
        if isinstance(found, Exception):
            raise found
        return copy.copy(found)

    async def wait_for_pool(self, token_info: TokenInfo) -> bool:
        self.waited.append(token_info.mint)
        return self.pools.get(token_info.mint, True)


class _Listener:
    """Plays launches and migration reports to the callbacks it is given."""

    def __init__(self, events: list, linger: float = 3600) -> None:
        self.events, self.calls, self.linger = events, [], linger

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        *args: object,
        pool_callback: Callable[[PoolReady], Awaitable[None]] | None = None,
    ) -> None:
        self.calls.append((args, pool_callback is not None))
        for event in self.events:
            if isinstance(event, PoolReady):
                if pool_callback is not None:
                    await pool_callback(event)
            else:
                await token_callback(event)
            await asyncio.sleep(0.02)
        await asyncio.sleep(self.linger)


def _intake(events: list, resolver: _Resolver, **settings: object) -> TokenIntake:
    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": "",
        "buy_amount": 0.01,
        "buy_slippage": 0.1,
        "sell_slippage": 0.1,
        "entry": "migration",
        "token_wait_timeout": 5,
        "max_token_age": 30,
    }
    s = TraderSettings(**(fields | settings))
    quote_amounts = {WSOL_MINT: 0.01} | ({USDC_MINT: 1.0} if s.quote_amounts else {})
    allowed = {WSOL_MINT} if s.allowed_quote_mints else None
    return TokenIntake(
        s, _Listener(events), quote_amounts, allowed, pool_resolver=resolver
    )


def _report(mint: Pubkey, *, pre: bool = False, signature: str = "sig") -> PoolReady:
    return PoolReady(
        Platform.PUMP_FUN,
        mint,
        Pubkey.new_unique(),
        WSOL_MINT,
        signature,
        pre_execution=pre,
    )


def _wait(intake: TokenIntake) -> TokenInfo | None:
    return asyncio.run(intake.wait_for_migrated_token())


def check_e_single_token() -> bool:  # noqa: C901, PLR0912, PLR0915
    problems = []
    creator = Pubkey.new_unique()
    good = _token("GOODCOIN", creator=creator)
    launch = _token("LAUNCH")
    # A launch first: ignored. The listener is asked for migrations.
    intake = _intake([launch, _report(good.mint)], _Resolver({good.mint: good}))
    found = _wait(intake)
    if found is None or found.mint != good.mint:
        problems.append(f"launch then migration: found {found and found.symbol}")
    if intake.token_listener.calls != [((), True)]:
        problems.append(f"listener called {intake.token_listener.calls}")

    # Each filter refuses its coin; the good coin after it is the one returned.
    usdc = _token("USDCOIN", quote_mint=USDC_MINT, creator=creator)
    feecoin = _token("FEECOIN", transfer_fee_bps=300, creator=creator)
    stranger = _token("GOODSTRANGER")
    named = _token("OTHER", creator=creator)
    for label, refused, settings in (
        ("name", named, {"match_string": "good"}),
        ("creator", stranger, {"bro_address": str(creator)}),
        (
            "allowed quote",
            usdc,
            {"allowed_quote_mints": ["sol"], "quote_amounts": {"usdc": 1.0}},
        ),
        ("quote amount", usdc, {}),
        ("transfer fee", feecoin, {"max_transfer_fee_bps": 100}),
    ):
        resolver = _Resolver({refused.mint: refused, good.mint: good})
        intake = _intake(
            [_report(refused.mint), _report(good.mint)], resolver, **settings
        )
        found = _wait(intake)
        if found is None or found.mint != good.mint:
            problems.append(f"{label} filter: found {found and found.symbol}")
        if refused.mint in resolver.waited:
            problems.append(f"{label} filter: waited for a refused coin's pool")
    # The same filter accepts what it should.
    for label, settings in (
        ("name", {"match_string": "good"}),
        ("creator", {"bro_address": str(creator)}),
    ):
        intake = _intake([_report(good.mint)], _Resolver({good.mint: good}), **settings)
        if (found := _wait(intake)) is None or found.mint != good.mint:
            problems.append(f"{label} filter refused a coin it matches")

    # Shreds: an attempt that cannot land, or whose pool never shows, leaves
    # the mint open for the real one.
    coin = _token("SHREDCOIN")
    for label, first in (
        ("unresolvable attempt", PoolNotTradable("its curve has not completed")),
        ("pool never appears", None),
    ):
        coins = {(coin.mint, "real"): coin, (coin.mint, "first"): first or coin}
        resolver = _Resolver(coins)
        if first is None:
            # The first attempt's pool does not appear; the real one's does.
            async def wait(token_info: TokenInfo, r: _Resolver = resolver) -> bool:
                r.waited.append(token_info.mint)
                return len(r.waited) > 1

            resolver.wait_for_pool = wait
        events = [
            _report(coin.mint, pre=True, signature="first"),
            _report(coin.mint, pre=True, signature="real"),
        ]
        if (
            found := _wait(_intake(events, resolver))
        ) is None or found.mint != coin.mint:
            problems.append(f"shreds {label}: the real attempt was not bought")
    # Executed: a report that leaves nothing to buy uses the mint up.
    coin = _token("EXECCOIN")
    late = _token("LATECOIN")
    resolver = _Resolver({coin.mint: coin, late.mint: late}, {coin.mint: False})
    intake = _intake(
        [_report(coin.mint), _report(coin.mint, signature="again"), _report(late.mint)],
        resolver,
    )
    found = _wait(intake)
    if (
        found is None
        or found.mint != late.mint
        or resolver.waited.count(coin.mint) != 1
    ):
        problems.append(
            f"executed report without a pool: found {found and found.symbol}, waits {resolver.waited}"
        )

    # Nothing acceptable: times out with None.
    intake = _intake(
        [_report(named.mint)],
        _Resolver({named.mint: named}),
        match_string="good",
        token_wait_timeout=0.5,
    )
    if _wait(intake) is not None:
        problems.append("a refused coin was returned")

    # max_token_age: a report that waited in the queue behind another's pool
    # wait is too old by the time it is taken.
    class _SlowPool(_Resolver):
        async def wait_for_pool(self, token_info: TokenInfo) -> bool:
            await asyncio.sleep(0.3)
            return await super().wait_for_pool(token_info)

    first, queued = _token("FIRST", creator=creator), _token("QUEUED", creator=creator)
    resolver = _SlowPool({first.mint: first, queued.mint: queued}, {first.mint: False})
    intake = _intake(
        [_report(first.mint), _report(queued.mint)],
        resolver,
        max_token_age=0.1,
        token_wait_timeout=1.0,
    )
    found = _wait(intake)
    if found is not None or queued.mint in resolver.waited:
        problems.append(
            f"a report older than max_token_age was taken: found "
            f"{found and found.symbol}, waits {resolver.waited}"
        )
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_f_continuous_and_trader() -> bool:  # noqa: C901, PLR0915
    problems = []
    a, b = _token("ACOIN"), _token("BCOIN")
    handled: list = []

    async def handle(token_info: TokenInfo) -> None:
        handled.append(token_info.mint)

    async def run(intake: TokenIntake) -> None:
        task = asyncio.create_task(intake.run_migrations(handle))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    events = [
        _report(a.mint),
        _report(a.mint, pre=True, signature="x"),
        _report(b.mint),
    ]
    intake = _intake(events, _Resolver({a.mint: a, b.mint: b}))
    asyncio.run(run(intake))
    if handled != [a.mint, b.mint]:
        problems.append(f"continuous: handled {handled}")
    handled.clear()
    asyncio.run(
        run(_intake(events, _Resolver({a.mint: a, b.mint: b}), max_token_age=-1))
    )
    if handled:
        problems.append(f"stale reports handled: {handled}")

    # The trader: migration mode buys the coin the intake hands it.
    coin = FIXTURE[0]
    keypair = Keypair()

    def trader_for(
        entry: str, listener: _Listener, *, yolo_mode: bool = False, **extra: object
    ) -> universal_trader.UniversalTrader:
        settings = TraderSettings(
            **extra,
            yolo_mode=yolo_mode,
            wait_time_before_new_token=0,
            max_token_age=30,
            rpc_endpoint="",
            wss_endpoint="",
            private_key=base58.b58encode(bytes(keypair)).decode(),
            buy_amount=0.01,
            buy_slippage=0.1,
            sell_slippage=0.1,
            entry=entry,
            wait_time_after_creation=0,
            token_wait_timeout=5,
        )
        originals = (
            universal_trader.SolanaClient,
            universal_trader.ListenerFactory.create_listener,
        )
        client = _FixtureClient(coin["accounts"])
        client.get_health = lambda: asyncio.sleep(0, "ok")
        client.close = lambda: asyncio.sleep(0)
        universal_trader.SolanaClient = lambda *_a, **_k: client
        universal_trader.ListenerFactory.create_listener = staticmethod(
            lambda **_k: listener
        )
        try:
            trader = universal_trader.UniversalTrader(settings)
        finally:
            universal_trader.SolanaClient = originals[0]
            universal_trader.ListenerFactory.create_listener = staticmethod(
                originals[1]
            )

        async def no_quotes() -> None:
            return None

        trader._resolve_quote_token_programs = no_quotes  # noqa: SLF001
        return trader

    bought: list = []
    # The regular path saves each coin under trades/; not from a verifier.
    universal_trader.trade_log.save_token_info = lambda _token_info: None

    async def buy(token_info: TokenInfo) -> TradeResult:
        bought.append(token_info)
        return TradeResult(success=False, error_message="offline")

    listener = _Listener([_token("LAUNCH"), _pool_ready(coin)])
    trader = trader_for("migration", listener)
    trader.buyer.execute = buy
    original_wait = MigrationResolver.wait_for_pool

    async def pool_ready(_self: MigrationResolver, _token_info: TokenInfo) -> bool:
        return True

    mint = Pubkey.from_string(coin["pool_ready"]["mint"])
    MigrationResolver.wait_for_pool = pool_ready
    try:
        asyncio.run(trader.start())
        if [t.mint for t in bought] != [mint] or (
            bought[0].name != coin["expected"]["name"]
        ):
            problems.append(
                f"migration trader bought {[(t.symbol, t.mint) for t in bought]}"
            )
        if listener.calls != [((), True)]:
            problems.append(f"migration trader's listener calls {listener.calls}")
        # Continuous (yolo) mode: the same, until the listener stops.
        bought.clear()
        listener = _Listener([_token("LAUNCH"), _pool_ready(coin)], linger=0.5)
        trader = trader_for("migration", listener, yolo_mode=True)
        trader.buyer.execute = buy
        asyncio.run(trader.start())
        if [t.mint for t in bought] != [mint] or listener.calls != [((), True)]:
            problems.append(
                f"continuous migration trader bought {[t.symbol for t in bought]}, "
                f"listener calls {listener.calls}"
            )
    finally:
        MigrationResolver.wait_for_pool = original_wait

    # new_launch: the listener is asked for launches only, and buys the launch.
    bought.clear()
    launch = _token("LAUNCH")
    listener = _Listener([_pool_ready(coin), launch])
    trader = trader_for("new_launch", listener)
    trader.buyer.execute = buy
    asyncio.run(trader.start())
    if [t.mint for t in bought] != [launch.mint]:
        problems.append(f"new_launch trader bought {[t.symbol for t in bought]}")
    if [pool for _, pool in listener.calls] != [False]:
        problems.append(f"new_launch trader asked for migrations: {listener.calls}")

    # The resolver refuses a curve under a launchpad the bot does not trade:
    # the preset's configs by default, `platform_configs` when narrowed.
    own_config = next(iter(STONKFUN_CONFIGS))
    for label, extra, want in (
        ("pump.fun", {}, None),
        ("stonk_fun", {"platform": Platform.STONK_FUN}, STONKFUN_CONFIGS),
        ("launchlab", {"platform": Platform.LAUNCHLAB}, None),
        (
            "launchlab narrowed",
            {"platform": Platform.LAUNCHLAB, "platform_configs": [str(own_config)]},
            frozenset({own_config}),
        ),
    ):
        resolver = trader_for("migration", _Listener([]), **extra).intake.pool_resolver
        if resolver.platform_configs != want:
            problems.append(
                f"{label} migration trader resolves under {resolver.platform_configs}"
            )
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ------------------------------------------------- G: curve read lags the stream


def _not_complete(coin: dict) -> object:
    pump = coin["pool_ready"]["platform"] == "pump_fun"

    def edit(data: bytearray) -> None:
        if pump:
            # The curve before its completing buy: reserves left, not complete.
            data[8:24] = struct.pack("<QQ", 300_000_000_000_000, 80_000_000_000)
            data[48] = 0
        else:
            data[_LAUNCHLAB_STATUS_OFFSET] = 0

    return edit


class _LaggingClient(_FixtureClient):
    """Serves a coin's curve as not completed until the node "catches up".

    The first `lag` curve reads are stale (with `missing`, the account is not
    found); with `confirmed_lags`, every read not at `processed` is stale too,
    as on a node whose confirmed view trails the stream.
    """

    def __init__(
        self,
        coin: dict,
        lag: int,
        *,
        confirmed_lags: bool = False,
        missing: bool = False,
    ) -> None:
        super().__init__(coin["accounts"])
        self.curve = Pubkey.from_string(coin["curve"])
        stale = _with_data(coin, coin["curve"], _not_complete(coin))[coin["curve"]]
        self.stale = SimpleNamespace(
            data=base64.b64decode(stale["data"]),
            owner=Pubkey.from_string(stale["owner"]),
        )
        self.lag, self.confirmed_lags, self.curve_reads = lag, confirmed_lags, 0
        self.missing = missing

    async def get_account_info(
        self, address: Pubkey, commitment: str | None = None
    ) -> SimpleNamespace:
        if address == self.curve:
            self.curve_reads += 1
            if self.curve_reads <= self.lag or (
                self.confirmed_lags and commitment != "processed"
            ):
                if self.missing:
                    raise ValueError(f"Account {address} not found")  # noqa: TRY003
                return self.stale
        return await super().get_account_info(address, commitment)


def check_g_curve_read_lags_stream() -> bool:  # noqa: C901, PLR0912, PLR0915
    original_poll = migration.POOL_POLL_SECONDS
    problems = []
    try:
        migration.POOL_POLL_SECONDS = 0.01
        for coin in FIXTURE:
            label = coin["label"]
            client = _LaggingClient(coin, 3)
            try:
                got = asyncio.run(
                    MigrationResolver(client, 5.0).resolve(_pool_ready(coin))
                )
            except PoolNotTradable as e:
                got = e
            if not isinstance(got, TokenInfo) or client.curve_reads != 4:  # noqa: PLR2004
                problems.append(
                    f"{label}: completed on the 4th read: {got}, {client.curve_reads} reads"
                )
            client = _LaggingClient(coin, 0, confirmed_lags=True)
            try:
                got = asyncio.run(
                    MigrationResolver(client, 0.0).resolve(_pool_ready(coin))
                )
            except PoolNotTradable as e:
                got = e
            if not isinstance(got, TokenInfo):
                problems.append(f"{label}: not read at processed: {got}")
            for case, client in (
                ("not completed", _LaggingClient(coin, 10**6)),
                ("not readable", _LaggingClient(coin, 10**6, missing=True)),
            ):
                resolve = MigrationResolver(client, 0.2).resolve(_pool_ready(coin))
                try:
                    got = asyncio.run(asyncio.wait_for(resolve, 3))
                except (PoolNotTradable, TimeoutError) as e:
                    got = e
                if not isinstance(got, PoolNotTradable) or not got.not_yet_visible:
                    problems.append(f"{label}: never {case}: {got!r}")
            client = _LaggingClient(coin, 3, missing=True)
            try:
                got = asyncio.run(
                    MigrationResolver(client, 5.0).resolve(_pool_ready(coin))
                )
            except PoolNotTradable as e:
                got = e
            if not isinstance(got, TokenInfo):
                problems.append(f"{label}: readable on the 4th read: {got}")
            mint = coin["pool_ready"]["mint"]
            no_mint = {k: v for k, v in coin["accounts"].items() if k != mint}
            # A mint missing through the wait is a node behind the stream too
            # (verify_migration_sees_fast_graduations covers it).
            if not (
                isinstance(r := _resolve(coin, no_mint), PoolNotTradable)
                and r.not_yet_visible
            ):
                problems.append(f"{label}: a missing mint used the mint up: {r!r}")

        # Intake: a curve that never showed completed leaves the mint open for a
        # later executed report; a refusal for another reason uses it up.
        for label, first, want_bought in (
            ("not yet visible", PoolNotTradable("x", not_yet_visible=True), True),
            ("refused", PoolNotTradable("the pool is quoted in another mint"), False),
        ):
            coin = _token("LAGCOIN")
            resolver = _Resolver(
                {(coin.mint, "first"): first, (coin.mint, "again"): coin}
            )
            intake = _intake(
                [
                    _report(coin.mint, signature="first"),
                    _report(coin.mint, signature="again"),
                ],
                resolver,
                token_wait_timeout=1,
            )
            found = _wait(intake)
            if (found is not None) is not want_bought:
                problems.append(f"intake, {label}: found {found and found.symbol}")

        # End to end, as on mainnet: an executed LaunchLab report whose curve the
        # node still shows as not completed is bought once it catches up.
        coin = next(
            c
            for c in FIXTURE
            if c["pool_ready"]["platform"] != "pump_fun"
            and Pubkey.from_string(c["pool_ready"]["quote_mint"]) == WSOL_MINT
        )
        resolver = MigrationResolver(_LaggingClient(coin, 3, confirmed_lags=True), 5.0)

        async def pool_ready(_token_info: TokenInfo) -> bool:
            return True

        resolver.wait_for_pool = pool_ready
        intake = _intake([_pool_ready(coin)], resolver, token_wait_timeout=3)
        found = _wait(intake)
        if found is None or str(found.mint) != coin["pool_ready"]["mint"]:
            problems.append(f"executed LaunchLab report, curve behind: found {found}")
    finally:
        migration.POOL_POLL_SECONDS = original_poll
    for problem in problems:
        print(f"    {problem}")
    return not problems


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: config validates trade.entry", check_a_config),
        ("B: metadata decodes from either home", check_b_metadata),
        ("C: a report resolves to the coin the buyer needs", check_c_resolve),
        ("D: the pool wait polls within its budget", check_d_pool_wait),
        ("E: single-token mode filters and keeps attempts open", check_e_single_token),
        ("F: continuous mode and the trader", check_f_continuous_and_trader),
        ("G: a curve read behind the stream", check_g_curve_read_lags_stream),
    ]
    passed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"    {type(error).__name__}: {error}")
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

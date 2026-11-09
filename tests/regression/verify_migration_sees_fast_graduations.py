"""Verify migration entry buys a coin that graduates seconds after its launch.

A pump.fun coin can be created, bought out and migrated in the same second,
and a node a slot behind the stream does not find a mint that young. The
resolver refused a missing mint at once, as "mint account not found", outside
the wait that re-reads the curve, so such a coin was skipped for good. These
are the coins a migration sniper is after. The mint is now re-read within that
wait, and read at `processed`, since the client's default commitment can lag.

A LaunchLab coin's transfer fee came from the curve manager's own read of the
mint, which a node behind the stream can miss; it then reported no fee and
`max_transfer_fee_bps` let a high-fee coin through.

`max_token_age` measures the time since the bot detected the coin, not its age
on chain; the skip lines and the shipped configs must say so.

Fixture: `raw_migrated_coins_from_getmultipleaccounts.json` -- real migrated
coins (pump.fun and LaunchLab, SPL Token and Token-2022, SOL, USDC and xStock
quotes) with the accounts the resolver reads.

Offline machine checks, no network and no funds moved:

  A. The mint is missing on the first reads and there later: every coin
     resolves, to the same coin as with the mint there from the start, once
     the mint shows.
  B. The mint is read at `processed`: a node that shows it only at
     `processed` resolves the coin on the first read.
  C. The mint stays missing through the wait: the coin is refused as not yet
     visible, with a reason saying its mint was not seen within the wait, only
     once the wait has run out.
  D. Through the intake (single-token mode, real resolver): a coin whose mint
     shows late is bought; one whose mint never shows is skipped with that
     reason and does not use the mint up.
  E. A coin older than `max_token_age` when taken up is skipped with a line
     saying the age is counted from its detection, for launches and
     migrations, and every shipped config says what the clock measures and
     when it is checked.
  F. A transfer-fee coin whose mint the curve manager's own read misses still
     resolves to its fee, and `max_transfer_fee_bps` below it refuses the coin.

Usage:
    uv run tests/regression/verify_migration_sees_fast_graduations.py
"""

import asyncio
import base64
import json
import logging
import re
import sys
from itertools import count
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from core.token_metadata import metadata_address  # noqa: E402
from interfaces.core import Platform, PoolReady, TokenInfo  # noqa: E402
from platforms.launchlab.curve_math import (  # noqa: E402
    mint_transfer_fee,
    transfer_fee_bps,
)
from trading import migration  # noqa: E402
from trading.intake import TokenIntake  # noqa: E402
from trading.migration import MigrationResolver, PoolNotTradable  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads(
    (HERE / "raw_migrated_coins_from_getmultipleaccounts.json").read_text()
)["coins"]
_endpoints = count()


class _YoungMintClient:
    """Serves a coin's accounts; its mint shows only from a given read on.

    `shows_from` is the 1-based mint read at which the mint is first served
    (None: never). With `processed_only`, a read at any other commitment never
    finds it, as on a node whose `finalized` view trails the stream. With
    `fee_read_misses`, a batch that reads the mint without its metadata
    account (the curve manager's fee read) never finds it.
    """

    def __init__(
        self,
        coin: dict,
        shows_from: int | None,
        *,
        processed_only: bool = False,
        fee_read_misses: bool = False,
    ) -> None:
        self.rpc_endpoint = f"young-mint-{next(_endpoints)}"
        self.accounts = {
            Pubkey.from_string(k): SimpleNamespace(
                data=base64.b64decode(v["data"]), owner=Pubkey.from_string(v["owner"])
            )
            for k, v in coin["accounts"].items()
            if v is not None
        }
        self.mint = Pubkey.from_string(coin["pool_ready"]["mint"])
        self.shows_from, self.processed_only = shows_from, processed_only
        self.fee_read_misses = fee_read_misses
        self.mint_reads: list[str | None] = []
        self.misses = 0

    def _mint_visible(self, commitment: str | None) -> bool:
        self.mint_reads.append(commitment)
        visible = not (self.processed_only and commitment != "processed") and (
            self.shows_from is not None and len(self.mint_reads) >= self.shows_from
        )
        self.misses += not visible
        return visible

    async def get_account_info(
        self, address: Pubkey, commitment: str | None = None
    ) -> SimpleNamespace:
        if address == self.mint and not self._mint_visible(commitment):
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        if address not in self.accounts:
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        return self.accounts[address]

    async def get_multiple_accounts(
        self, addresses: list, commitment: str | None = None
    ) -> list:
        if self.fee_read_misses and metadata_address(self.mint) not in addresses:
            return [None if a == self.mint else self.accounts.get(a) for a in addresses]
        return [
            None
            if a == self.mint and not self._mint_visible(commitment)
            else self.accounts.get(a)
            for a in addresses
        ]


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


def _resolve(client: _YoungMintClient, coin: dict, wait: float) -> object:
    resolve = MigrationResolver(client, wait).resolve(_pool_ready(coin))
    try:
        return asyncio.run(asyncio.wait_for(resolve, wait + 5))
    except (PoolNotTradable, TimeoutError) as e:
        return e


def _same_coin(got: TokenInfo, want: TokenInfo) -> list[str]:
    fields = (
        "name",
        "symbol",
        "uri",
        "mint",
        "creator",
        "token_program_id",
        "quote_mint",
        "bonding_curve",
        "associated_bonding_curve",
        "creator_vault",
        "pool_state",
        "platform_config",
        "transfer_fee_bps",
    )
    return [
        f"{f}: {getattr(got, f)} != {getattr(want, f)}"
        for f in fields
        if getattr(got, f) != getattr(want, f)
    ]


# --------------------------------------------------- A: the mint shows late


def check_a_mint_shows_late() -> bool:
    problems = []
    for coin in FIXTURE:
        label = coin["label"]
        want = _resolve(_YoungMintClient(coin, 1), coin, 0.0)
        if not isinstance(want, TokenInfo):
            problems.append(f"{label}: does not resolve with its mint there: {want}")
            continue
        client = _YoungMintClient(coin, 4)
        got = _resolve(client, coin, 5.0)
        if not isinstance(got, TokenInfo):
            problems.append(f"{label}: mint shown on the 4th read, refused: {got!r}")
            continue
        if client.misses != 3:  # noqa: PLR2004
            problems.append(f"{label}: resolved after {client.misses} misses, not 3")
        problems += [f"{label}: {p}" for p in _same_coin(got, want)]
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ---------------------------------------------- B: the mint read at processed


def check_b_mint_read_at_processed() -> bool:
    problems = []
    for coin in FIXTURE:
        client = _YoungMintClient(coin, 1, processed_only=True)
        got = _resolve(client, coin, 0.0)
        if not isinstance(got, TokenInfo):
            problems.append(
                f"{coin['label']}: mint read at {client.mint_reads}, refused: {got!r}"
            )
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ---------------------------------------- C: the mint never shows in the wait


def check_c_mint_never_shows() -> bool:
    problems = []
    wait = 0.3
    for coin in FIXTURE:
        label = coin["label"]
        client = _YoungMintClient(coin, None)
        started = monotonic()
        got = _resolve(client, coin, wait)
        elapsed = monotonic() - started
        if not isinstance(got, PoolNotTradable):
            problems.append(f"{label}: resolved to {got!r}")
            continue
        if not got.not_yet_visible:
            problems.append(f"{label}: refused for good, not as not yet visible: {got}")
        if not re.search(r"mint was not seen .*within \d", str(got)):
            problems.append(
                f"{label}: reason does not say the mint was not visible in time: {got}"
            )
        if elapsed < wait or len(client.mint_reads) < 2:  # noqa: PLR2004
            problems.append(
                f"{label}: gave up after {elapsed:.2f}s and "
                f"{len(client.mint_reads)} reads, before the {wait}s wait ran out"
            )
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ------------------------------------------------------ D: through the intake


class _Listener:
    """Plays migration reports to the pool callback, then idles."""

    def __init__(self, events: list) -> None:
        self.events = events

    async def listen_for_tokens(
        self, _token_callback: object, *_args: object, pool_callback: object = None
    ) -> None:
        for event in self.events:
            await pool_callback(event)
            await asyncio.sleep(0.02)
        await asyncio.sleep(3600)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _intake(events: list, resolver: object, **settings: object) -> TokenIntake:
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
    return TokenIntake(
        s, _Listener(events), {WSOL_MINT: 0.01}, None, pool_resolver=resolver
    )


def _logged(logger_name: str, run: object) -> tuple[object, list[str]]:
    handler = _Lines()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        return run(), handler.lines
    finally:
        logger.removeHandler(handler)


def check_d_intake() -> bool:
    problems = []
    coin = next(
        c
        for c in FIXTURE
        if c["pool_ready"]["platform"] == "pump_fun"
        and Pubkey.from_string(c["pool_ready"]["quote_mint"]) == WSOL_MINT
    )
    mint = coin["pool_ready"]["mint"]

    async def pool_ready(_token_info: TokenInfo) -> bool:
        return True

    resolver = MigrationResolver(_YoungMintClient(coin, 4), 5.0)
    resolver.wait_for_pool = pool_ready
    intake = _intake([_pool_ready(coin)], resolver)
    found, lines = _logged(
        "trading.intake", lambda: asyncio.run(intake.wait_for_migrated_token())
    )
    if found is None or str(found.mint) != mint:
        problems.append(f"mint shows late: found {found}; log: {lines}")

    # The first report's wait runs out before the mint shows; the second sees it.
    client = _YoungMintClient(coin, 10**6)
    resolver = MigrationResolver(client, 0.2)
    resolver.wait_for_pool = pool_ready
    reports = [
        _pool_ready(coin, signature="first"),
        _pool_ready(coin, signature="again"),
    ]
    intake = _intake(reports, resolver)
    original_resolve = resolver.resolve

    async def resolve(pool: PoolReady) -> TokenInfo:
        if pool.signature == "again":
            client.shows_from = len(client.mint_reads) + 1
        return await original_resolve(pool)

    resolver.resolve = resolve
    found, lines = _logged(
        "trading.intake", lambda: asyncio.run(intake.wait_for_migrated_token())
    )
    skipped = [
        line
        for line in lines
        if line.startswith(f"Skipping migrated coin {mint}")
        and re.search(r"mint was not seen .*within \d", line)
    ]
    if not skipped:
        problems.append(f"mint never shows: no skip line naming it; log: {lines}")
    if found is None or str(found.mint) != mint:
        problems.append(f"a later report after a mint not yet visible: found {found}")
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ---------------------------------------------- E: what max_token_age measures


def check_e_token_age_clock() -> bool:  # noqa: C901
    problems = []

    # Launch: a coin detected long ago and taken now.
    intake = _intake([], None, entry="new_launch", max_token_age=0.5)
    token = TokenInfo(
        name="Old",
        symbol="OLD",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
    )
    intake.token_timestamps[str(token.mint)] = monotonic() - 5
    handled: list = []

    async def handle(token_info: TokenInfo) -> None:
        handled.append(token_info)

    async def run_launch() -> None:
        await intake.token_queue.put(token)
        task = asyncio.create_task(intake._process_token_queue(handle))  # noqa: SLF001
        await asyncio.wait_for(intake.token_queue.join(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _, lines = _logged("trading.intake", lambda: asyncio.run(run_launch()))
    line = next((x for x in lines if x.startswith("Skipping token OLD")), None)
    if handled or line is None or not re.search(r"since detected", line):
        problems.append(f"launch: skip line does not name its clock: {line!r}")

    # Migration, single-token and continuous: a report taken after max_token_age.
    coin = FIXTURE[0]

    async def continuous(intake: TokenIntake) -> None:
        try:
            await asyncio.wait_for(intake.run_migrations(handle), 1)
        except TimeoutError:
            pass

    for mode in ("single-token", "continuous"):
        resolver = MigrationResolver(_YoungMintClient(coin, 1), 0.0)
        intake = _intake(
            [_pool_ready(coin)], resolver, max_token_age=0, token_wait_timeout=1
        )
        found, lines = _logged(
            "trading.intake",
            lambda intake=intake, mode=mode: asyncio.run(
                intake.wait_for_migrated_token()
                if mode == "single-token"
                else continuous(intake)
            ),
        )
        line = next((x for x in lines if "too old" in x), None)
        if (
            found is not None
            or handled
            or line is None
            or not re.search(r"since .*detected", line)
        ):
            problems.append(
                f"migration, {mode}: skip line does not name its clock: {line!r}"
            )

    # The comment on the key, inline or in the lines just above it.
    for config in sorted((PROJECT_ROOT / "bots").glob("*.yaml")):
        lines = config.read_text().splitlines()
        for i, text in enumerate(lines):
            if not text.strip().startswith("max_token_age:"):
                continue
            above = []
            for prior in reversed(lines[:i]):
                if not prior.strip().startswith("#"):
                    break
                above.insert(0, prior.strip().lstrip("# "))
            comment = " ".join([*above, text.partition("#")[2]])
            if not re.search(
                r"[Cc]hecked once.*detected.*not its age on\s+chain", comment
            ):
                problems.append(f"{config.name}: max_token_age comment: {comment!r}")
    readme = " ".join((PROJECT_ROOT / "README.md").read_text().split())
    for where, pattern in (
        ("filters entry", r"`max_token_age` \(checked once, [^)]*detected"),
        ("migration section", r"`max_token_age` is checked once, .{0,200}detected"),
    ):
        if not re.search(pattern, readme):
            problems.append(f"README {where}: does not say what max_token_age measures")
    for problem in problems:
        print(f"    {problem}")
    return not problems


# ------------------------------------- F: the transfer fee from the mint read


def check_f_transfer_fee() -> bool:
    problems = []
    fee_coins = [
        c
        for c in FIXTURE
        if mint_transfer_fee(
            base64.b64decode(c["accounts"][c["pool_ready"]["mint"]]["data"])
        )
    ]
    if not fee_coins:
        problems.append("fixture has no transfer-fee coin")
    for coin in fee_coins:
        label = coin["label"]
        mint = coin["accounts"][coin["pool_ready"]["mint"]]
        want = transfer_fee_bps(mint_transfer_fee(base64.b64decode(mint["data"])))
        client = _YoungMintClient(coin, 1, fee_read_misses=True)
        got = _resolve(client, coin, 0.0)
        if not isinstance(got, TokenInfo):
            problems.append(f"{label}: refused: {got!r}")
            continue
        if got.transfer_fee_bps != want:
            problems.append(
                f"{label}: transfer fee {got.transfer_fee_bps} bps, the mint's {want}"
            )
        intake = _intake([], object(), max_transfer_fee_bps=want - 1)
        if not intake.migration_skip_reason(got):
            problems.append(f"{label}: max_transfer_fee_bps {want - 1} let it through")
    for problem in problems:
        print(f"    {problem}")
    return not problems


def main() -> int:
    """Run every check and report."""
    original_poll = migration.POOL_POLL_SECONDS
    migration.POOL_POLL_SECONDS = 0.01
    checks = [
        (
            "A: a mint that shows late is read again until it does",
            check_a_mint_shows_late,
        ),
        ("B: the mint is read at processed", check_b_mint_read_at_processed),
        (
            "C: a mint that never shows is refused as not yet visible",
            check_c_mint_never_shows,
        ),
        (
            "D: the intake buys a fast graduation and keeps the mint open",
            check_d_intake,
        ),
        ("E: max_token_age says it counts from detection", check_e_token_age_clock),
        ("F: a LaunchLab coin's transfer fee is the mint's", check_f_transfer_fee),
    ]
    passed = 0
    try:
        for label, check in checks:
            try:
                ok = check()
            except Exception as error:  # noqa: BLE001 - report and continue
                print(f"    {type(error).__name__}: {error}")
                ok = False
            print(f"{'PASS' if ok else 'FAIL'} {label}")
            passed += ok
    finally:
        migration.POOL_POLL_SECONDS = original_poll
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

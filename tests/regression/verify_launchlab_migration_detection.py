"""Verify the LaunchLab listeners report a curve's migration to its CPMM pool.

When a LaunchLab curve completes, Raydium's migrate wallet calls
`migrate_to_cpswap`, which has Raydium CPMM create the coin's pool. The
`blocks`, `geyser` and `shreds` listeners report it to an optional
`pool_callback` as a `PoolReady` with the mint, pool and quote mint. LaunchLab's
logs never name the coin, so every listener decodes the instruction: the
executed ones from the transaction (top-level or inner), `shreds` from the
top-level instruction before it runs.

The migrate wallet retries a migration that already landed; the retry fails
with PoolMigrated. A report needs the instruction's platform config to be one
of the parser's and its CPMM pool to be the one CPMM derives from the named
config and mints.

Fixture: `raw_launchlab_migrations_from_gettransaction.json`, mainnet
migrations quoted in SOL, USDC, two xStocks and another coin, of SPL Token and
Token-2022 coins, under StonkFun's two configs and three other launchpads',
and a failed retry. Each entry's `expected` comes from the LaunchLab and CPMM
pool accounts, not from the instruction.

Offline machine checks, no network and no funds moved. Each listener runs its
real `listen_for_tokens` loop over scripted frames:

  A. `blocks` and `geyser` report every real migration once, as `expected`,
     with `pre_execution` unset and the transaction's signature, under the
     `launchlab` platform, and under `stonk_fun` only for StonkFun's configs;
     a block holding them all reports each; a migration reached by CPI is
     reported; a listener on pump.fun and LaunchLab reports both platforms'
     migrations.
  B. They report nothing for the failed retry, a real migration marked
     reverted, one naming a pool that is not CPMM's for its config and mints,
     one under a platform config the listener is narrowed away from, or a
     LaunchLab launch.
  C. `shreds` reports every top-level migrate_to_cpswap with `pre_execution`
     set, the failed retry included, and nothing for a non-derived pool or a
     narrowed-away config. Asked for pools it subscribes to nothing more: each
     migration names the platform config and the program, the accounts its
     creation filter already holds.
  D. A pool is reported once per mint across repeated frames (on shreds once
     per transaction), and a callback that raises does not stop the listener.
  E. Without `pool_callback` nothing is parsed, and a launch is still detected
     with a pool callback set.
  F. The fixture is sound: the frames carry the JSON transaction's signature.

Usage:
    uv run tests/regression/verify_launchlab_migration_detection.py
"""

# The listeners' entry points open sockets and gRPC streams; these checks swap
# those for scripted ones and drive the real loops.
# ruff: noqa: SLF001

import asyncio
import base64
import copy
import json
import sys
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import websockets  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform, PoolReady, TokenInfo  # noqa: E402
from monitoring.base_listener import BaseTokenListener  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from platforms.stonkfun.addresses import PLATFORM_CONFIGS as STONKFUN  # noqa: E402
from utils.idl_parser import IDLParser  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "raw_launchlab_migrations_from_gettransaction.json"
LAUNCH_FIXTURE = HERE / "raw_launchlab_launches_from_gettransaction.json"
PUMP_FIXTURE = HERE / "raw_pumpfun_migrations_from_gettransaction.json"
IDL = IDLParser(str(PROJECT_ROOT / "idl/raydium_launchlab_idl.json"))
MIGRATE = next(
    ix for ix in IDL.idl["instructions"] if ix["name"] == "migrate_to_cpswap"
)
MIGRATE_DISCRIMINATOR = bytes(MIGRATE["discriminator"])
POSITION = {account["name"]: i for i, account in enumerate(MIGRATE["accounts"])}
LISTENERS = ("blocks", "geyser")
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


class _FeedEnd(BaseException):
    """Ends a listener loop once its scripted frames run out."""


def _migrations() -> list[dict]:
    return json.loads(FIXTURE.read_text())["migrations"]


def _real() -> list[dict]:
    return [entry for entry in _migrations() if "expected" in entry]


def _expected(entry: dict) -> tuple[str, str, str]:
    e = entry["expected"]
    return e["mint"], e["pool"], e["quote_mint"]


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _triple(pool: PoolReady) -> tuple[str, str, str]:
    return str(pool.mint), str(pool.pool), str(pool.quote_mint)


def _migrate_ix(tx: dict) -> dict:
    """The top-level migrate_to_cpswap instruction of a JSON transaction."""
    keys = _keys(tx)
    return next(
        ix
        for ix in tx["transaction"]["message"]["instructions"]
        if keys[ix["programIdIndex"]] == str(LAUNCHLAB_PROGRAM)
        and base58.b58decode(ix["data"])[:8] == MIGRATE_DISCRIMINATOR
    )


def _named(entry: dict, name: str) -> str:
    tx = entry["get_transaction"]
    return _keys(tx)[_migrate_ix(tx)["accounts"][POSITION[name]]]


def _instruction_triple(entry: dict) -> tuple[str, str, str]:
    return tuple(
        _named(entry, name) for name in ("base_mint", "cpswap_pool", "quote_mint")
    )


# --- forging ----------------------------------------------------------------------


def _forged(entry: dict, accounts: dict[str, str] | None = None) -> dict:
    """The entry as another transaction, with named migrate accounts swapped.

    The key-table slot the instruction points at is rewritten, static or loaded
    through a lookup table. The block frame then carries the JSON transaction.
    """
    forged = copy.deepcopy(entry)
    forged.pop("transaction_base64", None)
    forged.pop("deshred_update_base64", None)
    tx = forged["get_transaction"]
    signature = base58.b58encode(bytes(Pubkey.new_unique()) * 2).decode()
    forged["signature"] = signature
    tx["transaction"]["signatures"][0] = signature
    ix = _migrate_ix(tx)
    static = tx["transaction"]["message"]["accountKeys"]
    loaded = tx["meta"]["loadedAddresses"]
    tables = (static, loaded["writable"], loaded["readonly"])
    for name, key in (accounts or {}).items():
        index = ix["accounts"][POSITION[name]]
        for table in tables:
            if index < len(table):
                table[index] = key
                break
            index -= len(table)
    return forged


def _to_other_program(entry: dict) -> dict:
    """The migrate instruction's bytes and accounts, addressed to another program."""
    forged = _forged(entry)
    tx = forged["get_transaction"]
    message = tx["transaction"]["message"]
    _migrate_ix(tx)["programIdIndex"] = message["accountKeys"].index(COMPUTE_BUDGET)
    return forged


def _reached_by_cpi(entry: dict) -> dict:
    """The migration moved under a top-level wrapper, as an inner instruction."""
    forged = _forged(entry)
    tx = forged["get_transaction"]
    message = tx["transaction"]["message"]
    ix = _migrate_ix(tx)
    position = message["instructions"].index(ix)
    budget = message["accountKeys"].index(COMPUTE_BUDGET)
    message["instructions"][position] = {
        "programIdIndex": budget,
        "accounts": [],
        "data": base58.b58encode(bytes([2, 0, 0, 0, 0])).decode(),
    }
    tx["meta"]["innerInstructions"] = [
        {"index": position, "instructions": [{**ix, "stackHeight": 2}]},
        *[g for g in tx["meta"]["innerInstructions"] if g["index"] != position],
    ]
    return forged


# --- frames -------------------------------------------------------------------


def _block_frame(entries: list[dict]) -> dict:
    transactions = [
        {
            "transaction": [entry["transaction_base64"], "base64"]
            if "transaction_base64" in entry
            else entry["get_transaction"]["transaction"],
            "meta": entry["get_transaction"]["meta"],
            "version": entry["get_transaction"]["version"],
        }
        for entry in entries
    ]
    return {
        "jsonrpc": "2.0",
        "method": "blockNotification",
        "params": {
            "result": {"value": {"slot": 1, "block": {"transactions": transactions}}},
            "subscription": 1,
        },
    }


def _geyser_frame(entry: dict) -> geyser_pb2.SubscribeUpdate:
    """The executed transaction as geyser's Subscribe stream carries it."""
    tx = entry["get_transaction"]
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    info.signature = base58.b58decode(entry["signature"])
    message = info.transaction.message
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    meta = tx["meta"]
    if meta.get("err") is not None:
        info.meta.err.err = json.dumps(meta["err"]).encode()
    info.meta.log_messages.extend(meta.get("logMessages") or [])
    loaded = meta.get("loadedAddresses") or {}
    info.meta.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.meta.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    for group in meta.get("innerInstructions") or []:
        inner = info.meta.inner_instructions.add()
        inner.index = group["index"]
        for ix in group["instructions"]:
            compiled = inner.instructions.add()
            compiled.program_id_index = ix["programIdIndex"]
            compiled.accounts = bytes(ix["accounts"])
            compiled.data = base58.b58decode(ix["data"])
    return update


def _deshred_frame(entry: dict) -> geyser_pb2.SubscribeUpdateDeshred:
    """The captured deshred frame, or one built from a forged entry's JSON."""
    update = geyser_pb2.SubscribeUpdateDeshred()
    if "deshred_update_base64" in entry:
        update.ParseFromString(base64.b64decode(entry["deshred_update_base64"]))
        return update
    tx = entry["get_transaction"]
    info = update.deshred_transaction.transaction
    info.signature = base58.b58decode(entry["signature"])
    message = info.transaction.message
    info.transaction.signatures.append(info.signature)
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    message.versioned = True
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    for lookup in json_message.get("addressTableLookups") or []:
        table = message.address_table_lookups.add()
        table.account_key = base58.b58decode(lookup["accountKey"])
        table.writable_indexes = bytes(lookup["writableIndexes"])
        table.readonly_indexes = bytes(lookup["readonlyIndexes"])
    loaded = tx["meta"].get("loadedAddresses") or {}
    info.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    return update


# --- scripted transports --------------------------------------------------------


class _FakeSocket:
    """A websocket that answers subscriptions, then plays frames, then ends."""

    def __init__(self, frames: list[dict]) -> None:
        self.replies: deque[str] = deque()
        self.frames = deque(json.dumps(frame) for frame in frames)

    async def send(self, message: str) -> None:
        self.replies.append(json.dumps({"id": json.loads(message)["id"], "result": 7}))

    async def recv(self) -> str:
        if self.replies:
            return self.replies.popleft()
        if self.frames:
            return self.frames.popleft()
        raise _FeedEnd

    async def ping(self) -> None:
        await asyncio.sleep(3600)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "_FakeSocket":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeChannel:
    async def close(self) -> None:
        return None


class _FakeStub:
    """A geyser stub whose first stream plays frames, then ends."""

    def __init__(self, updates: list) -> None:
        self.updates = updates
        self.requests: list = []

    def _stream(self, requests):  # noqa: ANN001, ANN202
        self.requests.extend(requests)
        updates, self.updates = self.updates, []

        async def stream():  # noqa: ANN202
            for update in updates:
                yield update
            raise _FeedEnd

        return stream()

    Subscribe = _stream
    SubscribeDeshred = _stream


def _listener(
    kind: str,
    platforms: tuple[Platform, ...] = (Platform.LAUNCHLAB,),
    platform_configs: frozenset[Pubkey] | None = None,
) -> BaseTokenListener:
    return ListenerFactory.create_listener(
        listener_type=kind,
        wss_endpoint="wss://rpc.offline.invalid",
        geyser_endpoint="geyser.offline.invalid:443",
        geyser_api_token="unused",  # noqa: S106 — never sent; nothing connects
        platforms=list(platforms),
        platform_configs=platform_configs,
    )


def _frames(kind: str, entries: list[dict]) -> list:
    if kind == "blocks":
        return [_block_frame([entry]) for entry in entries]
    if kind == "geyser":
        return [_geyser_frame(entry) for entry in entries]
    return [_deshred_frame(entry) for entry in entries]


def _run(
    kind: str,
    frames: list,
    *,
    with_pools: bool = True,
    pool_callback=None,  # noqa: ANN001
    listener=None,  # noqa: ANN001
) -> tuple[list[TokenInfo], list[PoolReady], object]:
    """Run one listener's real loop over `frames`; return what it reported."""
    listener = listener or _listener(kind)
    tokens: list[TokenInfo] = []
    pools: list[PoolReady] = []

    async def on_token(token: TokenInfo) -> None:
        tokens.append(token)

    async def on_pool(pool: PoolReady) -> None:
        pools.append(pool)

    stub = _FakeStub(frames)

    async def connection():  # noqa: ANN202
        return stub, _FakeChannel()

    listener._create_geyser_connection = connection
    original_connect = websockets.connect
    feeds = deque([frames])
    websockets.connect = lambda *_a, **_k: _FakeSocket(feeds.popleft() if feeds else [])

    async def go() -> None:
        try:
            await listener.listen_for_tokens(
                on_token,
                pool_callback=(pool_callback or on_pool) if with_pools else None,
            )
        except _FeedEnd:
            pass

    try:
        asyncio.run(go())
    finally:
        websockets.connect = original_connect
    return tokens, pools, stub


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _stonkfun(entry: dict) -> bool:
    """Whether the migration is under one of StonkFun's platform configs."""
    return Pubkey.from_string(_named(entry, "platform_config")) in STONKFUN


# --- checks ---------------------------------------------------------------------


def _executed_problems(entry: dict, kind: str, platform: Platform) -> list[str]:
    """What is wrong with `kind`'s report of one real migration on `platform`."""
    listener = _listener(kind, (platform,))
    _, pools, _ = _run(kind, _frames(kind, [entry]), listener=listener)
    covered = platform == Platform.LAUNCHLAB or _stonkfun(entry)
    wanted = [_expected(entry)] if covered else []
    got = [_triple(pool) for pool in pools]
    where = f"{kind} / {platform.value} / {entry['label']}"
    problems = []
    if got != wanted:
        problems.append(f"{where}: {got} != {wanted}")
    if any(pool.pre_execution for pool in pools):
        problems.append(f"{where}: marked pre-execution")
    if any(pool.signature != entry["signature"] for pool in pools):
        problems.append(f"{where}: wrong signature")
    if any(pool.platform != platform for pool in pools):
        problems.append(f"{where}: wrong platform")
    return problems


def check_a_executed_listeners_report_real_migrations() -> bool:
    """A: blocks and geyser report every real migration as the accounts show."""
    problems = []
    real = _real()
    if len(real) < 6 or sum(map(_stonkfun, real)) in (0, len(real)):  # noqa: PLR2004
        problems.append("fixture lacks real migrations on and off StonkFun")
    for entry in real:
        for kind in LISTENERS:
            for platform in (Platform.LAUNCHLAB, Platform.STONK_FUN):
                problems += _executed_problems(entry, kind, platform)
            _, pools, _ = _run(kind, _frames(kind, [_reached_by_cpi(entry)]))
            if [_triple(pool) for pool in pools] != [_expected(entry)]:
                problems.append(f"{kind} / {entry['label']} by CPI: not reported")

    _, pools, _ = _run("blocks", [_block_frame(real)])
    if sorted(_triple(pool) for pool in pools) != sorted(map(_expected, real)):
        problems.append(f"blocks: one block of {len(real)} gave {len(pools)} pools")

    # Both platforms on one listener: each parser reads only its own migrations.
    pump = [
        e
        for e in json.loads(PUMP_FIXTURE.read_text())["migrations"]
        if "already migrated" not in e["label"] and not e["label"].startswith("failed")
    ]
    both = (Platform.PUMP_FUN, Platform.LAUNCHLAB)
    for kind in LISTENERS:
        listener = _listener(kind, both)
        _, pools, _ = _run(kind, _frames(kind, [*pump, *real]), listener=listener)
        platforms = [pool.platform for pool in pools]
        wanted = [Platform.PUMP_FUN] * len(pump) + [Platform.LAUNCHLAB] * len(real)
        if platforms != wanted:
            problems.append(f"{kind} with both platforms: {platforms}")
    return _report(problems)


def _negatives() -> list[tuple[str, dict, frozenset[Pubkey] | None]]:
    """(label, entry, narrowing) that must not report a pool."""
    real = _real()
    first = real[0]
    cases = [
        (entry["label"], entry, None)
        for entry in _migrations()
        if "expected" not in entry
    ]
    reverted = copy.deepcopy(first)
    reverted["get_transaction"]["meta"]["err"] = {
        "InstructionError": [2, {"Custom": 1}]
    }
    cases.append(("real migration marked reverted", reverted, None))
    for entry in real:
        cases.append(
            (
                f"{entry['label']} naming a pool CPMM does not derive",
                _forged(entry, {"cpswap_pool": str(Pubkey.new_unique())}),
                None,
            )
        )
        cases.append(
            (
                f"{entry['label']} with another CPMM config",
                _forged(entry, {"cpswap_config": str(Pubkey.new_unique())}),
                None,
            )
        )
        cases.append(
            (
                f"{entry['label']} sent to another program",
                _to_other_program(entry),
                None,
            )
        )
        cases.append(
            (
                f"{entry['label']} under a config the listener is narrowed from",
                entry,
                frozenset({Pubkey.new_unique()}),
            )
        )
    for tx in json.loads(LAUNCH_FIXTURE.read_text()):
        cases.append(
            (
                "LaunchLab launch",
                {
                    "signature": tx["transaction"]["signatures"][0],
                    "label": "launch",
                    "get_transaction": tx,
                },
                None,
            )
        )
    return cases


def check_b_executed_listeners_ignore_non_migrations() -> bool:
    """B: no pool from a failure, a forgery, a narrowed-away config or a launch."""
    problems = []
    for label, entry, narrowing in _negatives():
        for kind in LISTENERS:
            listener = _listener(kind, platform_configs=narrowing)
            _, pools, _ = _run(kind, _frames(kind, [entry]), listener=listener)
            if pools:
                problems.append(f"{kind} / {label}: reported {_triple(pools[0])}")
    return _report(problems)


def _subscription_problems(entry: dict) -> list[str]:
    """Asked for pools, shreds subscribes to nothing more, and needs nothing more.

    The creation filter already carries the migration: it names the platform
    config and the program as static keys.
    """
    problems = []
    static = entry["get_transaction"]["transaction"]["message"]["accountKeys"]
    platforms = (Platform.LAUNCHLAB, Platform.STONK_FUN)
    for platform in platforms if _stonkfun(entry) else platforms[:1]:
        listener = _listener("shreds", (platform,))
        requests = []
        for with_pools in (False, True):
            _, _, stub = _run("shreds", [], with_pools=with_pools, listener=listener)
            requests.append(stub.requests[0].SerializeToString(deterministic=True))
        if requests[0] != requests[1]:
            problems.append(f"shreds / {platform.value}: subscription changed")
        accounts = [
            list(f.account_include)
            for f in stub.requests[0].deshred_transactions.values()
        ]
        if not any(key in static for group in accounts for key in group):
            problems.append(
                f"shreds / {platform.value} / {entry['label']}: "
                f"no subscribed account among the static keys"
            )
    return problems


def check_c_shreds_reports_migrate_instructions() -> bool:
    """C: shreds decodes each top-level migrate_to_cpswap before it runs."""
    problems = []
    for entry in _migrations():
        _, pools, _ = _run("shreds", _frames("shreds", [entry]))
        got = [_triple(pool) for pool in pools]
        # Before execution the failed retry looks like the migration it repeats.
        wanted = [
            _expected(entry) if "expected" in entry else _instruction_triple(entry)
        ]
        if got != wanted:
            problems.append(f"shreds / {entry['label']}: {got} != {wanted}")
        if not all(
            p.pre_execution and p.signature == entry["signature"] for p in pools
        ):
            problems.append(
                f"shreds / {entry['label']}: not pre-execution, or signature"
            )

        # A frame rebuilt from the JSON reads as the captured one, so the
        # forgery below fails on its pool and nothing else.
        _, pools, _ = _run("shreds", [_deshred_frame(_forged(entry))])
        if [_triple(pool) for pool in pools] != wanted:
            problems.append(f"shreds / {entry['label']} rebuilt: {pools}")
        forged = _forged(entry, {"cpswap_pool": str(Pubkey.new_unique())})
        _, pools, _ = _run("shreds", [_deshred_frame(forged)])
        if pools:
            problems.append(f"shreds / {entry['label']} with a stranger pool: reported")
        narrowed = _listener(
            "shreds", platform_configs=frozenset({Pubkey.new_unique()})
        )
        _, pools, _ = _run("shreds", _frames("shreds", [entry]), listener=narrowed)
        if pools:
            problems.append(f"shreds / {entry['label']} narrowed away: reported")

        problems += _subscription_problems(entry)
    return _report(problems)


def check_d_once_per_mint() -> bool:
    """D: one report per mint; a raising callback does not stop the listener."""
    problems = []
    real = _real()
    for kind in (*LISTENERS, "shreds"):
        _, pools, _ = _run(kind, _frames(kind, [real[0], real[0], real[1]]))
        if len(pools) != 2:  # noqa: PLR2004
            problems.append(f"{kind}: {len(pools)} reports for two mints")

        seen = []

        async def flaky(pool: PoolReady, seen: list = seen) -> None:
            seen.append(pool)
            if len(seen) == 1:
                raise RuntimeError("callback failed")  # noqa: TRY003

        _run(kind, _frames(kind, real[:2]), pool_callback=flaky)
        if len(seen) != 2:  # noqa: PLR2004
            problems.append(f"{kind}: {len(seen)} reports after a raising callback")

    # On shreds the failed retry is another transaction for the same mint.
    failed = next(e for e in _migrations() if "expected" not in e)
    original = next(
        e for e in real if e["expected"]["mint"] == _instruction_triple(failed)[0]
    )
    _, pools, _ = _run("shreds", _frames("shreds", [original, failed]))
    if [p.signature for p in pools] != [original["signature"], failed["signature"]]:
        problems.append("shreds: the retry was not reported as its own attempt")
    return _report(problems)


def check_e_default_is_unchanged() -> bool:
    """E: no pool callback, no migration parsing; launches still detected."""
    problems = []
    names = (
        "parse_pools_ready_from_block_transaction",
        "parse_pools_ready_from_geyser",
        "parse_pool_ready_from_instruction",
    )
    for kind in (*LISTENERS, "shreds"):
        listener = _listener(kind)
        parser = next(iter(listener.platform_parsers.values()))
        calls = []

        def spy(*args, calls: list = calls, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append(args)
            return []

        # The parser instance is shared by every listener, so the spies come off
        # before the next check runs.
        for name in names:
            setattr(parser, name, spy)
        try:
            _, pools, _ = _run(
                kind, _frames(kind, _real()), with_pools=False, listener=listener
            )
        finally:
            for name in names:
                delattr(parser, name)
        if calls or pools:
            problems.append(f"{kind}: parsed migrations without a pool callback")

    for tx in json.loads(LAUNCH_FIXTURE.read_text()):
        entry = {
            "signature": tx["transaction"]["signatures"][0],
            "label": "launch",
            "get_transaction": tx,
        }
        mints = set()
        for kind in LISTENERS:
            tokens, pools, _ = _run(kind, _frames(kind, [entry]))
            if len(tokens) != 1 or pools:
                problems.append(f"{kind}: launch gave tokens {tokens}, pools {pools}")
            mints |= {str(t.mint) for t in tokens}
        if len(mints) != 1:
            problems.append(f"launch: listeners disagree on the mint {mints}")
    return _report(problems)


def check_f_fixture_is_sound() -> bool:
    """F: each frame carries the JSON transaction's signature."""
    problems = []
    for entry in _migrations():
        deshred = _deshred_frame(entry).deshred_transaction.transaction
        if base58.b58encode(deshred.signature).decode() != entry["signature"]:
            problems.append(f"{entry['label']}: deshred signature differs")
        envelope = VersionedTransaction.from_bytes(
            base64.b64decode(entry["transaction_base64"])
        )
        if str(envelope.signatures[0]) != entry["signature"]:
            problems.append(f"{entry['label']}: envelope signature differs")
        tx = entry["get_transaction"]
        if tx["transaction"]["signatures"][0] != entry["signature"]:
            problems.append(f"{entry['label']}: json signature differs")
        if ("expected" in entry) != (tx["meta"]["err"] is None):
            problems.append(f"{entry['label']}: expected block and outcome disagree")
    return _report(problems)


CHECKS = [
    check_a_executed_listeners_report_real_migrations,
    check_b_executed_listeners_ignore_non_migrations,
    check_c_shreds_reports_migrate_instructions,
    check_d_once_per_mint,
    check_e_default_is_unchanged,
    check_f_fixture_is_sound,
]


def main() -> int:
    results = []
    for check in CHECKS:
        print(check.__doc__.splitlines()[0])
        ok = check()
        print("  PASS" if ok else "  FAIL")
        results.append(ok)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Verify every listener reports a pump.fun curve's migration to its PumpSwap pool.

When a curve completes, anyone can call `migrate` or `migrate_v2`, which has
PumpSwap create the coin's canonical pool. The listeners report it to an
optional `pool_callback` as a `PoolReady` with the mint, pool and quote mint.

`logs`, `blocks` and `geyser` read PumpSwap's `CreatePoolEvent` from the logs.
pump.fun's own `CompletePumpAmmMigrationEvent` is an event CPI, an inner
instruction that logsSubscribe never carries, so it cannot serve all three; it
is the independent answer the checks below compare against. `shreds` has no
logs and decodes the top-level migrate instruction before it executes.

Most migrate calls on mainnet find the curve already migrated and succeed doing
nothing, so a migrate instruction is no proof of a pool. A payload counts only
when PumpSwap's own invocation emitted it and its pool is the canonical one,
whose creator is pump.fun's pool-authority PDA; nothing but a migration can
create that pool.

Fixture: `raw_pumpfun_migrations_from_gettransaction.json`, mainnet
transactions: `migrate_v2` with SOL, USDC, an SPL Token and a Token-2022 quote,
legacy `migrate`, v0 and v1 envelopes, a migration and a buy in one
transaction, a migrate on a curve already migrated, and a failed transaction.

Offline machine checks, no network and no funds moved. Each listener runs its
real `listen_for_tokens` loop over scripted frames:

  A. `logs`, `blocks` and `geyser` report every real migration once, with the
     mint, pool and quote mint of pump.fun's CompletePumpAmmMigrationEvent in
     the same transaction (its SOL quote, the default key, as wrapped SOL), and
     `pre_execution` unset; a block holding them all reports each.
  B. They report nothing for a migrate on a migrated curve, a failed
     transaction, a real migration marked reverted, a CreatePoolEvent emitted
     by another program's invocation, one naming a pool that is not the
     coin's canonical pool, or a coin creation.
  C. `shreds` reports every top-level migrate instruction with the same mint,
     pool and quote mint and `pre_execution` set, including the no-op and the
     failed one, which it cannot tell apart before execution, and nothing for
     one naming a pool that is not the canonical pool of its mints. Asked for
     pools it adds a subscription on Global's withdraw authority, which every
     migration names as account 1 and no buy or sell names.
  D. A pool is reported once per mint across repeated frames, and a callback
     that raises does not stop the listener reporting the next one.
  G. On `shreds` an earlier attempt for the same mint does not hide the one
     that lands: a migrate that will fail (the same instruction in another
     transaction) and one naming another quote mint and its pool are each
     reported, and the real one after them still is.
  E. Without `pool_callback` nothing changes: no listener parses a migration,
     the shreds subscription is the creation filter alone, and a coin creation
     is still detected with a pool callback set.
  F. The fixture is sound: the deshred frame and the base64 envelope carry the
     JSON transaction's signature.

Usage:
    uv run tests/regression/verify_pumpfun_migration_detection.py
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

from core.pubkeys import WSOL_MINT  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import Platform, PoolReady, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.pumpswap_market import canonical_pool_address  # noqa: E402
from utils.idl_parser import IDLParser  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "raw_pumpfun_migrations_from_gettransaction.json"
CREATE_FIXTURE = (
    PROJECT_ROOT
    / "cookbook/pumpfun/decode/raw_create_v2_with_fee_bps_from_gettransaction.json"
)
PUMP_IDL = IDLParser(str(PROJECT_ROOT / "idl/pump_fun_idl.json"))
PUMP_SWAP_IDL = IDLParser(str(PROJECT_ROOT / "idl/pump_swap_idl.json"))
EVENT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")
LISTENERS = ("logs", "blocks", "geyser")


class _FeedEnd(BaseException):
    """Ends a listener loop once its scripted frames run out."""


def _migrations() -> list[dict]:
    return json.loads(FIXTURE.read_text())["migrations"]


def _real(entry: dict) -> bool:
    """Whether the transaction created a pool; the fixture also holds two that did not."""
    label = entry["label"]
    return "already migrated" not in label and not label.startswith("failed")


def _expected(entry: dict) -> tuple[str, str, str] | None:
    """(mint, pool, quote mint) from pump.fun's own event CPI, if it ran."""
    tx = entry["get_transaction"]
    keys = _keys(tx)
    discriminator = PUMP_IDL.get_event_discriminators()["CompletePumpAmmMigrationEvent"]
    for group in tx["meta"].get("innerInstructions") or []:
        for ix in group["instructions"]:
            data = base58.b58decode(ix["data"])
            if keys[ix["programIdIndex"]] != str(PumpFunAddresses.PROGRAM):
                continue
            if data[:8] != EVENT_CPI_TAG or data[8:16] != discriminator:
                continue
            fields = PUMP_IDL.decode_event_data(data[8:])["fields"]
            quote = str(fields["quote_mint"])
            if quote == str(Pubkey.default()):
                quote = str(WSOL_MINT)
            return str(fields["mint"]), str(fields["pool"]), quote
    return None


def _create_tx() -> dict:
    """A real pump.fun create_v2, shaped as the migration fixture's entries."""
    tx = json.loads(CREATE_FIXTURE.read_text())
    return {"slot": 1, **tx}


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _triple(pool: PoolReady) -> tuple[str, str, str]:
    return str(pool.mint), str(pool.pool), str(pool.quote_mint)


# --- frames -------------------------------------------------------------------


def _logs_frame(entry: dict) -> dict:
    tx = entry["get_transaction"]
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": tx["slot"]},
                "value": {
                    "signature": entry["signature"],
                    "err": tx["meta"]["err"],
                    "logs": tx["meta"]["logMessages"],
                },
            },
            "subscription": 1,
        },
    }


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
    update = geyser_pb2.SubscribeUpdateDeshred()
    update.ParseFromString(base64.b64decode(entry["deshred_update_base64"]))
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
    """A geyser stub whose first stream plays frames, then ends.

    A reconnect gets nothing: frames a real stream dropped are not sent again.
    """

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


def _listener(kind: str):  # noqa: ANN202
    return ListenerFactory.create_listener(
        listener_type=kind,
        wss_endpoint="wss://rpc.offline.invalid",
        geyser_endpoint="geyser.offline.invalid:443",
        geyser_api_token="unused",  # noqa: S106 — never sent; nothing connects
        platforms=[Platform.PUMP_FUN],
    )


def _frames(kind: str, entries: list[dict]) -> list:
    if kind == "logs":
        return [_logs_frame(entry) for entry in entries]
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
    # One connection's worth of frames; a reconnect gets none.
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


# --- checks ---------------------------------------------------------------------


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def check_a_executed_listeners_report_real_migrations() -> bool:
    """A: logs, blocks and geyser report every real migration as pump.fun does."""
    problems = []
    real = [entry for entry in _migrations() if _real(entry)]
    if len(real) < 6:  # noqa: PLR2004
        problems.append(f"fixture holds only {len(real)} real migrations")
    for entry in real:
        expected = _expected(entry)
        if expected is None:
            problems.append(f"{entry['label']}: no CompletePumpAmmMigrationEvent")
            continue
        for kind in LISTENERS:
            _, pools, _ = _run(kind, _frames(kind, [entry]))
            got = [_triple(pool) for pool in pools]
            if got != [expected]:
                problems.append(f"{kind} / {entry['label']}: {got} != {[expected]}")
            if any(pool.pre_execution for pool in pools):
                problems.append(f"{kind} / {entry['label']}: marked pre-execution")
            if any(pool.signature != entry["signature"] for pool in pools):
                problems.append(f"{kind} / {entry['label']}: wrong signature")
            if any(pool.platform != Platform.PUMP_FUN for pool in pools):
                problems.append(f"{kind} / {entry['label']}: wrong platform")

    # A block holding several migrations reports each of them.
    _, pools, _ = _run("blocks", [_block_frame(real)])
    if sorted(_triple(pool) for pool in pools) != sorted(map(_expected, real)):
        problems.append(f"blocks: one block of {len(real)} gave {len(pools)} pools")
    return _report(problems)


def _with_logs(entry: dict, logs: list[str], err: object = None) -> dict:
    forged = copy.deepcopy(entry)
    forged["get_transaction"]["meta"]["logMessages"] = logs
    forged["get_transaction"]["meta"]["err"] = err
    forged["get_transaction"]["meta"]["innerInstructions"] = []
    return forged


def _create_pool_line(logs: list[str]) -> tuple[int, bytes]:
    discriminator = PUMP_SWAP_IDL.get_event_discriminators()["CreatePoolEvent"]
    for index, line in enumerate(logs):
        if line.startswith("Program data: "):
            data = base64.b64decode(line.split(": ", 1)[1])
            if data[:8] == discriminator:
                return index, data
    raise AssertionError("no CreatePoolEvent in the logs")  # noqa: TRY003


def _negatives() -> list[tuple[str, dict]]:
    """Transactions that must not report a pool on any executed listener."""
    cases = [(entry["label"], entry) for entry in _migrations() if not _real(entry)]
    real = next(entry for entry in _migrations() if _real(entry))
    logs = real["get_transaction"]["meta"]["logMessages"]

    cases.append(
        (
            "real migration marked reverted",
            _with_logs(real, logs, {"InstructionError": [3, {"Custom": 1}]}),
        )
    )

    index, payload = _create_pool_line(logs)
    line = logs[index]
    stranger = "Stranger1111111111111111111111111111111111"
    cases.append(
        (
            "CreatePoolEvent emitted by pump.fun, not PumpSwap",
            _with_logs(
                real,
                [
                    f"Program {PumpFunAddresses.PROGRAM} invoke [1]",
                    "Program log: Instruction: MigrateV2",
                    line,
                    f"Program {PumpFunAddresses.PROGRAM} success",
                ],
            ),
        )
    )
    cases.append(
        (
            "CreatePoolEvent emitted by another program",
            _with_logs(
                real,
                [
                    f"Program {PumpFunAddresses.PROGRAM} invoke [1]",
                    "Program log: Instruction: MigrateV2",
                    f"Program {stranger} invoke [2]",
                    line,
                    f"Program {stranger} success",
                    f"Program {PumpFunAddresses.PROGRAM} success",
                ],
            ),
        )
    )

    pool = bytes(Pubkey.from_string(_expected(real)[1]))
    other = bytes(Pubkey.new_unique())
    if payload.count(pool) != 1:
        raise AssertionError("the pool is not named once in the event")  # noqa: TRY003
    swapped = payload.replace(pool, other)
    non_canonical = list(logs)
    non_canonical[index] = "Program data: " + base64.b64encode(swapped).decode()
    cases.append(
        ("CreatePoolEvent naming a non-canonical pool", _with_logs(real, non_canonical))
    )
    create = _create_tx()
    cases.append(
        (
            "pump.fun create_v2",
            {
                "signature": create["transaction"]["signatures"][0],
                "label": "create",
                "get_transaction": create,
            },
        )
    )
    return cases


def check_b_executed_listeners_ignore_non_migrations() -> bool:
    """B: no pool from a no-op, a failure, a forgery or a creation."""
    problems = []
    for label, entry in _negatives():
        for kind in LISTENERS:
            _, pools, _ = _run(kind, _frames(kind, [entry]))
            if pools:
                problems.append(f"{kind} / {label}: reported {_triple(pools[0])}")
    return _report(problems)


def check_c_shreds_reports_migrate_instructions() -> bool:
    """C: shreds decodes each migrate instruction and subscribes for it."""
    problems = []
    for entry in _migrations():
        _, pools, stub = _run("shreds", _frames("shreds", [entry]))
        got = [_triple(pool) for pool in pools]
        if _real(entry):
            expected = [_expected(entry)]
        else:
            # Before execution the no-op looks like a migration: the accounts
            # its migrate instruction names.
            expected = [_instruction_triple(entry)]
        if got != expected:
            problems.append(f"shreds / {entry['label']}: {got} != {expected}")
        if not all(pool.pre_execution for pool in pools):
            problems.append(f"shreds / {entry['label']}: pre_execution unset")

        # The same instruction naming a pool that is not its mints' canonical one.
        name, _ = _migrate_instruction(entry)
        stranger = {_LAYOUTS[name][2]: bytes(Pubkey.new_unique())}
        _, pools, _ = _run("shreds", [_forged_deshred(entry, stranger)])
        if pools:
            problems.append(
                f"shreds / {entry['label']} with a non-canonical pool: "
                f"reported {_triple(pools[0])}"
            )

    request = stub.requests[0]
    migration_filters = [
        list(f.account_include)
        for name, f in request.deshred_transactions.items()
        if "migration" in name
    ]
    authority = str(PumpFunAddresses.LIQUIDITY_MIGRATOR)
    if migration_filters != [[authority]]:
        problems.append(f"migration subscription {migration_filters}")
    for entry in _migrations():
        named = _migrate_accounts(entry)
        if named is None or named[1] != authority:
            problems.append(
                f"{entry['label']}: account 1 is not the withdraw authority"
            )
    trading = [
        ix["name"]
        for ix in PUMP_IDL.idl["instructions"]
        if ix["name"].startswith(("buy", "sell"))
        and any(a["name"] == "withdraw_authority" for a in ix["accounts"])
    ]
    if trading:
        problems.append(f"trades naming the withdraw authority: {trading}")
    return _report(problems)


# Positions of base mint, quote mint and pool in each migrate instruction, from
# the IDL; legacy `migrate` names no quote mint and pairs with SOL.
_LAYOUTS = {"migrate": (2, None, 9), "migrate_v2": (2, 3, 10)}


def _migrate_instruction(entry: dict) -> tuple[str, list[str]] | None:
    """The top-level migrate instruction's name and the accounts it names."""
    tx = entry["get_transaction"]
    keys = _keys(tx)
    names = {
        bytes(PUMP_IDL.get_instruction_discriminators()[name]): name
        for name in _LAYOUTS
    }
    for ix in tx["transaction"]["message"]["instructions"]:
        data = base58.b58decode(ix["data"])
        if keys[ix["programIdIndex"]] == str(PumpFunAddresses.PROGRAM) and (
            data[:8] in names
        ):
            return names[data[:8]], [keys[i] for i in ix["accounts"]]
    return None


def _migrate_accounts(entry: dict) -> list[str] | None:
    found = _migrate_instruction(entry)
    return None if found is None else found[1]


def _instruction_triple(entry: dict) -> tuple[str, str, str]:
    """(mint, pool, quote mint) as the migrate instruction names them."""
    name, accounts = _migrate_instruction(entry)
    mint_at, quote_at, pool_at = _LAYOUTS[name]
    quote = str(WSOL_MINT) if quote_at is None else accounts[quote_at]
    return accounts[mint_at], accounts[pool_at], quote


def _forged_deshred(
    entry: dict, accounts: dict[int, bytes] | None = None
) -> geyser_pb2.SubscribeUpdateDeshred:
    """The entry's deshred frame as another transaction, with accounts swapped.

    `accounts` maps a position in the migrate instruction to the key it should
    name instead; the key table entry it points at is rewritten, wherever it
    lives (static, or loaded through a lookup table).
    """
    update = _deshred_frame(entry)
    info = update.deshred_transaction.transaction
    signature = bytes(Pubkey.new_unique()) * 2
    info.signature = signature
    info.transaction.signatures[0] = signature
    message = info.transaction.message
    tables = (
        message.account_keys,
        info.loaded_writable_addresses,
        info.loaded_readonly_addresses,
    )
    migrates = {
        bytes(PUMP_IDL.get_instruction_discriminators()[name]) for name in _LAYOUTS
    }
    program = bytes(PumpFunAddresses.PROGRAM)
    ix = next(
        ix
        for ix in message.instructions
        if bytes(message.account_keys[ix.program_id_index]) == program
        and bytes(ix.data[:8]) in migrates
    )
    for position, key in (accounts or {}).items():
        index = ix.accounts[position]
        for table in tables:
            if index < len(table):
                table[index] = key
                break
            index -= len(table)
        else:
            raise AssertionError(f"account {position} is past the key table")  # noqa: TRY003
    return update


def check_d_once_per_mint() -> bool:
    """D: one report per mint; a raising callback does not stop the listener."""
    problems = []
    real = [entry for entry in _migrations() if _real(entry)]
    for kind in (*LISTENERS, "shreds"):
        frames = _frames(kind, [real[0], real[0], real[1]])
        _, pools, _ = _run(kind, frames)
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
    return _report(problems)


def check_g_shreds_failed_attempt_does_not_hide_the_real_one() -> bool:
    """G: on shreds, attempts before the real migration do not hide it."""
    problems = []
    for entry in (e for e in _migrations() if _real(e)):
        name, _ = _migrate_instruction(entry)
        _, quote_at, pool_at = _LAYOUTS[name]
        real = _expected(entry)
        if quote_at is None:
            # Legacy migrate names no quote mint to get wrong.
            attempts = [_forged_deshred(entry)]
            expected = [real]
        else:
            mint = Pubkey.from_string(real[0])
            other = Pubkey.new_unique()
            pool = canonical_pool_address(mint, other)
            wrong = {quote_at: bytes(other), pool_at: bytes(pool)}
            attempts = [_forged_deshred(entry), _forged_deshred(entry, wrong)]
            expected = [real, (real[0], str(pool), str(other))]
        _, pools, _ = _run("shreds", [*attempts, _deshred_frame(entry)])
        got = [_triple(pool) for pool in pools]
        if got != [*expected, real]:
            problems.append(f"shreds / {entry['label']}: {got}")
        elif pools[-1].signature != entry["signature"]:
            problems.append(f"shreds / {entry['label']}: last report not the real tx")
    return _report(problems)


def check_e_default_is_unchanged() -> bool:
    """E: no pool callback, no migration parsing; creations still detected."""
    problems = []
    real = [entry for entry in _migrations() if _real(entry)]
    for kind in (*LISTENERS, "shreds"):
        listener = _listener(kind)
        parser = next(iter(listener.platform_parsers.values()))
        calls = []

        def spy(*args, calls: list = calls, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append(args)
            return []

        # The parser instance is shared by every listener, so the spies come off
        # before the next check runs.
        parser.parse_pools_ready_from_logs = spy
        parser.parse_pool_ready_from_instruction = spy
        try:
            _, pools, stub = _run(
                kind, _frames(kind, real), with_pools=False, listener=listener
            )
        finally:
            del parser.parse_pools_ready_from_logs
            del parser.parse_pool_ready_from_instruction
        if calls or pools:
            problems.append(f"{kind}: parsed migrations without a pool callback")
        if kind == "shreds":
            names = list(stub.requests[0].deshred_transactions)
            if names != [f"platform_filter_{Platform.PUMP_FUN.value}"]:
                problems.append(f"shreds subscription without pools: {names}")

    create = _create_tx()
    entry = {
        "signature": create["transaction"]["signatures"][0],
        "label": "create",
        "get_transaction": create,
    }
    created = _listener("logs").platform_parsers[Platform.PUMP_FUN]
    mint = created.parse_token_creation_from_logs(
        create["meta"]["logMessages"], ""
    ).mint
    for kind in LISTENERS:
        tokens, pools, _ = _run(kind, _frames(kind, [entry]))
        if [t.mint for t in tokens] != [mint] or pools:
            problems.append(f"{kind}: create gave tokens {tokens}, pools {pools}")
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
        if (
            entry["get_transaction"]["transaction"]["signatures"][0]
            != entry["signature"]
        ):
            problems.append(f"{entry['label']}: json signature differs")
    return _report(problems)


CHECKS = [
    check_a_executed_listeners_report_real_migrations,
    check_b_executed_listeners_ignore_non_migrations,
    check_c_shreds_reports_migrate_instructions,
    check_d_once_per_mint,
    check_e_default_is_unchanged,
    check_f_fixture_is_sound,
    check_g_shreds_failed_attempt_does_not_hide_the_real_one,
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

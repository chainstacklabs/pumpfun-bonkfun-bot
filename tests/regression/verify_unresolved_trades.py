"""Verify a sale or burn whose outcome is unknown is resolved before the coin moves again.

When a sale or a burn is sent but never confirmed, it may still land. Until
it is known not to, selling the position again could sell coins the wallet
holds for another reason, and a later burn could take them. The positions
file keeps such a trade's signature and expiry height, and both the bot and
`pump_bot_positions sell-all` resolve it first.

Offline machine checks, no network and no funds moved:

  1. The positions file keeps an unresolved trade's kind, signature, expiry
     height and when it was marked, and clears it on request.
  2. A lookup says SUCCESS or REVERTED once a node does; "can no longer land"
     only when the block height, read before the signature, is past the
     expiry height and no node knows it; otherwise still unknown. Without an
     expiry height a trade counts as expired once `NO_EXPIRY_AFTER` has passed.
  3. An exit sale the bot gives up on as unresolved is recorded with its
     signature, and session-end burning leaves that position alone.
  4. On start the bot resolves such a record first: a sale that landed takes
     what it sold off the position before the rest resumes; one that did not
     land clears the record and the position resumes; one still unknown is
     not resumed this run; a burn that landed drops the position.
  5. `sell-all` does the same before selling: an earlier sale that landed is
     settled, and coins bought by hand stay unsold; one that expired lets the
     position be sold again. Its own unresolved sale is recorded with the
     expiry height, so a later run can resolve it.

Usage:
    uv run tests/regression/verify_unresolved_trades.py
"""

import asyncio
import io
import struct
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

import positions_cli  # noqa: E402
from core import session  # noqa: E402
from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading import trade_log, unresolved  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.unresolved import BURN, SALE, Unresolved  # noqa: E402

AMOUNT_RAW = 1_000_000_000
PRICE = 2.0e-6
WALLET = Keypair().pubkey()
T22 = SystemAddresses.TOKEN_2022_PROGRAM
SUCCESS, REVERTED = ConfirmationStatus.SUCCESS, ConfirmationStatus.REVERTED
UNKNOWN = ConfirmationStatus.UNCONFIRMED


class _Client:
    """Signature outcomes, block heights, sales read back and token balances."""

    def __init__(self) -> None:
        self.outcomes: dict[str, ConfirmationStatus | None] = {}
        self.height = 0
        self.calls: list[str] = []
        self.sold: dict[str, int] = {}  # signature -> raw tokens it sold
        self.balances: dict[Pubkey, int] = {}

    async def get_block_height_and_slot(self) -> tuple[int, int]:
        self.calls.append("height")
        return self.height, 0

    async def get_signature_outcome(
        self, signature: str, **_k: object
    ) -> tuple[ConfirmationStatus | None, int | None]:
        self.calls.append("signature")
        return self.outcomes.get(signature), None

    async def get_trade_deltas(
        self, signature: str, *_a: object, **_k: object
    ) -> TradeDeltas | None:
        if signature not in self.sold:
            return None
        return TradeDeltas(
            tokens_raw=-self.sold[signature],
            quote_raw=1_000_000,
            network_fee_lamports=5_000,
        )

    async def get_account_info(self, key: Pubkey, **_k: object) -> SimpleNamespace:
        for mint, held in self.balances.items():
            if key == mint:
                return SimpleNamespace(owner=T22, data=b"")
            if key == get_associated_token_address(WALLET, mint, T22):
                data = bytes(64) + struct.pack("<Q", held) + bytes(165 - 72)
                return SimpleNamespace(owner=T22, data=data)
        raise ValueError(f"Account {key} not found")  # noqa: TRY003


def _coin() -> TokenInfo:
    return TokenInfo(
        name="U",
        symbol="U",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        token_program_id=T22,
        quote_mint=WSOL_MINT,
    )


def _buy(coin: TokenInfo) -> TradeResult:
    return TradeResult(
        success=True,
        platform=coin.platform,
        tx_signature="buy",
        amount=AMOUNT_RAW / 1e6,
        amount_raw=AMOUNT_RAW,
        price=1.0e-6,
    )


def _store(tmp: str, *coins: TokenInfo) -> PositionStore:
    store = PositionStore(Path(tmp) / "positions" / "bot.json")
    for coin in coins:
        store.add(coin, _buy(coin), datetime.now(UTC).replace(tzinfo=None), WALLET)
    return store


def _record(store: PositionStore, coin: TokenInfo) -> object:
    return {p.token_info.mint: p for p in PositionStore.peek(store.path)}.get(coin.mint)


def check_store_roundtrip() -> bool:
    coin = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, coin)
        store.mark_unresolved(coin.mint, Unresolved(SALE, "sig-a", 321))
        marked = _record(store, coin).unresolved
        store.clear_unresolved(coin.mint)
        cleared = _record(store, coin).unresolved
    ok = (
        marked is not None
        and (marked.kind, marked.signature, marked.last_valid_block_height)
        == (SALE, "sig-a", 321)
        and marked.marked_at is not None
        and cleared is None
    )
    return _check(
        "the file keeps an unresolved trade and clears it",
        ok,
        f"{marked!r} / {cleared!r}",
    )


async def check_look_up() -> bool:
    client = _Client()
    client.height = 500
    client.outcomes = {"landed": SUCCESS, "reverted": REVERTED, "seen": UNKNOWN}
    cases = {
        "landed": (Unresolved(SALE, "landed", 100), SUCCESS),
        "reverted": (Unresolved(SALE, "reverted", 100), REVERTED),
        "unknown, expired": (Unresolved(SALE, "gone", 100), None),
        "unknown, not expired": (Unresolved(SALE, "gone", 900), UNKNOWN),
        "seen, unconfirmed": (Unresolved(SALE, "seen", 100), UNKNOWN),
        "no expiry height, old": (
            Unresolved(SALE, "gone", None, marked_at=time.time() - 3600),
            None,
        ),
        "no expiry height, fresh": (Unresolved(SALE, "gone", None), UNKNOWN),
    }
    results = []
    for label, (trade, want) in cases.items():
        client.calls.clear()
        got = await unresolved.look_up(client, trade, looks=1, pause=0)
        results.append(_check(f"look-up: {label}", got is want, repr(got)))
    client.calls.clear()
    await unresolved.look_up(client, Unresolved(SALE, "gone", 100), looks=1, pause=0)
    results.append(
        _check(
            "the height is read before the signature",
            client.calls[:2] == ["height", "signature"],
            str(client.calls),
        )
    )
    return all(results)


def _manager(store: PositionStore, client: _Client) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.store = store
    manager.solana_client = client
    manager.wallet = SimpleNamespace(
        pubkey=WALLET,
        get_associated_token_address=lambda m, p=None: get_associated_token_address(
            WALLET, m, p or T22
        ),
    )
    manager.platform = Platform.PUMP_FUN
    manager.max_exit_sell_attempts = 1
    manager.price_check_interval = 0
    manager.wait_time_after_buy = 0
    manager.cleanup_mode = "post_session"
    manager.cleanup_with_priority_fee = False
    manager.cleanup_force_close_with_burn = False
    manager.cleanup_burn_given_up_below = 1.0
    manager.priority_fee_manager = None
    manager._graduated_mints = set()  # noqa: SLF001
    return manager


async def check_bot_records_unresolved_sale() -> bool:
    session.start("verify")
    trade_log.log_trade = lambda *_a, **_k: None
    coin = _coin()
    client = _Client()

    async def unverifiable(_signature: str) -> ConfirmationStatus:
        raise ConnectionError("node down")  # noqa: TRY003

    client.verify_transaction_status = unverifiable
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, coin)
        manager = _manager(store, client)

        async def execute(*_a: object, **_k: object) -> TradeResult:
            return TradeResult(
                success=False,
                platform=coin.platform,
                tx_signature="sig-exit",
                failure_reason=TradeFailureReason.UNCONFIRMED,
                last_valid_block_height=777,
            )

        manager.seller = SimpleNamespace(execute=execute)
        position = Position.create_from_buy_result(
            mint=coin.mint,
            symbol=coin.symbol,
            entry_price=PRICE,
            quantity=AMOUNT_RAW / 1e6,
            quantity_raw=AMOUNT_RAW,
        )
        await manager._run_exit_attempt(  # noqa: SLF001
            coin, position, ExitReason.STOP_LOSS, PRICE, attempt=1
        )
        recorded = _record(store, coin).unresolved
        burned: list = []

        class _Cleanup:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            async def burn_down(self, *a: object, **_k: object) -> object:
                burned.append(a)
                return None

        from trading import position_manager  # noqa: PLC0415

        original = position_manager.AccountCleanupManager
        position_manager.AccountCleanupManager = _Cleanup
        try:
            await manager.burn_given_up()
        finally:
            position_manager.AccountCleanupManager = original
    ok = (
        recorded is not None
        and (recorded.kind, recorded.signature, recorded.last_valid_block_height)
        == (SALE, "sig-exit", 777)
        and not burned
    )
    return _check(
        "an unresolved exit sale is recorded, and not burned at shutdown",
        ok,
        f"{recorded!r}, burned {burned}",
    )


async def _resume(store: PositionStore, client: _Client) -> list:
    manager = _manager(store, client)
    opened: list = []
    manager.open = lambda t, b, **_k: opened.append((t.mint, b.amount_raw))

    async def resolved(_t: TokenInfo, _label: str) -> bool:
        return True

    manager._quote_resolved = resolved  # noqa: SLF001
    [stored] = PositionStore.peek(store.path)
    await manager._resume_one(stored)  # noqa: SLF001
    return opened


async def check_bot_resolves_on_start() -> bool:
    results = []
    quarter = AMOUNT_RAW // 4
    cases = {
        # label: (trade, outcome, wallet balance, want opened, want record)
        "sale landed": (
            Unresolved(SALE, "s1", 10),
            SUCCESS,
            AMOUNT_RAW - quarter,
            [AMOUNT_RAW - quarter],
            "kept, reduced",
        ),
        "sale did not land": (
            Unresolved(SALE, "s2", 10),
            None,
            AMOUNT_RAW,
            [AMOUNT_RAW],
            "kept, cleared",
        ),
        "sale still unknown": (
            Unresolved(SALE, "s3", 10),
            UNKNOWN,
            AMOUNT_RAW,
            [],
            "kept, unresolved",
        ),
        "burn landed": (Unresolved(BURN, "b1", 10), SUCCESS, 0, [], "gone"),
    }
    for label, (trade, outcome, balance, want_opened, want_record) in cases.items():
        coin = _coin()
        client = _Client()
        client.height = 100  # past every expiry height
        client.outcomes[trade.signature] = outcome
        client.sold["s1"] = quarter
        client.balances[coin.mint] = balance
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp, coin)
            store.mark_unresolved(coin.mint, trade)
            opened = await _resume(store, client)
            record = _record(store, coin)
        if record is None:
            state = "gone"
        elif record.unresolved is not None:
            state = "kept, unresolved"
        elif record.buy_result.amount_raw < AMOUNT_RAW:
            state = "kept, reduced"
        else:
            state = "kept, cleared"
        ok = [raw for _m, raw in opened] == want_opened and state == want_record
        results.append(_check(f"bot start, {label}", ok, f"opened {opened}, {state}"))
    return all(results)


class _Seller:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.result: TradeResult | None = None

    async def execute(self, token: TokenInfo, **kw: object) -> TradeResult:
        self.calls.append(kw["token_amount_raw"])
        return self.result or TradeResult(
            success=True,
            platform=token.platform,
            tx_signature="later",
            amount=kw["token_amount_raw"] / 1e6,
            amount_raw=kw["token_amount_raw"],
            price=kw["token_price"],
        )


class _Market:
    name = "bonding curve"

    async def get_price(self, _token: TokenInfo) -> float:
        return PRICE


class _Cleanup:
    async def cleanup_ata(self, *_a: object, **_k: object) -> bool:
        return True


def _tools(client: _Client) -> SimpleNamespace:
    return SimpleNamespace(
        client=client,
        wallet=SimpleNamespace(
            pubkey=WALLET,
            get_associated_token_address=lambda m, p=None: get_associated_token_address(
                WALLET, m, p or T22
            ),
        ),
        seller=_Seller(),
        implementations=SimpleNamespace(curve_market=_Market(), graduated_market=None),
        cleanup=_Cleanup(),
        platform=Platform.PUMP_FUN,
    )


async def check_sell_all_resolves_first() -> bool:
    results = []
    # The earlier sale sold all of the position; the wallet now holds coins
    # bought by hand. They are not the bot's and stay.
    landed = _coin()
    client = _Client()
    client.height = 100
    client.outcomes["e1"] = SUCCESS
    client.sold["e1"] = AMOUNT_RAW
    client.balances[landed.mint] = AMOUNT_RAW * 2
    tools = _tools(client)
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, landed)
        store.mark_unresolved(landed.mint, Unresolved(SALE, "e1", 10))
        await positions_cli.sell_all(store.path, tools, yes=True, out=io.StringIO())
        record = _record(store, landed)
    results.append(
        _check(
            "sell-all: an earlier sale that landed is settled, extra coins stay",
            record is None and not tools.seller.calls,
            f"record {record and record.buy_result.amount_raw}, "
            f"sold {tools.seller.calls}",
        )
    )
    # The earlier sale expired unseen: the position is sold now.
    expired = _coin()
    client = _Client()
    client.height = 100
    client.balances[expired.mint] = AMOUNT_RAW
    tools = _tools(client)
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, expired)
        store.mark_unresolved(expired.mint, Unresolved(SALE, "e2", 10))
        await positions_cli.sell_all(store.path, tools, yes=True, out=io.StringIO())
    results.append(
        _check(
            "sell-all: an earlier sale that expired lets it sell again",
            tools.seller.calls == [AMOUNT_RAW],
            str(tools.seller.calls),
        )
    )
    # Its own sale goes unconfirmed: recorded with the expiry height.
    own = _coin()
    client = _Client()
    client.height = 5  # not expired yet
    client.balances[own.mint] = AMOUNT_RAW
    tools = _tools(client)
    tools.seller.result = TradeResult(
        success=False,
        platform=own.platform,
        tx_signature="own",
        failure_reason=TradeFailureReason.UNCONFIRMED,
        last_valid_block_height=50,
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, own)
        await positions_cli.sell_all(store.path, tools, yes=True, out=io.StringIO())
        recorded = _record(store, own).unresolved
    results.append(
        _check(
            "sell-all: its own unresolved sale is recorded with its expiry",
            recorded is not None
            and recorded.signature == "own"
            and recorded.last_valid_block_height == 50,  # noqa: PLR2004
            repr(recorded),
        )
    )
    return all(results)


async def main() -> int:
    print("Unresolved trades")
    positions_cli._RECHECK_SECONDS = 0  # noqa: SLF001
    unresolved.LOOKS_BEFORE_GIVING_UP = 2
    results = [
        check_store_roundtrip(),
        await check_look_up(),
        await check_bot_records_unresolved_sale(),
        await check_bot_resolves_on_start(),
        await check_sell_all_resolves_first(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Verify `pump_bot_positions sell-all` never loses track of what it sold.

A sale is settled in the positions file before anything is printed or read
back, an unconfirmed one is resolved before another can go out, and a record
stays until its account is closed, so a failed or interrupted step can always
be picked up by the next run.

Offline machine checks, no network and no funds moved:

  1. A balance of zero is read again before the record is dropped: a lagging
     node that shows a fresh buy as absent doesn't lose the coin.
  2. A coin quoted in a mint the process has not seen is priced and sold once
     its quote mint is resolved, in `status` and `sell-all`.
  3. An unconfirmed sale that landed counts as sold; one past its last valid
     block height and unknown counts as not sold; a later run checks an
     unresolved one before it sells again.
  4. A record stays, marked sold, until its account's close lands; the next
     run retries the close and only then drops it. The exit status says so.
  5. The token program read from the mint is the one the close is sent to.
  6. A sale is in the file before its report is printed, and before its
     transaction is read back: a broken pipe or a cancellation then loses
     nothing.
  7. The last sale of a position sold in parts reports the whole position.
  8. A balance read asks a node that has reached the position's last trade:
     one behind it fails the read and the record stays.

Usage:
    uv run tests/regression/verify_sell_all_settles.py
"""

import asyncio
import io
import struct
import sys
import tempfile
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
from core.pubkeys import WSOL_MINT, SystemAddresses, quote_units_per_token  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from interfaces.core import (  # noqa: E402
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from trading import trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.pnl import Realised  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402

AMOUNT_RAW = 1_000_000_000
PRICE = 2.0e-6
WALLET = Keypair().pubkey()
SPL = SystemAddresses.TOKEN_PROGRAM
T22 = SystemAddresses.TOKEN_2022_PROGRAM


class _Client:
    """Mints and this wallet's token accounts, as a node would serve them."""

    def __init__(self) -> None:
        self.programs: dict[Pubkey, Pubkey] = {}  # mint -> owning token program
        self.balances: dict[Pubkey, list[int]] = {}  # mint -> reads, last repeats
        self.quote_mints: set[Pubkey] = set()
        self.outcomes: dict[str, ConfirmationStatus | None] = {}
        self.height = 0
        self.deltas_raise: BaseException | None = None
        self.node_slot: int | None = None  # None: the node is never behind

    async def get_account_info(
        self,
        key: Pubkey,
        commitment: str | None = None,
        min_context_slot: int | None = None,
    ) -> SimpleNamespace:
        assert commitment == "confirmed"  # noqa: S101
        if (
            min_context_slot is not None
            and self.node_slot is not None
            and self.node_slot < min_context_slot
        ):
            raise RuntimeError("node behind")  # noqa: TRY003
        if key in self.quote_mints:  # a mint with 6 decimals at offset 44
            return SimpleNamespace(owner=SPL, data=bytes(44) + bytes([6]) + bytes(37))
        if key in self.programs:
            return SimpleNamespace(owner=self.programs[key], data=b"")
        for mint, program in self.programs.items():
            if key == get_associated_token_address(WALLET, mint, program):
                reads = self.balances.get(mint, [AMOUNT_RAW])
                held = reads.pop(0) if len(reads) > 1 else reads[0]
                data = bytes(64) + struct.pack("<Q", held) + bytes(165 - 72)
                return SimpleNamespace(owner=program, data=data)
        raise ValueError(f"Account {key} not found")  # noqa: TRY003

    async def get_trade_deltas(self, *_a: object, **_k: object) -> None:
        if self.deltas_raise is not None:
            raise self.deltas_raise

    async def get_signature_outcome(
        self, signature: str, **_k: object
    ) -> tuple[ConfirmationStatus | None, int | None]:
        return self.outcomes.get(signature), None

    async def get_block_height_and_slot(self) -> tuple[int, int]:
        return self.height, 0


class _Wallet:
    pubkey = WALLET

    def get_associated_token_address(
        self, mint: Pubkey, program: Pubkey | None = None
    ) -> Pubkey:
        return get_associated_token_address(WALLET, mint, program or T22)


class _Seller:
    def __init__(self) -> None:
        self.results: dict[Pubkey, TradeResult] = {}
        self.calls: list[Pubkey] = []

    async def execute(self, token: TokenInfo, **kw: object) -> TradeResult:
        self.calls.append(token.mint)
        quote_units_per_token(token.quote_mint)  # raises unless resolved
        return self.results.get(token.mint) or TradeResult(
            success=True,
            platform=token.platform,
            tx_signature="sold",
            amount=kw["token_amount_raw"] / 1e6,
            amount_raw=kw["token_amount_raw"],
            price=kw["token_price"],
        )


class _Market:
    name = "bonding curve"

    async def get_price(self, token: TokenInfo) -> float:
        quote_units_per_token(token.quote_mint)  # raises unless resolved
        return PRICE


class _Cleanup:
    def __init__(self) -> None:
        self.calls: list[tuple[Pubkey, Pubkey | None]] = []
        self.fails: set[Pubkey] = set()

    async def cleanup_ata(
        self, mint: Pubkey, program: Pubkey | None = None, **_kw: object
    ) -> bool:
        self.calls.append((mint, program))
        return mint not in self.fails


def _coin(program: Pubkey | None = T22, quote: Pubkey = WSOL_MINT) -> TokenInfo:
    return TokenInfo(
        name="C",
        symbol="C",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        token_program_id=program,
        quote_mint=quote,
    )


def _tools() -> SimpleNamespace:
    return SimpleNamespace(
        client=_Client(),
        wallet=_Wallet(),
        seller=_Seller(),
        implementations=SimpleNamespace(curve_market=_Market(), graduated_market=None),
        cleanup=_Cleanup(),
        platform=Platform.PUMP_FUN,
    )


def _store(tmp: str, tools: SimpleNamespace, *coins: TokenInfo) -> PositionStore:
    store = PositionStore(Path(tmp) / "positions" / "bot.json")
    now = datetime.now(UTC).replace(tzinfo=None)
    for coin in coins:
        tools.client.programs[coin.mint] = coin.token_program_id or SPL
        store.add(
            coin,
            TradeResult(
                success=True,
                platform=coin.platform,
                tx_signature="buy",
                amount=AMOUNT_RAW / 1e6,
                amount_raw=AMOUNT_RAW,
                price=1.0e-6,
            ),
            now,
            WALLET,
        )
    return store


async def _sell_all(
    store: PositionStore, tools: SimpleNamespace, out: io.StringIO | None = None
) -> int:
    return await positions_cli.sell_all(
        store.path, tools, yes=True, out=out or io.StringIO()
    )


def _records(store: PositionStore) -> dict:
    return {p.token_info.mint: p for p in PositionStore.peek(store.path)}


async def check_zero_read_again() -> bool:
    tools = _tools()
    lagging = _coin()
    tools.client.balances[lagging.mint] = [0, AMOUNT_RAW]
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, lagging)
        await _sell_all(store, tools)
    ok = tools.seller.calls == [lagging.mint]
    return _check(
        "a zero balance is read again before the record goes",
        ok,
        str(tools.seller.calls),
    )


async def check_quote_resolved() -> bool:
    tools = _tools()
    quote = Pubkey.new_unique()
    tools.client.quote_mints.add(quote)
    coin = _coin(quote=quote)
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, coin)
        out = io.StringIO()
        await positions_cli.status(store.path, tools, out)
        priced = "n/a" not in out.getvalue().splitlines()[-1]
        await _sell_all(store, tools)
    ok = priced and tools.seller.calls == [coin.mint]
    return _check(
        "an unseen quote mint is resolved before pricing",
        ok,
        f"priced {priced}, sold {tools.seller.calls}",
    )


def _unconfirmed(coin: TokenInfo, signature: str) -> TradeResult:
    return TradeResult(
        success=False,
        platform=coin.platform,
        tx_signature=signature,
        failure_reason=TradeFailureReason.UNCONFIRMED,
        last_valid_block_height=100,
    )


async def check_unconfirmed_resolved() -> bool:
    tools = _tools()
    landed, expired, unknown = _coin(), _coin(), _coin()
    tools.seller.results[landed.mint] = _unconfirmed(landed, "sig-landed")
    tools.seller.results[expired.mint] = _unconfirmed(expired, "sig-expired")
    tools.seller.results[unknown.mint] = _unconfirmed(unknown, "sig-unknown")
    tools.client.outcomes = {
        "sig-landed": ConfirmationStatus.SUCCESS,
        "sig-unknown": ConfirmationStatus.UNCONFIRMED,
    }
    tools.client.height = 200  # past every last valid height
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, landed, expired, unknown)
        await _sell_all(store, tools)
        first = _records(store)
        # The next run: the unknown sale is checked before anything is sent.
        tools.seller.calls.clear()
        await _sell_all(store, tools)
        second_calls = list(tools.seller.calls)
    results = [
        _check(
            "an unconfirmed sale that landed counts as sold",
            landed.mint not in first,
            str(first.get(landed.mint)),
        ),
        _check(
            "one expired and unknown counts as not sold, kept",
            expired.mint in first
            and "could not sell" in (first[expired.mint].given_up or ""),
            str(first.get(expired.mint) and first[expired.mint].given_up),
        ),
        _check(
            "one still unknown is kept, and not sold again while unknown",
            unknown.mint in first
            and getattr(first[unknown.mint].unresolved, "signature", None)
            == "sig-unknown"
            and unknown.mint not in second_calls,
            f"{first.get(unknown.mint) and first[unknown.mint].unresolved!r}, "
            f"second run sold {second_calls}",
        ),
    ]
    return all(results)


async def check_record_until_closed() -> bool:
    tools = _tools()
    coin = _coin()
    tools.cleanup.fails.add(coin.mint)
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, coin)
        code = await _sell_all(store, tools)
        kept = _records(store).get(coin.mint)
        tools.cleanup.fails.clear()
        tools.client.balances[coin.mint] = [0]
        tools.seller.calls.clear()
        code_again = await _sell_all(store, tools)
        gone = coin.mint not in _records(store)
    closes = [m for m, _p in tools.cleanup.calls if m == coin.mint]
    ok = (
        code == 1
        and kept is not None
        and kept.sold
        and code_again == 0
        and gone
        and not tools.seller.calls
        and len(closes) == 2  # noqa: PLR2004 - the failed close, then its retry
    )
    return _check(
        "a record stays, sold, until its close lands; the next run closes it",
        ok,
        f"exit {code}/{code_again}, kept sold {kept and kept.sold}, gone {gone}, "
        f"sold again {tools.seller.calls}",
    )


async def check_close_uses_mint_program() -> bool:
    tools = _tools()
    legacy = _coin(program=None)  # its mint is owned by SPL Token
    tools.client.balances[legacy.mint] = [0]
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, legacy)
        await _sell_all(store, tools)
    ok = tools.cleanup.calls == [(legacy.mint, SPL)]
    return _check(
        "the close goes to the mint's token program", ok, str(tools.cleanup.calls)
    )


class _BrokenPipe(io.StringIO):
    def write(self, text: str) -> int:
        if "sold" in text:
            raise BrokenPipeError
        return super().write(text)


async def check_settled_before_print_and_readback() -> bool:
    results = []
    for label, setup in (
        ("a broken pipe", lambda _t: _BrokenPipe()),
        (
            "a cancelled read-back",
            lambda t: (
                setattr(t.client, "deltas_raise", asyncio.CancelledError())
                or io.StringIO()
            ),
        ),
    ):
        tools = _tools()
        coin = _coin()
        tools.seller.results[coin.mint] = TradeResult(
            success=True,
            platform=coin.platform,
            tx_signature="part",
            amount=AMOUNT_RAW / 4e6,
            amount_raw=AMOUNT_RAW // 4,
            price=PRICE,
        )
        out = setup(tools)
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp, tools, coin)
            try:
                await _sell_all(store, tools, out)
            except (BrokenPipeError, asyncio.CancelledError):
                pass
            record = _records(store).get(coin.mint)
        ok = (
            record is not None
            and record.buy_result.amount_raw == AMOUNT_RAW * 3 // 4
            and record.realised is not None
            and record.realised.sales == 1
        )
        results.append(
            _check(
                f"{label} loses no settled sale or its PnL",
                ok,
                str(record and (record.buy_result.amount_raw, record.realised)),
            )
        )
    return all(results)


async def check_reads_reach_the_buy() -> bool:
    tools = _tools()
    coin = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        store = PositionStore(Path(tmp) / "positions" / "bot.json")
        tools.client.programs[coin.mint] = T22
        store.add(
            coin,
            TradeResult(
                success=True,
                platform=coin.platform,
                tx_signature="buy",
                amount=AMOUNT_RAW / 1e6,
                amount_raw=AMOUNT_RAW,
                price=1.0e-6,
                deltas=TradeDeltas(
                    tokens_raw=AMOUNT_RAW,
                    quote_raw=-1_000_000,
                    network_fee_lamports=5_000,
                    slot=1_000,
                ),
            ),
            datetime.now(UTC).replace(tzinfo=None),
            WALLET,
        )
        tools.client.node_slot = 900  # behind the buy
        out = io.StringIO()
        await _sell_all(store, tools, out)
        kept = coin.mint in _records(store) and not tools.seller.calls
        tools.client.node_slot = 1_100
        await _sell_all(store, tools)
        sold = tools.seller.calls == [coin.mint]
    return _check(
        "a node behind the buy can't drop the record; one past it sells",
        kept and sold and "unreadable" in out.getvalue(),
        f"kept {kept}, sold {sold}",
    )


async def check_whole_position_reported() -> bool:
    tools = _tools()
    coin = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, tools, coin)
        earlier = Realised()
        earlier.pnl, earlier.sales = 0.001, 1
        store.reduce(coin.mint, AMOUNT_RAW / 1e6, AMOUNT_RAW, sold=True)
        store.set_realised(coin.mint, earlier)
        out = io.StringIO()
        await _sell_all(store, tools, out)
    ok = "the whole position over 2 sales" in out.getvalue()
    return _check("the last sale reports the whole position", ok, out.getvalue())


async def main() -> int:
    print("sell-all settles every sale")
    trade_log.log_trade = lambda *_a, **_k: None
    positions_cli._RECHECK_SECONDS = 0  # noqa: SLF001
    results = [
        await check_zero_read_again(),
        await check_quote_resolved(),
        await check_unconfirmed_resolved(),
        await check_record_until_closed(),
        await check_close_uses_mint_program(),
        await check_settled_before_print_and_readback(),
        await check_whole_position_reported(),
        await check_reads_reach_the_buy(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

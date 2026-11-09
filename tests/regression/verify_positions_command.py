"""Verify `pump_bot_positions status` and `sell-all`.

`status` shows what a bot's positions file holds and what it is worth;
`sell-all` sells all of it through the bot's own seller. `status` must never
change the file, since the bot may be running; `sell-all` must never run beside
the bot, sell what the wallet no longer holds, or burn anything.

Offline machine checks, no network and no funds moved:

  1. `status` leaves the file byte for byte, creates no lock, temporary or
     set-aside file, and runs while the file is locked.
  2. `status` on an unreadable file says so in one line and exits 1, leaving it.
  3. `status` flags given-up, pending and other-wallet positions, and shows
     `n/a` for a price it can't read.
  4. `sell-all` refuses while the file is locked; asked and answered no, it
     sells nothing.
  5. A position the wallet holds none of is dropped without a sale; one held
     in part sells what is held.
  6. Pending and other-wallet positions are never sold.
  7. A partial sale leaves the rest in the file marked given up and listed
     under "Left in the wallet"; nothing is burned.
  8. Nothing printed carries the RPC endpoint or its host, even when a read
     or a sale fails with it in the error.
  9. A bot name another config in the folder shares needs `--positions`.

Usage:
    uv run tests/regression/verify_positions_command.py
"""

import asyncio
import io
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

import positions_cli  # noqa: E402
from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo, TradeFailureReason  # noqa: E402
from trading import trade_log  # noqa: E402
from trading.base import PendingBuy, TradeResult  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.run_lock import RunLock  # noqa: E402

RPC = "https://secret-host.example/abc123key"
ENTRY = 1.0e-6
NOW_PRICE = 2.0e-6
AMOUNT_RAW = 1_000_000_000  # 1000 tokens
WALLET = Keypair().pubkey()


class _Market:
    name = "bonding curve"

    def __init__(self, prices: dict) -> None:
        self.prices = prices

    async def get_price(self, token: TokenInfo) -> float:
        price = self.prices.get(token.mint, NOW_PRICE)
        if price is None:
            raise ValueError("pool not readable")  # noqa: TRY003
        return price


class _Client:
    def __init__(self, balances: dict) -> None:
        self.balances = balances

    async def get_token_account_balance(
        self, account: Pubkey, commitment: str = "confirmed"
    ) -> int:
        assert commitment == "confirmed"  # noqa: S101
        return self.balances.get(account, AMOUNT_RAW)


class _Wallet:
    pubkey = WALLET

    def get_associated_token_address(
        self, mint: Pubkey, _program: Pubkey | None = None
    ) -> Pubkey:
        return mint  # the fake client keys balances by mint


class _Seller:
    def __init__(self, sells: dict) -> None:
        self.sells = sells  # mint -> raw amount the sale sells, None to fail
        self.calls: list[tuple[Pubkey, int]] = []

    async def execute(
        self,
        token: TokenInfo,
        *,
        token_amount: float,  # noqa: ARG002 - passed by keyword
        token_price: float,
        token_amount_raw: int,
    ) -> TradeResult:
        self.calls.append((token.mint, token_amount_raw))
        sold = self.sells.get(token.mint, token_amount_raw)
        if sold is None:
            return TradeResult(
                success=False,
                platform=token.platform,
                error_message="reverted",
                failure_reason=TradeFailureReason.REVERTED,
            )
        return TradeResult(
            success=True,
            platform=token.platform,
            tx_signature="sig",
            amount=sold / 1e6,
            amount_raw=sold,
            price=token_price,
            venue="bonding curve",
        )


class _Cleanup:
    def __init__(self) -> None:
        self.closed: list[Pubkey] = []
        self.burns = 0

    async def cleanup_ata(
        self, mint: Pubkey, _program: Pubkey | None = None, **_kw: object
    ) -> bool:
        self.closed.append(mint)
        return True


def _coin(symbol: str) -> TokenInfo:
    return TokenInfo(
        name=symbol,
        symbol=symbol,
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
    )


def _buy(token: TokenInfo) -> TradeResult:
    return TradeResult(
        success=True,
        platform=token.platform,
        tx_signature="buy",
        amount=AMOUNT_RAW / 1e6,
        amount_raw=AMOUNT_RAW,
        price=ENTRY,
    )


def _file(tmp: str) -> tuple[Path, dict[str, TokenInfo]]:
    """A positions file with one coin of each kind."""
    path = Path(tmp) / "positions" / "bot.json"
    store = PositionStore(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    coins = {
        k: _coin(k.upper()[:4])
        for k in ("plain", "gaveup", "pending", "other", "gone", "part", "unpriced")
    }
    for key, token in coins.items():
        wallet = Keypair().pubkey() if key == "other" else WALLET
        pending = (
            PendingBuy(max_amount_raw=AMOUNT_RAW, last_valid_block_height=1)
            if key == "pending"
            else None
        )
        store.add(token, _buy(token), now, wallet, pending=pending)
    store.mark_given_up(coins["gaveup"].mint, "no sale after 3 attempts")
    return path, coins


def _tools(
    coins: dict, *, sells: dict | None = None, balances: dict | None = None
) -> SimpleNamespace:
    prices = {coins["unpriced"].mint: None}
    return SimpleNamespace(
        client=_Client(balances or {}),
        wallet=_Wallet(),
        seller=_Seller(sells or {}),
        implementations=SimpleNamespace(
            curve_market=_Market(prices), graduated_market=None
        ),
        cleanup=_Cleanup(),
    )


def _listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir())


async def check_status_read_only() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path, coins = _file(tmp)
        before, names = path.read_bytes(), _listing(path)
        out = io.StringIO()
        code = await positions_cli.status(path, _tools(coins), out)
        unchanged = path.read_bytes() == before and _listing(path) == names
        lock = RunLock(path)
        lock.acquire()
        try:
            locked_code = await positions_cli.status(path, _tools(coins), io.StringIO())
        finally:
            lock.release()
    ok = code == 0 and unchanged and locked_code == 0
    return _check(
        "status leaves the file alone, runs while locked",
        ok,
        f"exit {code}/{locked_code}, unchanged {unchanged}",
    )


async def check_status_unreadable() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.json"
        path.write_text("{not json")
        out = io.StringIO()
        code = await positions_cli.status(path, SimpleNamespace(), out)
        left = path.read_text() == "{not json" and _listing(path) == ["bot.json"]
    lines = out.getvalue().strip().splitlines()
    ok = code == 1 and left and len(lines) == 1
    return _check(
        "status on an unreadable file: one line, exit 1", ok, f"exit {code}, {lines}"
    )


async def check_status_flags() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path, coins = _file(tmp)
        out = io.StringIO()
        await positions_cli.status(path, _tools(coins), out)
    text = out.getvalue()

    def row(key: str) -> str:
        return next((x for x in text.splitlines() if str(coins[key].mint) in x), "")

    ok = (
        "given up: no sale after 3 attempts" in row("gaveup")
        and "pending" in row("pending")
        and "other wallet" in row("other")
        and "n/a" in row("unpriced")
        and "+100" in row("plain")  # price doubled
    )
    return _check("status flags and prices each position", ok, text)


async def check_sell_all_refuses() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path, coins = _file(tmp)
        tools = _tools(coins)
        lock = RunLock(path)
        lock.acquire()
        try:
            locked = await positions_cli.sell_all(
                path, tools, yes=True, ask=lambda _q: "y", out=io.StringIO()
            )
        finally:
            lock.release()
        declined = await positions_cli.sell_all(
            path, tools, yes=False, ask=lambda _q: "n", out=io.StringIO()
        )
    ok = locked == 1 and declined == 0 and not tools.seller.calls
    return _check(
        "sell-all refuses a locked file and a no",
        ok,
        f"locked exit {locked}, declined exit {declined}, sales {tools.seller.calls}",
    )


async def check_sell_all_sells() -> bool:
    trade_log.log_trade = lambda *_a, **_k: None
    with tempfile.TemporaryDirectory() as tmp:
        path, coins = _file(tmp)
        half = AMOUNT_RAW // 2
        tools = _tools(
            coins,
            balances={coins["gone"].mint: 0, coins["plain"].mint: half},
            sells={coins["part"].mint: AMOUNT_RAW // 4},
        )
        out = io.StringIO()
        code = await positions_cli.sell_all(
            path, tools, yes=True, ask=lambda _q: "n", out=out
        )
        left = {p.token_info.mint: p for p in PositionStore.peek(path)}
    sold = dict(tools.seller.calls)
    text = out.getvalue()
    part = left.get(coins["part"].mint)
    results = [
        _check(
            "a coin the wallet no longer holds is dropped unsold",
            coins["gone"].mint not in sold and coins["gone"].mint not in left,
            str(sold.get(coins["gone"].mint)),
        ),
        _check(
            "a coin held in part sells what is held",
            sold.get(coins["plain"].mint) == half and coins["plain"].mint not in left,
            str(sold.get(coins["plain"].mint)),
        ),
        _check(
            "pending and other-wallet positions are not sold",
            coins["pending"].mint not in sold
            and coins["other"].mint not in sold
            and coins["pending"].mint in left
            and coins["other"].mint in left,
            str(list(sold)),
        ),
        _check(
            "a partial sale keeps the rest, given up, listed",
            part is not None
            and part.given_up is not None
            and part.buy_result.amount_raw == AMOUNT_RAW - AMOUNT_RAW // 4
            and "Left in the wallet" in text
            and part.token_info.symbol in text,
            f"{part and part.given_up!r}",
        ),
        _check(
            "a coin with no price is not sold, kept and listed",
            coins["unpriced"].mint not in sold and coins["unpriced"].mint in left,
            str(list(sold)),
        ),
        _check(
            "sold coins' accounts are closed, nothing burned",
            set(tools.cleanup.closed) == {coins["plain"].mint, coins["gaveup"].mint}
            and tools.cleanup.burns == 0,
            str(tools.cleanup.closed),
        ),
        _check("exit 1 while anything is left", code == 1, str(code)),
    ]
    return all(results)


async def check_no_endpoint_printed() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path, coins = _file(tmp)
        out = io.StringIO()
        tools = _tools(coins)

        async def leaks(*_a: object, **_k: object) -> TradeResult:
            raise ConnectionError(f"cannot reach {RPC}")  # noqa: TRY003

        async def leaky_price(_token: TokenInfo) -> float:
            raise ConnectionError(f"cannot reach {RPC}")  # noqa: TRY003

        tools.seller.execute = leaks
        tools.implementations.curve_market.get_price = leaky_price
        await positions_cli.status(path, tools, out)
        await positions_cli.sell_all(path, tools, yes=True, ask=lambda _q: "y", out=out)
    text = out.getvalue()
    ok = RPC not in text and "secret-host" not in text
    return _check("nothing printed carries the endpoint", ok, "checked")


def check_shared_name() -> bool:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        bots = Path(tmp) / "bots"
        bots.mkdir()
        (bots / "a.yaml").write_text("name: Sniper\n")
        (bots / "b.yaml").write_text("name: sniper\n")
        (bots / "c.yaml").write_text("name: solo\n")
        solo = positions_cli.resolve_positions_file(
            bots / "c.yaml", {"name": "solo"}, None
        )
        results.append(
            _check(
                "a unique name gets positions/<name>.json",
                solo == Path("positions/solo.json"),
                str(solo),
            )
        )
        try:
            positions_cli.resolve_positions_file(
                bots / "a.yaml", {"name": "Sniper"}, None
            )
        except SystemExit as e:
            results.append(
                _check(
                    "a shared name needs --positions",
                    "--positions" in str(e.code),
                    str(e.code),
                )
            )
        else:
            results.append(
                _check(
                    "a shared name needs --positions", passed=False, detail="resolved"
                )
            )
        given = positions_cli.resolve_positions_file(
            bots / "a.yaml", {"name": "Sniper"}, "positions/x.json"
        )
        results.append(
            _check("--positions wins", given == Path("positions/x.json"), str(given))
        )
    return all(results)


async def main() -> int:
    print("pump_bot_positions")
    results = [
        await check_status_read_only(),
        await check_status_unreadable(),
        await check_status_flags(),
        await check_sell_all_refuses(),
        await check_sell_all_sells(),
        await check_no_endpoint_printed(),
        check_shared_name(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

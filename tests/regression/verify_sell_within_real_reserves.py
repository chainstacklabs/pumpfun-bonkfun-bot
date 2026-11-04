"""Verify a pump.fun curve sell never asks for more than the curve really holds.

A sell is priced on the curve's virtual reserves but paid, fees included, out of
`real_quote_reserves`; the program reverts 6024 Overflow when the payout is
more. On a mayhem-mode coin the virtual reserves drift far above the real ones,
so selling the whole position reverted on every attempt, and again after every
restart, while part of it could have been sold.

The fixture is a real mayhem coin (`recorded_short_real_reserves_from_getaccountinfo.json`):
its curve read from mainnet, unchanged since before three full-position sells
reverted 6024 on it, those sells, and the last sell that landed on the curve.

Offline machine checks, no network and no funds moved. The sells run the real
seller, curve manager and instruction builder on that curve:

  Fixture: the landed sell shows the program's accounting (the payout is
     `amount * virtual_sol / (virtual_tokens + amount)` rounded down, comes out
     of the real reserves and the curve's lamports, and the fees out of the
     payout), and the curve keeps exactly its rent-exempt minimum beyond its
     real reserves, so they can all be paid out. The full position pays more
     than the curve holds.
  A. Selling the whole position sends the largest amount whose payout fits the
     real reserves.
  B. Its slippage floor is priced on that amount's payout from the same read
     when the caller's price is higher, and a lower caller price is kept.
  C. A sell the curve can pay for in full goes out whole, floor unchanged.
  D. A curve with no real reserves gets no transaction, and neither does a
     sell the curve would pay nothing for (its floor of one unit would revert).
  E. The capped sell reports the amount it sold.

The exit strategies then run the real position manager over that seller and
curve; the curve takes each landed sell off its reserves:

  F. After a capped sale (`tp_sl`), the position stays open and in the
     positions file with what is left, no cleanup runs, and attempts the
     drained curve can't pay send nothing and still count against
     `max_exit_sell_attempts`. Its PnL counts the part sold only.
  G. Once the curve refills, the rest is sold, the position closed, removed
     from the file and cleaned up once; the final PnL counts that sale only.
  H. The same for `time_based`: a capped sale keeps the rest held and in the
     file with no cleanup, and a refilled curve takes the rest.
  I. A capped sale whose confirmation came late is settled the same way in
     both strategies: the rest stays held, no cleanup.

Usage:
    uv run tests/regression/verify_sell_within_real_reserves.py
"""

import asyncio
import base64
import json
import logging
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.instruction_builder import (  # noqa: E402
    PumpFunInstructionBuilder,
)
from trading import platform_aware, position_manager, trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = json.loads(
    (
        Path(__file__).parent / "recorded_short_real_reserves_from_getaccountinfo.json"
    ).read_text()
)
TRADER = Pubkey.from_string("11111111111111111111111111111112")
MINT = Pubkey.from_string(FIXTURE["mint"])
CURVE = Pubkey.from_string(FIXTURE["bonding_curve"])
CURVE_DATA = base64.b64decode(FIXTURE["curve_account"]["data"])
POSITION = FIXTURE["reverted_sells"][0]["amount"]
SLIPPAGE = 0.5
EXIT_ATTEMPTS = 3
# Offsets of the reserves in BondingCurve, after the 8-byte discriminator.
VIRTUAL_TOKENS, VIRTUAL_QUOTE, REAL_QUOTE = 8, 16, 32

PROVIDER = PumpFunAddressProvider()
PARSER = get_idl_manager().get_parser(Platform.PUMP_FUN)


def _reserves(data: bytes) -> tuple[int, int, int]:
    return tuple(
        struct.unpack_from("<Q", data, o)[0]
        for o in (VIRTUAL_TOKENS, VIRTUAL_QUOTE, REAL_QUOTE)
    )


def _payout(data: bytes, amount: int) -> int:
    virtual_tokens, virtual_quote, _ = _reserves(data)
    return amount * virtual_quote // (virtual_tokens + amount)


class _Client:
    """Serves one curve; records what would have been sent."""

    def __init__(self, curve_data: bytes, status: ConfirmationStatus) -> None:
        self.curve_data = curve_data
        self.status = status
        self.sent: list = []

    def _account(self, key: Pubkey) -> SimpleNamespace | None:
        if key == CURVE:
            return SimpleNamespace(data=self.curve_data, owner=PumpFunAddresses.PROGRAM)
        if key == MINT:
            return SimpleNamespace(data=b"", owner=SystemAddresses.TOKEN_2022_PROGRAM)
        return None

    async def get_account_info(
        self,
        key: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> SimpleNamespace | None:
        return self._account(key)

    async def get_multiple_accounts(
        self,
        keys: list[Pubkey],
        commitment: str | None = None,  # noqa: ARG002
    ) -> list[SimpleNamespace | None]:
        return [self._account(k) for k in keys]

    async def build_and_send_transaction(
        self, instructions: list, *_args: object, **_kwargs: object
    ) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction_detailed(
        self, *_args: object, **_kwargs: object
    ) -> ConfirmationStatus:
        return self.status


async def _no_fee(_accounts: list) -> None:
    return None


def _token_info() -> TokenInfo:
    return TokenInfo(
        name="buzz cut",
        symbol="buzz",
        uri="",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=CURVE,
        associated_bonding_curve=PROVIDER.derive_associated_bonding_curve(
            MINT, CURVE, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        creator=TRADER,
        creator_vault=PROVIDER.derive_creator_vault(TRADER),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
        state_from_event=True,
    )


def _sell(
    amount: int,
    price: float,
    curve_data: bytes = CURVE_DATA,
    status: ConfirmationStatus = ConfirmationStatus.REVERTED,
) -> tuple[tuple[int, int] | None, object]:
    """Run the seller; return the sent sell_v2 (amount, min_sol_output) and the result."""
    client = _Client(curve_data, status)
    platform_aware.get_platform_implementations = lambda _p, _c: (
        PlatformImplementations(
            address_provider=PROVIDER,
            instruction_builder=PumpFunInstructionBuilder(PARSER),
            curve_manager=PumpFunCurveManager(client, PARSER),
            event_parser=None,
        )
    )
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=SLIPPAGE,
        max_retries=1,
    )
    result = asyncio.run(
        seller.execute(
            _token_info(),
            token_amount=amount / 1e6,
            token_price=price,
            token_amount_raw=amount,
        )
    )
    if not client.sent:
        return None, result
    ix = next(i for i in client.sent[0] if i.program_id == PumpFunAddresses.PROGRAM)
    return struct.unpack_from("<QQ", bytes(ix.data), 8), result


def _spot_price(data: bytes) -> float:
    virtual_tokens, virtual_quote, _ = _reserves(data)
    return (virtual_quote / 1e9) / (virtual_tokens / 1e6)


def check_fixture() -> bool:
    """The program's accounting, the rent margin, and a position too big to pay."""
    last = FIXTURE["last_sell"]
    before = last["reserves_before"]
    payout = (
        last["token_amount"]
        * before["virtual_sol_reserves"]
        // (before["virtual_token_reserves"] + last["token_amount"])
    )
    lamports_per_byte_year, threshold = struct.unpack_from(
        "<Qd", base64.b64decode(FIXTURE["rent_sysvar"])
    )
    rent_exempt = int((128 + len(CURVE_DATA)) * lamports_per_byte_year * threshold)
    _, _, real_quote = _reserves(CURVE_DATA)
    seller_gets = last["sol_amount"] - last["fee"] - last["creator_fee"]
    checks = {
        "payout formula": payout == last["sol_amount"],
        "real reserves pay it": before["real_sol_reserves"] - payout
        == last["real_sol_reserves_after"],
        "curve lamports pay it": last["curve_lamports_before"] - payout
        == last["curve_lamports_after"],
        "fees come out of it": last["seller_lamports_change"]
        == seller_gets - last["transaction_fee"],
        "curve unchanged since": real_quote == last["real_sol_reserves_after"]
        and FIXTURE["curve_account"]["lamports"] == last["curve_lamports_after"],
        "rent margin": FIXTURE["curve_account"]["lamports"] - real_quote == rent_exempt,
        "position too big": _payout(CURVE_DATA, POSITION) > real_quote,
    }
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        print(f"    failed: {bad}")
    return not bad


def check_a_capped_amount() -> bool:
    """A: the whole position sends the largest amount the real reserves can pay."""
    sent, _ = _sell(POSITION, _spot_price(CURVE_DATA))
    _, _, real_quote = _reserves(CURVE_DATA)
    if sent is None:
        print("    nothing sent")
        return False
    amount = sent[0]
    ok = (
        0 < amount < POSITION
        and _payout(CURVE_DATA, amount) <= real_quote
        and _payout(CURVE_DATA, amount + 1) > real_quote
    )
    if not ok:
        print(
            f"    sent {amount} of {POSITION}: pays "
            f"{_payout(CURVE_DATA, amount)}, the curve holds {real_quote}"
        )
    return ok


def check_b_floor_from_same_read() -> bool:
    """B: a higher caller price is lowered to the payout; a lower one is kept."""
    stale_high = _spot_price(CURVE_DATA) * 3
    sent, _ = _sell(POSITION, stale_high)
    if sent is None:
        print("    nothing sent")
        return False
    amount, floor = sent
    target = _payout(CURVE_DATA, amount) * (1 - SLIPPAGE)
    ok_high = abs(floor - target) <= 1
    low = _spot_price(CURVE_DATA) / 4
    sent_low, _ = _sell(POSITION, low)
    kept = int(amount / 1e6 * low * (1 - SLIPPAGE) * 1e9)
    ok_low = sent_low is not None and abs(sent_low[1] - kept) <= 1
    if not (ok_high and ok_low):
        print(
            f"    high price: floor {floor}, payout floor {target:.0f}; "
            f"low price: floor {sent_low and sent_low[1]}, caller's floor {kept}"
        )
    return ok_high and ok_low


def check_c_full_sell_unchanged() -> bool:
    """C: a sell the curve can pay for in full is sent whole at the caller's floor."""
    amount = 1_000_000_000
    price = _spot_price(CURVE_DATA)
    sent, _ = _sell(amount, price)
    floor = int(amount / 1e6 * price * (1 - SLIPPAGE) * 1e9)
    # Real reserves as large as the virtual ones: every sell fits.
    data = bytearray(CURVE_DATA)
    struct.pack_into("<Q", data, REAL_QUOTE, _reserves(CURVE_DATA)[1])
    sent_rich, _ = _sell(POSITION, price, bytes(data))
    ok = (
        sent is not None
        and sent[0] == amount
        and abs(sent[1] - floor) <= 1
        and sent_rich is not None
        and sent_rich[0] == POSITION
    )
    if not ok:
        print(f"    sent {sent}, expected ({amount}, {floor}); rich curve {sent_rich}")
    return ok


def check_d_empty_curve() -> bool:
    """D: a curve with no real reserves, or a sell it pays nothing for, sends nothing."""
    data = bytearray(CURVE_DATA)
    struct.pack_into("<Q", data, REAL_QUOTE, 0)
    sent, result = _sell(POSITION, _spot_price(CURVE_DATA), bytes(data))
    ok = sent is None and not result.success
    if not ok:
        print(f"    sent {sent}, success={result.success}")
    # One raw token: the payout rounds down to nothing, though it fits.
    if _payout(CURVE_DATA, 1) != 0:
        print(f"    one raw token pays {_payout(CURVE_DATA, 1)}, not nothing")
        return False
    sent_dust, result_dust = _sell(1, _spot_price(CURVE_DATA))
    if sent_dust is not None or result_dust.success:
        print(f"    a sell paid nothing was sent: {sent_dust}")
        ok = False
    return ok


def check_e_result_amount() -> bool:
    """E: the capped sell, once confirmed, reports what it sold."""
    sent, result = _sell(
        POSITION, _spot_price(CURVE_DATA), status=ConfirmationStatus.SUCCESS
    )
    ok = (
        sent is not None
        and result.success
        and round(result.amount * 1e6) == sent[0]
        and sent[0] < POSITION
    )
    if not ok:
        print(f"    sent {sent}, reported {result.amount}")
    return ok


class _LiveCurve(_Client):
    """A curve that pays out each sell it lands, like the program does.

    `refills` maps a 1-based sell call to the curve served from that call on.
    """

    def __init__(
        self,
        curve_data: bytes,
        status: ConfirmationStatus,
        refills: dict[int, object] | None = None,
    ) -> None:
        super().__init__(curve_data, status)
        self.refills = refills or {}

    async def build_and_send_transaction(
        self, instructions: list, *args: object, **kwargs: object
    ) -> str:
        ix = next(i for i in instructions if i.program_id == PumpFunAddresses.PROGRAM)
        amount, _ = struct.unpack_from("<QQ", bytes(ix.data), 8)
        payout = _payout(self.curve_data, amount)
        virtual_tokens, virtual_quote, real_quote = _reserves(self.curve_data)
        if payout > real_quote:
            raise ValueError("the program would revert this sell (6024)")  # noqa: TRY003
        data = bytearray(self.curve_data)
        struct.pack_into("<Q", data, VIRTUAL_TOKENS, virtual_tokens + amount)
        struct.pack_into("<Q", data, VIRTUAL_QUOTE, virtual_quote - payout)
        struct.pack_into("<Q", data, REAL_QUOTE, real_quote - payout)
        self.curve_data = bytes(data)
        return await super().build_and_send_transaction(instructions, *args, **kwargs)

    async def verify_transaction_status(self, _signature: str) -> ConfirmationStatus:
        return ConfirmationStatus.SUCCESS


def _refilled(data: bytes) -> bytes:
    """The curve with real reserves as large as its virtual ones: any sell fits."""
    out = bytearray(data)
    struct.pack_into("<Q", out, REAL_QUOTE, _reserves(data)[1])
    return bytes(out)


class _Seller:
    """The real seller, counting calls and refilling the curve on schedule."""

    def __init__(self, seller: PlatformAwareSeller, client: _LiveCurve) -> None:
        self.seller, self.client, self.results = seller, client, []

    async def execute(self, *args: object, **kwargs: object) -> TradeResult:
        refill = self.client.refills.get(len(self.results) + 1)
        if refill is not None:
            self.client.curve_data = refill(self.client.curve_data)
        result = await self.seller.execute(*args, **kwargs)
        self.results.append(result)
        return result


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _manager(
    strategy: str,
    seller: _Seller,
    client: _LiveCurve,
    implementations: PlatformImplementations,
) -> PositionManager:
    """A manager carrying what the exit strategies touch, cleanup set to burn."""
    manager = object.__new__(PositionManager)
    manager.seller = seller
    manager.solana_client = client
    manager.wallet = None
    manager.priority_fee_manager = None
    manager.platform_implementations = implementations
    manager.exit_strategy = strategy
    manager.marry_mode = False
    manager.take_profit_percentage = None
    manager.stop_loss_percentage = 0.2
    manager.max_hold_time = None
    manager.wait_time_after_buy = 0
    manager.price_check_interval = 0
    manager.max_exit_sell_attempts = EXIT_ATTEMPTS
    manager.cleanup_mode = "after_sell"
    manager.cleanup_with_priority_fee = False
    manager.cleanup_force_close_with_burn = True
    manager._graduated_mints = set()  # noqa: SLF001
    return manager


def _exit_run(
    strategy: str,
    status: ConfirmationStatus = ConfirmationStatus.SUCCESS,
    refills: dict[int, object] | None = None,
) -> SimpleNamespace:
    """Hold the whole position to its exit through the real manager and seller.

    The exit fires at once: `tp_sl` with a stop loss under the entry price,
    `time_based` with no wait. Three exit attempts, no wait between them.
    """
    client = _LiveCurve(CURVE_DATA, status, refills)
    implementations = PlatformImplementations(
        address_provider=PROVIDER,
        instruction_builder=PumpFunInstructionBuilder(PARSER),
        curve_manager=PumpFunCurveManager(client, PARSER),
        event_parser=None,
    )
    platform_aware.get_platform_implementations = lambda _p, _c: implementations
    seller = _Seller(
        PlatformAwareSeller(
            client,
            SimpleNamespace(pubkey=TRADER, keypair=None),
            SimpleNamespace(calculate_priority_fee=_no_fee),
            slippage=SLIPPAGE,
            max_retries=1,
        ),
        client,
    )
    cleanups: list = []

    async def _cleanup(*args: object) -> None:
        cleanups.append(args)

    position_manager.handle_cleanup_after_sell = _cleanup
    trade_log.log_trade = lambda *_args, **_kwargs: None

    manager = _manager(strategy, seller, client, implementations)

    spot = _spot_price(CURVE_DATA)
    # tp_sl: bought at twice the price now, so the stop loss fires at once.
    entry = spot * 2 if strategy == "tp_sl" else spot
    buy = TradeResult(
        success=True,
        platform=Platform.PUMP_FUN,
        tx_signature="STUB_BUY",
        amount=POSITION / 1e6,
        amount_raw=POSITION,
        price=entry,
    )
    token_info = _token_info()
    logs = _Logs()
    log = logging.getLogger(position_manager.__name__)
    log.addHandler(logs)
    with tempfile.TemporaryDirectory() as tmp:
        manager.store = PositionStore(Path(tmp) / "positions.json")
        manager.store.add(token_info, buy, datetime.now(UTC).replace(tzinfo=None))
        positions: list = []
        create = position_manager.Position.create_from_buy_result

        def _keep(*args: object, **kwargs: object) -> object:
            positions.append(create(*args, **kwargs))
            return positions[-1]

        position_manager.Position.create_from_buy_result = _keep
        try:
            asyncio.run(asyncio.wait_for(manager.manage(token_info, buy), 30))
        finally:
            position_manager.Position.create_from_buy_result = create
            log.removeHandler(logs)
        stored = {p.token_info.symbol: p.buy_result for p in manager.store.load()}
    sold = [
        struct.unpack_from(
            "<QQ",
            bytes(
                next(i for i in sent if i.program_id == PumpFunAddresses.PROGRAM).data
            ),
            8,
        )[0]
        for sent in client.sent
    ]
    return SimpleNamespace(
        sold=sold,
        calls=len(seller.results),
        results=seller.results,
        cleanups=len(cleanups),
        stored=stored.get("buzz"),
        position=positions[0] if positions else None,
        entry=entry,
        logs=logs.lines,
    )


def _held_after_partial(run: SimpleNamespace, label: str) -> bool:
    """One capped sale, the rest held and stored, no cleanup, bounded retries."""
    left = POSITION - run.sold[0] if run.sold else None
    stored = run.stored
    checks = {
        "one capped sale": len(run.sold) == 1 and 0 < run.sold[0] < POSITION,
        "rest still in the file": stored is not None
        and stored.amount_raw == left
        and round(stored.amount * 1e6) == left,
        "no cleanup": run.cleanups == 0,
        # The sale, then a full budget of attempts the drained curve can't pay.
        "unpayable attempts bounded": run.calls == 1 + EXIT_ATTEMPTS,
    }
    if run.position is not None:
        checks["position open with the rest"] = (
            run.position.is_active and run.position.quantity_raw == left
        )
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        print(
            f"    {label}: failed {bad}; sold {run.sold}, {run.calls} sell "
            f"call(s), stored {stored and stored.amount_raw}, "
            f"{run.cleanups} cleanup(s)"
        )
    return not bad


def _pnl_logged(run: SimpleNamespace, prefix: str, result: TradeResult) -> bool:
    """`prefix` was logged with the PnL of what `result` sold, and only that."""
    want = f"({(result.price - run.entry) * result.amount:.6f} "
    lines = [line for line in run.logs if line.startswith(prefix)]
    if not (lines and want in lines[-1]):
        print(f"    {prefix!r}: logged {lines}, expected {want}")
        return False
    return True


def check_f_tp_sl_keeps_the_rest() -> bool:
    """F: tp_sl keeps what the curve can't pay for, bounded, PnL of the sale only."""
    run = _exit_run("tp_sl")
    ok = _held_after_partial(run, "tp_sl")
    return ok and _pnl_logged(run, "PnL on the part sold", run.results[0])


def check_g_tp_sl_sells_the_rest() -> bool:
    """G: once the curve refills, tp_sl sells the rest and then cleans up."""
    run = _exit_run("tp_sl", refills={2: _refilled})
    ok = (
        len(run.sold) == 2  # noqa: PLR2004
        and sum(run.sold) == POSITION
        and run.stored is None
        and run.cleanups == 1
        and run.position is not None
        and not run.position.is_active
    )
    if not ok:
        print(
            f"    sold {run.sold} of {POSITION}, stored "
            f"{run.stored and run.stored.amount_raw}, {run.cleanups} cleanup(s)"
        )
        return False
    return _pnl_logged(run, "Final PnL", run.results[1])


def check_h_time_based() -> bool:
    """H: time_based keeps the rest after a capped sale and sells it on a refill."""
    held = _held_after_partial(_exit_run("time_based"), "time_based")
    run = _exit_run("time_based", refills={2: _refilled})
    refilled = sum(run.sold) == POSITION and run.stored is None and run.cleanups == 1
    if not refilled:
        print(
            f"    refilled: sold {run.sold}, stored "
            f"{run.stored and run.stored.amount_raw}, {run.cleanups} cleanup(s)"
        )
    return held and refilled


def check_i_late_capped_sale() -> bool:
    """I: a capped sale confirmed late keeps the rest in both strategies."""
    # Both strategies run, so a failure in either is reported.
    held = [
        _held_after_partial(
            _exit_run(strategy, ConfirmationStatus.UNCONFIRMED), f"{strategy}, late"
        )
        for strategy in ("tp_sl", "time_based")
    ]
    return all(held)


def main() -> int:
    """Run every check and report."""
    checks = [
        ("fixture: program accounting, rent margin, short curve", check_fixture),
        ("A: sells the most the real reserves can pay", check_a_capped_amount),
        (
            "B: floor priced on the same read, never raised",
            check_b_floor_from_same_read,
        ),
        ("C: a sell that fits goes out whole", check_c_full_sell_unchanged),
        ("D: an empty curve gets no transaction", check_d_empty_curve),
        ("E: the result reports the amount sold", check_e_result_amount),
        (
            "F: tp_sl keeps the rest open, bounded, PnL of the sale",
            check_f_tp_sl_keeps_the_rest,
        ),
        (
            "G: tp_sl sells the rest once the curve refills",
            check_g_tp_sl_sells_the_rest,
        ),
        ("H: time_based keeps the rest, sells it on a refill", check_h_time_based),
        ("I: a late-confirmed capped sale keeps the rest", check_i_late_capped_sale),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

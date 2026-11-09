"""Verify `cleanup.burn_given_up_below` burns only worthless given-up leftovers.

A coin the bot gave up selling stays in the wallet and the positions file,
holding its account's rent. With `cleanup.burn_given_up_below` set, the bot
burns such a coin at the end of the session when it is worth less than that
many SOL, closes its account and drops it from the file. A burn can't be
undone, so everything it is not sure of is left alone.

Offline machine checks, no network and no funds moved:

  1. A SOL-quoted coin given up this session and worth less than the
     threshold is burned, its account closed, and it leaves the file and the
     session summary. A pump.fun coin on its curve is worth what the curve can
     pay for it now, out of its real reserves, not its price: a drained curve
     makes it worthless. A graduated coin is worth its pool price.
  2. Left alone: a coin in the file not given up this session (resumed or
     still monitored), one worth the threshold or more, one quoted in another
     asset, one whose curve can't be read or whose pool prices it at zero,
     and one whose burn did not land.
  3. With the setting off nothing is burned. A burn takes no more than the
     position's recorded amount: coins bought by hand stay, and the account
     stays open around them, and session-end cleanup spares it. The decision
     rests on the balance the burn was built from. A burn that landed drops
     the position; one whose outcome is unknown stays recorded with its
     signature, to be resolved before the coin moves again; one that surely
     failed keeps the position as it was.
  4. The config is refused under `cleanup.mode: disabled`, and for zero, a
     negative number or text.

Usage:
    uv run tests/regression/verify_burn_given_up.py
"""

import asyncio
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from cleanup.manager import AccountCleanupManager, BurnResult  # noqa: E402
from config_loader import validate_cleanup_config  # noqa: E402
from core import session  # noqa: E402
from core.client import TransactionNotSent  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from trading import position_manager, universal_trader  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

THRESHOLD = 0.001  # SOL
AMOUNT = 1_000.0  # whole tokens held
AMOUNT_RAW = int(AMOUNT * 1e6)
# SPL Token instruction ids.
BURN, CLOSE = 8, 9

# A pump.fun curve whose price says the coin is worth 0.01 SOL, ten times the
# threshold, while its real reserves can pay far less.
VIRTUAL = {"virtual_token_reserves": 10**12, "virtual_quote_reserves": 10**10}
SHORT = {**VIRTUAL, "real_quote_reserves": 10_000, "graduated": False}
FULL = {**VIRTUAL, "real_quote_reserves": 10**10, "graduated": False}
DRAINED = {**VIRTUAL, "real_quote_reserves": 0, "graduated": False}
GRADUATED = {"graduated": True}
#: Raw tokens the wallet holds of a coin, where not the recorded amount.
HELD: dict[Pubkey, int] = {}
SPOT = 1.0e-5  # SOL per token on that curve, from its virtual reserves
POOL_CHEAP = THRESHOLD / AMOUNT / 10


class _Curves:
    """Serves a curve state per bonding curve; an exception is raised."""

    def __init__(self, states: dict) -> None:
        self.states = states

    async def get_pool_state(
        self, curve: Pubkey, commitment: str | None = None
    ) -> dict:
        assert commitment == "confirmed"  # noqa: S101
        state = self.states[curve]
        if isinstance(state, Exception):
            raise state
        return state


class _Cleanup:
    """Stands in for AccountCleanupManager; records each burn it is asked for.

    The burn closes the account when the wallet holds no more than it burns
    (`HELD`), and ends as `status` says.
    """

    burned: list[Pubkey] = []  # noqa: RUF012 - reset per run
    status: ConfirmationStatus | None = ConfirmationStatus.SUCCESS

    def __init__(self, *_args: object, force_burn: bool = False, **_kw: object) -> None:
        self.force_burn = force_burn

    async def burn_down(
        self, mint: Pubkey, _program: Pubkey | None, amount: int
    ) -> BurnResult:
        if not self.force_burn or amount != AMOUNT_RAW:
            return BurnResult(None, closes=True)
        _Cleanup.burned.append(mint)
        return BurnResult(
            _Cleanup.status,
            closes=HELD.get(mint, AMOUNT_RAW) <= amount,
            signature="burn-sig",
            last_valid_block_height=50,
        )


def _coin(quote: Pubkey = WSOL_MINT) -> TokenInfo:
    return TokenInfo(
        name="Left",
        symbol="LEFT",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=quote,
    )


def _buy(token: TokenInfo) -> TradeResult:
    return TradeResult(
        success=True,
        platform=token.platform,
        tx_signature="buy",
        amount=AMOUNT,
        amount_raw=AMOUNT_RAW,
        price=SPOT,
    )


def _manager(
    tmp: str,
    states: dict[Pubkey, object],
    spots: dict[Pubkey, float],
    threshold: float | None,
) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.store = PositionStore(Path(tmp) / "bot.json")
    manager.cleanup_mode = "post_session"
    manager.cleanup_with_priority_fee = True
    manager.cleanup_burn_given_up_below = threshold
    manager.solana_client = None
    manager.wallet = None
    manager.priority_fee_manager = None
    manager.platform_implementations = SimpleNamespace(curve_manager=_Curves(states))

    async def read_price(token: TokenInfo) -> float:
        return spots.get(token.mint, SPOT)

    async def held_raw(token: TokenInfo) -> int:
        return HELD.get(token.mint, AMOUNT_RAW)

    manager._read_price = read_price  # noqa: SLF001
    manager._held_raw = held_raw  # noqa: SLF001
    return manager


def _hold(manager: PositionManager, token: TokenInfo, *, given_up: bool) -> None:
    manager.store.add(token, _buy(token), datetime.now(UTC).replace(tzinfo=None))
    if given_up:
        manager._give_up(token, "attempts spent", int(AMOUNT * 1e6))  # noqa: SLF001


async def _run(
    threshold: float | None, *, lands: bool = True
) -> tuple[dict[str, bool], PositionManager]:
    """Burn over one coin per case; True where the coin was burned and dropped."""
    session.start("verify")
    _Cleanup.burned = []
    _Cleanup.status = (
        ConfirmationStatus.SUCCESS if lands else ConfirmationStatus.REVERTED
    )
    # label: (coin, curve state, pool spot price, given up, should burn)
    cases = {
        "curve pays little": (_coin(), SHORT, None, True, True),
        "drained curve": (_coin(), DRAINED, None, True, True),
        "graduated, cheap": (_coin(), GRADUATED, POOL_CHEAP, True, True),
        "not given up": (_coin(), SHORT, None, False, False),
        "curve pays enough": (_coin(), FULL, None, True, False),
        "quoted in USDC": (_coin(USDC_MINT), SHORT, None, True, False),
        "curve unreadable": (_coin(), ConnectionError("down"), None, True, False),
        "pool priced zero": (_coin(), GRADUATED, 0.0, True, False),
    }
    with tempfile.TemporaryDirectory() as tmp:
        states = {c.bonding_curve: st for c, st, _sp, _g, _b in cases.values()}
        spots = {c.mint: sp for c, _st, sp, _g, _b in cases.values() if sp is not None}
        manager = _manager(tmp, states, spots, threshold)
        for token, _st, _sp, given_up, _b in cases.values():
            _hold(manager, token, given_up=given_up)
        await manager.burn_given_up()
        left = manager.store.mints()
    # Each coin's (burn asked for, record left in the file), apart: a burn
    # that leaves its record behind must still show up.
    burned = {
        label: (token.mint in _Cleanup.burned, str(token.mint) in left)
        for label, (token, *_rest) in cases.items()
    }
    _run.expected = {label: case[4] for label, case in cases.items()}
    _run.cases = cases
    return burned, manager


async def check_burns_only_cheap_given_up() -> bool:
    burned, manager = await _run(THRESHOLD)
    results = [
        _check(
            f"{label}: {'burned' if want else 'kept'}",
            burned[label] == ((True, False) if want else (False, True)),
            f"burn asked {burned[label][0]}, record kept {burned[label][1]}",
        )
        for label, want in _run.expected.items()
    ]
    gone = {_run.cases[label][0].mint for label, want in _run.expected.items() if want}
    listed = {g.mint for g in session.current().given_up}
    results.append(
        _check(
            "a burned coin leaves the summary and the rent read",
            not (gone & listed) and not (gone & set(manager._given_up)),  # noqa: SLF001
            f"{len(gone & listed)} still listed",
        )
    )
    return all(results)


async def check_failed_burn_kept() -> bool:
    _burned, manager = await _run(THRESHOLD, lands=False)
    token = _run.cases["curve pays little"][0]
    kept = str(token.mint) in manager.store.mints()
    listed = token.mint in {g.mint for g in session.current().given_up}
    return _check(
        "a burn that did not land keeps the coin, still listed",
        kept and listed,
        f"kept {kept}, listed {listed}",
    )


async def check_extra_coins_spared() -> bool:
    results = []
    cases = {
        # label: (burn status, record kept, unresolved burn kept, spared)
        "landed": (ConfirmationStatus.SUCCESS, False, False, True),
        "outcome unknown": (ConfirmationStatus.UNCONFIRMED, True, True, True),
        "reverted": (ConfirmationStatus.REVERTED, True, False, False),
        "never sent": (None, True, False, False),
    }
    for label, (status, want_kept, want_unresolved, want_spared) in cases.items():
        session.start("verify")
        _Cleanup.burned, _Cleanup.status = [], status
        token = _coin()
        HELD[token.mint] = AMOUNT_RAW * 3  # coins bought by hand besides
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(tmp, {token.bonding_curve: SHORT}, {}, THRESHOLD)
            _hold(manager, token, given_up=True)
            await manager.burn_given_up()
            records = {
                p.token_info.mint: p for p in PositionStore.peek(manager.store.path)
            }
        record = records.get(token.mint)
        kept = record is not None
        unresolved_burn = (
            kept and record.unresolved is not None and record.unresolved.kind == "burn"
        )
        spared = token.mint in manager.spared_mints
        ok = (kept, unresolved_burn, spared) == (
            want_kept,
            want_unresolved,
            want_spared,
        )
        results.append(
            _check(
                f"extra coins, burn {label}: kept {want_kept}, recorded "
                f"{want_unresolved}, spared {want_spared}",
                ok,
                f"kept {kept}, recorded {unresolved_burn}, spared {spared}",
            )
        )
    HELD.clear()
    _Cleanup.status = ConfirmationStatus.SUCCESS
    return all(results)


async def check_cleanup_spares_remainder() -> bool:
    spared = Pubkey.new_unique()
    seen: dict = {}

    async def post_session(
        _client: object,
        _wallet: object,
        mints: list[Pubkey],
        _programs: object,
        _fees: object,
        _mode: object,
        _with_fee: object,
        _burn: object,
        sweep_except: set[Pubkey] | None = None,
        **_k: object,
    ) -> None:
        seen["mints"], seen["except"] = list(mints), set(sweep_except or ())

    async def nothing() -> None:
        return None

    trader = object.__new__(UniversalTrader)
    trader.settings = SimpleNamespace(
        cleanup_mode="post_session",
        cleanup_force_close_with_burn=True,
        cleanup_with_priority_fee=True,
    )
    trader.positions = SimpleNamespace(
        burn_given_up=nothing,
        stored_mints=set,
        spared_mints={spared},
        monitors=True,
        last_sale_slot=None,
    )
    trader.traded_mints = {spared}
    trader.traded_token_programs = {}
    trader.solana_client = SimpleNamespace(close=nothing)
    trader.wallet = None
    trader.priority_fee_manager = None
    trader.intake = SimpleNamespace(forget_unprocessed=lambda: None)
    original = universal_trader.handle_cleanup_post_session
    universal_trader.handle_cleanup_post_session = post_session
    try:
        await trader._cleanup_resources()  # noqa: SLF001
    finally:
        universal_trader.handle_cleanup_post_session = original
    ok = spared not in seen.get("mints", [spared]) and spared in seen.get(
        "except", set()
    )
    return _check("session-end cleanup leaves a spared account alone", ok, str(seen))


async def check_off() -> bool:
    _burned, _manager = await _run(None)
    return _check(
        "setting off: nothing burned", not _Cleanup.burned, str(_Cleanup.burned)
    )


def check_config() -> bool:
    results = []
    good = [("post_session", 0.001), ("after_sell", 1), ("disabled", None)]
    for mode, value in good:
        cleanup = {"mode": mode, "burn_given_up_below": value}
        try:
            validate_cleanup_config({"cleanup": cleanup})
            results.append(
                _check(f"config {mode}/{value} accepted", passed=True, detail="ok")
            )
        except ValueError as e:
            results.append(
                _check(f"config {mode}/{value} accepted", passed=False, detail=str(e))
            )
    bad = [
        ("disabled", 0.001),
        ("post_session", 0),
        ("post_session", -1),
        ("after_sell", "0.1"),
        ("post_session", float("inf")),
        ("post_session", float("nan")),
        ("post_session", True),
    ]
    for mode, value in bad:
        cleanup = {"mode": mode, "burn_given_up_below": value}
        try:
            validate_cleanup_config({"cleanup": cleanup})
        except ValueError as e:
            results.append(
                _check(f"config {mode}/{value!r} refused", passed=True, detail=str(e))
            )
        else:
            results.append(
                _check(
                    f"config {mode}/{value!r} refused", passed=False, detail="accepted"
                )
            )
    return all(results)


class _AccountClient:
    """Serves one token account holding `balance` and records what is sent."""

    def __init__(self, balance: int, *, refusals: int = 0) -> None:
        self.data = bytes(64) + struct.pack("<Q", balance) + bytes(165 - 72)
        self.sent: list = []
        self.refusals = refusals  # sends the node refuses before taking one

    async def get_account_info(self, _address: Pubkey, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(
            data=self.data, owner=SystemAddresses.TOKEN_PROGRAM, lamports=2_039_280
        )

    async def build_and_send_transaction(
        self, instructions: list, *_a: object, **_k: object
    ) -> str:
        if self.refusals:
            self.refusals -= 1
            raise TransactionNotSent("node refused it")  # noqa: TRY003
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a: object, **_k: object) -> bool:
        return True


async def _burned_and_closed(
    balance: int, *, refusals: int = 0
) -> tuple[bool, list[tuple[int, int | None]]]:
    client = _AccountClient(balance, refusals=refusals)
    account = Pubkey.new_unique()
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=None,
        get_associated_token_address=lambda _mint, _program: account,
    )
    manager = AccountCleanupManager(client, wallet, None, force_burn=True)
    result = await manager.burn_down(
        Pubkey.new_unique(), SystemAddresses.TOKEN_PROGRAM, AMOUNT_RAW
    )
    done = result.status is ConfirmationStatus.SUCCESS and result.closes == (
        balance <= AMOUNT_RAW
    )
    # (instruction id, amount for a burn)
    sent = [
        (
            bytes(ix.data)[0],
            struct.unpack_from("<Q", bytes(ix.data), 1)[0]
            if bytes(ix.data)[0] == BURN
            else None,
        )
        for tx in client.sent
        for ix in tx
    ]
    return done, sent


async def check_burn_limited_to_record() -> bool:
    session.start("verify")
    more_done, more = await _burned_and_closed(AMOUNT_RAW * 3)
    exact_done, exact = await _burned_and_closed(AMOUNT_RAW)
    refused_done, refused = await _burned_and_closed(AMOUNT_RAW * 3, refusals=1)
    a = _check(
        "coins beyond the record are kept: burn the record, leave the account",
        more_done and more == [(BURN, AMOUNT_RAW)],
        str(more),
    )
    b = _check(
        "no more than the record: burn it all and close",
        exact_done and exact == [(BURN, AMOUNT_RAW), (CLOSE, None)],
        str(exact),
    )
    c = _check(
        "a burn the node refused (nothing sent) is sent again",
        refused_done and refused == [(BURN, AMOUNT_RAW)],
        str(refused),
    )
    return a and b and c


async def main() -> int:
    print("Burn given-up leftovers")
    position_manager.AccountCleanupManager = _Cleanup
    results = [
        await check_burns_only_cheap_given_up(),
        await check_failed_burn_kept(),
        await check_off(),
        await check_extra_coins_spared(),
        await check_cleanup_spares_remainder(),
        await check_burn_limited_to_record(),
        check_config(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

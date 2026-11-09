"""Verify `cleanup.burn_given_up_below` burns only worthless given-up leftovers.

A coin the bot gave up selling stays in the wallet and the positions file,
holding its account's rent. With `cleanup.burn_given_up_below` set, the bot
burns such a coin at the end of the session when it is worth less than that
many SOL, closes its account and drops it from the file. A burn can't be
undone, so everything it is not sure of is left alone.

Offline machine checks, no network and no funds moved:

  1. A SOL-quoted coin given up this session and worth less than the
     threshold is burned, its account closed, and it leaves the file.
  2. Left alone: a coin in the file not given up this session (resumed or
     still monitored), one worth the threshold or more, one quoted in another
     asset, one whose price can't be read or reads zero, and one whose burn
     did not land.
  3. With the setting off nothing is burned.
  4. The config is refused under `cleanup.mode: disabled`, and for zero, a
     negative number or text.

Usage:
    uv run tests/regression/verify_burn_given_up.py
"""

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_cleanup_config  # noqa: E402
from core import session  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading import position_manager  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402

THRESHOLD = 0.001  # SOL
AMOUNT = 1_000.0  # whole tokens held
CHEAP = THRESHOLD / AMOUNT / 10  # worth a tenth of the threshold
DEAR = THRESHOLD / AMOUNT * 10


class _Cleanup:
    """Stands in for AccountCleanupManager; records each burn-and-close."""

    burned: list[Pubkey] = []  # noqa: RUF012 - reset per run
    lands = True

    def __init__(self, *_args: object, force_burn: bool = False, **_kw: object) -> None:
        self.force_burn = force_burn

    async def cleanup_ata(
        self, mint: Pubkey, _program: Pubkey | None = None, **_kw: object
    ) -> bool:
        if not self.force_burn:
            return False
        _Cleanup.burned.append(mint)
        return _Cleanup.lands


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
        amount_raw=int(AMOUNT * 1e6),
        price=CHEAP,
    )


def _manager(
    tmp: str, prices: dict[Pubkey, float | None], threshold: float | None
) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.store = PositionStore(Path(tmp) / "bot.json")
    manager.cleanup_mode = "post_session"
    manager.cleanup_with_priority_fee = True
    manager.cleanup_burn_given_up_below = threshold
    manager.solana_client = None
    manager.wallet = None
    manager.priority_fee_manager = None

    async def read_price(token: TokenInfo) -> float:
        price = prices[token.mint]
        if price is None:
            raise ValueError("pool not readable")  # noqa: TRY003
        return price

    manager._read_price = read_price  # noqa: SLF001
    return manager


def _hold(manager: PositionManager, token: TokenInfo, *, given_up: bool) -> None:
    manager.store.add(token, _buy(token), datetime.now(UTC).replace(tzinfo=None))
    if given_up:
        manager._give_up(token, "attempts spent", int(AMOUNT * 1e6))  # noqa: SLF001


async def _run(threshold: float | None, *, lands: bool = True) -> dict[str, bool]:
    """Burn over one coin per case; True where the coin was burned and dropped."""
    session.start("verify")
    _Cleanup.burned, _Cleanup.lands = [], lands
    cases = {
        "cheap given up": (_coin(), CHEAP, True),
        "not given up": (_coin(), CHEAP, False),
        "worth more": (_coin(), DEAR, True),
        "quoted in USDC": (_coin(USDC_MINT), CHEAP, True),
        "price unreadable": (_coin(), None, True),
        "price zero": (_coin(), 0.0, True),
    }
    with tempfile.TemporaryDirectory() as tmp:
        prices = {token.mint: price for token, price, _ in cases.values()}
        manager = _manager(tmp, prices, threshold)
        for token, _price, given_up in cases.values():
            _hold(manager, token, given_up=given_up)
        await manager.burn_given_up()
        left = manager.store.mints()
    return {
        label: token.mint in _Cleanup.burned and str(token.mint) not in left
        for label, (token, _p, _g) in cases.items()
    }


async def check_burns_only_cheap_given_up() -> bool:
    burned = await _run(THRESHOLD)
    others = {k: v for k, v in burned.items() if k != "cheap given up"}
    a = _check(
        "a cheap given-up SOL coin is burned and dropped",
        burned["cheap given up"],
        str(burned),
    )
    b = _check("every other coin is left alone", not any(others.values()), str(others))
    return a and b


async def check_failed_burn_kept() -> bool:
    session.start("verify")
    _Cleanup.burned, _Cleanup.lands = [], False
    token = _coin()
    with tempfile.TemporaryDirectory() as tmp:
        manager = _manager(tmp, {token.mint: CHEAP}, THRESHOLD)
        _hold(manager, token, given_up=True)
        await manager.burn_given_up()
        kept = str(token.mint) in manager.store.mints()
    return _check(
        "a burn that did not land keeps the coin in the file",
        kept,
        str(_Cleanup.burned),
    )


async def check_off() -> bool:
    burned = await _run(None)
    return _check("setting off: nothing burned", not _Cleanup.burned, str(burned))


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


async def main() -> int:
    print("Burn given-up leftovers")
    position_manager.AccountCleanupManager = _Cleanup
    results = [
        await check_burns_only_cheap_given_up(),
        await check_failed_burn_kept(),
        await check_off(),
        check_config(),
    ]
    if all(results):
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

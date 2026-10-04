"""Verify a pump.fun buy asks for the whole token amount, not the slippage floor.

`buy_v2` takes an exact token count and a maximum quote cost. The trader passed
it `token_amount * (1 - slippage)` as the count — a floor meant for exact-in
programs — so every pump.fun buy received that much fewer tokens than sized:
half at slippage 0.5. Slippage belongs on the cost ceiling, which is where the
cookbook buys put it.

Offline machine checks, no network and no funds moved. The buy runs through
the real pump.fun instruction builder; the `buy_v2` instruction data is decoded:

  A. extreme_fast_mode: `amount` is exactly `extreme_fast_token_amount` and
     `max_sol_cost` is the buy amount plus slippage.
  B. Regular path: `amount` is the buy amount over the curve price, unreduced,
     and `max_sol_cost` is the buy amount plus slippage.

Usage:
    uv run tests/regression/verify_pumpfun_buy_token_amount.py
"""

import asyncio
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

TRADER = Pubkey.from_string("11111111111111111111111111111112")
MINT = Pubkey.new_unique()
PROVIDER = PumpFunAddressProvider()
BUILDER = PumpFunInstructionBuilder(get_idl_manager().get_parser(Platform.PUMP_FUN))

BUY_AMOUNT = 0.002
SLIPPAGE = 0.5
FAST_TOKENS = 50_000
PRICE = 0.00000004


def _token_info() -> TokenInfo:
    bonding_curve = PROVIDER.derive_pool_address(MINT)
    return TokenInfo(
        name="T",
        symbol="T",
        uri="",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=PROVIDER.derive_associated_bonding_curve(
            MINT, bonding_curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        user=TRADER,
        creator=TRADER,
        creator_vault=PROVIDER.derive_creator_vault(TRADER),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
        state_from_event=True,
    )


class _Client:
    def __init__(self) -> None:
        self.sent: list = []

    async def build_and_send_transaction(self, instructions, *_a, **_k) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a, **_k) -> bool:
        return False


class _Curve:
    async def get_pool_state(self, _pool, commitment=None) -> dict:  # noqa: ARG002
        return {
            "price_per_token": PRICE,
            "quote_mint": WSOL_MINT,
            "creator": str(TRADER),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
        }


async def _no_fee(_accounts: list) -> None:
    return None


def _buy_v2_args(*, extreme_fast: bool) -> tuple[int, int]:
    """Run one buy and return buy_v2's (amount, max_sol_cost)."""
    platform_aware.get_platform_implementations = lambda _p, _c: SimpleNamespace(
        address_provider=PROVIDER, instruction_builder=BUILDER, curve_manager=_Curve()
    )
    client = _Client()
    buyer = PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        amount=BUY_AMOUNT,
        slippage=SLIPPAGE,
        max_retries=1,
        extreme_fast_token_amount=FAST_TOKENS,
        extreme_fast_mode=extreme_fast,
    )
    asyncio.run(buyer.execute(_token_info()))
    buy = next(ix for ix in client.sent[0] if ix.program_id == PumpFunAddresses.PROGRAM)
    return struct.unpack_from("<QQ", bytes(buy.data), 8)


def check_a_extreme_fast() -> bool:
    """A: the fixed token count, whole; slippage on the cost."""
    amount, max_cost = _buy_v2_args(extreme_fast=True)
    ok = amount == FAST_TOKENS * 10**6 and max_cost == int(
        BUY_AMOUNT * 1e9 * (1 + SLIPPAGE)
    )
    if not ok:
        print(f"    amount={amount} max_sol_cost={max_cost}")
    return ok


def check_b_regular_path() -> bool:
    """B: buy amount over price, unreduced; slippage on the cost."""
    amount, max_cost = _buy_v2_args(extreme_fast=False)
    expected = int(BUY_AMOUNT / PRICE * 10**6)
    ok = abs(amount - expected) <= 1 and max_cost == int(
        BUY_AMOUNT * 1e9 * (1 + SLIPPAGE)
    )
    if not ok:
        print(f"    amount={amount} expected {expected} max_sol_cost={max_cost}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: extreme_fast_mode buys the whole token count", check_a_extreme_fast),
        ("B: regular path buys the whole token count", check_b_regular_path),
    ]
    passed = 0
    for label, check in checks:
        ok = check()
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

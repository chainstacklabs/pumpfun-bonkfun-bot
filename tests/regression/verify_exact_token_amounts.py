"""Verify a position sells exactly what the buy received, and an exact-in buy spends what was configured.

The buy read the exact raw token count off its transaction, stored it as a float
in whole tokens, and the sell rebuilt the raw count with `int(amount * 10**6)`.
For some counts, at every position size, that comes out one raw unit short, so
the sell left dust behind and the token account could not be closed without a
burn. The same float scaling could also put an exact-in buy's spend one raw
unit below the configured amount.

Offline machine checks, no network and no funds moved. Buyer and seller run
through the real pump.fun instruction builder; instruction data is decoded:

  A. The chosen raw count really is one the float round trip loses.
  B. A landed buy reports the transaction's raw token count as `amount_raw`,
     also when the spend could not be read back.
  C. When the amounts cannot be parsed back, `amount_raw` is the wallet's raw
     balance, capped at what the buy asked for.
  D. The sell's `sell_v2` amount is `amount_raw` exactly when it is passed.
  E. Both exit paths in the trader hand the raw amount to the seller, and the
     tp/sl position keeps it.
  F. An exact-in buy spends the configured amount to the raw unit.

Usage:
    uv run tests/regression/verify_exact_token_amounts.py
"""

import asyncio
import inspect
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder  # noqa: E402
from trading import platform_aware, universal_trader  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

TRADER = Pubkey.from_string("11111111111111111111111111111112")
MINT = Pubkey.new_unique()
PROVIDER = PumpFunAddressProvider()
BUILDER = PumpFunInstructionBuilder(get_idl_manager().get_parser(Platform.PUMP_FUN))

PRICE = 0.00000004
# A raw count the float round trip loses; check A proves it.
LOSSY_RAW = 65_758_096_180
EXPECTED_TOKENS = 50_000


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
    def __init__(self, *, tokens_raw=None, balance_raw=None, spent=1_000_000) -> None:
        self.sent: list = []
        self.tokens_raw = tokens_raw
        self.balance_raw = balance_raw
        self.spent = spent

    async def build_and_send_transaction(self, instructions, *_a, **_k) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a, **_k) -> bool:
        return True

    async def confirm_transaction_detailed(self, *_a, **_k):
        return ConfirmationStatus.SUCCESS

    async def get_buy_transaction_details(self, *_a, **_k):
        if self.tokens_raw is None:
            return None, None
        return self.tokens_raw, self.spent

    async def get_token_account_balance(self, _ata) -> int:
        return self.balance_raw

    async def get_sell_transaction_details(self, *_a, **_k):
        return None, None


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


def _wire(builder=BUILDER) -> None:
    platform_aware.get_platform_implementations = lambda _p, _c: SimpleNamespace(
        address_provider=PROVIDER,
        instruction_builder=builder,
        curve_manager=_Curve(),
        graduated_market=None,
    )


def _wallet() -> SimpleNamespace:
    return SimpleNamespace(
        pubkey=TRADER,
        keypair=None,
        get_associated_token_address=lambda mint, program: Pubkey.find_program_address(
            [bytes(TRADER), bytes(program), bytes(mint)],
            SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        )[0],
    )


def _buy(client: _Client, *, amount: float = 0.002, builder=BUILDER):
    _wire(builder)
    buyer = PlatformAwareBuyer(
        client,
        _wallet(),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        amount=amount,
        slippage=0.5,
        max_retries=1,
        extreme_fast_token_amount=EXPECTED_TOKENS,
        extreme_fast_mode=True,
    )
    return asyncio.run(buyer.execute(_token_info()))


def check_a_round_trip_is_lossy() -> bool:
    """A: the float round trip really drops a unit for this raw count."""
    rebuilt = int((LOSSY_RAW / 10**6) * 10**6)
    ok = rebuilt == LOSSY_RAW - 1
    if not ok:
        print(f"    rebuilt {rebuilt}, expected {LOSSY_RAW - 1}")
    return ok


def check_b_buy_reports_exact_raw() -> bool:
    """B: amount_raw is the transaction's raw token count, spend read or not."""
    full = _buy(_Client(tokens_raw=LOSSY_RAW))
    # No spend and no balance: only the transaction's token count is left.
    no_spend = _buy(_Client(tokens_raw=LOSSY_RAW, spent=None, balance_raw=None))
    ok = (
        full.success
        and full.amount_raw == LOSSY_RAW
        and no_spend.amount_raw == LOSSY_RAW
    )
    if not ok:
        print(f"    full={full.amount_raw} no_spend={no_spend.amount_raw}")
    return ok


def check_c_balance_fallback_is_raw() -> bool:
    """C: unparsed buy falls back to the raw balance, capped at the request."""
    held = 12_345_678_901  # below the 50,000 tokens asked for
    below = _buy(_Client(tokens_raw=None, balance_raw=held))
    above = _buy(_Client(tokens_raw=None, balance_raw=EXPECTED_TOKENS * 10**6 + 999))
    ok = below.amount_raw == held and above.amount_raw == EXPECTED_TOKENS * 10**6
    if not ok:
        print(f"    below={below.amount_raw} above={above.amount_raw}")
    return ok


def _sell_amount(**kwargs) -> int:
    _wire()
    client = _Client()
    seller = PlatformAwareSeller(
        client,
        _wallet(),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=0.5,
        max_retries=1,
    )
    asyncio.run(seller.execute(_token_info(), token_price=PRICE, **kwargs))
    sell = next(
        ix for ix in client.sent[0] if ix.program_id == PumpFunAddresses.PROGRAM
    )
    return struct.unpack_from("<Q", bytes(sell.data), 8)[0]


def check_d_sell_uses_exact_raw() -> bool:
    """D: sell_v2 sells amount_raw exactly; without it, the old floor stays."""
    exact = _sell_amount(token_amount=LOSSY_RAW / 10**6, token_amount_raw=LOSSY_RAW)
    legacy = _sell_amount(token_amount=LOSSY_RAW / 10**6)
    ok = exact == LOSSY_RAW and legacy == LOSSY_RAW - 1
    if not ok:
        print(f"    exact={exact} legacy={legacy}")
    return ok


def check_e_trader_passes_raw() -> bool:
    """E: both exit paths hand the raw amount on; the position keeps it."""
    source = inspect.getsource(universal_trader)
    calls = source.count("self.seller.execute(")
    with_raw = source.count("token_amount_raw=buy_result.amount_raw") + source.count(
        "token_amount_raw=position.quantity_raw"
    )
    keeps = "quantity_raw=buy_result.amount_raw" in source
    ok = calls == 2 and with_raw == 2 and keeps
    if not ok:
        print(f"    seller calls={calls} passing raw={with_raw} position keeps={keeps}")
    return ok


class _ExactInBuilder:
    """Stands in for LaunchLab's builder: exact-in, records what it is asked to spend."""

    spends_exact_amount_in = True

    def __init__(self) -> None:
        self.amount_in: int | None = None

    async def build_buy_instruction(self, _info, _user, amount_in, _min_out, _provider):
        self.amount_in = amount_in
        return []

    def get_required_accounts_for_buy(self, *_a) -> list:
        return []

    def get_buy_compute_unit_limit(self, *_a) -> int:
        return 100_000


def check_f_exact_in_spend() -> bool:
    """F: an exact-in buy spends the configured amount to the raw unit."""
    amount = 0.00013  # 130,000 lamports; as a float product, 129,999
    builder = _ExactInBuilder()
    _buy(_Client(tokens_raw=1), amount=amount, builder=builder)
    ok = int(amount * 10**9) == 129_999 and builder.amount_in == 130_000
    if not ok:
        print(f"    float={int(amount * 10**9)} spent={builder.amount_in}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        (
            "A: the chosen raw count is lossy through a float",
            check_a_round_trip_is_lossy,
        ),
        ("B: a landed buy reports the exact raw count", check_b_buy_reports_exact_raw),
        (
            "C: the balance fallback reports a raw count",
            check_c_balance_fallback_is_raw,
        ),
        ("D: the sell uses the exact raw count", check_d_sell_uses_exact_raw),
        ("E: the trader passes the raw count to every sell", check_e_trader_passes_raw),
        ("F: an exact-in buy spends the configured amount", check_f_exact_in_spend),
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

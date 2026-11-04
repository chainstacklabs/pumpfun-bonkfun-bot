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
  D. A curve with no real reserves gets no transaction.
  E. The capped sell reports the amount it sold.

Usage:
    uv run tests/regression/verify_sell_within_real_reserves.py
"""

import asyncio
import base64
import json
import struct
import sys
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
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
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
    """D: a curve with no real reserves gets no transaction."""
    data = bytearray(CURVE_DATA)
    struct.pack_into("<Q", data, REAL_QUOTE, 0)
    sent, result = _sell(POSITION, _spot_price(CURVE_DATA), bytes(data))
    ok = sent is None and not result.success
    if not ok:
        print(f"    sent {sent}, success={result.success}")
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

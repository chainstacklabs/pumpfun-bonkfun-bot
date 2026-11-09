"""Verify every pump.fun buy and sell pays the creator the curve names now.

A coin with creator-fee sharing can have `BondingCurve.creator` rewritten after
launch, by a separate transaction, to its fee-sharing config. The listener's
`creator` is then stale, and an instruction whose `creator_vault` is derived
from it reverts `ConstraintSeeds` (2006). The regular buy path read the curve
and took its flags and quote mint but kept the listener's creator, so it
reverted on such coins although it held the current creator.

The fixture is a real coin (`recorded_creator_rewrite_from_getaccountinfo.json`):
its CreateEvent creator, its bonding curve read from mainnet after the rewrite,
and the `creator_vault` the program asked for when a buy built from the
CreateEvent creator reverted.

Offline machine checks, no network and no funds moved. Each check runs the
real pump.fun curve manager and instruction builder on that curve, starting
from the TokenInfo the logs listener built:

  A. Regular path (extreme_fast_mode off): the `buy_v2` names the vault of the
     curve's creator, the one the program expected.
  B. extreme_fast_mode with a curve refresh: the same.
  C. The sell: the same.

Usage:
    uv run tests/regression/verify_buy_uses_curve_creator.py
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

from _fakes import _no_fee  # noqa: E402
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
    _BUY_V2_ACCOUNTS,
    _SELL_V2_ACCOUNTS,
    PumpFunInstructionBuilder,
)
from trading import platform_aware  # noqa: E402
from trading.platform_aware import (  # noqa: E402
    PlatformAwareBuyer,
    PlatformAwareSeller,
)
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = json.loads(
    (
        Path(__file__).parent / "recorded_creator_rewrite_from_getaccountinfo.json"
    ).read_text()
)
TRADER = Pubkey.from_string("11111111111111111111111111111112")
MINT = Pubkey.from_string(FIXTURE["mint"])
CURVE = Pubkey.from_string(FIXTURE["bonding_curve"])
EVENT_CREATOR = Pubkey.from_string(FIXTURE["event_creator"])
EXPECTED_VAULT = Pubkey.from_string(FIXTURE["program_expected_creator_vault"])
CURVE_DATA = base64.b64decode(FIXTURE["curve_account"]["data"])
# Byte offsets in a pump.fun BondingCurve account.
CURVE_VIRTUAL_QUOTE, CURVE_REAL_QUOTE = 16, 32

PROVIDER = PumpFunAddressProvider()
PARSER = get_idl_manager().get_parser(Platform.PUMP_FUN)
BUY_VAULT_INDEX = [name for name, _ in _BUY_V2_ACCOUNTS].index("creator_vault")
SELL_VAULT_INDEX = [name for name, _ in _SELL_V2_ACCOUNTS].index("creator_vault")


def _listener_token_info(*, state_from_event: bool) -> TokenInfo:
    """The TokenInfo the logs listener built from the coin's CreateEvent."""
    return TokenInfo(
        name="CLAWD",
        symbol="CLAWD",
        uri="",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=CURVE,
        associated_bonding_curve=PROVIDER.derive_associated_bonding_curve(
            MINT, CURVE, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        user=EVENT_CREATOR,
        creator=EVENT_CREATOR,
        creator_vault=PROVIDER.derive_creator_vault(EVENT_CREATOR),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
        state_from_event=state_from_event,
    )


class _Client:
    """Serves the recorded curve; records what would have been sent."""

    def __init__(self, curve_data: bytes = CURVE_DATA) -> None:
        self.sent: list = []
        self.curve_data = curve_data

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
        return ConfirmationStatus.REVERTED


def _install(client: _Client) -> None:
    platform_aware.get_platform_implementations = lambda _p, _c: (
        PlatformImplementations(
            address_provider=PROVIDER,
            instruction_builder=PumpFunInstructionBuilder(PARSER),
            curve_manager=PumpFunCurveManager(client, PARSER),
            event_parser=None,
        )
    )


def _sent_vault(client: _Client, index: int) -> Pubkey | None:
    if not client.sent:
        return None
    ix = next(i for i in client.sent[0] if i.program_id == PumpFunAddresses.PROGRAM)
    return ix.accounts[index].pubkey


def _buy_vault(*, extreme_fast: bool, state_from_event: bool) -> Pubkey | None:
    client = _Client()
    _install(client)
    buyer = PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        amount=0.002,
        slippage=0.3,
        max_retries=1,
        extreme_fast_token_amount=50_000,
        extreme_fast_mode=extreme_fast,
        curve_refresh_budget=0.3,
    )
    asyncio.run(buyer.execute(_listener_token_info(state_from_event=state_from_event)))
    return _sent_vault(client, BUY_VAULT_INDEX)


def _report(vault: Pubkey | None) -> bool:
    ok = vault == EXPECTED_VAULT
    if not ok:
        stale = PROVIDER.derive_creator_vault(EVENT_CREATOR)
        why = "the CreateEvent creator's" if vault == stale else "unexpected"
        print(f"    creator_vault={vault} ({why}), program expects {EXPECTED_VAULT}")
    return ok


def check_fixture_is_a_rewrite() -> bool:
    """The recorded curve names a creator other than the CreateEvent's."""
    state = asyncio.run(PumpFunCurveManager(_Client(), PARSER).get_pool_state(CURVE))
    creator = Pubkey.from_string(str(state["creator"]))
    ok = (
        creator != EVENT_CREATOR
        and PROVIDER.derive_creator_vault(creator) == EXPECTED_VAULT
        and Pubkey.from_string(FIXTURE["reverted_buy_creator_vault"])
        == PROVIDER.derive_creator_vault(EVENT_CREATOR)
        and not state["complete"]
    )
    if not ok:
        print(f"    curve creator={creator} complete={state['complete']}")
    return ok


def check_a_regular_path() -> bool:
    """A: extreme_fast_mode off buys with the curve's creator vault."""
    return _report(_buy_vault(extreme_fast=False, state_from_event=True))


def check_b_extreme_fast_refresh() -> bool:
    """B: extreme_fast_mode, when it reads the curve, uses its creator vault."""
    return _report(_buy_vault(extreme_fast=True, state_from_event=False))


def _full_reserves(data: bytes) -> bytes:
    """The curve with real quote reserves as large as its virtual ones.

    The recorded curve holds almost nothing, so a sell of any size would be cut
    to a part worth less than its fee and not sent.
    """
    rich = bytearray(data)
    virtual_quote = struct.unpack_from("<Q", data, CURVE_VIRTUAL_QUOTE)[0]
    struct.pack_into("<Q", rich, CURVE_REAL_QUOTE, virtual_quote)
    return bytes(rich)


def check_c_sell() -> bool:
    """C: the sell uses the curve's creator vault."""
    client = _Client(_full_reserves(CURVE_DATA))
    _install(client)
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=0.5,
        max_retries=1,
    )
    asyncio.run(
        seller.execute(
            _listener_token_info(state_from_event=True),
            token_amount=1000.0,
            token_price=2.8e-08,
        )
    )
    return _report(_sent_vault(client, SELL_VAULT_INDEX))


def main() -> int:
    """Run every check and report."""
    checks = [
        ("fixture: the curve's creator was rewritten", check_fixture_is_a_rewrite),
        ("A: regular buy pays the curve's creator vault", check_a_regular_path),
        (
            "B: extreme_fast_mode refresh pays the curve's creator vault",
            check_b_extreme_fast_refresh,
        ),
        ("C: sell pays the curve's creator vault", check_c_sell),
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

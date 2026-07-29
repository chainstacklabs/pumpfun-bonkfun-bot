"""Verify the pumpportal-sourced buy path hardening from issue #170.

Offline machine checks, no network and no funds moved:

  A. The pumpportal processor derives bonding_curve from the mint instead of
     trusting the payload's bondingCurveKey (observed stale once in #170).
  B. In extreme_fast_mode, a buy is SKIPPED when the curve state cannot be
     read within the refresh budget, instead of submitting a buy built from
     listener-guessed defaults (the "racing a doomed buy" failure).
  C. The curve refresh reads curve + mint in one slot-consistent
     getMultipleAccounts round trip and corrects token_program_id (pumpportal
     cannot know it and guesses Token-2022; legacy coins are SPL Token).

Usage:
    uv run learning-examples/verify_pumpportal_buy_path.py
"""

import asyncio
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.pumpportal_processor import (  # noqa: E402
    PumpFunPumpPortalProcessor,
)
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
TRADER = Pubkey.from_string("11111111111111111111111111111112")
WRONG_BC = Pubkey.from_string("Vote111111111111111111111111111111111111111")

PROVIDER = PumpFunAddressProvider()


def _fabricated_curve_bytes(creator: Pubkey, *, is_mayhem: bool) -> bytes:
    """Build a 151-byte BondingCurve account image matching the IDL layout."""
    idl = json.loads((PROJECT_ROOT / "idl" / "pump_fun_idl.json").read_text())
    disc = next(
        bytes(a["discriminator"])
        for a in idl["accounts"]
        if a["name"] == "BondingCurve"
    )
    reserves = struct.pack(
        "<QQQQQ",
        1_000_000_000_000,  # virtual_token_reserves
        30_000_000_000,  # virtual_quote_reserves
        800_000_000_000,  # real_token_reserves
        0,  # real_quote_reserves
        1_000_000_000_000,  # token_total_supply
    )
    return (
        disc
        + reserves
        + b"\x00"  # complete
        + bytes(creator)
        + (b"\x01" if is_mayhem else b"\x00")  # is_mayhem_mode
        + b"\x00"  # is_cashback_coin
        + bytes(32)  # quote_mint = Pubkey::default() (SOL-paired)
        + bytes(36)  # reserved padding
    )


def _pumpportal_token_info(**overrides: object) -> TokenInfo:
    """TokenInfo shaped like the pumpportal processor's output."""
    bonding_curve = PROVIDER.derive_pool_address(MINT)
    defaults: dict = {
        "name": "T",
        "symbol": "T",
        "uri": "",
        "mint": MINT,
        "platform": Platform.PUMP_FUN,
        "bonding_curve": bonding_curve,
        "associated_bonding_curve": PROVIDER.derive_associated_bonding_curve(
            MINT, bonding_curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        "user": TRADER,
        "creator": TRADER,
        "creator_vault": PROVIDER.derive_creator_vault(TRADER),
        "token_program_id": SystemAddresses.TOKEN_2022_PROGRAM,
    }
    defaults.update(overrides)
    return TokenInfo(**defaults)


class _StubClient:
    """Records submissions; never touches the network."""

    def __init__(self) -> None:
        self.sent: list = []

    async def build_and_send_transaction(
        self, instructions: list, *_args: object, **_kwargs: object
    ) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, _signature: str, **_kwargs: object) -> bool:
        return False


def _stub_implementations(curve_manager: object) -> SimpleNamespace:
    async def build_buy_instruction(*_args: object, **_kwargs: object) -> list[str]:
        return ["stub-instruction"]

    instruction_builder = SimpleNamespace(
        build_buy_instruction=build_buy_instruction,
        get_required_accounts_for_buy=lambda *_a, **_k: [],
        get_buy_compute_unit_limit=lambda _override: 100_000,
    )
    return SimpleNamespace(
        address_provider=PROVIDER,
        instruction_builder=instruction_builder,
        curve_manager=curve_manager,
    )


def _make_buyer(client: _StubClient, **kwargs: float) -> PlatformAwareBuyer:
    async def no_fee(_accounts: list) -> None:
        return None

    fee_manager = SimpleNamespace(calculate_priority_fee=no_fee)
    return PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=TRADER, keypair=None),
        fee_manager,
        amount=0.0001,
        slippage=0.3,
        max_retries=1,
        extreme_fast_token_amount=20,
        extreme_fast_mode=True,
        **kwargs,
    )


def check_a_processor_derives_bonding_curve() -> bool:
    """A: payload bondingCurveKey is not trusted; the PDA is derived."""
    token_data = {
        "name": "T",
        "symbol": "T",
        "mint": str(MINT),
        "bondingCurveKey": str(WRONG_BC),  # deliberately stale/wrong
        "traderPublicKey": str(TRADER),
        "uri": "",
        "pool": "pump",
    }
    token_info = PumpFunPumpPortalProcessor().process_token_data(token_data)
    expected = PROVIDER.derive_pool_address(MINT)
    ok = token_info is not None and token_info.bonding_curve == expected
    if not ok:
        got = token_info.bonding_curve if token_info else None
        print(f"    expected derived BC {expected}, got {got}")
    return ok


def check_b_skips_when_curve_unreadable() -> bool:
    """B: refresh failure -> buy skipped, nothing submitted."""

    class NeverReadable:
        async def get_pool_state(
            self,
            _pool: Pubkey,
            commitment: str | None = None,  # noqa: ARG002
        ) -> dict:
            raise ValueError("Account not found")  # noqa: TRY003

    client = _StubClient()
    buyer = _make_buyer(client, curve_refresh_budget=0.3)
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        NeverReadable()
    )
    result = asyncio.run(buyer.execute(_pumpportal_token_info()))
    ok = not result.success and not client.sent
    if not ok:
        print(f"    success={result.success} submissions={len(client.sent)}")
    return ok


def check_b_still_buys_when_curve_readable() -> bool:
    """B guard: a readable curve still reaches submission."""

    class Readable:
        async def get_pool_state(
            self,
            _pool: Pubkey,
            commitment: str | None = None,  # noqa: ARG002
        ) -> dict:
            return {
                "creator": str(TRADER),
                "is_mayhem_mode": False,
                "is_cashback_coin": False,
                "quote_mint": WSOL_MINT,
            }

    client = _StubClient()
    buyer = _make_buyer(client, curve_refresh_budget=0.3)
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        Readable()
    )
    asyncio.run(buyer.execute(_pumpportal_token_info()))
    ok = len(client.sent) == 1
    if not ok:
        print(f"    submissions={len(client.sent)} (expected 1)")
    return ok


def check_c_curve_manager_batch_read() -> bool:
    """C: curve manager reads curve + mint owner in one batch call."""
    creator = TRADER
    curve_bytes = _fabricated_curve_bytes(creator, is_mayhem=True)

    class BatchClient:
        def __init__(self) -> None:
            self.batch_calls = 0

        async def get_multiple_accounts(
            self,
            pubkeys: list[Pubkey],
            commitment: str | None = None,  # noqa: ARG002
        ) -> list[SimpleNamespace]:
            self.batch_calls += 1
            if len(pubkeys) != 2:  # noqa: PLR2004
                raise ValueError("expected [curve, mint]")  # noqa: TRY003
            return [
                SimpleNamespace(data=curve_bytes, owner=PROVIDER.program_id),
                SimpleNamespace(data=b"", owner=SystemAddresses.TOKEN_PROGRAM),
            ]

    client = BatchClient()
    manager = PumpFunCurveManager(
        client, get_idl_manager().get_parser(Platform.PUMP_FUN)
    )
    if not hasattr(manager, "get_pool_state_and_token_program"):
        print("    PumpFunCurveManager.get_pool_state_and_token_program missing")
        return False
    state, token_program = asyncio.run(
        manager.get_pool_state_and_token_program(
            PROVIDER.derive_pool_address(MINT), MINT, commitment="processed"
        )
    )
    ok = (
        client.batch_calls == 1
        and token_program == SystemAddresses.TOKEN_PROGRAM
        and state.get("is_mayhem_mode") is True
        and str(state.get("creator")) == str(creator)
    )
    if not ok:
        print(
            f"    batch_calls={client.batch_calls} token_program={token_program} state={state}"
        )
    return ok


def check_c_buyer_corrects_token_program() -> bool:
    """C: buyer applies the batch-read token program and re-derives the ATA."""

    class BatchCurveManager:
        async def get_pool_state_and_token_program(
            self,
            _pool: Pubkey,
            _mint: Pubkey,
            commitment: str | None = None,  # noqa: ARG002
        ) -> tuple[dict, Pubkey]:
            state = {
                "creator": str(TRADER),
                "is_mayhem_mode": False,
                "is_cashback_coin": False,
                "quote_mint": WSOL_MINT,
            }
            return state, SystemAddresses.TOKEN_PROGRAM

        async def get_pool_state(
            self,
            _pool: Pubkey,
            commitment: str | None = None,  # noqa: ARG002
        ) -> dict:
            raise AssertionError("batch method should be preferred")  # noqa: TRY003

    client = _StubClient()
    buyer = _make_buyer(client, curve_refresh_budget=0.3)
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        BatchCurveManager()
    )
    token_info = _pumpportal_token_info()
    asyncio.run(buyer.execute(token_info))
    expected_ata = PROVIDER.derive_associated_bonding_curve(
        MINT, token_info.bonding_curve, SystemAddresses.TOKEN_PROGRAM
    )
    ok = (
        token_info.token_program_id == SystemAddresses.TOKEN_PROGRAM
        and token_info.associated_bonding_curve == expected_ata
    )
    if not ok:
        print(
            f"    token_program_id={token_info.token_program_id} "
            f"ata={token_info.associated_bonding_curve} (expected {expected_ata})"
        )
    return ok


def main() -> int:
    checks = [
        (
            "A: processor derives bonding_curve from mint",
            check_a_processor_derives_bonding_curve,
        ),
        ("B: unreadable curve -> buy skipped", check_b_skips_when_curve_unreadable),
        ("B: readable curve -> buy proceeds", check_b_still_buys_when_curve_readable),
        (
            "C: curve manager batch-reads curve + mint owner",
            check_c_curve_manager_batch_read,
        ),
        (
            "C: buyer corrects token_program_id and ATA",
            check_c_buyer_corrects_token_program,
        ),
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

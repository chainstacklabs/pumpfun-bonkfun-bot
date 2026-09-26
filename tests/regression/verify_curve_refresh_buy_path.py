"""Verify a buy off unflagged TokenInfo reads its own curve state.

A TokenInfo the listener did not mark `state_from_event` carries guessed
creator/flags/token program, so the buy path refreshes them from chain first.

Offline machine checks, no network and no funds moved:

  A. In extreme_fast_mode, a buy is SKIPPED when the curve state cannot be
     read within the refresh budget, instead of submitting a buy built from
     listener-guessed defaults (the "racing a doomed buy" failure).
  B. The curve refresh reads curve + mint in one slot-consistent
     getMultipleAccounts round trip and corrects token_program_id (the logs
     CreateEvent path guesses Token-2022; legacy coins are SPL Token).

Usage:
    uv run tests/regression/verify_curve_refresh_buy_path.py
"""

import asyncio
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
TRADER = Pubkey.from_string("11111111111111111111111111111112")

PROVIDER = PumpFunAddressProvider()


def _fabricated_curve_bytes(
    creator: Pubkey, *, is_mayhem: bool, curve_len: int = 125
) -> bytes:
    """Build a BondingCurve account image matching the IDL layout.

    `create_v2` allocates exactly 125 bytes; pass curve_len=151 to get the
    shape an account has once extend_account has run on it (padded with
    reserved zero bytes past the struct). `extend_account` can in fact grow a
    curve to 151, 256, or any other length the program allows — this helper
    only exercises 125 and 151 because those are the two shapes the checks
    below need — but every length decodes identically, since everything read
    here sits at the same offsets regardless of total size.
    """
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
    account = (
        disc
        + reserves
        + b"\x00"  # complete
        + bytes(creator)
        + (b"\x01" if is_mayhem else b"\x00")  # is_mayhem_mode
        + b"\x00"  # is_cashback_coin
        + bytes(32)  # quote_mint = Pubkey::default() (SOL-paired)
        + struct.pack("<Q", 0)  # creator_fee_bps
        + b"\x00"  # can_edit_creator_fee
        + b"\x00"  # is_holder_reward
    )
    return account + bytes(curve_len - len(account))


def _unflagged_token_info(**overrides: object) -> TokenInfo:
    """TokenInfo shaped like a listener decode that set no state_from_event."""
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


def check_a_skips_when_curve_unreadable() -> bool:
    """A: refresh failure -> buy skipped, nothing submitted."""

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
    result = asyncio.run(buyer.execute(_unflagged_token_info()))
    ok = not result.success and not client.sent
    if not ok:
        print(f"    success={result.success} submissions={len(client.sent)}")
    return ok


def check_a_still_buys_when_curve_readable() -> bool:
    """A guard: a readable curve still reaches submission."""

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
    asyncio.run(buyer.execute(_unflagged_token_info()))
    ok = len(client.sent) == 1
    if not ok:
        print(f"    submissions={len(client.sent)} (expected 1)")
    return ok


def _check_curve_manager_batch_read(curve_len: int) -> bool:
    """B: curve manager reads curve + mint owner in one batch call.

    Args:
        curve_len: Bonding curve account length to fabricate (125 or 151)
    """
    creator = TRADER
    curve_bytes = _fabricated_curve_bytes(creator, is_mayhem=True, curve_len=curve_len)

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
            f"    curve_len={curve_len} batch_calls={client.batch_calls} "
            f"token_program={token_program} state={state}"
        )
    return ok


def check_b_curve_manager_batch_read() -> bool:
    """B: curve manager decodes a curve at its as-created length (125 bytes)."""
    return _check_curve_manager_batch_read(125)


def check_b_curve_manager_batch_read_extended_curve() -> bool:
    """B: curve manager decodes a curve extend_account has grown to 151 bytes."""
    return _check_curve_manager_batch_read(151)


def check_b_buyer_corrects_token_program() -> bool:
    """B: buyer applies the batch-read token program and re-derives the ATA."""

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
    token_info = _unflagged_token_info()
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
        ("A: unreadable curve -> buy skipped", check_a_skips_when_curve_unreadable),
        ("A: readable curve -> buy proceeds", check_a_still_buys_when_curve_readable),
        (
            "B: curve manager batch-reads curve + mint owner",
            check_b_curve_manager_batch_read,
        ),
        (
            "B: curve manager batch-reads a 151-byte extended curve",
            check_b_curve_manager_batch_read_extended_curve,
        ),
        (
            "B: buyer corrects token_program_id and ATA",
            check_b_buyer_corrects_token_program,
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

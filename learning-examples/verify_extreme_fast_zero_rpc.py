"""Verify extreme_fast_mode makes zero RPC calls for event-sourced tokens.

extreme_fast_mode's contract is that nothing sits between detecting a token
and submitting the buy — no reads, no price fetch. The pump.fun CreateEvent
carries the canonical creator (instruction args.creator is user-supplied and
may differ post-2026-04-28), mayhem/cashback flags and quote_mint, so a
TokenInfo built from it needs no pre-buy curve refresh. PumpPortal payloads
carry none of that, so they keep the refresh.

Offline machine checks, no network and no funds moved:

  1. The logs parser marks CreateEvent-sourced TokenInfo as state_from_event.
  2. The instruction parser stays conservative (args.creator not canonical).
  3. The geyser parser prefers the CreateEvent from meta.log_messages.
  4. The geyser LISTENER delegates to that parser (it used to inline
     instruction decoding, bypassing the event path).
  5. The block parser rides the same CreateEvent logs.
  6. The pumpportal processor never sets state_from_event.
  7. An event-sourced buy submits with ZERO curve-manager/RPC calls.
  8. A pumpportal-sourced buy still refreshes from chain.
  9. trade.trust_create_event=false forces the refresh even for event data.

Usage:
    uv run learning-examples/verify_extreme_fast_zero_rpc.py
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from platforms.pumpfun.pumpportal_processor import (  # noqa: E402
    PumpFunPumpPortalProcessor,
)
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = (
    PROJECT_ROOT
    / "learning-examples"
    / "blocksubscribe-transactions"
    / "raw_create_tx_from_blocksubscribe.json"
)

PROVIDER = PumpFunAddressProvider()
TRADER = Pubkey.from_string("11111111111111111111111111111112")


def _event_parser() -> PumpFunEventParser:
    """Real pump.fun event parser with the vendored IDL, no RPC client needed."""
    return PumpFunEventParser(
        idl_parser=get_idl_manager().get_parser(Platform.PUMP_FUN)
    )


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _event_sourced_token_info() -> TokenInfo:
    """Parse the fixture's CreateEvent through the real logs parser."""
    parser = _event_parser()
    return parser.parse_token_creation_from_logs(
        _fixture()["meta"]["logMessages"], signature="fixture"
    )


class _StubClient:
    """Records submissions and account reads; never touches the network."""

    def __init__(self) -> None:
        self.sent: list = []
        self.reads = 0

    async def build_and_send_transaction(
        self, instructions: list, *_args: object, **_kwargs: object
    ) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, _signature: str, **_kwargs: object) -> bool:
        return False

    async def get_account_info(self, *_args: object, **_kwargs: object) -> None:
        self.reads += 1
        raise ValueError("unexpected RPC read in zero-RPC path")  # noqa: TRY003

    async def get_multiple_accounts(self, *_args: object, **_kwargs: object) -> None:
        self.reads += 1
        raise ValueError("unexpected RPC read in zero-RPC path")  # noqa: TRY003


class _CountingCurveManager:
    """Counts refresh calls; returns benign state."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_pool_state_and_token_program(
        self,
        _pool: Pubkey,
        _mint: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> tuple[dict, Pubkey]:
        self.calls += 1
        state = {
            "creator": str(TRADER),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "quote_mint": WSOL_MINT,
        }
        return state, SystemAddresses.TOKEN_2022_PROGRAM

    async def get_pool_state(
        self,
        _pool: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> dict:
        self.calls += 1
        return {
            "creator": str(TRADER),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "quote_mint": WSOL_MINT,
        }


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


def _make_buyer(client: _StubClient, **kwargs: object) -> PlatformAwareBuyer:
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


def _run_buy(
    token_info: TokenInfo, curve_manager: object, **buyer_kwargs: object
) -> tuple[_StubClient, object]:
    client = _StubClient()
    buyer = _make_buyer(client, **buyer_kwargs)
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        curve_manager
    )
    result = asyncio.run(buyer.execute(token_info))
    return client, result


def check_logs_parser_marks_event_state() -> bool:
    """CreateEvent-sourced TokenInfo carries everything -> flag set."""
    token_info = _event_sourced_token_info()
    if token_info is None:
        print("    fixture logs did not parse into a TokenInfo")
        return False
    ok = (
        getattr(token_info, "state_from_event", False) is True
        and token_info.quote_mint is not None
        and token_info.creator is not None
    )
    if not ok:
        print(
            f"    state_from_event={getattr(token_info, 'state_from_event', None)} "
            f"quote_mint={token_info.quote_mint} creator={token_info.creator}"
        )
    return ok


def check_instruction_parser_stays_conservative() -> bool:
    """args.creator is user-supplied, not canonical -> flag must stay unset."""
    fixture = _fixture()
    raw = base64.b64decode(fixture["transaction"][0])
    tx = VersionedTransaction.from_bytes(raw)
    msg = tx.message
    account_keys = [bytes(k) for k in msg.account_keys]
    parser = _event_parser()
    for ix in msg.instructions:
        # The fixture's create_v2 omits the trailing is_cashback_enabled
        # OptionBool — a legal wire form the decoder accepts since #184
        # (verify_create_v2_optional_args.py covers the decode itself).
        token_info = parser.parse_token_creation_from_instruction(
            bytes(ix.data), list(ix.accounts), account_keys
        )
        if token_info is not None:
            ok = getattr(token_info, "state_from_event", False) is False
            if not ok:
                print("    instruction-sourced TokenInfo must not set the flag")
            return ok
    print("    fixture create instruction did not parse")
    return False


def check_geyser_parser_prefers_event_logs() -> bool:
    """Geyser meta carries log_messages; the CreateEvent there is canonical."""
    logs = _fixture()["meta"]["logMessages"]
    stub = SimpleNamespace(
        transaction=SimpleNamespace(
            transaction=SimpleNamespace(
                transaction=SimpleNamespace(
                    message=SimpleNamespace(instructions=[], account_keys=[])
                ),
                meta=SimpleNamespace(log_messages=logs),
            )
        )
    )
    parser = _event_parser()
    token_info = parser.parse_token_creation_from_geyser(stub)
    ok = (
        token_info is not None
        and getattr(token_info, "state_from_event", False) is True
    )
    if not ok:
        print(f"    geyser parse returned {token_info}")
    return ok


def check_geyser_listener_delegates_to_parser() -> bool:
    """The listener must route updates through the event-first parser.

    Live run showed state_from_event=False on a geyser token: the listener
    inlined instruction parsing and never reached the parser's log-preferring
    geyser method.
    """
    # Imported here: the listener module pulls in grpc, which the other
    # checks don't need.
    from monitoring.universal_geyser_listener import (  # noqa: PLC0415
        UniversalGeyserListener,
    )

    listener = UniversalGeyserListener(
        geyser_endpoint="dummy:443",
        geyser_api_token="dummy",  # noqa: S106 - offline stub, never connects
        geyser_auth_type="x-token",
        platforms=[Platform.PUMP_FUN],
    )
    logs = _fixture()["meta"]["logMessages"]
    update = SimpleNamespace(
        HasField=lambda field: field == "transaction",
        transaction=SimpleNamespace(
            transaction=SimpleNamespace(
                transaction=SimpleNamespace(
                    message=SimpleNamespace(instructions=[], account_keys=[])
                ),
                meta=SimpleNamespace(log_messages=logs),
            )
        ),
    )
    token_info = asyncio.run(listener._process_update(update))  # noqa: SLF001
    ok = (
        token_info is not None
        and getattr(token_info, "state_from_event", False) is True
    )
    if not ok:
        print(f"    listener _process_update returned {token_info}")
    return ok


def check_block_parser_marks_event_state() -> bool:
    """The block listener's parse path also rides the CreateEvent logs."""
    parser = _event_parser()
    token_info = parser.parse_token_creation_from_block({"transactions": [_fixture()]})
    ok = (
        token_info is not None
        and getattr(token_info, "state_from_event", False) is True
    )
    if not ok:
        print(f"    block parse returned {token_info}")
    return ok


def check_pumpportal_never_sets_flag() -> bool:
    """PumpPortal payloads carry no curve state -> flag must stay unset."""
    mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    token_info = PumpFunPumpPortalProcessor().process_token_data(
        {
            "name": "T",
            "symbol": "T",
            "mint": str(mint),
            "bondingCurveKey": str(PROVIDER.derive_pool_address(mint)),
            "traderPublicKey": str(TRADER),
            "uri": "",
            "pool": "pump",
        }
    )
    ok = (
        token_info is not None
        and getattr(token_info, "state_from_event", False) is False
    )
    if not ok:
        print("    pumpportal TokenInfo must not set state_from_event")
    return ok


def check_event_sourced_buy_is_zero_rpc() -> bool:
    """The killer feature: detection -> submission with no reads at all."""
    token_info = _event_sourced_token_info()
    if token_info is None:
        print("    fixture logs did not parse into a TokenInfo")
        return False
    curve_manager = _CountingCurveManager()
    client, _result = _run_buy(token_info, curve_manager)
    ok = curve_manager.calls == 0 and client.reads == 0 and len(client.sent) == 1
    if not ok:
        print(
            f"    curve_manager.calls={curve_manager.calls} "
            f"client.reads={client.reads} submissions={len(client.sent)}"
        )
    return ok


def check_pumpportal_buy_still_refreshes() -> bool:
    """Listener-guessed data must still be refreshed from chain."""
    mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    bonding_curve = PROVIDER.derive_pool_address(mint)
    token_info = TokenInfo(
        name="T",
        symbol="T",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=PROVIDER.derive_associated_bonding_curve(
            mint, bonding_curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        user=TRADER,
        creator=TRADER,
        creator_vault=PROVIDER.derive_creator_vault(TRADER),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )
    curve_manager = _CountingCurveManager()
    client, _result = _run_buy(token_info, curve_manager)
    ok = curve_manager.calls >= 1 and len(client.sent) == 1
    if not ok:
        print(
            f"    curve_manager.calls={curve_manager.calls} "
            f"submissions={len(client.sent)}"
        )
    return ok


def check_trust_flag_forces_refresh() -> bool:
    """trust_create_event=false is the escape hatch back to always-refresh."""
    token_info = _event_sourced_token_info()
    if token_info is None:
        print("    fixture logs did not parse into a TokenInfo")
        return False
    curve_manager = _CountingCurveManager()
    _client, _result = _run_buy(token_info, curve_manager, trust_create_event=False)
    ok = curve_manager.calls >= 1
    if not ok:
        print(f"    curve_manager.calls={curve_manager.calls} (expected >=1)")
    return ok


def main() -> int:
    checks = [
        ("logs parser marks CreateEvent state", check_logs_parser_marks_event_state),
        (
            "instruction parser stays conservative",
            check_instruction_parser_stays_conservative,
        ),
        (
            "geyser parser prefers CreateEvent logs",
            check_geyser_parser_prefers_event_logs,
        ),
        (
            "geyser listener delegates to event-first parser",
            check_geyser_listener_delegates_to_parser,
        ),
        ("block parser marks CreateEvent state", check_block_parser_marks_event_state),
        ("pumpportal never sets state_from_event", check_pumpportal_never_sets_flag),
        ("event-sourced buy makes zero RPC calls", check_event_sourced_buy_is_zero_rpc),
        ("pumpportal buy still refreshes", check_pumpportal_buy_still_refreshes),
        ("trust_create_event=false forces refresh", check_trust_flag_forces_refresh),
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

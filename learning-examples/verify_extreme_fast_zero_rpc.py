"""Verify zero-RPC buys are limited to verified, correlated CreateEvents.

The pump.fun CreateEvent carries the canonical creator (instruction
args.creator is user-supplied and may differ post-2026-04-28),
mayhem/cashback flags, and quote_mint. Normalized blocks and Geyser
transactions can correlate that event with the create instruction and retain
the verification needed to skip the pre-buy curve refresh. A logsSubscribe
notification has no transaction instructions, so parser dispatch deliberately
downgrades its event candidate and the buy must refresh.

Offline machine checks, no network and no funds moved:

  1. The raw logs parser can decode a CreateEvent candidate.
  2. The instruction parser stays conservative (args.creator not canonical).
  3. The logs listener normalization/dispatch path downgrades the uncorrelated
     candidate and the buyer refreshes curve state.
  4. The geyser parser prefers the CreateEvent from meta.log_messages.
  5. The geyser listener retains correlated verification and buys with zero RPC.
  6. The block listener retains correlated verification and buys with zero RPC.
  7. The pumpportal processor never sets state_from_event.
  8. A pumpportal-sourced buy still refreshes from chain.
  9. trade.trust_create_event=false forces a refresh for verified event data.

Usage:
    uv run learning-examples/verify_extreme_fast_zero_rpc.py
"""

import asyncio
import base64
import json
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.client import TransactionStatus  # noqa: E402
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


class _SingleFrameWebSocket:
    """Return one offline JSON-RPC frame without opening a connection."""

    def __init__(self, frame: dict) -> None:
        self.frame = json.dumps(frame)

    async def recv(self) -> str:
        return self.frame


def _logs_notification(subscription_id: int = 1) -> dict:
    fixture = _fixture()
    transaction = VersionedTransaction.from_bytes(
        base64.b64decode(fixture["transaction"][0])
    )
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "subscription": subscription_id,
            "result": {
                "context": {"slot": 1},
                "value": {
                    "signature": str(transaction.signatures[0]),
                    "err": None,
                    "logs": fixture["meta"]["logMessages"],
                },
            },
        },
    }


def _logs_listener_token_info() -> TokenInfo | None:
    """Exercise the real logs listener normalization and parser dispatch."""
    from monitoring.base_listener import BaseTokenListener  # noqa: PLC0415
    from monitoring.universal_logs_listener import (  # noqa: PLC0415
        UniversalLogsListener,
    )

    listener = object.__new__(UniversalLogsListener)
    BaseTokenListener.__init__(listener)
    listener.platform_parsers = {Platform.PUMP_FUN: _event_parser()}
    listener._pending_frames = deque()  # noqa: SLF001
    listener._subscription_ids = frozenset({1})  # noqa: SLF001
    return asyncio.run(
        listener._wait_for_token_creation(  # noqa: SLF001
            _SingleFrameWebSocket(_logs_notification())
        )
    )


def _block_listener_token_info() -> TokenInfo | None:
    """Exercise the real block listener normalization and parser dispatch."""
    from monitoring.base_listener import BaseTokenListener  # noqa: PLC0415
    from monitoring.universal_block_listener import (  # noqa: PLC0415
        UniversalBlockListener,
    )

    listener = object.__new__(UniversalBlockListener)
    BaseTokenListener.__init__(listener)
    listener.platform_parsers = {Platform.PUMP_FUN: _event_parser()}
    listener._recent_creation_order = deque()  # noqa: SLF001
    listener._recent_creation_keys = set()  # noqa: SLF001
    tokens = listener._process_block_transactions(  # noqa: SLF001
        [_fixture()],
        slot=1,
        commitment="confirmed",
        platform=Platform.PUMP_FUN,
    )
    return tokens[0] if len(tokens) == 1 else None


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

    async def confirm_transaction_outcome(
        self, _signature: str, **_kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            status=TransactionStatus.UNKNOWN,
            slot=None,
            error="offline verifier",
        )

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
            "complete": False,
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
            "complete": False,
            "quote_mint": WSOL_MINT,
        }


def _stub_implementations(curve_manager: object) -> SimpleNamespace:
    async def build_buy_instruction(
        token_info: TokenInfo, *_args: object, **_kwargs: object
    ) -> list[Instruction]:
        accounts = [
            AccountMeta(Pubkey.new_unique(), is_signer=False, is_writable=True)
            for _ in range(27)
        ]
        accounts[10] = AccountMeta(
            token_info.bonding_curve,
            is_signer=False,
            is_writable=True,
        )
        return [Instruction(PROVIDER.program_id, b"", accounts)]

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

    wallet = SimpleNamespace(
        pubkey=TRADER,
        keypair=None,
        get_associated_token_address=lambda mint, token_program: (
            PROVIDER.derive_user_token_account(TRADER, mint, token_program)
        ),
    )
    return PlatformAwareBuyer(
        client,
        wallet,
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
    """The raw platform parser decodes event state before provenance dispatch."""
    token_info = _event_sourced_token_info()
    if token_info is None:
        print("    fixture logs did not parse into a TokenInfo")
        return False
    ok = (
        getattr(token_info, "state_from_event", False) is True
        and token_info.metadata_verified is False
        and token_info.quote_mint is not None
        and token_info.creator is not None
    )
    if not ok:
        print(
            f"    state_from_event={getattr(token_info, 'state_from_event', None)} "
            f"metadata_verified={token_info.metadata_verified} "
            f"quote_mint={token_info.quote_mint} creator={token_info.creator}"
        )
    return ok


def check_logs_listener_downgrades_and_refreshes() -> bool:
    """logsSubscribe lacks instruction correlation, so zero-RPC is forbidden."""
    token_info = _logs_listener_token_info()
    if token_info is None:
        print("    logs listener did not return a TokenInfo")
        return False
    downgraded = (
        token_info.source == "logs"
        and token_info.state_from_event is False
        and token_info.metadata_verified is False
        and token_info.quote_mint is None
    )
    curve_manager = _CountingCurveManager()
    client, _result = _run_buy(token_info, curve_manager)
    refreshed = curve_manager.calls >= 1 and len(client.sent) == 1
    if not (downgraded and refreshed):
        print(
            f"    source={token_info.source} "
            f"state_from_event={token_info.state_from_event} "
            f"metadata_verified={token_info.metadata_verified} "
            f"quote_mint={token_info.quote_mint} "
            f"curve_manager.calls={curve_manager.calls} "
            f"submissions={len(client.sent)}"
        )
    return downgraded and refreshed


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
    """Geyser normalization retains correlated event state for zero-RPC."""
    from monitoring.base_listener import BaseTokenListener  # noqa: PLC0415
    from monitoring.universal_geyser_listener import (  # noqa: PLC0415
        UniversalGeyserListener,
    )

    listener = object.__new__(UniversalGeyserListener)
    BaseTokenListener.__init__(listener)
    listener.platform_parsers = {Platform.PUMP_FUN: _event_parser()}
    fixture = _fixture()
    raw_transaction = base64.b64decode(fixture["transaction"][0])
    transaction = VersionedTransaction.from_bytes(raw_transaction)
    message = SimpleNamespace(
        account_keys=transaction.message.account_keys,
        instructions=[
            SimpleNamespace(
                program_id_index=instruction.program_id_index,
                accounts=instruction.accounts,
                data=instruction.data,
            )
            for instruction in transaction.message.instructions
        ],
    )
    update = SimpleNamespace(
        HasField=lambda field: field == "transaction",
        transaction=SimpleNamespace(
            slot=1,
            transaction=SimpleNamespace(
                signature=transaction.signatures[0],
                transaction=SimpleNamespace(
                    signatures=transaction.signatures,
                    message=message,
                ),
                meta=SimpleNamespace(
                    log_messages=fixture["meta"]["logMessages"],
                    loaded_writable_addresses=[],
                    loaded_readonly_addresses=[],
                ),
            ),
        ),
    )
    token_info = asyncio.run(listener._process_update(update))  # noqa: SLF001
    if token_info is None:
        print("    listener _process_update returned no TokenInfo")
        return False
    verified = (
        token_info.source == "geyser"
        and token_info.state_from_event is True
        and token_info.metadata_verified is True
    )
    curve_manager = _CountingCurveManager()
    client, _result = _run_buy(token_info, curve_manager)
    zero_rpc = curve_manager.calls == 0 and client.reads == 0 and len(client.sent) == 1
    if not (verified and zero_rpc):
        print(
            f"    source={token_info.source} "
            f"state_from_event={token_info.state_from_event} "
            f"metadata_verified={token_info.metadata_verified} "
            f"curve_manager.calls={curve_manager.calls} "
            f"client.reads={client.reads} submissions={len(client.sent)}"
        )
    return verified and zero_rpc


def check_block_listener_retains_verified_event_state() -> bool:
    """Block normalization retains correlated event state for zero-RPC."""
    token_info = _block_listener_token_info()
    if token_info is None:
        print("    block listener did not return exactly one TokenInfo")
        return False
    verified = (
        token_info.source == "blocks"
        and token_info.state_from_event is True
        and token_info.metadata_verified is True
    )
    curve_manager = _CountingCurveManager()
    client, _result = _run_buy(token_info, curve_manager)
    zero_rpc = curve_manager.calls == 0 and client.reads == 0 and len(client.sent) == 1
    if not (verified and zero_rpc):
        print(
            f"    source={token_info.source} "
            f"state_from_event={token_info.state_from_event} "
            f"metadata_verified={token_info.metadata_verified} "
            f"curve_manager.calls={curve_manager.calls} "
            f"client.reads={client.reads} submissions={len(client.sent)}"
        )
    return verified and zero_rpc


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
    """trust_create_event=false refreshes even verified correlated event data."""
    token_info = _block_listener_token_info()
    if token_info is None:
        print("    block listener did not return exactly one TokenInfo")
        return False
    curve_manager = _CountingCurveManager()
    _client, _result = _run_buy(token_info, curve_manager, trust_create_event=False)
    ok = curve_manager.calls >= 1
    if not ok:
        print(f"    curve_manager.calls={curve_manager.calls} (expected >=1)")
    return ok


def main() -> int:
    checks = [
        (
            "raw logs parser decodes CreateEvent candidate",
            check_logs_parser_marks_event_state,
        ),
        (
            "instruction parser stays conservative",
            check_instruction_parser_stays_conservative,
        ),
        (
            "logs listener downgrades and refreshes",
            check_logs_listener_downgrades_and_refreshes,
        ),
        (
            "geyser parser prefers CreateEvent logs",
            check_geyser_parser_prefers_event_logs,
        ),
        (
            "geyser listener retains verification and stays zero-RPC",
            check_geyser_listener_delegates_to_parser,
        ),
        (
            "block listener retains verification and stays zero-RPC",
            check_block_listener_retains_verified_event_state,
        ),
        ("pumpportal never sets state_from_event", check_pumpportal_never_sets_flag),
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

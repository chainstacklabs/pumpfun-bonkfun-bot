"""Verify a buy that landed on chain is never reported as a failed buy.

`getTransaction` returns a null result for a transaction the node has not caught
up to yet. On a load-balanced endpoint the node that serves it is not
necessarily the one that just confirmed the signature, so a perfectly good buy
can read back as "not found" for a moment. Two call sites turned that into a
failed buy:

  1. `SolanaClient._get_transaction_result` returned None on the first null
     result, with no retry, so `verify_transaction_succeeded` reported the buy
     as unconfirmed and `confirm_transaction` returned False.
  2. `PlatformAwareBuyer.execute` raised ValueError when the amounts could not
     be read back — inside the `if success:` branch, so the outer handler
     turned a confirmed buy into `TradeResult(success=False)`.

Either way the bot holds tokens it believes it never bought: `_handle_failed_buy`
runs instead of the exit strategy, so the position is never sold, and with
`cleanup.mode: "on_fail"` plus `force_close_with_burn: true` the tokens are
burned.

The opposite mistake is issue #175 — reporting a reverted buy as successful — so
the checks below pin both directions.

Offline machine checks, no network and no funds moved. A stub RPC serves
scripted `getTransaction` responses:

  1. `_get_transaction_result` retries while the result is null and returns the
     transaction once the node catches up.
  2. The retry is bounded, so a signature that never appears still returns None.
  3. `verify_transaction_succeeded` is True for a tx that lands after a lag.
  4. A reverted transaction is still False, however many times it is read
     (issue #175 stays fixed).
  5. A confirmed buy whose amounts never parse is reported successful, with the
     token amount taken from the wallet balance so the sell has a real figure.
  6. That fallback amount is the balance actually held, not the expected amount.
  7. A balance that predates the buy is capped at what this buy asked for, so
     the exit cannot liquidate holdings this trade never created.
  8. A buy whose transaction genuinely failed is still reported unsuccessful.

Usage:
    uv run learning-examples/verify_buy_result_not_lost.py
"""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402

SIGNATURE = "5" * 88  # shape only; nothing decodes it in these checks
MINT = Pubkey.from_string("So11111111111111111111111111111111111111113")
CURVE = Pubkey.from_string("So11111111111111111111111111111111111111114")

TOKENS_RECEIVED_RAW = 14_000_000  # what a 20-token dust buy actually delivers
PRIOR_HOLDING_RAW = 3_374_396_272  # a balance that predates this buy entirely
EXPECTED_TOKENS = 20.0  # what extreme_fast_mode asked for
BUY_AMOUNT_SOL = 0.0001
AMOUNT_TOLERANCE = 1e-9  # float round-trip through raw token units

LANDED_TX = {"meta": {"err": None, "preBalances": [], "postBalances": []}}
REVERTED_TX = {"meta": {"err": {"InstructionError": [3, {"Custom": 6062}]}}}
NULL_RESULT = {"jsonrpc": "2.0", "id": 1, "result": None}


class StubRpc:
    """Serves scripted getTransaction responses, counting the calls."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls = 0
        self.deadlines_seen: list[float | None] = []

    async def post_rpc(self, _body: dict, **kwargs: float) -> dict:
        # _get_transaction_result passes the budget it has left as
        # deadline_seconds, so the stub has to tolerate it. What that budget is
        # worth is checked in verify_rpc_deadline.py.
        self.deadlines_seen.append(kwargs.get("deadline_seconds"))
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


def _client_with(responses: list[dict]) -> tuple[SolanaClient, StubRpc]:
    """Build a SolanaClient that never opens a socket."""
    client = object.__new__(SolanaClient)
    rpc = StubRpc(responses)
    client.post_rpc = rpc.post_rpc
    return client, rpc


def _ok(result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_token_info() -> TokenInfo:
    return TokenInfo(
        name="VerifyBuy",
        symbol="VBUY",
        uri="",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=CURVE,
        associated_bonding_curve=CURVE,
        creator=CURVE,
        creator_vault=CURVE,
        quote_mint=WSOL_MINT,
        state_from_event=True,  # extreme_fast_mode skips the curve read
    )


class StubBuyClient:
    """Minimal client for driving PlatformAwareBuyer.execute."""

    def __init__(
        self,
        *,
        confirmed: bool,
        details: tuple[int | None, int | None],
        balance_raw: int = TOKENS_RECEIVED_RAW,
    ) -> None:
        self.confirmed = confirmed
        self.details = details
        self.balance_raw = balance_raw
        self.balance_reads = 0

    async def build_and_send_transaction(
        self, *_args: object, **_kwargs: object
    ) -> str:
        return SIGNATURE

    async def confirm_transaction(self, _signature: object) -> bool:
        return self.confirmed

    async def get_buy_transaction_details(
        self, *_args: object, **_kwargs: object
    ) -> tuple[int | None, int | None]:
        return self.details

    async def get_token_account_balance(self, _account: Pubkey) -> int:
        self.balance_reads += 1
        return self.balance_raw


def _stub_implementations() -> SimpleNamespace:
    """Platform implementations that build an empty, unsendable instruction set."""
    address_provider = SimpleNamespace(
        derive_pool_address=lambda _mint: CURVE,
        derive_creator_vault=lambda _creator: CURVE,
    )
    instruction_builder = SimpleNamespace(
        build_buy_instruction=_async_return([]),
        get_required_accounts_for_buy=lambda *_args, **_kwargs: [],
        get_buy_compute_unit_limit=lambda _override: 100_000,
    )
    return SimpleNamespace(
        address_provider=address_provider,
        instruction_builder=instruction_builder,
        curve_manager=SimpleNamespace(),
    )


def _async_return(value: object) -> Callable[..., object]:
    """Build a coroutine function that always returns `value`."""

    async def _inner(*_args: object, **_kwargs: object) -> object:
        return value

    return _inner


async def _run_buy(client: StubBuyClient) -> TradeResult:
    """Drive the real PlatformAwareBuyer.execute against stubs."""
    wallet = SimpleNamespace(
        pubkey=CURVE,
        keypair=None,
        get_associated_token_address=lambda _mint, _program: CURVE,
    )
    buyer = PlatformAwareBuyer(
        client=client,
        wallet=wallet,
        priority_fee_manager=SimpleNamespace(
            calculate_priority_fee=_async_return(None)
        ),
        amount=BUY_AMOUNT_SOL,
        slippage=0.3,
        extreme_fast_token_amount=int(EXPECTED_TOKENS),
        extreme_fast_mode=True,
    )
    original = platform_aware.get_platform_implementations
    platform_aware.get_platform_implementations = (
        lambda *_args, **_kwargs: _stub_implementations()
    )
    try:
        return await buyer.execute(_make_token_info())
    finally:
        platform_aware.get_platform_implementations = original


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def check_lagging_node_is_retried() -> bool:
    print("\n1. A transaction the node has not caught up to yet is retried")
    client, rpc = _client_with([NULL_RESULT, NULL_RESULT, _ok(LANDED_TX)])
    result = await client._get_transaction_result(SIGNATURE)  # noqa: SLF001
    return _check(
        "transaction returned once visible",
        result is not None,
        f"{rpc.calls} getTransaction call(s), result={'found' if result else 'None'}",
    )


async def check_retry_is_bounded() -> bool:
    print("\n2. A signature that never appears still gives up")
    client, rpc = _client_with([NULL_RESULT])
    result = await asyncio.wait_for(
        client._get_transaction_result(SIGNATURE),  # noqa: SLF001
        timeout=30,
    )
    return _check(
        "gives up and returns None",
        result is None,
        f"None after {rpc.calls} call(s) — bounded, not an infinite wait",
    )


async def check_verify_succeeds_after_lag() -> bool:
    print("\n3. A buy that lands after a lag verifies as successful")
    client, _ = _client_with([NULL_RESULT, _ok(LANDED_TX)])
    ok = await client.verify_transaction_succeeded(SIGNATURE)
    return _check("verify_transaction_succeeded", ok is True, f"{ok}")


async def check_revert_still_fails() -> bool:
    print("\n4. A reverted transaction still verifies as failed (issue #175)")
    client, rpc = _client_with([_ok(REVERTED_TX)])
    ok = await client.verify_transaction_succeeded(SIGNATURE)
    return _check(
        "verify_transaction_succeeded",
        ok is False,
        f"{ok} after {rpc.calls} call(s) — a revert is not retried away",
    )


async def check_unparseable_buy_is_not_lost() -> bool:
    print("\n5. A confirmed buy whose amounts never parse is still a buy")
    client = StubBuyClient(confirmed=True, details=(None, None))
    result = await _run_buy(client)
    return _check(
        "TradeResult.success",
        bool(result.success),
        f"{result.success} (error_message={result.error_message!r})",
    )


async def check_fallback_amount_is_the_balance_held() -> bool:
    print("\n6. The fallback sells the balance actually held, not the expected amount")
    client = StubBuyClient(confirmed=True, details=(None, None))
    result = await _run_buy(client)
    held = TOKENS_RECEIVED_RAW / 10**TOKEN_DECIMALS
    return _check(
        "TradeResult.amount",
        result.success and abs(result.amount - held) < AMOUNT_TOLERANCE,
        f"{result.amount} tokens (wallet holds {held}, "
        f"extreme_fast_mode expected {EXPECTED_TOKENS}), "
        f"{client.balance_reads} balance read(s)",
    )


async def check_fallback_never_exceeds_what_was_bought() -> bool:
    print("\n7. A balance predating the buy is capped at what this buy asked for")
    client = StubBuyClient(
        confirmed=True, details=(None, None), balance_raw=PRIOR_HOLDING_RAW
    )
    result = await _run_buy(client)
    inflated = PRIOR_HOLDING_RAW / 10**TOKEN_DECIMALS
    return _check(
        "TradeResult.amount",
        result.success and abs(result.amount - EXPECTED_TOKENS) < AMOUNT_TOLERANCE,
        f"{result.amount} tokens, not the full {inflated} on the books — "
        f"the exit cannot sell a position this trade never opened",
    )


async def check_failed_buy_still_fails() -> bool:
    print("\n8. A buy whose transaction did not succeed is still a failure")
    client = StubBuyClient(confirmed=False, details=(None, None))
    result = await _run_buy(client)
    return _check(
        "TradeResult.success",
        result.success is False,
        f"{result.success} — an unconfirmed buy is not reported as bought",
    )


async def main() -> None:
    print("=" * 72)
    print("Verifying a landed buy is never reported as a failed buy")
    print("=" * 72)

    results = [
        await check_lagging_node_is_retried(),
        await check_retry_is_bounded(),
        await check_verify_succeeds_after_lag(),
        await check_revert_still_fails(),
        await check_unparseable_buy_is_not_lost(),
        await check_fallback_amount_is_the_balance_held(),
        await check_fallback_never_exceeds_what_was_bought(),
        await check_failed_buy_still_fails(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

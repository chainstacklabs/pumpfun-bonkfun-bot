"""Verify that a landed-but-reverted transaction is never reported as a success.

`AsyncClient.confirm_transaction` only waits for a signature to land in a block.
A landed transaction can still have reverted, so every trade path has to read
`meta.err` before it prints or returns success. Skipping that check is how issue
#175 happened: buys reverting with `BuybackFeeRecipientMissing` (6062) were
reported as confirmed, and the only way to notice was to inspect the signatures
by hand.

Two layers are checked:

* `tx_status.assert_transaction_succeeded` — the helper the learning examples use
* `SolanaClient.verify_transaction_succeeded` — the check the bot itself runs

Offline stub checks run always. Pass `--live` to additionally replay the three
reverted signatures from issue #175 against mainnet: both layers must fetch them
successfully and reject them on the strength of `meta.err`, not because the fetch
failed.

Usage:
    uv run learning-examples/verify_tx_status_checks.py
    uv run learning-examples/verify_tx_status_checks.py --live
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "learning-examples"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import tx_status  # noqa: E402

from core.client import SolanaClient  # noqa: E402

# Signatures from issue #175: reported as confirmed buys, actually reverted with
# BuybackFeeRecipientMissing (6062). They are permanent mainnet history, so they
# make a stable regression fixture for "landed but failed".
REVERTED_SIGNATURES = (
    "2bHRaovWyYNTX3K1KfMbMBSvefuT34ZExL2J2N83DDh9aB3Fynjyae5bcaCdRuQoihHAMHK4PzcRYoqc9hYypWb2",
    "2Quu1uNZB7oSFstpKHv8KR1aKweizyvGaoGeXXKFLbMro4aqWykqZoeq7hSrHVZ8xmi6GX9J5nQVC5Ut3VV88S7T",
    "4Bu9LrFK7QmUiCuLcjvejedJZRLfpKPZDiZgNqJRmw2EFjAAutmukGTErjzzJuA68emxpPQ8Pztu3NLkYhuvXKsd",
)


class _FakeMeta:
    def __init__(self, err: object) -> None:
        self.err = err


class _FakeTransaction:
    def __init__(self, meta: _FakeMeta | None) -> None:
        self.meta = meta


class _FakeValue:
    def __init__(self, transaction: _FakeTransaction) -> None:
        self.transaction = transaction


class _FakeResponse:
    def __init__(self, value: _FakeValue | None) -> None:
        self.value = value


class _StubClient:
    """Minimal stand-in for `AsyncClient.get_transaction`."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def get_transaction(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return self._response


def _reverted_response() -> _FakeResponse:
    # Shape of a real revert: {"InstructionError": [2, {"Custom": 6062}]}
    err = {"InstructionError": [2, {"Custom": 6062}]}
    return _FakeResponse(_FakeValue(_FakeTransaction(_FakeMeta(err))))


async def check_helper_rejects_revert() -> None:
    client = _StubClient(_reverted_response())
    try:
        await tx_status.assert_transaction_succeeded(client, "SIG")
    except RuntimeError as exc:
        assert "6062" in str(exc), f"error should name the program error: {exc}"
        return
    raise AssertionError("assert_transaction_succeeded accepted a reverted transaction")


async def check_helper_rejects_missing() -> None:
    client = _StubClient(_FakeResponse(None))
    try:
        await tx_status.assert_transaction_succeeded(client, "SIG")
    except RuntimeError:
        return
    raise AssertionError("assert_transaction_succeeded accepted a missing transaction")


async def check_helper_accepts_success() -> None:
    client = _StubClient(_FakeResponse(_FakeValue(_FakeTransaction(_FakeMeta(None)))))
    await tx_status.assert_transaction_succeeded(client, "SIG")


async def check_confirm_wrapper_rejects_revert() -> None:
    """`confirm_and_assert` must surface a revert, not just a landing."""
    confirmed: list[str] = []

    class _ConfirmStub(_StubClient):
        async def confirm_transaction(self, signature: str, **_kwargs: Any) -> None:
            confirmed.append(signature)

    client = _ConfirmStub(_reverted_response())
    try:
        await tx_status.confirm_and_assert(client, "SIG")
    except RuntimeError:
        assert confirmed == ["SIG"], "confirm_transaction should still be awaited"
        return
    raise AssertionError("confirm_and_assert accepted a reverted transaction")


async def check_examples_call_a_status_check() -> None:
    """Every example that confirms a trade must also verify it succeeded.

    Guards against a new example (or an edit to an existing one) reintroducing a
    bare `confirm_transaction` that prints success unconditionally.
    """
    examples_dir = PROJECT_ROOT / "learning-examples"
    # Files that confirm transactions without calling the helper, each for a
    # reason. Adding an entry here is a deliberate act; forgetting the check in a
    # new example is not.
    exempt = {
        # defines the helper
        "tx_status.py",
        # this file
        Path(__file__).name,
        # stubs confirm_transaction out; never sends a transaction
        "simulate_bot_buy_path.py",
        # uses the bot's SolanaClient wrapper, which folds meta.err into its
        # return value; the boolean is read at the call site
        "cleanup_accounts.py",
    }
    offenders = []
    for path in sorted(examples_dir.rglob("*.py")):
        if path.name in exempt or "__pycache__" in path.parts:
            continue
        source = path.read_text()
        if "confirm_transaction" not in source:
            continue
        if not (
            "assert_transaction_succeeded" in source or "confirm_and_assert" in source
        ):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, "examples confirm without checking meta.err: " + ", ".join(
        offenders
    )


async def check_helper_rejects_missing_meta() -> None:
    """Absent execution metadata must fail closed, not read as success.

    `meta=None` means the outcome is unknown. Folding that into "no error" is the
    exact mistake this module exists to prevent.
    """
    client = _StubClient(_FakeResponse(_FakeValue(_FakeTransaction(None))))
    try:
        await tx_status.assert_transaction_succeeded(client, "SIG")
    except RuntimeError as exc:
        assert "metadata" in str(exc), f"unclear reason: {exc}"
        return
    raise AssertionError("a transaction with no execution metadata was accepted")


async def check_revert_is_a_distinct_terminal_error() -> None:
    """A landed revert must be distinguishable from a transient failure.

    The retry loops in the manual buy/sell examples resubmit identical signed
    bytes, so a revert can never be repaired by retrying — they need to tell it
    apart from a fetch error.
    """
    client = _StubClient(_reverted_response())
    try:
        await tx_status.assert_transaction_succeeded(client, "SIG")
    except tx_status.TransactionRevertedError:
        pass
    else:
        raise AssertionError("revert did not raise TransactionRevertedError")

    assert issubclass(tx_status.TransactionRevertedError, RuntimeError), (
        "TransactionRevertedError must stay a RuntimeError so existing "
        "except RuntimeError handlers keep working"
    )

    # not-found is transient, so it must NOT be the terminal type
    missing = _StubClient(_FakeResponse(None))
    try:
        await tx_status.assert_transaction_succeeded(missing, "SIG")
    except tx_status.TransactionRevertedError:
        raise AssertionError("not-found was reported as a terminal revert") from None
    except RuntimeError:
        pass


async def check_commitment_is_propagated() -> None:
    """The status read must use the commitment the caller asked for.

    Confirming at "finalized" but reading status at "confirmed" reports success
    before the finalization the caller requested.
    """
    seen: dict[str, Any] = {}

    class _Recorder(_StubClient):
        async def get_transaction(self, *_args: Any, **kwargs: Any) -> _FakeResponse:
            seen["commitment"] = kwargs.get("commitment")
            return self._response

        async def confirm_transaction(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    client = _Recorder(_FakeResponse(_FakeValue(_FakeTransaction(_FakeMeta(None)))))
    await tx_status.confirm_and_assert(client, "SIG", commitment="finalized")
    assert seen["commitment"] == "finalized", (
        f"status read at {seen['commitment']!r}, not the requested 'finalized'"
    )


async def check_endpoint_logging_hides_userinfo() -> None:
    """Endpoint logging must use hostname, not netloc.

    netloc keeps any `user:pass@` userinfo, so redacting with it still prints the
    credential for providers that put the key there.
    """
    offenders = []
    for path in sorted((PROJECT_ROOT / "learning-examples").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == Path(__file__).name:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "urlsplit" in line and ".netloc" in line:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{n}")
    assert not offenders, "urlsplit(...).netloc leaks userinfo at: " + ", ".join(
        offenders
    )


async def check_bot_reads_the_confirmation_result() -> None:
    """No caller in `src/` may throw away `confirm_transaction`'s boolean.

    Inside `src/` the only client is `SolanaClient`, whose return value carries
    the `meta.err` verdict. Calling it as a bare statement discards that verdict
    and whatever gets logged next is unconditional — which is how cleanup
    reported "Closed successfully" for reverted close transactions.
    """
    import ast

    offenders = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # client.py holds the wrapper itself; its inner call is solana-py's,
        # which signals failure by raising rather than by returning False.
        if path.name == "client.py" and path.parent.name == "core":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Expr):
                continue
            call = node.value.value if isinstance(node.value, ast.Await) else node.value
            if (
                isinstance(call, ast.Call)
                and getattr(call.func, "attr", None) == "confirm_transaction"
            ):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert not offenders, "confirm_transaction result discarded at: " + ", ".join(
        offenders
    )


async def check_client_accepts_both_signature_types() -> None:
    """`SolanaClient` must handle a base58 string and a `Signature` alike.

    The RPC client rejects a string and `json.dumps` rejects a `Signature`, so
    whichever form a caller has, one of the two layers used to break. Both
    failures surfaced as "not confirmed" for a transaction that in fact landed.
    """
    from solders.signature import Signature

    sig_str = REVERTED_SIGNATURES[0]
    sig_obj = Signature.from_string(sig_str)

    client = SolanaClient("http://127.0.0.1:1")  # never contacted
    bodies: list[dict[str, Any]] = []

    async def capture_rpc(body: dict[str, Any], **_kwargs: Any) -> None:
        bodies.append(body)

    client.post_rpc = capture_rpc

    for form in (sig_str, sig_obj):
        bodies.clear()
        await client._get_transaction_result(form)  # noqa: SLF001
        assert len(bodies) == 1, f"no RPC issued for {type(form).__name__}"
        param = bodies[0]["params"][0]
        assert param == sig_str, f"signature not normalized: {param!r}"
        # aiohttp serializes the body with json.dumps; a Signature would raise.
        json.dumps(bodies[0])

    # Malformed strings must be reported, not raised through.
    assert not await client.confirm_transaction("not-a-signature")
    await client.close()


async def check_rpc_timeouts_are_retried_not_raised() -> None:
    """An RPC timeout must be retried and reported, never raised at the caller.

    aiohttp signals a request timeout with `asyncio.TimeoutError`, which is not
    an `aiohttp.ClientError`. While `post_rpc` caught only the latter, every
    timeout escaped un-retried — and `str()` on it is empty, so callers logged a
    blank reason. This is what broke three of the four listeners in
    live_listener_matrix mid-run.
    """
    client = SolanaClient("http://127.0.0.1:1")
    attempts = 0

    class _TimingOutSession:
        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1

            class _Ctx:
                async def __aenter__(_self) -> Any:
                    raise TimeoutError

                async def __aexit__(_self, *_exc: Any) -> bool:
                    return False

            return _Ctx()

    async def _session() -> Any:
        return _TimingOutSession()

    client._get_session = _session  # noqa: SLF001
    client._rate_limiter.acquire = lambda: asyncio.sleep(0)  # noqa: SLF001

    result = await client.post_rpc({"method": "getTransaction"}, max_retries=2)
    await client.close()

    assert result is None, f"expected None on repeated timeout, got {result!r}"
    assert attempts == 2, f"timeout should be retried; saw {attempts} attempt(s)"


async def check_versioned_transactions_are_requested() -> None:
    """getTransaction must opt in to versioned transactions.

    Without `maxSupportedTransactionVersion` the RPC answers -32015 for every v0
    transaction, so `meta.err` is unreadable and a successful trade reads back as
    unconfirmed. The bot currently sends legacy transactions, which is the only
    reason this was survivable.
    """
    client = SolanaClient("http://127.0.0.1:1")  # never contacted
    bodies: list[dict[str, Any]] = []

    async def capture_rpc(body: dict[str, Any], **_kwargs: Any) -> None:
        bodies.append(body)

    client.post_rpc = capture_rpc
    await client._get_transaction_result(REVERTED_SIGNATURES[0])  # noqa: SLF001
    await client.close()

    assert bodies, "no RPC issued"
    config = bodies[0]["params"][1]
    assert config.get("maxSupportedTransactionVersion") == 0, (
        f"getTransaction omits maxSupportedTransactionVersion: {config}"
    )


async def check_live_signatures() -> None:
    load_dotenv()
    rpc_endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    if not rpc_endpoint:
        raise RuntimeError("SOLANA_NODE_RPC_ENDPOINT is required for --live")

    from solana.rpc.async_api import AsyncClient
    from solders.signature import Signature

    bot_client = SolanaClient(rpc_endpoint)
    raw_client = AsyncClient(rpc_endpoint)
    try:
        for signature in REVERTED_SIGNATURES:
            sig = Signature.from_string(signature)

            try:
                await tx_status.assert_transaction_succeeded(raw_client, sig)
            except RuntimeError as exc:
                print(f"  examples helper rejected {signature[:16]}...: {exc}")
            else:
                raise AssertionError(f"examples helper accepted reverted {signature}")

            # The transaction must be readable at all — a rejection because the
            # fetch failed would pass the assertion below for the wrong reason,
            # which is how the missing maxSupportedTransactionVersion hid.
            fetched = await bot_client._get_transaction_result(signature)  # noqa: SLF001
            assert fetched, f"SolanaClient could not fetch {signature}"
            assert fetched["meta"]["err"], f"expected a revert on {signature}"

            # verify_transaction_succeeded rather than confirm_transaction: these
            # signatures are old, and the landing-wait polls signature statuses,
            # which the RPC only keeps for recent history.
            succeeded = await bot_client.verify_transaction_succeeded(signature)
            assert not succeeded, f"SolanaClient accepted reverted {signature}"
            print(
                f"  SolanaClient rejected  {signature[:16]}...: "
                f"{fetched['meta']['err']}"
            )
    finally:
        await raw_client.close()
        await bot_client.close()


CHECKS = (
    ("helper rejects a reverted transaction", check_helper_rejects_revert),
    ("helper rejects a missing transaction", check_helper_rejects_missing),
    ("helper accepts a successful transaction", check_helper_accepts_success),
    ("confirm_and_assert surfaces a revert", check_confirm_wrapper_rejects_revert),
    ("helper rejects missing execution metadata", check_helper_rejects_missing_meta),
    ("revert is a distinct terminal error", check_revert_is_a_distinct_terminal_error),
    ("requested commitment is propagated", check_commitment_is_propagated),
    ("endpoint logging hides userinfo", check_endpoint_logging_hides_userinfo),
    (
        "SolanaClient accepts str and Signature",
        check_client_accepts_both_signature_types,
    ),
    (
        "getTransaction requests versioned transactions",
        check_versioned_transactions_are_requested,
    ),
    ("RPC timeouts are retried, not raised", check_rpc_timeouts_are_retried_not_raised),
    ("every example checks meta.err", check_examples_call_a_status_check),
    ("src/ reads the confirmation result", check_bot_reads_the_confirmation_result),
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also replay issue #175's reverted signatures against mainnet",
    )
    args = parser.parse_args()

    failures = 0
    for label, check in CHECKS:
        # Any exception is a failure of that check, not of the run: a check that
        # raises must not stop the remaining ones from reporting. Some of these
        # verify that a call does NOT raise, so the raise IS the finding.
        try:
            await check()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            print(f"{label} -> FAIL\n  {reason}")
        else:
            print(f"{label} -> OK")

    if args.live:
        print("\nreplaying issue #175 signatures against mainnet...")
        try:
            await check_live_signatures()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            print(f"live signature replay -> FAIL\n  {reason}")
        else:
            print("live signature replay -> OK")

    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print("\nAll transaction-status checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

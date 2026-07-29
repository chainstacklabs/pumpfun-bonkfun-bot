"""Transaction status checks shared by the learning examples.

`AsyncClient.confirm_transaction` answers one question: did this signature land
in a block? It says nothing about whether the transaction succeeded. A landed
transaction can have reverted, and RPC reports that only in `meta.err`.

Skipping the `meta.err` read is how a broken trade path looks healthy: the script
prints "Transaction confirmed", returns a signature, and the wallet balance never
moves. That was issue #175 — buys reverting with `BuybackFeeRecipientMissing`
(6062) reported as successful buys.

Deliberately standalone: imports nothing from `src/`, so every example under
`learning-examples/` (including the subdirectories) can use it.

`learning-examples/verify_tx_status_checks.py` verifies the behaviour below.
"""

from typing import Any, Protocol


class TransactionRevertedError(RuntimeError):
    """The transaction landed in a block but reverted.

    Terminal: the signature is already on chain, so resubmitting the same signed
    bytes cannot change the outcome. Callers with a retry loop should stop rather
    than spend attempts on it.
    """


class _TransactionFetcher(Protocol):
    """The slice of `AsyncClient` these helpers need."""

    async def get_transaction(self, *args: Any, **kwargs: Any) -> Any: ...


async def assert_transaction_succeeded(
    client: _TransactionFetcher, signature: Any, commitment: str = "confirmed"
) -> None:
    """Raise if a landed transaction actually reverted on-chain.

    Call this after `confirm_transaction` and before reporting success.

    Args:
        client: Solana RPC client
        signature: Transaction signature to inspect
        commitment: Commitment to read the transaction at. Must be at least as
            strong as the one it was confirmed at, or this can pass before the
            confirmation the caller asked for.

    Raises:
        TransactionRevertedError: If the transaction landed but reverted
        RuntimeError: If it cannot be found or carries no execution metadata
    """
    result = await client.get_transaction(
        signature, commitment=commitment, max_supported_transaction_version=0
    )
    value = result.value
    if value is None:
        raise RuntimeError(f"Transaction {signature} not found after confirmation")
    # No metadata means the outcome is unknown, not that it succeeded. Treating a
    # missing meta as "no error" is the exact mistake this module exists to
    # prevent, so fail closed.
    meta = value.transaction.meta
    if meta is None:
        raise RuntimeError(
            f"Transaction {signature} returned without execution metadata; "
            f"cannot tell whether it succeeded"
        )
    if meta.err is not None:
        raise TransactionRevertedError(
            f"Transaction {signature} landed but failed on-chain: {meta.err}"
        )


async def confirm_and_assert(
    client: Any,
    signature: Any,
    commitment: str = "confirmed",
    sleep_seconds: float = 1,
) -> None:
    """Wait for a transaction to land, then verify it succeeded.

    The one-call replacement for `await client.confirm_transaction(...)` in a
    trade path — there is no reason for an example to do the first half without
    the second.

    Args:
        client: Solana RPC client
        signature: Transaction signature to confirm
        commitment: Confirmation commitment level
        sleep_seconds: Poll interval while waiting for the signature to land

    Raises:
        TransactionRevertedError: If the transaction landed but reverted
        RuntimeError: If it cannot be found or carries no execution metadata
    """
    await client.confirm_transaction(
        signature, commitment=commitment, sleep_seconds=sleep_seconds
    )
    await assert_transaction_succeeded(client, signature, commitment=commitment)

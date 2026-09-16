"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import random
import struct
from time import monotonic
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.account import Account
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from core.pubkeys import is_sol_paired
from core.rpc_rate_limiter import TokenBucketRateLimiter
from interfaces.core import ConfirmationStatus, Platform
from utils.idl_manager import get_idl_parser
from utils.logger import get_logger

logger = get_logger(__name__)

HTTP_TOO_MANY_REQUESTS = 429

# Length of the `[instruction_index, error_detail]` pair inside
# `{"InstructionError": [...]}` -- fixed by the RPC's `meta.err` shape, not a
# tunable.
INSTRUCTION_ERROR_PAIR_LEN = 2

# How long getTransaction keeps retrying a signature the node has not caught
# up to yet, and the pause between attempts. A null result means "I cannot
# see this transaction", which on a load-balanced endpoint is not the same
# as "it failed": the node that serves getTransaction is not necessarily the
# one that just confirmed the signature. Treating the two as one reported
# landed buys as failed buys, leaving the tokens held and unsold.
TX_RESULT_RETRY_BUDGET = 5.0
TX_RESULT_RETRY_DELAY = 0.4


class _Deadline:
    """An optional wall-clock budget shared by every attempt of one RPC call.

    Attempts and elapsed time are different bounds, and post_rpc has always had
    only the first. This carries the second, so a caller can say how long an
    answer is worth waiting for without touching the retry counts.
    """

    def __init__(self, seconds: float | None) -> None:
        """Start the clock.

        Args:
            seconds: Budget in seconds, or None for no deadline at all
        """
        self.seconds = seconds
        self._expires_at = None if seconds is None else monotonic() + seconds

    def expired(self) -> bool:
        """Check whether the budget is already spent.

        Returns:
            True if there is a deadline and it has passed
        """
        return self._expires_at is not None and monotonic() >= self._expires_at

    def allows(self, wait: float) -> bool:
        """Check whether a backoff fits in what is left.

        A sleep longer than the remaining time would overshoot the budget the
        caller asked for, so it is not taken at all.

        Args:
            wait: How long the next backoff would sleep, in seconds

        Returns:
            True if the wait fits, or if there is no deadline
        """
        return self._expires_at is None or wait < self._expires_at - monotonic()

    def __str__(self) -> str:
        """Describe the deadline for a log line.

        Returns:
            Human-readable budget
        """
        return (
            "unbounded budget"
            if self.seconds is None
            else f"{self.seconds:.1f}s deadline"
        )


def _retry_after_seconds(header: str | None, attempt: int) -> float:
    """Work out how long to wait before retrying a rate-limited request.

    Args:
        header: Raw `Retry-After` header value, if the server sent one
        attempt: 1-based count of 429s seen so far on this call

    Returns:
        Seconds to wait, including jitter
    """
    try:
        wait_time = float(header) if header else None
    except (ValueError, TypeError):
        wait_time = None
    if wait_time is None:
        wait_time = min(2**attempt, 30)
    return wait_time + wait_time * random.uniform(0, 0.25)  # noqa: S311


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """
    Create SetLoadedAccountsDataSizeLimit instruction to reduce CU consumption.

    By default, Solana transactions can load up to 64MB of account data,
    costing 16k CU (8 CU per 32KB). Setting a lower limit reduces CU
    consumption and improves transaction priority.

    NOTE: CU savings are NOT visible in "consumed CU" metrics, which only
    show execution CU. The 16k CU loaded accounts overhead is counted
    separately for transaction priority/cost calculation.

    Args:
        bytes_limit: Max account data size in bytes (e.g., 512_000 = 512KB)

    Returns:
        Compute Budget instruction with discriminator 4

    Reference:
        https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
    """
    COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
        "ComputeBudget111111111111111111111111111111"
    )

    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


def _describe_program_error(err: object) -> str | None:
    """Best-effort human name for an Anchor `Custom(N)` error in `meta.err`.

    Digs `{"InstructionError": [idx, {"Custom": n}]}` out of `meta.err` and
    looks the code up in pump.fun's IDL error table. `SolanaClient` is shared
    across pump.fun and letsbonk.fun and carries no record of which program a
    given transaction actually invoked, so this only ever checks pump.fun's
    table (`idl/pump_fun_idl.json`) -- the program the bot's own `buy_v2` /
    `sell_v2` call directly, and the source of issue #175's
    `BuybackFeeRecipientMissing`. A revert on a different program (letsbonk's
    Raydium LaunchLab program, or one raised inside a pump-amm or pump-fees
    CPI) is described against the wrong table if its numeric code happens to
    also be defined there, and left unnamed otherwise -- picking the right
    table per invoked program id is not attempted here.

    Any shape this does not recognize (a non-Anchor failure such as compute
    budget exhaustion, `MaxLoadedAccountsDataSizeExceeded`, or a top-level
    string error) is reported as `None`, never raised.

    Args:
        err: The raw `meta.err` value from a `getTransaction` response.

    Returns:
        A description like "pump.fun IDL: 6062 BuybackFeeRecipientMissing"
        for a code pump.fun's IDL defines (the IDL error entry's own `msg`,
        if it has one, follows after a colon — 6062 has none, so there's no
        suffix here), or None if the shape doesn't match or the code is not
        in that table. The "pump.fun IDL:" prefix is
        deliberate: it is the only table checked, so it must stay visible in
        the rendered string, not just in this docstring -- a reader looking
        at a log line, not this source file, still needs to know the name is
        pump.fun's interpretation and not a fact about whichever program
        actually reverted.
    """
    if not isinstance(err, dict):
        return None
    instruction_error = err.get("InstructionError")
    if (
        not isinstance(instruction_error, list)
        or len(instruction_error) != INSTRUCTION_ERROR_PAIR_LEN
    ):
        return None
    detail = instruction_error[1]
    if not isinstance(detail, dict):
        return None
    code = detail.get("Custom")
    if not isinstance(code, int):
        return None
    description = get_idl_parser(Platform.PUMP_FUN).describe_error_code(code)
    if description is None:
        return None
    return f"pump.fun IDL: {description}"


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(self, rpc_endpoint: str, max_rps: float = 25.0):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
            max_rps: Maximum RPC requests per second (rate limiter)
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_updater_task = asyncio.create_task(
            self.start_blockhash_updater()
        )
        self._rate_limiter = TokenBucketRateLimiter(max_rps=max_rps)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def start_blockhash_updater(self, interval: float = 5.0):
        """Start background task to update recent blockhash."""
        while True:
            try:
                blockhash = await self.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = blockhash
            except Exception as e:
                logger.warning(f"Blockhash fetch failed: {e!s}")
            finally:
                await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        """Return the most recently cached blockhash."""
        async with self._blockhash_lock:
            if self._cached_blockhash is None:
                raise RuntimeError("No cached blockhash available yet")
            return self._cached_blockhash

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance.

        Returns:
            AsyncClient instance
        """
        if self._client is None:
            self._client = AsyncClient(self.rpc_endpoint)
        return self._client

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp session.

        Returns:
            Shared aiohttp.ClientSession instance.
        """
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            return self._session

    async def close(self):
        """Close the client connection and stop the blockhash updater."""
        if self._blockhash_updater_task:
            self._blockhash_updater_task.cancel()
            try:
                await self._blockhash_updater_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def get_health(self) -> str | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        }
        result = await self.post_rpc(body)
        if result and "result" in result:
            return result["result"]
        return None

    async def get_account_info(
        self, pubkey: Pubkey, commitment: str | None = None
    ) -> Account:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account
            commitment: Optional commitment override (e.g., "processed" for
                fresh state right after a geyser event; default "confirmed")

        Returns:
            The solders `Account` (verified 2026-09-15: `response.value` from
            `AsyncClient.get_account_info` is a `solders.account.Account`, not
            a dict -- callers read attributes like `.data` and `.owner`, never
            subscript it).

        Raises:
            ValueError: If account doesn't exist
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await client.get_account_info(pubkey, **kwargs)
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        return response.value

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Account | None]:
        """Get several accounts in one slot-consistent RPC round trip.

        A single getMultipleAccounts response is served by one node at one
        slot, unlike back-to-back get_account_info calls which a load-balanced
        endpoint may serve from nodes seconds apart (issue #170).

        Args:
            pubkeys: Public keys of the accounts
            commitment: Optional commitment override (default "confirmed")

        Returns:
            One entry per pubkey, in order -- each a solders `Account` (same
            type as `get_account_info` returns; attributes like `.data` and
            `.owner`, never subscriptable) or None for accounts that don't
            exist.
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await client.get_multiple_accounts(pubkeys, **kwargs)
        return list(response.value)

    async def get_token_account_balance(
        self, token_account: Pubkey, commitment: str = "confirmed"
    ) -> int:
        """Get token balance for an account.

        Defaults to "confirmed" rather than solana-py's "finalized": trades are
        confirmed at "confirmed", and finalization lags it. Reading the finalized
        balance right after a sell returns the pre-sell amount, and cleanup then
        builds a burn for tokens the account no longer holds — the whole burn +
        close transaction reverts with InsufficientFunds and the rent stays
        locked.

        Args:
            token_account: Token account address
            commitment: Commitment level for the balance read

        Returns:
            Token balance as integer
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        response = await client.get_token_account_balance(
            token_account, commitment=commitment
        )
        if response.value:
            return int(response.value.amount)
        return 0

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash.

        Returns:
            Recent blockhash as string
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        response = await client.get_latest_blockhash(commitment="processed")
        return response.value.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 3,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
    ) -> Signature:
        """
        Send a transaction with optional priority fee and compute unit limit.

        Args:
            instructions: List of instructions to include in the transaction.
            signer_keypair: Keypair to sign the transaction.
            skip_preflight: Whether to skip preflight checks.
            max_retries: Maximum number of retry attempts.
            priority_fee: Optional priority fee in microlamports.
            compute_unit_limit: Optional compute unit limit. Defaults to 85,000 if not provided.
            account_data_size_limit: Optional account data size limit in bytes (e.g., 512_000).
                                    Reduces CU cost from 16k to ~128 CU. Must be first instruction.

        Returns:
            Transaction signature.
        """
        client = await self.get_client()

        logger.info(
            f"Priority fee in microlamports: {priority_fee if priority_fee else 0}"
        )

        # Add compute budget instructions if applicable
        if (
            priority_fee is not None
            or compute_unit_limit is not None
            or account_data_size_limit is not None
        ):
            fee_instructions = []

            if account_data_size_limit is not None:
                fee_instructions.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
                logger.info(f"Account data size limit: {account_data_size_limit} bytes")

            # Set compute unit limit (use provided value or default to 85,000)
            cu_limit = compute_unit_limit if compute_unit_limit is not None else 85_000
            fee_instructions.append(set_compute_unit_limit(cu_limit))

            # Set priority fee if provided
            if priority_fee is not None:
                fee_instructions.append(set_compute_unit_price(priority_fee))

            instructions = fee_instructions + instructions

        recent_blockhash = await self.get_cached_blockhash()
        message = Message(instructions, signer_keypair.pubkey())
        transaction = Transaction([signer_keypair], message, recent_blockhash)

        for attempt in range(max_retries):
            try:
                await self._rate_limiter.acquire()
                tx_opts = TxOpts(
                    skip_preflight=skip_preflight, preflight_commitment=Processed
                )
                response = await client.send_transaction(transaction, tx_opts)
                return response.value

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.exception(
                        f"Failed to send transaction after {max_retries} attempts"
                    )
                    raise

                wait_time = 2**attempt
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed: {e!s}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    async def confirm_transaction(
        self, signature: str | Signature, commitment: str = "confirmed"
    ) -> bool:
        """Wait for transaction confirmation and verify execution success.

        Confirms the transaction landed on-chain, then checks meta.err to
        ensure the inner program instructions actually succeeded. A transaction
        can be "confirmed" (included in a block) but still fail execution.

        This deliberately stays a bool. Returning the richer
        :class:`ConfirmationStatus` here would be a silent trap: every enum
        member is truthy, so each existing `if await client.confirm_transaction(
        sig):` would start passing unconditionally. Callers that need to tell a
        revert from an unknown call :meth:`confirm_transaction_detailed`.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level

        Returns:
            Whether transaction was confirmed AND executed successfully
        """
        status = await self.confirm_transaction_detailed(signature, commitment)
        return status is ConfirmationStatus.SUCCESS

    async def confirm_transaction_detailed(
        self, signature: str | Signature, commitment: str = "confirmed"
    ) -> ConfirmationStatus:
        """Confirm a transaction, distinguishing a revert from an unknown.

        Same work as :meth:`confirm_transaction`, but it reports *why* a
        transaction did not succeed. A caller deciding whether to resubmit a
        non-idempotent transaction needs that: resubmitting after a confirmed
        revert is correct, while resubmitting after a lookup that simply never
        answered can send a second transaction for a position that is already
        closed.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level

        Returns:
            SUCCESS, REVERTED, or UNCONFIRMED
        """
        # The RPC client rejects a base58 string, and the resulting TypeError
        # would be swallowed by the handler below — reporting "not confirmed"
        # for a transaction that was never actually looked up.
        if isinstance(signature, str):
            try:
                signature = Signature.from_string(signature)
            except ValueError:
                logger.exception(f"Malformed transaction signature: {signature}")
                return ConfirmationStatus.UNCONFIRMED

        await self._rate_limiter.acquire()
        client = await self.get_client()
        try:
            await client.confirm_transaction(
                signature, commitment=commitment, sleep_seconds=1
            )
        except Exception:
            logger.exception(f"Failed to confirm transaction {signature}")
            return ConfirmationStatus.UNCONFIRMED

        return await self.verify_transaction_status(signature)

    async def verify_transaction_succeeded(self, signature: str | Signature) -> bool:
        """Check whether a landed transaction actually executed successfully.

        Bool wrapper around :meth:`verify_transaction_status`, kept because most
        callers only need to know whether to carry on. See
        :meth:`confirm_transaction` for why this does not return the enum.

        Args:
            signature: Transaction signature, base58 string or Signature

        Returns:
            Whether the transaction executed without a program error
        """
        status = await self.verify_transaction_status(signature)
        return status is ConfirmationStatus.SUCCESS

    async def verify_transaction_status(
        self, signature: str | Signature
    ) -> ConfirmationStatus:
        """Read what actually happened to a landed transaction.

        Landing in a block and succeeding are different things: RPC reports a
        revert in `meta.err`, so a transaction can be "confirmed" and still have
        done nothing. Split out from :meth:`confirm_transaction` so the check can
        be run against a transaction that landed some time ago — signature
        statuses fall out of the RPC's recent history, but `getTransaction` does
        not. That makes this the right call for re-checking a transaction whose
        first confirmation came back UNCONFIRMED.

        Args:
            signature: Transaction signature, base58 string or Signature

        Returns:
            SUCCESS, REVERTED, or UNCONFIRMED
        """
        signature = str(signature)
        result = await self._get_transaction_result(signature)
        if not result:
            logger.warning(
                f"Could not fetch transaction {signature[:16]}... "
                f"to verify execution — treating as unconfirmed"
            )
            return ConfirmationStatus.UNCONFIRMED

        tx_err = result.get("meta", {}).get("err")
        if tx_err:
            detail = _describe_program_error(tx_err)
            logger.error(
                f"Transaction {signature[:16]}... confirmed but failed: {tx_err}"
                + (f" ({detail})" if detail else "")
            )
            return ConfirmationStatus.REVERTED

        return ConfirmationStatus.SUCCESS

    async def get_transaction_token_balance(
        self, signature: str | Signature, user_pubkey: Pubkey, mint: Pubkey
    ) -> int | None:
        """Get the user's token balance after a transaction from postTokenBalances.

        Args:
            signature: Transaction signature, base58 string or Signature
            user_pubkey: User's wallet public key
            mint: Token mint address

        Returns:
            Token balance (raw amount) after transaction, or None if not found
        """
        result = await self._get_transaction_result(signature)
        if not result:
            return None

        meta = result.get("meta", {})
        post_token_balances = meta.get("postTokenBalances", [])

        user_str = str(user_pubkey)
        mint_str = str(mint)

        for balance in post_token_balances:
            if balance.get("owner") == user_str and balance.get("mint") == mint_str:
                ui_amount = balance.get("uiTokenAmount", {})
                amount_str = ui_amount.get("amount")
                if amount_str:
                    return int(amount_str)

        return None

    async def get_buy_transaction_details(
        self,
        signature: str | Signature,
        mint: Pubkey,
        sol_destination: Pubkey,
        quote_mint: Pubkey | None = None,
    ) -> tuple[int | None, int | None]:
        """Get actual tokens received and quote spent from a buy transaction.

        Uses preBalances/postBalances to find exact SOL transferred to the
        pool/curve and pre/post token balance diff to find tokens received.
        For coins paired against an SPL quote asset (e.g. USDC) the quote spend
        does not show up in lamport balances, so it is read from the quote
        mint's token balance deltas instead.

        Args:
            signature: Transaction signature, base58 string or Signature
            mint: Token mint address
            sol_destination: Address where SOL is sent (bonding curve for pump.fun,
                           quote_vault for letsbonk)
            quote_mint: Quote mint of the coin. Pass None or wrapped SOL for
                       SOL-paired coins.

        Returns:
            Tuple of (tokens_received_raw, quote_spent_raw), or (None, None)
        """
        # Normalized up front: the log lines below slice it, which a Signature
        # does not support.
        signature = str(signature)
        result = await self._get_transaction_result(signature)
        if not result:
            return None, None

        meta = result.get("meta", {})

        # Check for transaction execution errors (e.g., MaxLoadedAccountsDataSizeExceeded)
        tx_err = meta.get("err")
        if tx_err:
            detail = _describe_program_error(tx_err)
            logger.error(
                f"Transaction {signature[:16]}... failed with error: {tx_err}"
                + (f" ({detail})" if detail else "")
            )
            return None, None

        # Get tokens received from pre/post token balance diff
        # This works for Token2022 where owner might be different
        tokens_received = self._extract_positive_token_diff(meta, str(mint))
        if tokens_received is not None:
            logger.info(f"Tokens received from tx: {tokens_received}")

        # Non-SOL quote assets move as SPL token transfers, so the lamport
        # deltas below would report only rent/fees. Read the quote spend from
        # the quote mint's token balance deltas: the positive diff is the
        # curve's quote vault receiving what the buyer paid.
        if quote_mint is not None and not is_sol_paired(quote_mint):
            quote_spent = self._extract_positive_token_diff(meta, str(quote_mint))
            if quote_spent is None:
                logger.warning(
                    f"No positive {quote_mint} balance diff found in tx "
                    f"{signature[:16]}...; cannot determine quote spent"
                )
            else:
                logger.info(f"Quote spent from tx: {quote_spent} (mint {quote_mint})")
            return tokens_received, quote_spent

        # Get SOL spent from preBalances/postBalances at sol_destination
        sol_destination_str = str(sol_destination)
        sol_spent = None
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        account_keys = (
            result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )

        for i, key in enumerate(account_keys):
            key_str = key if isinstance(key, str) else key.get("pubkey", "")
            if key_str == sol_destination_str:
                if i < len(pre_balances) and i < len(post_balances):
                    sol_spent = post_balances[i] - pre_balances[i]
                    if sol_spent > 0:
                        logger.info(f"SOL to pool/curve: {sol_spent} lamports")
                    else:
                        logger.warning(
                            f"SOL destination balance change not positive: {sol_spent}"
                        )
                        sol_spent = None
                break

        return tokens_received, sol_spent

    @staticmethod
    def _extract_positive_token_diff(meta: dict, mint_str: str) -> int | None:
        """Find the largest positive token balance change for a mint in a tx.

        Args:
            meta: Transaction meta containing pre/postTokenBalances
            mint_str: Mint address to look for

        Returns:
            Raw positive balance delta, or None if no account gained this mint
        """
        pre_by_idx = {
            b.get("accountIndex"): b for b in meta.get("preTokenBalances", [])
        }
        post_by_idx = {
            b.get("accountIndex"): b for b in meta.get("postTokenBalances", [])
        }

        best: int | None = None
        for idx in set(pre_by_idx) | set(post_by_idx):
            pre = pre_by_idx.get(idx)
            post = post_by_idx.get(idx)

            if (post or pre).get("mint", "") != mint_str:
                continue

            pre_amount = (
                int(pre.get("uiTokenAmount", {}).get("amount", 0)) if pre else 0
            )
            post_amount = (
                int(post.get("uiTokenAmount", {}).get("amount", 0)) if post else 0
            )
            diff = post_amount - pre_amount

            if diff > 0 and (best is None or diff > best):
                best = diff

        return best

    async def _get_transaction_result(
        self,
        signature: str | Signature,
        budget_seconds: float = TX_RESULT_RETRY_BUDGET,
    ) -> dict | None:
        """Fetch transaction result from RPC, retrying while it is not visible.

        A null result is ambiguous: the transaction may not exist, or the node
        answering may simply be behind the one that confirmed the signature.
        Retrying within a budget separates the two, so a trade that landed is
        not read back as a failure.

        Args:
            signature: Transaction signature, base58 string or Signature
            budget_seconds: How long to keep retrying while the RPC cannot see
                the transaction. Bounded so a signature that truly does not
                exist still returns. It is the whole budget, not just the gap
                between attempts: each lookup is given the time left on it, so
                a stalled endpoint cannot stretch the call past it.

        Returns:
            Transaction result dict or None
        """
        # A Signature is not JSON serializable, so it has to be stringified here
        # rather than relying on every caller to remember.
        signature = str(signature)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    # Without this the RPC rejects every versioned (v0)
                    # transaction with -32015, so meta.err cannot be read and a
                    # perfectly good trade reads back as unconfirmed.
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }

        deadline = monotonic() + budget_seconds
        attempts = 0
        while True:
            attempts += 1
            # Hand the lookup what is left of the budget rather than letting it
            # run its own retry schedule to completion. The deadline check below
            # only runs once post_rpc returns, so without this the real worst
            # case is budget_seconds plus one full post_rpc backoff.
            response = await self.post_rpc(
                body, deadline_seconds=max(0.0, deadline - monotonic())
            )
            result = response.get("result") if response else None
            if result and "meta" in result:
                if attempts > 1:
                    logger.info(
                        f"Transaction {signature[:16]}... became visible on "
                        f"attempt {attempts}"
                    )
                return result

            if monotonic() + TX_RESULT_RETRY_DELAY > deadline:
                logger.warning(
                    f"Failed to get transaction {signature[:16]}... after "
                    f"{attempts} attempt(s) within {budget_seconds:.1f}s"
                )
                return None

            await asyncio.sleep(TX_RESULT_RETRY_DELAY)

    async def post_rpc(
        self,
        body: dict[str, Any],
        max_retries: int = 3,
        max_429_retries: int = 10,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Send a raw RPC request with rate limiting, retry, and 429 handling.

        Attempts and wall time are bounded separately. Without a deadline the
        retry schedule alone can keep one call going for minutes — three error
        retries backing off 1, 2, 4 ... 16s, or ten 429 retries waiting up to
        30s each and honouring a `Retry-After` header of any size. Every caller
        inherits that, and on the trade path a confirmation that blocks for
        minutes holds up the whole bot.

        Args:
            body: JSON-RPC request body.
            max_retries: Maximum number of retry attempts for errors.
            max_429_retries: Maximum number of retry attempts for 429 rate limits.
            deadline_seconds: Overall wall-clock budget for this call, covering
                every attempt and every backoff between them. `None` (the
                default) keeps the historical behaviour: attempts are bounded,
                elapsed time is not. A caller that would rather have a slow
                truthful answer than a punctual wrong one should leave it unset
                or pass a generous value.

        Returns:
            Parsed JSON response, or None if all attempts fail.
        """
        method = body.get("method", "unknown")
        error_attempts = 0
        rate_limit_attempts = 0
        deadline = _Deadline(deadline_seconds)

        while error_attempts < max_retries:
            if deadline.expired():
                logger.warning(f"RPC request {method} exceeded its {deadline}")
                break

            try:
                await self._rate_limiter.acquire()
                session = await self._get_session()

                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                ) as response:
                    if response.status == HTTP_TOO_MANY_REQUESTS:
                        rate_limit_attempts += 1
                        if rate_limit_attempts >= max_429_retries:
                            logger.error(
                                f"RPC rate limited (429) on {method}, "
                                f"exhausted {max_429_retries} rate-limit retries"
                            )
                            break
                        total_wait = _retry_after_seconds(
                            response.headers.get("Retry-After"), rate_limit_attempts
                        )
                        logger.warning(
                            f"RPC rate limited (429) on {method}, "
                            f"429 retry {rate_limit_attempts}/{max_429_retries}, "
                            f"waiting {total_wait:.1f}s"
                        )
                        if not deadline.allows(total_wait):
                            logger.warning(
                                f"RPC request {method} gave up on its {deadline} "
                                f"(next retry would wait {total_wait:.1f}s)"
                            )
                            break
                        await asyncio.sleep(total_wait)
                        continue

                    response.raise_for_status()
                    return await response.json()

            except aiohttp.ContentTypeError:
                logger.exception(f"Failed to decode RPC response for {method}")
                break

            # asyncio.TimeoutError is what aiohttp raises when the request
            # timeout fires, and it is not an aiohttp.ClientError — without it
            # here every RPC timeout propagated out of post_rpc unretried and
            # crashed the caller with an exception whose str() is empty.
            except (TimeoutError, aiohttp.ClientError):
                error_attempts += 1
                if error_attempts >= max_retries:
                    logger.exception(
                        f"RPC request {method} failed after {max_retries} attempts"
                    )
                    break

                wait_time = min(2 ** (error_attempts - 1), 16)
                jitter = wait_time * random.uniform(0, 0.25)  # noqa: S311
                logger.warning(
                    f"RPC request {method} failed "
                    f"(attempt {error_attempts}/{max_retries}), "
                    f"retrying in {wait_time + jitter:.1f}s"
                )
                if not deadline.allows(wait_time + jitter):
                    logger.warning(
                        f"RPC request {method} gave up on its {deadline} "
                        f"(next retry would wait {wait_time + jitter:.1f}s)"
                    )
                    break
                await asyncio.sleep(wait_time + jitter)

        return None

"""Solana client abstraction for blockchain operations."""

import asyncio
import base64
import json
import random
import socket
import struct
from collections.abc import Callable
from time import monotonic
from typing import Any

import aiohttp
import httpx2
from aiohttp.http_exceptions import HttpProcessingError
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.core import (
    RPCException,
    TransactionExpiredBlockheightExceededError,
    TxOptsModel,
    UnconfirmedTxError,
)
from solders.account import Account
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from core.rpc_rate_limiter import TokenBucketRateLimiter
from core.trade_deltas import TradeDeltas, paid_by, read_trade_deltas
from interfaces.core import ConfirmationStatus, Platform
from utils.idl_manager import get_idl_parser
from utils.logger import get_logger

logger = get_logger(__name__)

HTTP_TOO_MANY_REQUESTS = 429
HTTP_CLIENT_ERROR = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

# Length of the `[instruction_index, error_detail]` pair inside
# `{"InstructionError": [...]}` -- fixed by the RPC's `meta.err` shape.
INSTRUCTION_ERROR_PAIR_LEN = 2

# How long getTransaction keeps retrying a signature the node has not caught up
# to yet, and the pause between attempts. A null result means "I cannot see this
# transaction", which on a load-balanced endpoint is not the same as "it failed".
TX_RESULT_RETRY_BUDGET = 5.0
TX_RESULT_RETRY_DELAY = 0.4


class _Deadline:
    """An optional wall-clock budget shared by every attempt of one RPC call.

    Attempts and elapsed time are different bounds; this carries the second, so a
    caller can cap waiting without touching the retry counts.
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
        """Check whether a backoff fits in what is left of the budget.

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
    """Create a SetLoadedAccountsDataSizeLimit instruction to reduce CU cost.

    A transaction may load up to 64MB of account data by default, costing 16k CU
    (8 CU per 32KB); a lower limit reduces that and improves priority. The saving
    is not visible in "consumed CU", which only counts execution CU.

    Args:
        bytes_limit: Max account data size in bytes

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

    Digs `{"InstructionError": [idx, {"Custom": n}]}` out of `meta.err` and looks
    the code up in pump.fun's IDL error table. `SolanaClient` is shared across
    platforms and carries no record of which program a transaction invoked, so
    only pump.fun's table (`idl/pump_fun_idl.json`) is ever checked. A revert on
    another program is described against the wrong table if its numeric code is
    also defined there, and left unnamed otherwise. An unrecognized shape returns
    None, never raises.

    Args:
        err: The raw `meta.err` value from a `getTransaction` response.

    Returns:
        A description like "pump.fun IDL: 6062 BuybackFeeRecipientMissing", or
        None. The "pump.fun IDL:" prefix must stay in the rendered string: a
        reader of a log line needs to know the name is pump.fun's interpretation,
        not a fact about whichever program actually reverted.
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


# What a failed request says, matched on the error or on the OS error inside
# a connection error, in this order. Each is a plain reason naming no URL.
_UNREACHABLE_CAUSES = (
    (aiohttp.InvalidURL, "not an http(s) URL"),
    (aiohttp.NonHttpUrlClientError, "not an http(s) URL"),
    (aiohttp.TooManyRedirects, "too many redirects"),
    (aiohttp.ClientSSLError, "TLS handshake failed"),
    (aiohttp.ServerDisconnectedError, "the server closed the connection"),
    (aiohttp.ClientPayloadError, "the answer was cut off"),
    (socket.gaierror, "cannot resolve the host name"),
    (ConnectionRefusedError, "connection refused"),
    # IDNA rejects the name before any lookup, e.g. a label over 63 characters.
    (UnicodeError, "invalid host name"),
    (ValueError, "not a valid URL"),
)


def _unreachable_cause(error: Exception, timeout: float) -> str:
    """A few plain words on why a request got no answer.

    Never the exception's own text: aiohttp's carries the URL.
    """
    if isinstance(error, TimeoutError):
        return f"no answer within {timeout:g} s"
    os_error = getattr(error, "os_error", None)
    for kind, cause in _UNREACHABLE_CAUSES:
        if isinstance(error, kind) or isinstance(os_error, kind):
            return cause
    if isinstance(error, aiohttp.ClientResponseError):
        # aiohttp reports a reply it could not parse as a 400 of its own.
        if isinstance(error.__cause__, HttpProcessingError):
            return "the answer is not HTTP"
        return f"HTTP {error.status}"
    if isinstance(os_error, OSError) and os_error.strerror:
        return os_error.strerror
    return type(error).__name__


def _is_json_rpc(body: bytes) -> bool:
    """Whether a reply body is a JSON-RPC envelope (result or error).

    A Solana node always sends the `jsonrpc` key; a gateway's own
    `{"error": ...}` page does not, so it does not count as an answer.
    """
    try:
        answer = json.loads(body)
    except ValueError:
        return False
    return isinstance(answer, dict) and "jsonrpc" in answer


async def rpc_endpoint_problem(
    endpoint: str, attempts: int = 3, timeout: float = 10.0
) -> str | None:
    """What keeps `endpoint` from serving the bot, or None when it answers.

    Sends `getHealth` up to `attempts` times, 1 s then 2 s apart, each bounded
    by `timeout` seconds. A JSON-RPC envelope is an answer whatever the HTTP
    status, an error object included (a 503 from a node that is behind, a
    400), and so is HTTP 429. HTTP 401 and 403 are final at once.

    Returns:
        None, `rejected the request (HTTP 401)` (or 403), or
        `is unreachable (<cause>)`; never a URL
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
    reason = None
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(2 ** (attempt - 1))
            try:
                async with session.post(endpoint, json=body) as response:
                    if response.status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                        return f"rejected the request (HTTP {response.status})"
                    if response.status == HTTP_TOO_MANY_REQUESTS or _is_json_rpc(
                        await response.read()
                    ):
                        return None
                    reason = (
                        f"HTTP {response.status}"
                        if response.status >= HTTP_CLIENT_ERROR
                        else "the answer is not JSON-RPC"
                    )
            except (TimeoutError, aiohttp.ClientError, ValueError) as e:
                reason = _unreachable_cause(e, timeout)
    return f"is unreachable ({reason})"


class TransactionNotSent(Exception):  # noqa: N818 - names an outcome
    """No attempt of a send can have reached the network.

    Every attempt either never left this process (no connection, none free in
    the pool) or was refused by the node with an error before it took the
    transaction. Any other failure, a read timeout above all, may have been
    accepted and is raised as it is.
    """


# HTTP statuses from here up are server errors: a proxy may have passed it on.
_FIRST_SERVER_ERROR = 500


def _never_sent(error: Exception) -> bool:
    """Whether a failed send attempt provably did not reach the network."""
    if isinstance(error, RPCException):
        return True  # the node answered with an error: it took nothing
    cause = error.__cause__ or error
    if isinstance(
        cause, httpx2.ConnectError | httpx2.ConnectTimeout | httpx2.PoolTimeout
    ):
        return True
    return (
        isinstance(cause, httpx2.HTTPStatusError)
        and cause.response.status_code < _FIRST_SERVER_ERROR  # a 4xx: refused
    )


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    #: The block height past which the cached blockhash can no longer land.
    _cached_last_valid_block_height: int | None = None

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
                await self._rate_limiter.acquire()
                client = await self.get_client()
                latest = (
                    await client.get_latest_blockhash(commitment="processed")
                ).value
                async with self._blockhash_lock:
                    self._cached_blockhash = latest.blockhash
                    self._cached_last_valid_block_height = (
                        latest.last_valid_block_height
                    )
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
        """Get or create the AsyncClient instance."""
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
        self,
        pubkey: Pubkey,
        commitment: str | None = None,
        min_context_slot: int | None = None,
    ) -> Account:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account
            commitment: Optional commitment override (e.g., "processed" for
                fresh state right after a geyser event). None is solana-py's
                default, "finalized", which can lag "confirmed": an account a
                buy just created can read as missing.
            min_context_slot: Refuse an answer from a node that has not reached
                this slot: it raises instead of reporting an account it has not
                seen yet as missing. Reads at "confirmed" unless `commitment`.

        Returns:
            The solders `Account` -- callers read attributes like `.data` and
            `.owner`, never subscript it.

        Raises:
            ValueError: If account doesn't exist
            RuntimeError: With `min_context_slot`, the node could not answer,
                one behind that slot included
        """
        if min_context_slot is not None:
            return await self._account_at(
                pubkey, commitment or "confirmed", min_context_slot
            )
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await client.get_account_info(pubkey, **kwargs)
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        return response.value

    async def _account_at(
        self, pubkey: Pubkey, commitment: str, min_context_slot: int
    ) -> Account:
        """`getAccountInfo` with `minContextSlot`, which solana-py cannot send."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                str(pubkey),
                {
                    "encoding": "base64",
                    "commitment": commitment,
                    "minContextSlot": min_context_slot,
                },
            ],
        }
        response = await self.post_rpc(body)
        if not response or "result" not in response:
            error = (response or {}).get("error", {}).get("message", "no answer")
            raise RuntimeError(f"getAccountInfo {pubkey}: {error}")  # noqa: TRY003
        value = response["result"].get("value")
        if value is None:
            raise ValueError(f"Account {pubkey} not found")  # noqa: TRY003
        return Account(
            lamports=value["lamports"],
            data=base64.b64decode(value["data"][0]),
            owner=Pubkey.from_string(value["owner"]),
            executable=value.get("executable", False),
            rent_epoch=value.get("rentEpoch", 0),
        )

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Account | None]:
        """Get several accounts in one slot-consistent RPC round trip.

        A single getMultipleAccounts response is served by one node at one slot,
        unlike back-to-back get_account_info calls, which a load-balanced
        endpoint may serve from nodes seconds apart.

        Args:
            pubkeys: Public keys of the accounts
            commitment: Optional commitment override. None is solana-py's
                default, "finalized", which can lag "confirmed".

        Returns:
            One entry per pubkey, in order -- a solders `Account` or None.
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

        Defaults to "confirmed" rather than solana-py's "finalized", which can
        lag it: a finalized balance read right after a sell can still be the
        pre-sell amount, and cleanup then builds a burn for tokens the account no
        longer holds.

        Args:
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
        """Get the latest blockhash."""
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
        on_signed: Callable[[Signature, int | None], None] | None = None,
        *,
        quiet: bool = False,
    ) -> Signature:
        """Send a transaction with optional priority fee and compute unit limit.

        Args:
            instructions: Instructions to include in the transaction.
            signer_keypair: Keypair to sign the transaction.
            skip_preflight: Whether to skip preflight checks.
            max_retries: Maximum number of retry attempts.
            priority_fee: Priority fee in microlamports.
            compute_unit_limit: Compute unit limit. Defaults to 85,000.
            account_data_size_limit: Account data size limit in bytes. Reduces CU
                cost from 16k to ~128 CU; must be the first instruction.
            on_signed: Called with the signature and the block height past which
                the transaction can no longer land (None when unknown) once it
                is signed, before the first send, so a caller cancelled or failed
                mid-send still knows what may have reached the chain. Every retry
                resends the same transaction. Must not block: it runs on the
                send path.
            quiet: Log no traceback when the last attempt fails; the caller
                reports the failure.

        Returns:
            Transaction signature.

        Raises:
            TransactionNotSent: No attempt can have reached the network
            Exception: The last attempt's error, when one of them may have
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
        last_valid_block_height = self._cached_last_valid_block_height
        # No address lookup tables: the bot builds every account list itself.
        message = MessageV0.try_compile(
            signer_keypair.pubkey(), instructions, [], recent_blockhash
        )
        transaction = VersionedTransaction(message, [signer_keypair])
        if on_signed is not None:
            on_signed(transaction.signatures[0], last_valid_block_height)

        # Set once an attempt failed in a way that may still have delivered it.
        maybe_sent = False
        for attempt in range(max_retries):
            try:
                await self._rate_limiter.acquire()
                tx_opts = TxOptsModel(
                    skip_preflight=skip_preflight, preflight_commitment=Processed
                )
                response = await client.send_transaction(transaction, tx_opts)
                return response.value

            except Exception as e:
                maybe_sent = maybe_sent or not _never_sent(e)
                if attempt == max_retries - 1:
                    if not quiet:
                        logger.exception(
                            f"Failed to send transaction after {max_retries} attempts"
                        )
                    if not maybe_sent:
                        raise TransactionNotSent(str(e)) from e
                    raise

                wait_time = 2**attempt
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed: {e!s}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    async def confirm_transaction(
        self,
        signature: str | Signature,
        commitment: str = "confirmed",
        *,
        last_valid_block_height: int | None = None,
        quiet: bool = False,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        """Wait for transaction confirmation and verify execution success.

        Confirms the transaction landed on-chain, then checks meta.err: a
        transaction can be included in a block and still fail execution.

        Deliberately a bool. Returning :class:`ConfirmationStatus` would be a
        silent trap — every member is truthy, so every existing
        `if await client.confirm_transaction(sig):` would start passing
        unconditionally. Callers that need to tell a revert from an unknown call
        :meth:`confirm_transaction_detailed`.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level
            last_valid_block_height: Wait until the blockhash the transaction
                was signed with has expired, rather than a fixed 90 seconds
            quiet: Log nothing when the wait runs out (the blockhash expired,
                or 90 seconds passed without one), and any other error as one
                warning line without a traceback
            on_error: Called with any error other than the wait running out,
                in place of logging it; the caller decides how often to say so

        Returns:
            Whether transaction was confirmed AND executed successfully
        """
        status = await self.confirm_transaction_detailed(
            signature,
            commitment,
            last_valid_block_height=last_valid_block_height,
            quiet=quiet,
            on_error=on_error,
        )
        return status is ConfirmationStatus.SUCCESS

    async def confirm_transaction_detailed(  # noqa: PLR0913
        self,
        signature: str | Signature,
        commitment: str = "confirmed",
        *,
        last_valid_block_height: int | None = None,
        quiet: bool = False,
        on_error: Callable[[Exception], None] | None = None,
        on_revert: Callable[[str], None] | None = None,
    ) -> ConfirmationStatus:
        """Confirm a transaction, distinguishing a revert from an unknown.

        Same work as :meth:`confirm_transaction`, but it reports *why* a
        transaction did not succeed. Resubmitting a non-idempotent transaction
        after a confirmed revert is correct; resubmitting after a lookup that
        never answered can close an already-closed position twice.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level
            last_valid_block_height: As in :meth:`confirm_transaction`
            quiet: As in :meth:`confirm_transaction`
            on_error: As in :meth:`confirm_transaction`
            on_revert: As in :meth:`verify_transaction_status`

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
                signature,
                commitment=commitment,
                sleep_seconds=1,
                last_valid_block_height=last_valid_block_height,
            )
        except (TransactionExpiredBlockheightExceededError, UnconfirmedTxError):
            if not quiet:
                logger.exception(f"Failed to confirm transaction {signature}")
            return ConfirmationStatus.UNCONFIRMED
        except Exception as e:
            if on_error is not None:
                on_error(e)
            elif quiet:
                logger.warning(f"Could not confirm transaction {signature}: {e!s}")
            else:
                logger.exception(f"Failed to confirm transaction {signature}")
            return ConfirmationStatus.UNCONFIRMED

        return await self.verify_transaction_status(signature, on_revert=on_revert)

    async def get_signature_outcome(
        self,
        signature: str | Signature,
        *,
        search_history: bool = True,
        quiet: bool = False,
    ) -> tuple[ConfirmationStatus | None, int | None]:
        """What the cluster knows of a signature, and the slot it landed in.

        Args:
            search_history: Look past the node's recent status cache, which
                only holds the last few minutes; slower, so ask once
            quiet: Log nothing when the answer cannot be read

        Returns:
            The status: SUCCESS or REVERTED once confirmed; UNCONFIRMED when it
            is seen but not confirmed yet (an error at "processed" included: the
            fork may still be dropped), or the lookup failed; None when no node
            knows it, which settles nothing until its blockhash has expired.
            Then the slot it landed in, None unless seen.
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [
                [str(signature)],
                {"searchTransactionHistory": search_history},
            ],
        }
        response = await self.post_rpc(body)
        try:
            status = response["result"]["value"][0]
        except (KeyError, IndexError, TypeError):
            if not quiet:
                logger.warning(f"Could not read the status of {str(signature)[:16]}...")
            return ConfirmationStatus.UNCONFIRMED, None
        if status is None:
            return None, None
        slot = status.get("slot")
        if status.get("confirmationStatus") not in ("confirmed", "finalized"):
            return ConfirmationStatus.UNCONFIRMED, slot
        if status.get("err"):
            return ConfirmationStatus.REVERTED, slot
        return ConfirmationStatus.SUCCESS, slot

    async def get_token_accounts(
        self, owner: Pubkey, token_program: Pubkey
    ) -> list[tuple[Pubkey, bytes]]:
        """Every token account `owner` holds under `token_program`, at "confirmed".

        Returns:
            Each account's address and raw data

        Raises:
            RuntimeError: The node did not answer with them
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                str(owner),
                {"programId": str(token_program)},
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        }
        response = await self.post_rpc(body)
        try:
            return [
                (
                    Pubkey.from_string(entry["pubkey"]),
                    base64.b64decode(entry["account"]["data"][0]),
                )
                for entry in response["result"]["value"]
            ]
        except (KeyError, IndexError, TypeError, ValueError):
            error = (response or {}).get("error", {}).get("message", "no answer")
            raise RuntimeError(f"getTokenAccountsByOwner: {error}") from None  # noqa: TRY003

    async def get_block_height_and_slot(self) -> tuple[int, int]:
        """The cluster's block height and slot at "confirmed", read together.

        Raises:
            RuntimeError: The node did not answer with them
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getEpochInfo",
            "params": [{"commitment": "confirmed"}],
        }
        response = await self.post_rpc(body)
        info = response.get("result") if response else None
        try:
            return int(info["blockHeight"]), int(info["absoluteSlot"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("no block height in the answer") from None  # noqa: TRY003

    async def verify_transaction_succeeded(self, signature: str | Signature) -> bool:
        """Check whether a landed transaction actually executed successfully.

        Bool wrapper around :meth:`verify_transaction_status`. See
        :meth:`confirm_transaction` for why this does not return the enum.

        Args:
            signature: Transaction signature, base58 string or Signature

        Returns:
            Whether the transaction executed without a program error
        """
        status = await self.verify_transaction_status(signature)
        return status is ConfirmationStatus.SUCCESS

    async def verify_transaction_status(
        self,
        signature: str | Signature,
        *,
        on_revert: Callable[[str], None] | None = None,
    ) -> ConfirmationStatus:
        """Read what actually happened to a landed transaction.

        Landing in a block and succeeding are different things: a revert shows up
        in `meta.err`. Split out from :meth:`confirm_transaction` so it can run
        against a transaction that landed some time ago — signature statuses fall
        out of the RPC's recent history, `getTransaction` does not. Use this to
        re-check a transaction whose first confirmation came back UNCONFIRMED.

        Args:
            signature: Transaction signature, base58 string or Signature
            on_revert: Called with the error of a transaction that landed and
                reverted, in place of logging it, so the caller can say it in
                its own line

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
            error = f"{tx_err}" + (f" ({detail})" if detail else "")
            if on_revert is not None:
                on_revert(error)
            else:
                logger.error(
                    f"Transaction {signature[:16]}... landed and reverted: {error}"
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

    async def get_trade_deltas(
        self,
        signature: str | Signature,
        wallet: Pubkey,
        mint: Pubkey,
        quote_mint: Pubkey | None,
        venue_account: Pubkey | str | None = None,
    ) -> TradeDeltas | None:
        """What a landed buy or sell did to the wallet, from its transaction.

        Args:
            quote_mint: The coin's quote asset, raw or normalized
            venue_account: As in `read_trade_deltas`: where a buy's quote
                lands, to read its fill

        Returns:
            The deltas; None when the transaction cannot be read within
            `TX_RESULT_RETRY_BUDGET`, reverted, was not paid for by `wallet`,
            or shows no token account of the wallet's holding the coin
        """
        # Normalized up front: the log lines below slice it, which a Signature
        # does not support.
        signature = str(signature)
        result = await self._get_transaction_result(signature)
        if not result:
            return None
        tx_err = result.get("meta", {}).get("err")
        if tx_err:
            detail = _describe_program_error(tx_err)
            logger.error(
                f"Transaction {signature[:16]}... failed with error: {tx_err}"
                + (f" ({detail})" if detail else "")
            )
            return None
        if not paid_by(result, wallet):
            logger.warning(
                f"Transaction {signature[:16]}... was not paid for by {wallet}; "
                f"its amounts are not read"
            )
            return None
        deltas = read_trade_deltas(result, wallet, mint, quote_mint, venue_account)
        if deltas is None:
            logger.warning(
                f"Transaction {signature[:16]}... shows no token account of "
                f"{wallet} holding {mint}"
            )
        return deltas

    async def _get_transaction_result(
        self,
        signature: str | Signature,
        budget_seconds: float = TX_RESULT_RETRY_BUDGET,
    ) -> dict | None:
        """Fetch transaction result from RPC, retrying while it is not visible.

        A null result is ambiguous: the transaction may not exist, or the node
        answering may be behind the one that confirmed the signature. Retrying
        within a budget separates the two.

        Args:
            signature: Transaction signature, base58 string or Signature
            budget_seconds: Whole wall-clock budget, not just the gap between
                attempts — each lookup is given the time left on it, so a stalled
                endpoint cannot stretch the call past it.

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
                    # Without this the RPC rejects every versioned transaction
                    # with -32015, so meta.err cannot be read and a good trade
                    # reads back as unconfirmed. Must cover v1, not just v0: this
                    # also reads back signatures the bot did not send.
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        }

        deadline = monotonic() + budget_seconds
        attempts = 0
        while True:
            attempts += 1
            # Hand the lookup what is left of the budget: the deadline check
            # below only runs once post_rpc returns, so otherwise the worst case
            # is budget_seconds plus one full post_rpc backoff.
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
        retries backing off 1, 2, 4 ... 16s, or ten 429 retries waiting up to 30s
        each and honouring a `Retry-After` header of any size.

        Args:
            body: JSON-RPC request body.
            max_retries: Maximum retry attempts for errors.
            max_429_retries: Maximum retry attempts for 429 rate limits.
            deadline_seconds: Wall-clock budget covering every attempt and every
                backoff between them. `None` bounds attempts only. Leave it unset
                when a slow truthful answer beats a punctual wrong one.

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

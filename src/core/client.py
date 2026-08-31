"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import hashlib
import json
import random
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any

import aiohttp
from httpx import HTTPError
from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.core import RPCException
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from core.execution_policy import ExecutionBlocked, ExecutionPolicy
from core.pubkeys import is_sol_paired
from core.rpc_rate_limiter import TokenBucketRateLimiter
from core.transaction_ledger import TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus
from utils.logger import get_logger

logger = get_logger(__name__)

HTTP_TOO_MANY_REQUESTS = 429

DEFAULT_RPC_DEADLINE_SECONDS = 30.0
DEFAULT_BLOCKHASH_READY_TIMEOUT_SECONDS = 10.0
MAX_LOADED_ACCOUNT_DATA_SIZE_BYTES = 16 * 1024 * 1024
MAX_COMPUTE_UNIT_LIMIT = 1_400_000
MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS = 2**64 - 1
RAW_RPC_SUBMISSION_METHODS = frozenset({"sendTransaction", "requestAirdrop"})
LAMPORTS_PER_SIGNATURE = 5_000
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)


class JsonRpcError(RuntimeError):
    """A JSON-RPC error returned in an otherwise successful HTTP response."""

    def __init__(self, method: str, error: object):
        self.method = method
        self.error = error
        super().__init__(f"JSON-RPC {method} failed: {error}")


class TransactionSubmissionUnknown(RuntimeError):
    """Raised when a signed transaction may have reached the RPC node."""

    def __init__(self, signature: str, error: BaseException | str):
        self.signature = signature
        self.error = str(error) or type(error).__name__
        super().__init__(f"Submission outcome for {signature} is unknown: {self.error}")


@dataclass(frozen=True, slots=True)
class _BlockhashContext:
    blockhash: Hash
    last_valid_block_height: int


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
    if (
        isinstance(bytes_limit, bool)
        or not isinstance(bytes_limit, int)
        or not 0 < bytes_limit <= MAX_LOADED_ACCOUNT_DATA_SIZE_BYTES
    ):
        raise ValueError(
            "bytes_limit must be an integer between 1 and "
            f"{MAX_LOADED_ACCOUNT_DATA_SIZE_BYTES} bytes"
        )
    compute_budget_program = Pubkey.from_string(
        "ComputeBudget111111111111111111111111111111"
    )

    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(compute_budget_program, data, [])


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(
        self,
        rpc_endpoint: str,
        max_rps: float = 25.0,
        *,
        execution_policy: ExecutionPolicy | None = None,
        ledger: TransactionLedger | None = None,
    ):
        """Initialize Solana client with an explicit, dry-run-safe policy."""
        self.rpc_endpoint = rpc_endpoint
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.ledger = ledger
        self._client: AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._cached_blockhash: _BlockhashContext | None = None
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_ready = asyncio.Event()
        self._submission_validity: dict[str, int] = {}
        self._intent_signatures: dict[str, str] = {}
        self._signature_intents: dict[str, str] = {}
        self._blockhash_updater_task: asyncio.Task[None] | None = None
        self._rate_limiter = TokenBucketRateLimiter(max_rps=max_rps)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    def _ensure_blockhash_updater(self) -> None:
        if (
            self._blockhash_updater_task is not None
            and not self._blockhash_updater_task.done()
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._blockhash_updater_task = loop.create_task(self.start_blockhash_updater())

    async def start_blockhash_updater(self, interval: float = 5.0) -> None:
        """Keep a recent blockhash and its validity height available."""
        while True:
            try:
                context = await self._fetch_latest_blockhash_context()
                async with self._blockhash_lock:
                    self._cached_blockhash = context
                    self._blockhash_ready.set()
            except Exception as exc:
                logger.warning(f"Blockhash fetch failed: {exc!s}")
            finally:
                await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        """Wait for and return the most recently cached blockhash."""
        return (await self._get_cached_blockhash_context()).blockhash

    async def _get_cached_blockhash_context(self) -> _BlockhashContext:
        self._ensure_blockhash_updater()
        try:
            await asyncio.wait_for(
                self._blockhash_ready.wait(),
                timeout=DEFAULT_BLOCKHASH_READY_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise RuntimeError("Blockhash updater did not become ready") from exc
        async with self._blockhash_lock:
            if self._cached_blockhash is None:
                raise RuntimeError("Blockhash updater signaled without a blockhash")
            return self._cached_blockhash

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance safely under concurrency."""
        async with self._client_lock:
            if self._client is None:
                self._client = AsyncClient(self.rpc_endpoint)
            return self._client

    async def _read_rpc(
        self,
        operation: Callable[[AsyncClient], Awaitable[Any]],
        *,
        max_attempts: int = 3,
        deadline_seconds: float = DEFAULT_RPC_DEADLINE_SECONDS,
    ) -> Any:
        """Run an idempotent RPC read with bounded transport retries."""
        deadline = time.monotonic() + deadline_seconds
        last_error: BaseException | None = None
        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._rate_limiter.acquire(),
                    timeout=remaining,
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RPC read deadline exceeded")
                client = await self.get_client()
                return await asyncio.wait_for(
                    operation(client),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if not self._is_transport_exception(exc):
                    raise
                last_error = exc
                if attempt == max_attempts - 1:
                    raise
        raise TimeoutError("RPC read deadline exceeded") from last_error

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
    ) -> dict[str, Any]:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account
            commitment: Optional commitment override (e.g., "processed" for
                fresh state right after a geyser event; default "confirmed")

        Returns:
            Account info response

        Raises:
            ValueError: If account doesn't exist or has no data
        """
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await self._read_rpc(
            lambda client: client.get_account_info(pubkey, **kwargs)
        )
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        return response.value

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Any]:
        """Get several accounts in one slot-consistent RPC round trip.

        A single getMultipleAccounts response is served by one node at one
        slot, unlike back-to-back get_account_info calls which a load-balanced
        endpoint may serve from nodes seconds apart (issue #170).

        Args:
            pubkeys: Public keys of the accounts
            commitment: Optional commitment override (default "confirmed")

        Returns:
            One entry per pubkey, in order; None for accounts that don't exist
        """
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await self._read_rpc(
            lambda client: client.get_multiple_accounts(pubkeys, **kwargs)
        )
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
        response = await self._read_rpc(
            lambda client: client.get_token_account_balance(
                token_account,
                commitment=commitment,
            )
        )
        if response.value:
            return int(response.value.amount)
        return 0

    async def _fetch_latest_blockhash_context(self) -> _BlockhashContext:
        response = await self._read_rpc(
            lambda client: client.get_latest_blockhash(commitment="processed")
        )
        return _BlockhashContext(
            blockhash=response.value.blockhash,
            last_valid_block_height=int(response.value.last_valid_block_height),
        )

    async def _refresh_blockhash(
        self, *, exclude: Hash | None = None
    ) -> _BlockhashContext:
        for _ in range(3):
            context = await self._fetch_latest_blockhash_context()
            if exclude is None or context.blockhash != exclude:
                async with self._blockhash_lock:
                    self._cached_blockhash = context
                    self._blockhash_ready.set()
                return context
            await asyncio.sleep(0.2)
        raise RuntimeError("RPC did not provide a replacement blockhash")

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash while retaining its validity height."""
        return (await self._fetch_latest_blockhash_context()).blockhash

    async def _blockhash_is_expired(self, context: _BlockhashContext) -> bool:
        response = await self._read_rpc(
            lambda client: client.get_block_height(commitment="finalized")
        )
        return int(response.value) > context.last_valid_block_height

    @staticmethod
    def _is_blockhash_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return (
            "blockhash not found" in text
            or "block height exceeded" in text
            or "blockheight exceeded" in text
            or "transactionexpiredblockheightexceeded" in text
        )

    async def _signature_was_seen(self, signature: Signature) -> bool:
        try:
            response = await self._read_rpc(
                lambda client: client.get_signature_statuses(
                    [signature],
                    search_transaction_history=True,
                )
            )
        except Exception as exc:
            logger.warning(
                f"Could not resolve submission state for {str(signature)[:16]}...: "
                f"{exc!s}"
            )
            return False
        return bool(response.value and response.value[0] is not None)

    @staticmethod
    def _derive_intent_id(
        message: Message,
        signer: Pubkey,
        quote_amount_raw: int | None,
        fee_lamports: int,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(bytes(message))
        digest.update(bytes(signer))
        digest.update(str(quote_amount_raw).encode())
        digest.update(str(fee_lamports).encode())
        return digest.hexdigest()

    def _remember_intent_signature(self, intent_id: str, signature: str) -> None:
        self._intent_signatures[intent_id] = signature
        self._signature_intents[signature] = intent_id

    def _release_intent_signature(self, signature: str) -> None:
        intent_id = self._signature_intents.pop(signature, None)
        if intent_id is not None:
            self._intent_signatures.pop(intent_id, None)

    @staticmethod
    def _is_transport_exception(exc: BaseException) -> bool:
        """Identify provider/HTTP failures with an ambiguous send outcome."""
        if isinstance(exc, RPCException):
            return False
        current: BaseException | None = exc
        visited: set[int] = set()
        transport_types = (TimeoutError, aiohttp.ClientError, HTTPError, OSError)
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (SolanaRpcException, *transport_types)):
                return True
            cause = current.__cause__
            context = current.__context__
            current = cause if cause is not None else context
        return False

    async def _record_unknown_outcome(
        self, signature: str, error: BaseException | str
    ) -> None:
        """Persist an ambiguous send so callers cannot submit a duplicate."""
        if self.ledger is not None:
            error_text = str(error)
            if not error_text:
                error_text = type(error).__name__
            await asyncio.to_thread(
                self.ledger.record_outcome,
                TransactionOutcome(
                    TransactionStatus.UNKNOWN,
                    signature,
                    error=error_text,
                ),
                allow_prepared=True,
            )

    async def _mark_submission_after_send(self, signature: str) -> None:
        """Finish the ledger transition without abandoning a SQLite thread."""
        if self.ledger is None:
            raise ExecutionBlocked("Submission transition requires a durable ledger")
        mark_task = asyncio.create_task(
            asyncio.to_thread(self.ledger.mark_submission_submitted, signature)
        )
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(mark_task)
                break
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                if mark_task.done():
                    await mark_task
                    break
        if cancellation is not None:
            raise cancellation

    def _validate_prepared_replay_policy(self, record: Any) -> None:
        """Revalidate current wallet and budget policy against exact stored wire."""
        if record.quote_amount_raw is None or record.fee_lamports is None:
            raise ExecutionBlocked(
                "Prepared transaction is missing durable budget metadata"
            )
        try:
            intent_signer = Pubkey.from_string(record.signer)
            transaction = Transaction.from_bytes(record.wire_bytes)
            wire_signer = transaction.message.account_keys[0]
            wire_signature = transaction.signatures[0]
        except (IndexError, TypeError, ValueError) as exc:
            raise ExecutionBlocked(
                "Prepared transaction wire cannot be safely validated"
            ) from exc
        if wire_signer != intent_signer or str(wire_signature) != record.signature:
            raise ExecutionBlocked(
                "Prepared transaction wire does not match its durable intent"
            )
        self.execution_policy.validate_wallet(intent_signer)
        self.execution_policy.validate_budgets(
            record.quote_amount_raw,
            record.fee_lamports,
        )

    async def _resubmit_prepared_wire(
        self,
        signature: str,
        wire_bytes: bytes,
        tx_opts: TxOpts,
    ) -> Signature:
        """Submit the exact reserved wire bytes without rebuilding them."""
        if self.ledger is None:
            raise ExecutionBlocked("Prepared recovery requires a durable ledger")
        parsed_signature = Signature.from_string(signature)
        send_started = False
        try:
            client = await self.get_client()
            await asyncio.wait_for(
                self._rate_limiter.acquire(),
                timeout=DEFAULT_RPC_DEADLINE_SECONDS,
            )
            send_started = True
            response = await asyncio.wait_for(
                client.send_raw_transaction(wire_bytes, tx_opts),
                timeout=DEFAULT_RPC_DEADLINE_SECONDS,
            )
            try:
                response_signature = response.value
            except (AttributeError, TypeError) as exc:
                raise ValueError("RPC response missing transaction signature") from exc
            await self._mark_submission_after_send(signature)
        except asyncio.CancelledError:
            if not send_started:
                released = await asyncio.shield(
                    asyncio.to_thread(
                        self.ledger.release_prepared_submission,
                        signature,
                    )
                )
                if released:
                    self._submission_validity.pop(signature, None)
                    self._release_intent_signature(signature)
            else:
                await asyncio.shield(
                    self._record_unknown_outcome(
                        signature,
                        "submission cancelled after the network send began",
                    )
                )
            raise
        except RPCException as exc:
            await self._record_unknown_outcome(signature, exc)
            raise TransactionSubmissionUnknown(signature, exc) from exc
        except BaseException as exc:
            if not send_started:
                released = await asyncio.shield(
                    asyncio.to_thread(
                        self.ledger.release_prepared_submission,
                        signature,
                    )
                )
                if released:
                    self._submission_validity.pop(signature, None)
                    self._release_intent_signature(signature)
                raise
            await self._record_unknown_outcome(signature, exc)
            raise TransactionSubmissionUnknown(signature, exc) from exc
        if response_signature != parsed_signature:
            error = "RPC returned a signature different from the prepared wire"
            await self._record_unknown_outcome(signature, error)
            logger.error(
                "RPC returned a mismatched signature for prepared transaction %s",
                signature[:16],
            )
            raise TransactionSubmissionUnknown(signature, error)
        return parsed_signature

    async def _reuse_active_submission(
        self,
        intent_id: str,
        record: Any,
        tx_opts: TxOpts,
    ) -> Signature | None:
        """Recover prepared bytes or surface an unresolved prior submission."""
        if self.ledger is None:
            raise ExecutionBlocked("Submission recovery requires a durable ledger")
        signature = record.signature
        self._remember_intent_signature(intent_id, signature)
        if record.state == "prepared":
            if record.wire_bytes is None:
                released = await asyncio.to_thread(
                    self.ledger.release_prepared_submission,
                    signature,
                )
                if not released:
                    raise TransactionSubmissionUnknown(
                        signature,
                        "prepared reservation changed during recovery",
                    )
                self._submission_validity.pop(signature, None)
                self._release_intent_signature(signature)
                return None
            if await self._current_block_height_exceeds(
                record.last_valid_block_height,
                commitment="finalized",
            ):
                raise TransactionSubmissionUnknown(
                    signature,
                    "exact prepared wire expired without terminal on-chain evidence",
                )
            self._validate_prepared_replay_policy(record)
            return await self._resubmit_prepared_wire(
                signature,
                record.wire_bytes,
                tx_opts,
            )
        outcome = await asyncio.to_thread(self.ledger.get_outcome, signature)
        if outcome is not None and outcome.status is TransactionStatus.SUCCESS:
            return Signature.from_string(signature)
        error = (
            outcome.error
            if outcome is not None and outcome.error
            else "prior submission has no terminal on-chain outcome"
        )
        raise TransactionSubmissionUnknown(signature, error)

    async def get_submission_receipt_destinations(
        self, signature: str | Signature
    ) -> tuple[Pubkey, ...] | None:
        """Return exact native-quote recipients bound to the submitted wire."""
        if self.ledger is None:
            return None
        raw_destinations = await asyncio.to_thread(
            self.ledger.get_receipt_destinations,
            str(signature),
        )
        if raw_destinations is None:
            return None
        try:
            return tuple(Pubkey.from_string(item) for item in raw_destinations)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Durable submission receipt context contains an invalid pubkey"
            ) from exc

    async def recover_active_submission(
        self,
        intent_id: str,
        *,
        skip_preflight: bool = True,
    ) -> Signature | None:
        """Recover an exact ledger-bound wire before any caller rebuilds it."""
        if not isinstance(intent_id, str) or not intent_id:
            raise ValueError("intent_id is required")
        if not isinstance(skip_preflight, bool):
            raise ValueError("skip_preflight must be a boolean")
        self.execution_policy.require_submission()
        self.execution_policy.validate_preflight(skip_preflight)
        if self.ledger is None:
            raise ExecutionBlocked("Submission recovery requires a durable ledger")
        record = await asyncio.to_thread(
            self.ledger.get_active_submission_record,
            intent_id,
        )
        if record is None:
            return None
        tx_opts = TxOpts(
            skip_preflight=skip_preflight,
            preflight_commitment=Processed,
            max_retries=0,
        )
        return await self._reuse_active_submission(intent_id, record, tx_opts)

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 1,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
        *,
        quote_amount_raw: int | None = None,
        fee_lamports: int | None = None,
        intent_id: str | None = None,
        receipt_destinations: tuple[str, ...] | None = None,
    ) -> Signature:
        """Build, sign, and submit a policy-authorized transaction.

        Ambiguous sends are durably recorded and raised with their deterministic
        signature. Newly signed retries are disabled: ``max_retries`` is kept
        as an explicit safety assertion and must be exactly one.
        """
        policy = self.execution_policy
        policy.require_submission()
        policy.validate_wallet(signer_keypair.pubkey())
        policy.validate_preflight(skip_preflight)
        if self.ledger is None:
            raise ExecutionBlocked(
                "Live transaction submission requires a durable TransactionLedger"
            )
        if not isinstance(skip_preflight, bool):
            raise ValueError("skip_preflight must be a boolean")

        if not isinstance(instructions, list) or any(
            not isinstance(instruction, Instruction) for instruction in instructions
        ):
            raise TypeError("instructions must be a list of solders Instruction values")
        if any(
            instruction.program_id == COMPUTE_BUDGET_PROGRAM_ID
            for instruction in instructions
        ):
            raise ExecutionBlocked(
                "Caller-supplied Compute Budget instructions are prohibited; "
                "use the explicit fee arguments"
            )
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries != 1
        ):
            raise ValueError(
                "newly signed transaction retries are disabled; max_retries must be 1"
            )
        for name, value in (
            ("priority_fee", priority_fee),
            ("compute_unit_limit", compute_unit_limit),
            ("account_data_size_limit", account_data_size_limit),
            ("quote_amount_raw", quote_amount_raw),
            ("fee_lamports", fee_lamports),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if compute_unit_limit is not None and not (
            0 < compute_unit_limit <= MAX_COMPUTE_UNIT_LIMIT
        ):
            raise ValueError(
                f"compute_unit_limit must be between 1 and {MAX_COMPUTE_UNIT_LIMIT}"
            )
        if (
            priority_fee is not None
            and priority_fee > MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS
        ):
            raise ValueError(
                "priority_fee exceeds the unsigned 64-bit wire encoding limit"
            )

        effective_cu_limit = (
            compute_unit_limit if compute_unit_limit is not None else 85_000
        )
        estimated_fee = (
            LAMPORTS_PER_SIGNATURE
            + ((priority_fee or 0) * effective_cu_limit + 999_999) // 1_000_000
        )
        if quote_amount_raw is None:
            raise ExecutionBlocked(
                "quote_amount_raw is required for every transaction submission"
            )
        validated_quote_amount = quote_amount_raw
        if fee_lamports is not None:
            policy.validate_budgets(validated_quote_amount, fee_lamports)
        fee_lamports = max(fee_lamports or 0, estimated_fee)
        policy.validate_budgets(validated_quote_amount, fee_lamports)

        logger.info(
            f"Priority fee in microlamports: {priority_fee if priority_fee else 0}"
        )

        if (
            priority_fee is not None
            or compute_unit_limit is not None
            or account_data_size_limit is not None
        ):
            fee_instructions: list[Instruction] = []
            if account_data_size_limit is not None:
                fee_instructions.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
                logger.info(f"Account data size limit: {account_data_size_limit} bytes")
            fee_instructions.append(set_compute_unit_limit(effective_cu_limit))
            if priority_fee is not None:
                fee_instructions.append(set_compute_unit_price(priority_fee))
            instructions = fee_instructions + instructions

        message = Message(instructions, signer_keypair.pubkey())
        message_hash = hashlib.sha256(bytes(message)).hexdigest()
        resolved_intent_id = intent_id or self._derive_intent_id(
            message,
            signer_keypair.pubkey(),
            quote_amount_raw,
            fee_lamports,
        )
        tx_opts = TxOpts(
            skip_preflight=skip_preflight,
            preflight_commitment=Processed,
            max_retries=0,
        )
        # Explicit intent IDs are logical operation keys. On restart, their
        # newly built message may differ because fee recipients, WSOL seeds,
        # and priority fees are dynamic. Recover the exact durable wire first.
        if intent_id is not None:
            existing_record = await asyncio.to_thread(
                self.ledger.get_active_submission_record,
                resolved_intent_id,
            )
            if existing_record is not None:
                reused_signature = await self._reuse_active_submission(
                    resolved_intent_id,
                    existing_record,
                    tx_opts,
                )
                if reused_signature is not None:
                    return reused_signature

        await asyncio.to_thread(
            self.ledger.record_intent,
            resolved_intent_id,
            str(signer_keypair.pubkey()),
            quote_amount_raw,
            fee_lamports,
            message_hash,
        )

        # Re-check after binding the intent so a concurrent reservation cannot
        # be bypassed between the first lookup and record_intent.
        existing_record = await asyncio.to_thread(
            self.ledger.get_active_submission_record,
            resolved_intent_id,
        )
        if existing_record is not None:
            reused_signature = await self._reuse_active_submission(
                resolved_intent_id,
                existing_record,
                tx_opts,
            )
            if reused_signature is not None:
                return reused_signature

        client = await self.get_client()
        context = await self._get_cached_blockhash_context()

        for attempt in range(max_retries):
            if await self._blockhash_is_expired(context):
                context = await self._refresh_blockhash(exclude=context.blockhash)

            transaction = Transaction(
                [signer_keypair],
                message,
                context.blockhash,
            )
            signature = transaction.signatures[0]
            signature_text = str(signature)
            wire_bytes = bytes(transaction)
            self._submission_validity[signature_text] = context.last_valid_block_height
            reserved_signature = await asyncio.to_thread(
                self.ledger.record_submission,
                resolved_intent_id,
                signature_text,
                str(context.blockhash),
                context.last_valid_block_height,
                wire_bytes=wire_bytes,
                state="prepared",
                receipt_destinations=receipt_destinations,
            )
            if reserved_signature != signature_text:
                self._submission_validity.pop(signature_text, None)
                existing_record = await asyncio.to_thread(
                    self.ledger.get_active_submission_record,
                    resolved_intent_id,
                )
                if existing_record is None:
                    raise RuntimeError(
                        "Ledger returned a conflicting submission without a record"
                    )
                reused_signature = await self._reuse_active_submission(
                    resolved_intent_id,
                    existing_record,
                    tx_opts,
                )
                if reused_signature is not None:
                    return reused_signature
                reserved_signature = await asyncio.to_thread(
                    self.ledger.record_submission,
                    resolved_intent_id,
                    signature_text,
                    str(context.blockhash),
                    context.last_valid_block_height,
                    wire_bytes=wire_bytes,
                    state="prepared",
                    receipt_destinations=receipt_destinations,
                )
                if reserved_signature != signature_text:
                    raise TransactionSubmissionUnknown(
                        reserved_signature,
                        "concurrent submission replaced a stale reservation",
                    )

            self._remember_intent_signature(resolved_intent_id, signature_text)
            send_started = False
            try:
                await asyncio.wait_for(
                    self._rate_limiter.acquire(),
                    timeout=DEFAULT_RPC_DEADLINE_SECONDS,
                )
                send_started = True
                response = await asyncio.wait_for(
                    client.send_transaction(transaction, tx_opts),
                    timeout=DEFAULT_RPC_DEADLINE_SECONDS,
                )
                try:
                    response_signature = response.value
                except (AttributeError, TypeError) as exc:
                    raise ValueError(
                        "RPC response missing transaction signature"
                    ) from exc
                await self._mark_submission_after_send(signature_text)
            except asyncio.CancelledError:
                if not send_started:
                    released = await asyncio.shield(
                        asyncio.to_thread(
                            self.ledger.release_prepared_submission,
                            signature_text,
                        )
                    )
                    if released:
                        self._submission_validity.pop(signature_text, None)
                        self._release_intent_signature(signature_text)
                else:
                    await asyncio.shield(
                        self._record_unknown_outcome(
                            signature_text,
                            "submission cancelled after the network send began",
                        )
                    )
                raise
            except RPCException as exc:
                await self._record_unknown_outcome(signature_text, exc)
                raise TransactionSubmissionUnknown(
                    signature_text,
                    exc,
                ) from exc
            except BaseException as exc:
                if not send_started:
                    released = await asyncio.shield(
                        asyncio.to_thread(
                            self.ledger.release_prepared_submission,
                            signature_text,
                        )
                    )
                    if released:
                        self._submission_validity.pop(signature_text, None)
                        self._release_intent_signature(signature_text)
                    raise
                await self._record_unknown_outcome(signature_text, exc)
                logger.warning(
                    f"Submission outcome for {signature_text[:16]}... is "
                    f"unknown after send failure: {exc!s}"
                )
                raise TransactionSubmissionUnknown(signature_text, exc) from exc

            if response_signature != signature:
                error = "RPC returned a signature different from signed bytes"
                await self._record_unknown_outcome(signature_text, error)
                logger.error(
                    "RPC returned a mismatched signature for %s",
                    signature_text[:16],
                )
                raise TransactionSubmissionUnknown(signature_text, error)
            return signature

    @staticmethod
    def _status_satisfies_commitment(status: Any, commitment: str) -> bool:
        confirmation_status = getattr(status, "confirmation_status", None)
        if confirmation_status is None:
            return False
        try:
            observed_rank = int(confirmation_status)
        except (TypeError, ValueError):
            normalized = str(confirmation_status).rsplit(".", 1)[-1].lower()
            observed_rank = {
                "processed": 0,
                "confirmed": 1,
                "finalized": 2,
            }.get(normalized, -1)
        required_rank = {
            "processed": 0,
            "confirmed": 1,
            "finalized": 2,
        }[commitment]
        return observed_rank >= required_rank

    async def confirm_transaction(
        self, signature: str | Signature, commitment: str = "confirmed"
    ) -> bool:
        """Compatibility wrapper returning true only for verified success."""
        outcome = await self.confirm_transaction_outcome(signature, commitment)
        return outcome.succeeded

    async def confirm_transaction_outcome(
        self,
        signature: str | Signature,
        commitment: str = "confirmed",
        *,
        timeout_seconds: float = 45.0,
        last_valid_block_height: int | None = None,
    ) -> TransactionOutcome:
        """Confirm a transaction without conflating reverts and missing evidence."""
        signature_text = str(signature)
        try:
            parsed_signature = (
                Signature.from_string(signature)
                if isinstance(signature, str)
                else signature
            )
        except ValueError:
            return TransactionOutcome(
                TransactionStatus.UNKNOWN,
                signature_text,
                error="malformed transaction signature",
            )
        if commitment not in {"confirmed", "finalized"}:
            raise ValueError("commitment must be confirmed or finalized")
        if last_valid_block_height is not None and (
            isinstance(last_valid_block_height, bool)
            or not isinstance(last_valid_block_height, int)
            or last_valid_block_height < 0
        ):
            raise ValueError(
                "last_valid_block_height must be a non-negative integer or None"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")

        if last_valid_block_height is None:
            last_valid_block_height = self._submission_validity.get(signature_text)
        if last_valid_block_height is None and self.ledger is not None:
            last_valid_block_height = await asyncio.to_thread(
                self.ledger.get_last_valid_block_height,
                signature_text,
            )

        confirmation_error: str | None = None
        confirmation_status: Any | None = None

        deadline = time.monotonic() + timeout_seconds
        try:
            await asyncio.wait_for(
                self._rate_limiter.acquire(),
                timeout=timeout_seconds,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("confirmation deadline exceeded")
            client = await self.get_client()
            confirmation_response = await asyncio.wait_for(
                client.confirm_transaction(
                    parsed_signature,
                    commitment=commitment,
                    sleep_seconds=1,
                    last_valid_block_height=last_valid_block_height,
                ),
                timeout=remaining,
            )
            if confirmation_response.value:
                confirmation_status = confirmation_response.value[0]
        except Exception as exc:
            confirmation_error = str(exc) or type(exc).__name__

        try:
            result = await self._get_transaction_result(
                signature_text,
                commitment=commitment,
            )
        except Exception as exc:
            result = None
            if confirmation_error is None:
                confirmation_error = str(exc) or type(exc).__name__

        if result is not None:
            tx_error = result["meta"]["err"]
            outcome = TransactionOutcome(
                (
                    TransactionStatus.SUCCESS
                    if tx_error is None
                    else TransactionStatus.REVERTED
                ),
                signature_text,
                error=(
                    None
                    if tx_error is None
                    else json.dumps(tx_error, sort_keys=True, default=str)
                ),
                slot=result.get("slot"),
            )
        elif (
            confirmation_status is not None
            and hasattr(confirmation_status, "err")
            and self._status_satisfies_commitment(
                confirmation_status,
                commitment,
            )
        ):
            status_error = confirmation_status.err
            outcome = TransactionOutcome(
                (
                    TransactionStatus.SUCCESS
                    if status_error is None
                    else TransactionStatus.REVERTED
                ),
                signature_text,
                error=(
                    None
                    if status_error is None
                    else json.dumps(status_error, sort_keys=True, default=str)
                ),
                slot=getattr(confirmation_status, "slot", None),
            )
        elif (
            last_valid_block_height is not None
            and await self._prove_transaction_expired(
                parsed_signature,
                last_valid_block_height,
                commitment,
            )
        ):
            outcome = TransactionOutcome(
                TransactionStatus.EXPIRED,
                signature_text,
                error=confirmation_error,
            )
        else:
            outcome = TransactionOutcome(
                TransactionStatus.UNKNOWN,
                signature_text,
                error=confirmation_error,
            )

        if self.ledger is not None:
            stored_outcome = await asyncio.to_thread(
                self.ledger.get_outcome,
                signature_text,
            )
            if (
                stored_outcome is not None
                and stored_outcome.status is not TransactionStatus.UNKNOWN
                and outcome.status in {TransactionStatus.UNKNOWN, stored_outcome.status}
            ):
                outcome = stored_outcome

        if self.ledger is not None:
            tracked_height = await asyncio.to_thread(
                self.ledger.get_last_valid_block_height,
                signature_text,
            )
            if tracked_height is not None:
                await asyncio.to_thread(
                    self.ledger.record_outcome,
                    outcome,
                    allow_prepared=True,
                )
        if outcome.status is not TransactionStatus.UNKNOWN:
            self._submission_validity.pop(signature_text, None)
        if outcome.status is TransactionStatus.EXPIRED:
            self._release_intent_signature(signature_text)
        return outcome

    async def _current_block_height_exceeds(
        self,
        last_valid_height: int,
        *,
        commitment: str = "confirmed",
    ) -> bool:
        """Read block height at a non-regressing commitment."""
        if commitment not in {"processed", "confirmed", "finalized"}:
            raise ValueError("invalid commitment")
        try:
            response = await self._read_rpc(
                lambda client: client.get_block_height(commitment=commitment)
            )
        except Exception as exc:
            logger.warning(f"Could not check blockhash expiry: {exc!s}")
            return False
        return int(response.value) > last_valid_height

    async def _read_signature_status(
        self, signature: Signature
    ) -> tuple[bool, Any | None]:
        """Return (available, status), distinguishing no status from RPC failure."""
        try:
            response = await self._read_rpc(
                lambda client: client.get_signature_statuses(
                    [signature],
                    search_transaction_history=True,
                )
            )
        except Exception as exc:
            logger.warning(
                f"Could not read status for {str(signature)[:16]}...: {exc!s}"
            )
            return False, None
        values = response.value
        return True, (values[0] if values else None)

    async def _read_transaction_presence(
        self, signature: str, commitment: str
    ) -> tuple[bool, bool]:
        """Return (available, exists) for a transaction history query."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        try:
            response = await self.post_rpc(
                body,
                deadline_seconds=DEFAULT_RPC_DEADLINE_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                f"Could not read transaction presence for {signature[:16]}...: {exc!s}"
            )
            return False, False
        if response is None or "result" not in response:
            return False, False
        return True, response["result"] is not None

    async def _prove_transaction_expired(
        self,
        signature: Signature,
        last_valid_height: int,
        requested_commitment: str,
    ) -> bool:
        """Fail closed until durable ledger-range coverage can prove expiry.

        Finalized RPC history is prunable, so block-height advancement plus an
        absent status or transaction cannot prove that a signed transaction
        never landed. Avoid spending RPC reads on evidence that can only produce
        the same unresolved result.
        """
        del signature, last_valid_height, requested_commitment
        return False

    async def verify_transaction_succeeded(self, signature: str | Signature) -> bool:
        """Compatibility check returning true only for a proven on-chain success."""
        signature_text = str(signature)
        try:
            result = await self._get_transaction_result(signature_text)
        except Exception as exc:
            logger.warning(
                f"Could not fetch transaction {signature_text[:16]}...: {exc!s}"
            )
            return False
        if result is None:
            return False
        tx_error = result.get("meta", {}).get("err")
        if tx_error:
            logger.error(
                f"Transaction {signature_text[:16]}... confirmed but failed: {tx_error}"
            )
            return False
        return True

    async def get_transaction_token_balance(
        self, signature: str | Signature, user_pubkey: Pubkey, mint: Pubkey
    ) -> int | None:
        """Get the user's token balance after a proven transaction.

        Args:
            signature: Transaction signature, base58 string or Signature
            user_pubkey: User's wallet public key
            mint: Token mint address

        Returns:
            Token balance (raw amount) after transaction, or None if not found
        """
        signature_text = str(signature)
        result = await self._get_transaction_result(signature_text)
        if not self._is_canonical_transaction_result(result, signature_text):
            return None

        meta = result["meta"]
        post_token_balances = meta.get("postTokenBalances")
        if not isinstance(post_token_balances, list):
            return None

        user_str = str(user_pubkey)
        mint_str = str(mint)
        for balance in post_token_balances:
            if not isinstance(balance, dict):
                return None
            if balance.get("owner") == user_str and balance.get("mint") == mint_str:
                return self._parse_raw_token_amount(balance)

        return None

    @staticmethod
    def _validated_lamport_balances(
        meta: object, account_count: int
    ) -> tuple[list[int], list[int]] | None:
        """Return canonical pre/post lamport arrays or fail closed."""
        if (
            not isinstance(meta, dict)
            or isinstance(account_count, bool)
            or not isinstance(account_count, int)
            or account_count < 0
        ):
            return None
        balances: list[list[int]] = []
        for field_name in ("preBalances", "postBalances"):
            raw_values = meta.get(field_name)
            if not isinstance(raw_values, list) or len(raw_values) != account_count:
                return None
            parsed: list[int] = []
            for value in raw_values:
                if type(value) is not int or value < 0 or value > 0xFFFF_FFFF_FFFF_FFFF:
                    return None
                parsed.append(value)
            balances.append(parsed)
        return balances[0], balances[1]

    @staticmethod
    def _parse_raw_token_amount(balance: object) -> int | None:
        """Parse Solana's canonical decimal-string token amount."""
        if not isinstance(balance, dict):
            return None
        ui_amount = balance.get("uiTokenAmount")
        if not isinstance(ui_amount, dict):
            return None
        raw_amount = ui_amount.get("amount")
        if (
            not isinstance(raw_amount, str)
            or not raw_amount
            or not raw_amount.isascii()
            or not raw_amount.isdecimal()
        ):
            return None
        amount = int(raw_amount)
        if amount > 0xFFFF_FFFF_FFFF_FFFF:
            return None
        return amount

    async def get_buy_transaction_details(
        self,
        signature: str | Signature,
        mint: Pubkey,
        sol_destination: Pubkey,
        quote_mint: Pubkey | None = None,
        *,
        quote_destinations: list[Pubkey] | tuple[Pubkey, ...] | None = None,
    ) -> tuple[int | None, int | None]:
        """Get actual tokens received and quote spent from a buy transaction.

        Uses preBalances/postBalances to find exact SOL transferred to the
        venue's complete quote-recipient set and pre/post token balance diff
        to find tokens received. For coins paired against an SPL quote asset
        (e.g. USDC), the quote spend is read from signer-owned quote-token
        balance deltas instead.

        ``quote_destinations`` must contain every expected native-SOL recipient
        besides ``sol_destination`` when fee routing splits the payment. If it
        is omitted, only ``sol_destination`` is attributed for compatibility
        with callers that do not have a venue account bundle.

        Args:
            signature: Signature, base58 string or solders Signature
            mint: Base token mint
            sol_destination: Primary native-SOL venue destination
            quote_mint: Quote mint; None or wrapped SOL means native SOL
            quote_destinations: Additional native-SOL recipient accounts

        Returns:
            Tuple of (tokens_received_raw, quote_spent_raw), or (None, None)
        """
        signature = str(signature)
        result = await self._get_transaction_result(signature)
        if not result or not isinstance(result, dict):
            return None, None

        meta = result.get("meta")
        if not isinstance(meta, dict):
            return None, None
        tx_err = meta.get("err")
        if tx_err:
            logger.error(f"Transaction {signature[:16]}... failed with error: {tx_err}")
            return None, None

        account_keys = (
            result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )
        if not isinstance(account_keys, list):
            return None, None
        normalized_account_keys: list[str] = []
        for key in account_keys:
            raw_key = (
                key
                if isinstance(key, str)
                else (key.get("pubkey") if isinstance(key, dict) else None)
            )
            if not isinstance(raw_key, str):
                return None, None
            try:
                normalized_account_keys.append(str(Pubkey.from_string(raw_key)))
            except (TypeError, ValueError):
                return None, None

        buyer = normalized_account_keys[0] if normalized_account_keys else None
        tokens_received = (
            self._extract_positive_token_diff(
                meta,
                str(mint),
                owner=buyer,
                account_count=len(normalized_account_keys),
            )
            if buyer
            else None
        )
        if tokens_received is not None:
            logger.info(f"Tokens received from tx: {tokens_received}")

        if quote_mint is not None and not is_sol_paired(quote_mint):
            quote_spent = (
                self._extract_negative_token_diff(
                    meta,
                    str(quote_mint),
                    owner=buyer,
                    account_count=len(normalized_account_keys),
                )
                if buyer
                else None
            )
            if quote_spent is None:
                logger.warning(
                    f"No negative buyer-owned {quote_mint} balance diff found in tx "
                    f"{signature[:16]}...; cannot determine quote spent"
                )
            else:
                logger.info(f"Quote spent from tx: {quote_spent} (mint {quote_mint})")
            return tokens_received, quote_spent

        lamport_balances = self._validated_lamport_balances(
            meta, len(normalized_account_keys)
        )
        if lamport_balances is None:
            logger.warning(
                f"Malformed lamport balances in successful buy {signature[:16]}..."
            )
            return tokens_received, None
        pre_balances, post_balances = lamport_balances

        destinations = [sol_destination, *(quote_destinations or [])]
        destination_strings: list[str] = []
        for destination in destinations:
            if not isinstance(destination, Pubkey):
                return tokens_received, None
            destination_string = str(destination)
            if destination_string in destination_strings:
                logger.warning("Duplicate native-SOL quote destination in receipt")
                return tokens_received, None
            destination_strings.append(destination_string)

        total_quote_spent = 0
        for destination_string in destination_strings:
            indexes = [
                index
                for index, account_key in enumerate(normalized_account_keys)
                if account_key == destination_string
            ]
            if len(indexes) != 1:
                logger.warning(
                    "Native-SOL quote destination is absent or duplicated in receipt"
                )
                return tokens_received, None
            index = indexes[0]
            delta = post_balances[index] - pre_balances[index]
            if delta < 0:
                logger.warning(
                    f"Native-SOL quote recipient decreased by {delta} lamports"
                )
                return tokens_received, None
            total_quote_spent += delta

        if total_quote_spent <= 0:
            logger.warning(
                f"Native-SOL quote recipients did not receive funds in "
                f"{signature[:16]}..."
            )
            return tokens_received, None
        logger.info(f"SOL to quote recipients: {total_quote_spent} lamports")
        return tokens_received, total_quote_spent

    async def get_sell_transaction_details(
        self,
        signature: str | Signature,
        quote_mint: Pubkey,
        owner: Pubkey,
    ) -> int | None:
        """Return the actual quote amount received by a successful sell."""
        signature_text = str(signature)
        result = await self._get_transaction_result(signature_text)
        if not isinstance(result, dict):
            return None
        meta = result.get("meta")
        if not isinstance(meta, dict) or meta.get("err"):
            return None

        transaction = result.get("transaction")
        message = transaction.get("message") if isinstance(transaction, dict) else None
        account_keys = message.get("accountKeys") if isinstance(message, dict) else None
        if not isinstance(account_keys, list):
            return None
        normalized_account_keys: list[str] = []
        for key in account_keys:
            raw_key = (
                key
                if isinstance(key, str)
                else (key.get("pubkey") if isinstance(key, dict) else None)
            )
            if not isinstance(raw_key, str):
                return None
            try:
                normalized_account_keys.append(str(Pubkey.from_string(raw_key)))
            except (TypeError, ValueError):
                return None

        owner_string = str(owner)
        owner_indexes = [
            index
            for index, account_key in enumerate(normalized_account_keys)
            if account_key == owner_string
        ]
        if len(owner_indexes) != 1:
            return None

        if not is_sol_paired(quote_mint):
            return self._extract_positive_token_diff(
                meta,
                str(quote_mint),
                owner=owner_string,
                account_count=len(normalized_account_keys),
            )

        lamport_balances = self._validated_lamport_balances(
            meta, len(normalized_account_keys)
        )
        if lamport_balances is None:
            return None
        pre_balances, post_balances = lamport_balances
        fee = meta.get("fee")
        if type(fee) is not int or fee < 0 or fee > 0xFFFF_FFFF_FFFF_FFFF:
            return None
        owner_index = owner_indexes[0]
        received = post_balances[owner_index] - pre_balances[owner_index] + fee
        if not 0 < received <= 0xFFFF_FFFF_FFFF_FFFF:
            return None
        return received

    @staticmethod
    def _extract_positive_token_diff(
        meta: dict,
        mint_str: str,
        *,
        owner: str | None = None,
        account_index: int | None = None,
        account_count: int | None = None,
    ) -> int | None:
        """Return the attributed net positive mint delta, or fail closed."""
        if owner is None and account_index is None:
            return None
        if (
            account_count is None
            or isinstance(account_count, bool)
            or not isinstance(account_count, int)
            or account_count < 0
        ):
            return None
        if account_index is not None and (
            isinstance(account_index, bool)
            or not isinstance(account_index, int)
            or not 0 <= account_index < account_count
        ):
            return None

        indexed_balances: list[dict[int, dict]] = []
        for field in ("preTokenBalances", "postTokenBalances"):
            entries = meta.get(field, [])
            if not isinstance(entries, list):
                return None
            by_index: dict[int, dict] = {}
            for balance in entries:
                if not isinstance(balance, dict):
                    return None
                index = balance.get("accountIndex")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < account_count
                    or index in by_index
                ):
                    return None
                by_index[index] = balance
            indexed_balances.append(by_index)

        pre_by_idx, post_by_idx = indexed_balances
        total_delta = 0
        matched = False
        for index in set(pre_by_idx) | set(post_by_idx):
            if account_index is not None and index != account_index:
                continue
            pre = pre_by_idx.get(index)
            post = post_by_idx.get(index)
            if pre is not None and post is not None:
                if pre.get("mint") != post.get("mint"):
                    return None
                if (
                    pre.get("owner") is not None
                    and post.get("owner") is not None
                    and pre.get("owner") != post.get("owner")
                ):
                    return None
            balance = post or pre
            if balance is None or balance.get("mint") != mint_str:
                continue
            balance_owner = balance.get("owner")
            if owner is not None and balance_owner != owner:
                continue

            pre_amount = (
                SolanaClient._parse_raw_token_amount(pre) if pre is not None else 0
            )
            post_amount = (
                SolanaClient._parse_raw_token_amount(post) if post is not None else 0
            )
            if pre_amount is None or post_amount is None:
                return None
            total_delta += post_amount - pre_amount
            if (
                total_delta < -0xFFFF_FFFF_FFFF_FFFF
                or total_delta > 0xFFFF_FFFF_FFFF_FFFF
            ):
                return None
            matched = True

        return total_delta if matched and total_delta > 0 else None

    @staticmethod
    def _is_canonical_transaction_result(result: object, signature: str) -> bool:
        """Require the transaction envelope needed for receipt attribution."""
        if not isinstance(result, dict):
            return False
        try:
            requested_signature = Signature.from_string(signature)
        except ValueError:
            return False
        slot = result.get("slot")
        if type(slot) is not int or slot < 0:
            return False
        transaction = result.get("transaction")
        if not isinstance(transaction, dict):
            return False
        signatures = transaction.get("signatures")
        if not isinstance(signatures, list) or not signatures:
            return False
        try:
            parsed_signatures = [
                Signature.from_string(item)
                for item in signatures
                if isinstance(item, str)
            ]
        except ValueError:
            return False
        if (
            len(parsed_signatures) != len(signatures)
            or parsed_signatures[0] != requested_signature
        ):
            return False
        message = transaction.get("message")
        if not isinstance(message, dict):
            return False
        account_keys = message.get("accountKeys")
        if not isinstance(account_keys, list) or not account_keys:
            return False
        meta = result.get("meta")
        return isinstance(meta, dict) and "err" in meta

    async def _get_transaction_result(
        self,
        signature: str | Signature,
        *,
        commitment: str = "confirmed",
    ) -> dict[str, Any] | None:
        """Fetch a canonical transaction result, or None when unavailable."""
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
                    "commitment": commitment,
                    # Without this the RPC rejects every versioned (v0)
                    # transaction with -32015, so meta.err cannot be read and a
                    # perfectly good trade reads back as unconfirmed.
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }

        try:
            response = await self.post_rpc(body)
        except JsonRpcError as exc:
            logger.warning(
                "Could not read transaction %s...: %s",
                signature[:16],
                exc,
            )
            return None
        if not response or "result" not in response:
            logger.warning(f"Failed to get transaction {signature}")
            return None

        result = response["result"]
        if not self._is_canonical_transaction_result(result, signature):
            logger.warning(f"Malformed transaction envelope for {signature[:16]}...")
            return None

        return result

    @staticmethod
    def _extract_negative_token_diff(
        meta: dict,
        mint_str: str,
        *,
        owner: str | None = None,
        account_count: int | None = None,
    ) -> int | None:
        """Return an attributed net negative mint delta as a positive amount."""
        if not isinstance(meta, dict):
            return None
        swapped_meta = {
            "preTokenBalances": meta.get("postTokenBalances"),
            "postTokenBalances": meta.get("preTokenBalances"),
        }
        return SolanaClient._extract_positive_token_diff(
            swapped_meta,
            mint_str,
            owner=owner,
            account_count=account_count,
        )

    async def post_rpc(
        self,
        body: dict[str, Any],
        max_retries: int = 3,
        max_429_retries: int = 10,
        *,
        deadline_seconds: float = DEFAULT_RPC_DEADLINE_SECONDS,
    ) -> dict[str, Any] | None:
        """Send a bounded JSON-RPC request and reject RPC-level errors."""
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries <= 0
        ):
            raise ValueError("max_retries must be a positive integer")
        if (
            isinstance(max_429_retries, bool)
            or not isinstance(max_429_retries, int)
            or max_429_retries <= 0
        ):
            raise ValueError("max_429_retries must be a positive integer")
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, int | float)
            or not isfinite(deadline_seconds)
            or deadline_seconds <= 0
        ):
            raise ValueError("deadline_seconds must be a positive finite number")

        method = str(body.get("method", "unknown"))
        transport_attempts = 0
        rate_limit_attempts = 0
        if method in RAW_RPC_SUBMISSION_METHODS:
            raise ExecutionBlocked(
                f"Raw RPC submission method {method!r} is disabled; "
                "use build_and_send_transaction"
            )
        deadline = time.monotonic() + deadline_seconds

        while transport_attempts < max_retries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(f"RPC request {method} exceeded its deadline")
                return None
            try:
                await asyncio.wait_for(
                    self._rate_limiter.acquire(),
                    timeout=remaining,
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                session = await self._get_session()
                request_timeout = aiohttp.ClientTimeout(total=min(10.0, remaining))
                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    timeout=request_timeout,
                ) as response:
                    if response.status == HTTP_TOO_MANY_REQUESTS:
                        rate_limit_attempts += 1
                        if rate_limit_attempts >= max_429_retries:
                            logger.error(
                                f"RPC rate limited (429) on {method}, exhausted "
                                f"{max_429_retries} rate-limit attempts"
                            )
                            return None
                        retry_after = response.headers.get("Retry-After")
                        try:
                            retry_delay = (
                                float(retry_after) if retry_after is not None else None
                            )
                        except (TypeError, ValueError):
                            retry_delay = None
                        if (
                            retry_delay is None
                            or not isfinite(retry_delay)
                            or retry_delay < 0
                        ):
                            retry_delay = min(2**rate_limit_attempts, 30.0)
                            retry_delay += retry_delay * random.uniform(  # noqa: S311
                                0, 0.25
                            )
                        if retry_delay >= deadline - time.monotonic():
                            logger.error(
                                f"RPC retry-after for {method} exceeds its deadline"
                            )
                            return None
                        logger.warning(
                            f"RPC rate limited (429) on {method}, retry "
                            f"{rate_limit_attempts}/{max_429_retries} after "
                            f"{retry_delay:.1f}s"
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                    response.raise_for_status()
                    payload = await response.json()
                    if not isinstance(payload, dict):
                        raise JsonRpcError(method, "response is not a JSON object")
                    if "error" in payload:
                        raise JsonRpcError(method, payload["error"])
                    if "result" not in payload:
                        raise JsonRpcError(
                            method,
                            "response contains neither result nor error",
                        )
                    return payload

            except JsonRpcError:
                raise
            except aiohttp.ContentTypeError:
                logger.exception(f"Failed to decode RPC response for {method}")
                return None
            except (TimeoutError, aiohttp.ClientError):
                transport_attempts += 1
                if transport_attempts >= max_retries:
                    logger.exception(
                        f"RPC request {method} failed after "
                        f"{max_retries} transport attempts"
                    )
                    return None
                retry_delay = min(2 ** (transport_attempts - 1), 16.0)
                retry_delay += retry_delay * random.uniform(0, 0.25)  # noqa: S311
                if retry_delay >= deadline - time.monotonic():
                    logger.error(f"RPC retry for {method} exceeds its deadline")
                    return None
                logger.warning(
                    f"RPC request {method} failed (attempt "
                    f"{transport_attempts}/{max_retries}), retrying in "
                    f"{retry_delay:.1f}s"
                )
                await asyncio.sleep(retry_delay)

        return None

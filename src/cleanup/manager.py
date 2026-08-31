from __future__ import annotations

import asyncio
import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from solders.pubkey import Pubkey
from spl.token.instructions import BurnParams, CloseAccountParams, burn, close_account

from core.client import (
    SolanaClient,
    TransactionStatus,
    TransactionSubmissionUnknown,
)
from core.execution_policy import ExecutionPolicy
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from utils.durable_file import atomic_write_text
from utils.logger import get_logger

logger = get_logger(__name__)


class _UnsupportedTokenProgramError(ValueError):
    """Raised when mint ownership is not a supported token program."""


class CleanupStatus(StrEnum):
    """Stable cleanup outcomes suitable for retries and audit logs."""

    CONFIRMED = "confirmed"
    ALREADY_ABSENT = "already_absent"
    NONZERO_SKIPPED = "nonzero_skipped"
    FORCE_BURN_DENIED = "force_burn_denied"
    OWNERSHIP_UNPROVEN = "ownership_unproven"
    UNSUPPORTED_TOKEN_PROGRAM = "unsupported_token_program"  # noqa: S105
    UNRESOLVED = "unresolved"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Structured, idempotent result for one ATA cleanup attempt."""

    status: CleanupStatus
    mint: Pubkey
    ata: Pubkey | None = None
    token_program_id: Pubkey | None = None
    balance_raw: int | None = None
    tx_signature: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        """Whether the desired account-absent state is confirmed."""
        return self.status in {
            CleanupStatus.CONFIRMED,
            CleanupStatus.ALREADY_ABSENT,
        }


@dataclass(frozen=True, slots=True)
class _OwnershipRecord:
    baseline_raw: int
    acquired_raw: int
    generation: str
    confirmed_sold_raw: int = 0

    @property
    def attributable_residual_raw(self) -> int:
        return self.acquired_raw - self.confirmed_sold_raw


class AccountCleanupManager:
    """Handles ownership-safe, idempotent cleanup of token accounts."""

    _ownership_records: dict[tuple[str, str, str], _OwnershipRecord] = {}
    _pending_signatures: dict[tuple[str, str, str], str] = {}

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        use_priority_fee: bool = False,
        force_burn: bool = False,
        execution_policy: ExecutionPolicy | None = None,
        journal_path: str | Path | None = None,
    ):
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.use_priority_fee = use_priority_fee
        self.close_with_force_burn = force_burn
        self.execution_policy = execution_policy or getattr(
            client, "execution_policy", ExecutionPolicy()
        )
        self._journal_path = (
            Path(journal_path)
            if journal_path is not None
            else Path(".state") / "cleanup" / f"{self.wallet.pubkey}.json"
        )
        with self._journal_lock():
            self._journal_entries, migrated = self._load_journal()
            if migrated:
                self._write_journal()

    @classmethod
    def record_bot_owned_balance(
        cls,
        wallet: Pubkey,
        mint: Pubkey,
        token_program_id: Pubkey,
        *,
        baseline_raw: int,
        acquired_raw: int,
        ownership_id: str | None = None,
    ) -> None:
        """Record the pre-buy baseline and confirmed bot acquisition."""
        if (
            isinstance(baseline_raw, bool)
            or not isinstance(baseline_raw, int)
            or baseline_raw < 0
            or isinstance(acquired_raw, bool)
            or not isinstance(acquired_raw, int)
            or acquired_raw <= 0
            or (
                ownership_id is not None
                and (not isinstance(ownership_id, str) or not ownership_id)
            )
        ):
            raise ValueError("cleanup ownership metadata is invalid")
        key = (str(wallet), str(mint), str(token_program_id))
        current = cls._ownership_records.get(key)
        if ownership_id is not None:
            generation = uuid5(
                NAMESPACE_URL,
                f"cleanup-ownership:{':'.join(key)}:{ownership_id}",
            ).hex
        elif (
            current is not None
            and current.baseline_raw == baseline_raw
            and current.acquired_raw == acquired_raw
        ):
            generation = current.generation
        else:
            generation = uuid4().hex

        if current is not None and current.generation == generation:
            if (
                current.baseline_raw != baseline_raw
                or current.acquired_raw != acquired_raw
            ):
                raise ValueError("cleanup ownership lifecycle amounts changed")
            confirmed_sold_raw = current.confirmed_sold_raw
        else:
            confirmed_sold_raw = 0
        cls._ownership_records[key] = _OwnershipRecord(
            baseline_raw=baseline_raw,
            acquired_raw=acquired_raw,
            generation=generation,
            confirmed_sold_raw=confirmed_sold_raw,
        )

    @classmethod
    def record_confirmed_sell_delta(
        cls,
        wallet: Pubkey,
        mint: Pubkey,
        token_program_id: Pubkey,
        *,
        sold_raw: int,
    ) -> None:
        """Apply a confirmed bot sell to the attributable token residual."""
        if isinstance(sold_raw, bool) or not isinstance(sold_raw, int) or sold_raw <= 0:
            raise ValueError("confirmed cleanup sell amount is invalid")
        key = (str(wallet), str(mint), str(token_program_id))
        ownership = cls._ownership_records.get(key)
        if ownership is None:
            raise ValueError("cannot record a sell without confirmed bot ownership")
        confirmed_sold_raw = ownership.confirmed_sold_raw + sold_raw
        if confirmed_sold_raw > ownership.acquired_raw:
            raise ValueError("confirmed cleanup sells exceed the acquired balance")
        cls._ownership_records[key] = _OwnershipRecord(
            baseline_raw=ownership.baseline_raw,
            acquired_raw=ownership.acquired_raw,
            generation=ownership.generation,
            confirmed_sold_raw=confirmed_sold_raw,
        )

    @staticmethod
    def _journal_key(key: tuple[str, str, str]) -> str:
        return ":".join(key)

    @staticmethod
    def _legacy_generation(wallet: str, journal_key: str, intent_id: str) -> str:
        return uuid5(
            NAMESPACE_URL,
            f"cleanup-lifecycle:{wallet}:{journal_key}:{intent_id}",
        ).hex

    @staticmethod
    def _result_payload(
        result: CleanupResult,
        *,
        intent_id: str,
        generation: str,
        ownership: _OwnershipRecord | None,
    ) -> dict[str, object]:
        return {
            "status": result.status.value,
            "mint": str(result.mint),
            "ata": str(result.ata) if result.ata is not None else None,
            "token_program_id": (
                str(result.token_program_id)
                if result.token_program_id is not None
                else None
            ),
            "balance_raw": result.balance_raw,
            "tx_signature": result.tx_signature,
            "error": result.error,
            "intent_id": intent_id,
            "generation": generation,
            "ownership": (
                {
                    "baseline_raw": ownership.baseline_raw,
                    "acquired_raw": ownership.acquired_raw,
                    "confirmed_sold_raw": ownership.confirmed_sold_raw,
                    "generation": ownership.generation,
                }
                if ownership is not None
                else None
            ),
        }

    @staticmethod
    def _result_from_payload(payload: dict[str, object]) -> CleanupResult:
        status = CleanupStatus(str(payload["status"]))
        if status not in {CleanupStatus.UNRESOLVED, CleanupStatus.FAILED}:
            raise ValueError("cleanup journal contains a non-recoverable status")
        mint = Pubkey.from_string(str(payload["mint"]))
        ata_value = payload.get("ata")
        program_value = payload.get("token_program_id")
        balance_value = payload.get("balance_raw")
        signature_value = payload.get("tx_signature")
        error_value = payload.get("error")
        if balance_value is not None and (
            isinstance(balance_value, bool)
            or not isinstance(balance_value, int)
            or balance_value < 0
        ):
            raise ValueError("cleanup journal balance is invalid")
        if signature_value is not None and (
            not isinstance(signature_value, str) or not signature_value
        ):
            raise ValueError("cleanup journal signature is invalid")
        if error_value is not None and not isinstance(error_value, str):
            raise ValueError("cleanup journal error is invalid")
        return CleanupResult(
            status=status,
            mint=mint,
            ata=Pubkey.from_string(str(ata_value)) if ata_value else None,
            token_program_id=(
                Pubkey.from_string(str(program_value)) if program_value else None
            ),
            balance_raw=balance_value,
            tx_signature=signature_value,
            error=error_value,
        )

    @contextmanager
    def _journal_lock(self) -> Iterator[None]:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._journal_path.with_name(f"{self._journal_path.name}.lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _cleanup_worker_claim(self) -> Iterator[bool]:
        """Claim the wallet journal without blocking another worker's event loop."""
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        claim_path = self._journal_path.with_name(
            f"{self._journal_path.name}.worker.lock"
        )
        with claim_path.open("a+b") as claim_file:
            try:
                fcntl.flock(
                    claim_file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(claim_file.fileno(), fcntl.LOCK_UN)

    def _load_journal(self) -> tuple[dict[str, dict[str, object]], bool]:
        if not self._journal_path.exists():
            return {}, False
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cleanup journal payload is invalid")
            version = payload.get("version")
            if isinstance(version, bool) or version not in {1, 2, 3}:
                raise ValueError("unsupported cleanup journal version")
            wallet = str(self.wallet.pubkey)
            if payload.get("wallet") != wallet:
                raise ValueError("cleanup journal belongs to a different wallet")
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                raise ValueError("cleanup journal entries are invalid")
            loaded: dict[str, dict[str, object]] = {}
            pending: dict[tuple[str, str, str], str] = {}
            migrating_generation = version == 1
            needs_migration = version != 3
            for journal_key, raw_entry in entries.items():
                if not isinstance(journal_key, str) or not isinstance(raw_entry, dict):
                    raise ValueError("cleanup journal entry is invalid")
                result = self._result_from_payload(raw_entry)
                program_id = result.token_program_id
                if program_id is None:
                    raise ValueError("cleanup journal token program is missing")
                expected_key = self._journal_key(
                    (
                        wallet,
                        str(result.mint),
                        str(program_id),
                    )
                )
                if journal_key != expected_key:
                    raise ValueError("cleanup journal key does not match its result")
                intent_id = raw_entry.get("intent_id")
                if not isinstance(intent_id, str) or not intent_id:
                    raise ValueError("cleanup journal intent is invalid")
                normalized_entry = dict(raw_entry)
                if migrating_generation:
                    normalized_entry["generation"] = self._legacy_generation(
                        wallet, journal_key, intent_id
                    )
                generation = normalized_entry.get("generation")
                if not isinstance(generation, str) or not generation:
                    raise ValueError("cleanup journal generation is invalid")
                key = (wallet, str(result.mint), str(program_id))
                ownership_payload = normalized_entry.get("ownership")
                if ownership_payload is not None:
                    if not isinstance(ownership_payload, dict):
                        raise ValueError("cleanup journal ownership is invalid")
                    ownership = _OwnershipRecord(
                        baseline_raw=ownership_payload["baseline_raw"],
                        acquired_raw=ownership_payload["acquired_raw"],
                        confirmed_sold_raw=ownership_payload["confirmed_sold_raw"],
                        generation=ownership_payload["generation"],
                    )
                    if (
                        isinstance(ownership.baseline_raw, bool)
                        or not isinstance(ownership.baseline_raw, int)
                        or ownership.baseline_raw < 0
                        or isinstance(ownership.acquired_raw, bool)
                        or not isinstance(ownership.acquired_raw, int)
                        or ownership.acquired_raw <= 0
                        or isinstance(ownership.confirmed_sold_raw, bool)
                        or not isinstance(ownership.confirmed_sold_raw, int)
                        or ownership.confirmed_sold_raw < 0
                        or ownership.confirmed_sold_raw > ownership.acquired_raw
                        or not isinstance(ownership.generation, str)
                        or not ownership.generation
                        or ownership.generation != generation
                    ):
                        raise ValueError("cleanup journal ownership is invalid")
                    current_ownership = self._ownership_records.get(key)
                    if (
                        current_ownership is None
                        or current_ownership.generation == ownership.generation
                    ):
                        self._ownership_records[key] = ownership
                loaded[journal_key] = normalized_entry
                if (
                    result.status is CleanupStatus.UNRESOLVED
                    and result.tx_signature is not None
                ):
                    pending[key] = result.tx_signature
            self._pending_signatures.update(pending)
            return loaded, needs_migration
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot safely load cleanup journal {self._journal_path}"
            ) from exc

    def _write_journal(self) -> None:
        atomic_write_text(
            self._journal_path,
            json.dumps(
                {
                    "version": 3,
                    "wallet": str(self.wallet.pubkey),
                    "entries": self._journal_entries,
                },
                indent=2,
                sort_keys=True,
            ),
        )

    def _persist_result(
        self,
        key: tuple[str, str, str],
        result: CleanupResult,
        *,
        intent_id: str,
        generation: str,
    ) -> None:
        with self._journal_lock():
            self._journal_entries, _ = self._load_journal()
            ownership = self._ownership_records.get(key)
            self._journal_entries[self._journal_key(key)] = self._result_payload(
                result,
                intent_id=intent_id,
                generation=generation,
                ownership=ownership,
            )
            if (
                result.status is CleanupStatus.UNRESOLVED
                and result.tx_signature is not None
            ):
                self._pending_signatures[key] = result.tx_signature
            else:
                self._pending_signatures.pop(key, None)
            self._write_journal()

    def _remove_persisted_result(
        self,
        key: tuple[str, str, str],
        *,
        expected_generation: str | None = None,
    ) -> bool:
        with self._journal_lock():
            self._journal_entries, _ = self._load_journal()
            journal_key = self._journal_key(key)
            current = self._journal_entries.get(journal_key)
            if current is None or (
                expected_generation is not None
                and current.get("generation") != expected_generation
            ):
                return False
            self._pending_signatures.pop(key, None)
            self._journal_entries.pop(journal_key)
            self._write_journal()
            return True

    def stage_confirmed_sell_cleanup(
        self,
        mint: Pubkey,
        token_program_id: Pubkey,
        *,
        sold_raw: int,
    ) -> CleanupResult:
        """Durably stage cleanup ownership before a closed position is removed."""
        key = (
            str(self.wallet.pubkey),
            str(mint),
            str(token_program_id),
        )
        ata = self.wallet.get_associated_token_address(mint, token_program_id)
        with self._journal_lock():
            self._journal_entries, _ = self._load_journal()
            journal_key = self._journal_key(key)
            existing = self._journal_entries.get(journal_key)
            ownership = self._ownership_records.get(key)
            if ownership is None:
                raise ValueError("cannot stage cleanup without confirmed bot ownership")
            if existing is not None:
                existing_generation = str(existing["generation"])
                if existing_generation == ownership.generation:
                    return self._result_from_payload(existing)
                raise ValueError(
                    "cannot replace unresolved cleanup from another ownership lifecycle"
                )

            self.record_confirmed_sell_delta(
                self.wallet.pubkey,
                mint,
                token_program_id,
                sold_raw=sold_raw,
            )
            ownership = self._ownership_records[key]
            intent_id = (
                f"cleanup:{self.wallet.pubkey}:{mint}:{ata}:{ownership.generation}"
            )
            staged = CleanupResult(
                CleanupStatus.UNRESOLVED,
                mint,
                ata,
                token_program_id,
                balance_raw=ownership.attributable_residual_raw,
                error="cleanup work is durably staged",
            )
            self._journal_entries[journal_key] = self._result_payload(
                staged,
                intent_id=intent_id,
                generation=ownership.generation,
                ownership=ownership,
            )
            self._write_journal()
            return staged

    async def resume_pending_cleanups(self) -> list[CleanupResult]:
        """Resume every staged cleanup with one worker for this wallet journal."""
        with self._cleanup_worker_claim() as claimed:
            if not claimed:
                logger.warning(
                    "Another cleanup worker owns wallet journal %s",
                    self._journal_path,
                )
                return []
            with self._journal_lock():
                self._journal_entries, migrated = self._load_journal()
                if migrated:
                    self._write_journal()
                targets = [
                    (result.mint, result.token_program_id)
                    for entry in self._journal_entries.values()
                    if (result := self._result_from_payload(entry)).status
                    is CleanupStatus.UNRESOLVED
                    and result.token_program_id is not None
                ]
            results: list[CleanupResult] = []
            for mint, token_program_id in targets:
                results.append(await self.cleanup_ata(mint, token_program_id))
            return results

    async def _resolve_token_program(
        self, mint: Pubkey, supplied: Pubkey | None
    ) -> Pubkey:
        """Discover and verify the mint's actual owning token program."""
        mint_info = await self.client.get_account_info(mint)
        owner = getattr(mint_info, "owner", None)
        if owner is None and isinstance(mint_info, dict):
            owner = mint_info.get("owner")
        if isinstance(owner, str):
            owner = Pubkey.from_string(owner)
        supported = {
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        }
        if owner not in supported:
            raise _UnsupportedTokenProgramError(
                f"Mint {mint} has unsupported owner {owner}"
            )
        if supplied is not None and supplied != owner:
            raise _UnsupportedTokenProgramError(
                f"Supplied token program {supplied} does not own mint {mint}"
            )
        return owner

    async def _resolve_pending(
        self,
        key: tuple[str, str, str],
        mint: Pubkey,
        ata: Pubkey,
        token_program_id: Pubkey,
    ) -> CleanupResult | None:
        """Reconcile durable cleanup state before building another transaction."""
        with self._journal_lock():
            self._journal_entries, migrated = self._load_journal()
            if migrated:
                self._write_journal()
        journal_entry = self._journal_entries.get(self._journal_key(key))
        persisted_result = (
            self._result_from_payload(journal_entry)
            if journal_entry is not None
            else None
        )
        if (
            persisted_result is not None
            and persisted_result.status is CleanupStatus.FAILED
        ):
            persisted_generation = str(journal_entry["generation"])
            ownership = self._ownership_records.get(key)
            if ownership is not None and ownership.generation != persisted_generation:
                self._remove_persisted_result(key)
                return None
            try:
                await self.client.get_account_info(ata)
            except ValueError:
                self._ownership_records.pop(key, None)
                self._remove_persisted_result(key)
                return CleanupResult(
                    CleanupStatus.ALREADY_ABSENT,
                    mint,
                    ata,
                    token_program_id,
                )
            return persisted_result

        signature = (
            persisted_result.tx_signature
            if persisted_result is not None
            else self._pending_signatures.get(key)
        )
        if signature is None:
            return None

        if journal_entry is not None:
            intent_id = str(journal_entry["intent_id"])
            generation = str(journal_entry["generation"])
        else:
            generation = uuid5(
                NAMESPACE_URL,
                f"cleanup-orphan:{self.wallet.pubkey}:{mint}:{ata}:{signature}",
            ).hex
            intent_id = f"cleanup:{self.wallet.pubkey}:{mint}:{ata}:{generation}"
        try:
            outcome = await self.client.confirm_transaction_outcome(signature)
        except Exception as exc:
            unresolved = CleanupResult(
                CleanupStatus.UNRESOLVED,
                mint,
                ata,
                token_program_id,
                balance_raw=(
                    persisted_result.balance_raw
                    if persisted_result is not None
                    else None
                ),
                tx_signature=signature,
                error=str(exc),
            )
            self._persist_result(
                key,
                unresolved,
                intent_id=intent_id,
                generation=generation,
            )
            return unresolved
        if outcome.status is TransactionStatus.UNKNOWN:
            unresolved = CleanupResult(
                CleanupStatus.UNRESOLVED,
                mint,
                ata,
                token_program_id,
                balance_raw=(
                    persisted_result.balance_raw
                    if persisted_result is not None
                    else None
                ),
                tx_signature=signature,
                error=outcome.error,
            )
            self._pending_signatures[key] = signature
            self._persist_result(
                key,
                unresolved,
                intent_id=intent_id,
                generation=generation,
            )
            return unresolved

        current_ownership = self._ownership_records.get(key)
        if (
            outcome.status is TransactionStatus.SUCCESS
            and current_ownership is not None
            and current_ownership.generation != generation
        ):
            self._remove_persisted_result(
                key,
                expected_generation=generation,
            )
            return None

        self._pending_signatures.pop(key, None)
        if outcome.status is TransactionStatus.SUCCESS:
            current_ownership = self._ownership_records.get(key)
            if current_ownership is None or current_ownership.generation == generation:
                self._ownership_records.pop(key, None)
            self._remove_persisted_result(
                key,
                expected_generation=generation,
            )
            return CleanupResult(
                CleanupStatus.CONFIRMED,
                mint,
                ata,
                token_program_id,
                balance_raw=(
                    persisted_result.balance_raw
                    if persisted_result is not None
                    else None
                ),
                tx_signature=signature,
            )

        failed = CleanupResult(
            CleanupStatus.FAILED,
            mint,
            ata,
            token_program_id,
            balance_raw=(
                persisted_result.balance_raw if persisted_result is not None else None
            ),
            tx_signature=signature,
            error=outcome.error or outcome.status.value,
        )
        self._persist_result(
            key,
            failed,
            intent_id=intent_id,
            generation=generation,
        )
        return failed

    async def cleanup_ata(
        self, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> CleanupResult:
        """Burn only proven bot-owned dust, then close its ATA."""
        ata: Pubkey | None = None
        signature: str | None = None
        try:
            token_program_id = await self._resolve_token_program(mint, token_program_id)
            ata = self.wallet.get_associated_token_address(mint, token_program_id)
            key = (
                str(self.wallet.pubkey),
                str(mint),
                str(token_program_id),
            )

            pending = await self._resolve_pending(key, mint, ata, token_program_id)
            if pending is not None:
                return pending

            logger.info("Waiting for 15 seconds for RPC node to synchronize...")
            await asyncio.sleep(15)
            try:
                ata_info = await self.client.get_account_info(ata)
            except ValueError:
                self._ownership_records.pop(key, None)
                self._remove_persisted_result(key)
                return CleanupResult(
                    CleanupStatus.ALREADY_ABSENT,
                    mint,
                    ata,
                    token_program_id,
                )

            ata_owner = getattr(ata_info, "owner", None)
            if ata_owner is None and isinstance(ata_info, dict):
                ata_owner = ata_info.get("owner")
            if isinstance(ata_owner, str):
                ata_owner = Pubkey.from_string(ata_owner)
            if ata_owner != token_program_id:
                return CleanupResult(
                    CleanupStatus.UNSUPPORTED_TOKEN_PROGRAM,
                    mint,
                    ata,
                    token_program_id,
                    error=f"ATA owner is {ata_owner}",
                )

            balance = await self.client.get_token_account_balance(ata)
            ownership = self._ownership_records.get(key)
            if ownership is None:
                return CleanupResult(
                    CleanupStatus.OWNERSHIP_UNPROVEN,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    error="ATA was not recorded as bot-owned",
                )
            if ownership.baseline_raw != 0 or balance > ownership.acquired_raw:
                return CleanupResult(
                    CleanupStatus.OWNERSHIP_UNPROVEN,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    error=(
                        "cleanup requires a zero pre-buy baseline and a balance "
                        "no greater than the confirmed acquisition"
                    ),
                )

            instructions = []
            if balance > 0 and mint != SystemAddresses.WSOL_MINT:
                if not self.close_with_force_burn:
                    return CleanupResult(
                        CleanupStatus.NONZERO_SKIPPED,
                        mint,
                        ata,
                        token_program_id,
                        balance_raw=balance,
                    )
                if balance != ownership.attributable_residual_raw:
                    return CleanupResult(
                        CleanupStatus.OWNERSHIP_UNPROVEN,
                        mint,
                        ata,
                        token_program_id,
                        balance_raw=balance,
                        error=(
                            "live balance does not equal the residual proven by "
                            "confirmed bot buy and sell deltas"
                        ),
                    )
                try:
                    self.execution_policy.validate_force_burn(True)
                except Exception as exc:
                    return CleanupResult(
                        CleanupStatus.FORCE_BURN_DENIED,
                        mint,
                        ata,
                        token_program_id,
                        balance_raw=balance,
                        error=str(exc),
                    )
                instructions.append(
                    burn(
                        BurnParams(
                            account=ata,
                            mint=mint,
                            owner=self.wallet.pubkey,
                            amount=balance,
                            program_id=token_program_id,
                        )
                    )
                )

            instructions.append(
                close_account(
                    CloseAccountParams(
                        account=ata,
                        dest=self.wallet.pubkey,
                        owner=self.wallet.pubkey,
                        program_id=token_program_id,
                    )
                )
            )
            priority_fee = (
                await self.priority_fee_manager.calculate_priority_fee([ata])
                if self.use_priority_fee
                else 0
            )
            journal_entry = self._journal_entries.get(self._journal_key(key))
            if journal_entry is not None:
                generation = str(journal_entry["generation"])
                intent_id = str(journal_entry["intent_id"])
            else:
                generation = ownership.generation
                intent_id = f"cleanup:{self.wallet.pubkey}:{mint}:{ata}:{generation}"
            awaiting_submission = CleanupResult(
                CleanupStatus.UNRESOLVED,
                mint,
                ata,
                token_program_id,
                balance_raw=balance,
                error="cleanup submission is pending",
            )
            self._persist_result(
                key,
                awaiting_submission,
                intent_id=intent_id,
                generation=generation,
            )
            try:
                tx_sig = await self.client.build_and_send_transaction(
                    instructions,
                    self.wallet.keypair,
                    skip_preflight=False,
                    priority_fee=priority_fee or None,
                    quote_amount_raw=0,
                    intent_id=intent_id,
                )
            except TransactionSubmissionUnknown as exc:
                signature = exc.signature
                unresolved = CleanupResult(
                    CleanupStatus.UNRESOLVED,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    tx_signature=signature,
                    error=str(exc),
                )
                self._pending_signatures[key] = signature
                self._persist_result(
                    key,
                    unresolved,
                    intent_id=intent_id,
                    generation=generation,
                )
                return unresolved
            except Exception as exc:
                retryable = CleanupResult(
                    CleanupStatus.UNRESOLVED,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    error=f"cleanup submission did not start: {exc!s}",
                )
                self._persist_result(
                    key,
                    retryable,
                    intent_id=intent_id,
                    generation=generation,
                )
                return retryable

            signature = str(tx_sig)
            unresolved = CleanupResult(
                CleanupStatus.UNRESOLVED,
                mint,
                ata,
                token_program_id,
                balance_raw=balance,
                tx_signature=signature,
                error="cleanup confirmation is pending",
            )
            self._pending_signatures[key] = signature
            self._persist_result(
                key,
                unresolved,
                intent_id=intent_id,
                generation=generation,
            )
            try:
                outcome = await self.client.confirm_transaction_outcome(signature)
            except Exception as exc:
                unresolved = CleanupResult(
                    CleanupStatus.UNRESOLVED,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    tx_signature=signature,
                    error=str(exc),
                )
                self._persist_result(
                    key,
                    unresolved,
                    intent_id=intent_id,
                    generation=generation,
                )
                return unresolved
            if outcome.status is TransactionStatus.SUCCESS:
                self._pending_signatures.pop(key, None)
                current_ownership = self._ownership_records.get(key)
                if (
                    current_ownership is None
                    or current_ownership.generation == generation
                ):
                    self._ownership_records.pop(key, None)
                removed = self._remove_persisted_result(
                    key,
                    expected_generation=generation,
                )
                if not removed:
                    return await self.cleanup_ata(mint, token_program_id)
                return CleanupResult(
                    CleanupStatus.CONFIRMED,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    tx_signature=signature,
                )
            if outcome.status is TransactionStatus.UNKNOWN:
                unresolved = CleanupResult(
                    CleanupStatus.UNRESOLVED,
                    mint,
                    ata,
                    token_program_id,
                    balance_raw=balance,
                    tx_signature=signature,
                    error=outcome.error,
                )
                self._persist_result(
                    key,
                    unresolved,
                    intent_id=intent_id,
                    generation=generation,
                )
                return unresolved
            self._pending_signatures.pop(key, None)
            failed = CleanupResult(
                CleanupStatus.FAILED,
                mint,
                ata,
                token_program_id,
                balance_raw=balance,
                tx_signature=signature,
                error=outcome.error or outcome.status.value,
            )
            self._persist_result(
                key,
                failed,
                intent_id=intent_id,
                generation=generation,
            )
            return failed
        except _UnsupportedTokenProgramError as exc:
            return CleanupResult(
                CleanupStatus.UNSUPPORTED_TOKEN_PROGRAM,
                mint,
                ata,
                token_program_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.warning(f"Cleanup failed for mint {mint}: {exc!s}")
            return CleanupResult(
                (
                    CleanupStatus.UNRESOLVED
                    if signature is not None
                    else CleanupStatus.FAILED
                ),
                mint,
                ata,
                token_program_id,
                tx_signature=signature,
                error=str(exc),
            )

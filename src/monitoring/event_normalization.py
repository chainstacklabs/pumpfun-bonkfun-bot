"""Fail-closed normalization for transaction events from monitoring providers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import base58
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from interfaces.core import Platform, TokenInfo


def _signature_text(value: Any, context: str) -> str:
    """Return a canonical base58 signature or reject the value."""
    if isinstance(value, Signature):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 64:
            raise NormalizationError(f"{context} signature must contain 64 bytes")
        return str(Signature.from_bytes(raw))
    if isinstance(value, str):
        try:
            return str(Signature.from_string(value))
        except (TypeError, ValueError) as exc:
            raise NormalizationError(f"{context} signature is invalid") from exc
    raise NormalizationError(f"{context} signature is invalid")


def _validate_account_key_sets(
    static_accounts: tuple[str, ...],
    loaded_writable_accounts: tuple[str, ...],
    loaded_readonly_accounts: tuple[str, ...],
) -> None:
    """Reject duplicate effective keys whose index identity would be ambiguous."""
    seen: set[str] = set()
    for group in (
        static_accounts,
        loaded_writable_accounts,
        loaded_readonly_accounts,
    ):
        for account in group:
            if account in seen:
                raise NormalizationError(
                    f"Duplicate account key in effective transaction keys: {account}"
                )
            seen.add(account)


class NormalizationError(ValueError):
    """Raised when a provider event cannot be represented unambiguously."""


class NotificationRejected(NormalizationError):
    """Raised when a notification is failed, malformed, or not correlated."""


@dataclass(frozen=True, slots=True)
class NormalizedInstruction:
    """A compiled or parsed instruction with stable transaction coordinates."""

    program_id: str
    accounts: tuple[int, ...]
    data: bytes | None
    encoding: str
    instruction_index: int
    inner_index: int | None = None
    parent_instruction_index: int | None = None
    parsed: Any = None


@dataclass(frozen=True, slots=True)
class NormalizedTransactionEvent:
    """Provider-independent transaction/event metadata used by all listeners."""

    source: str
    platform: Platform | None
    signature: str
    slot: int | None
    commitment: str | None
    transaction_index: int | None
    static_accounts: tuple[str, ...]
    loaded_writable_accounts: tuple[str, ...]
    loaded_readonly_accounts: tuple[str, ...]
    encoding: str
    transaction_error: Any
    instructions: tuple[NormalizedInstruction, ...] = ()
    logs: tuple[str, ...] = ()
    raw: Any = field(default=None, repr=False, compare=False)

    @property
    def account_keys(self) -> tuple[str, ...]:
        """Return Solana's effective account-key order, including LUT entries."""
        return (
            *self.static_accounts,
            *self.loaded_writable_accounts,
            *self.loaded_readonly_accounts,
        )


def _account_text(value: Any) -> str:
    if isinstance(value, str):
        if not value:
            raise NormalizationError("Account key cannot be empty")
        try:
            return str(Pubkey.from_string(value))
        except ValueError as exc:
            raise NormalizationError(f"Invalid account key: {value!r}") from exc
    if isinstance(value, dict):
        if "pubkey" not in value:
            raise NormalizationError("Parsed account key is missing pubkey")
        return _account_text(value["pubkey"])
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 32:
            raise NormalizationError(
                f"Account key must contain 32 bytes, received {len(raw)}"
            )
        return str(Pubkey.from_bytes(raw))
    try:
        raw = bytes(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"Unsupported account-key value: {value!r}") from exc
    if len(raw) != 32:
        raise NormalizationError(
            f"Account key must contain 32 bytes, received {len(raw)}"
        )
    return str(Pubkey.from_bytes(raw))


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise NormalizationError("Expected a sequence, not a scalar")
    try:
        return list(value)
    except TypeError as exc:
        raise NormalizationError("Expected a sequence") from exc


def _mapping_or_attr(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _decode_payload(value: Any, default_encoding: str = "base58") -> tuple[bytes, str]:
    encoding = default_encoding
    payload = value
    if isinstance(value, (list, tuple)):
        if len(value) != 2 or not isinstance(value[1], str):
            raise NormalizationError("Encoded payload must be [data, encoding]")
        payload, encoding = value
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload), "bytes"
    if not isinstance(payload, str):
        raise NormalizationError("Instruction payload must be bytes or encoded text")
    try:
        if encoding == "base64":
            return base64.b64decode(payload, validate=True), encoding
        if encoding == "base58":
            return base58.b58decode(payload), encoding
    except (ValueError, TypeError) as exc:
        raise NormalizationError(f"Invalid {encoding} payload") from exc
    raise NormalizationError(f"Unsupported payload encoding: {encoding}")


def _normalize_account_indices(
    value: Any, account_keys: tuple[str, ...]
) -> tuple[int, ...]:
    """Normalize compiled indexes or parsed pubkeys into effective-key indexes."""
    raw_accounts = (
        list(bytes(value))
        if isinstance(value, (bytes, bytearray, memoryview))
        else _sequence(value)
    )
    indexes: list[int] = []
    for raw_account in raw_accounts:
        if type(raw_account) is int:
            index = raw_account
        else:
            account = _account_text(raw_account)
            try:
                index = account_keys.index(account)
            except ValueError as exc:
                raise NormalizationError(
                    f"Instruction account {account} is absent from effective keys"
                ) from exc
        if index < 0 or index >= len(account_keys):
            raise NormalizationError(
                f"Instruction account index {index} exceeds {len(account_keys)} keys"
            )
        indexes.append(index)
    return tuple(indexes)


def _normalize_instruction(
    instruction: Any,
    *,
    account_keys: tuple[str, ...],
    instruction_index: int,
    inner_index: int | None = None,
    parent_instruction_index: int | None = None,
) -> NormalizedInstruction:
    parsed = _mapping_or_attr(instruction, "parsed")
    program_id_value = _mapping_or_attr(instruction, "programId", "program_id")
    if callable(program_id_value):
        # solders.CompiledInstruction exposes ``program_id(account_keys)`` as
        # a resolver method, not a serialized pubkey. Its program_id_index is
        # the canonical wire reference normalized below.
        program_id_value = None
    program_id_index = _mapping_or_attr(
        instruction, "programIdIndex", "program_id_index"
    )
    indexed_program_id: str | None = None
    if program_id_index is not None:
        if type(program_id_index) is not int:
            raise NormalizationError("Instruction program index is invalid")
        if program_id_index < 0 or program_id_index >= len(account_keys):
            raise NormalizationError(
                f"Program index {program_id_index} exceeds {len(account_keys)} keys"
            )
        indexed_program_id = account_keys[program_id_index]
    if program_id_value is not None:
        program_id = _account_text(program_id_value)
        if indexed_program_id is not None and program_id != indexed_program_id:
            raise NormalizationError(
                "Instruction program reference disagrees with its program index"
            )
    elif indexed_program_id is not None:
        program_id = indexed_program_id
    else:
        raise NormalizationError("Instruction is missing its program reference")

    accounts = _normalize_account_indices(
        _mapping_or_attr(instruction, "accounts", default=[]), account_keys
    )
    raw_data = _mapping_or_attr(instruction, "data")
    if raw_data is None:
        if parsed is None:
            raise NormalizationError("Instruction has neither data nor parsed content")
        data = None
        encoding = "parsed"
    else:
        data, encoding = _decode_payload(raw_data)

    return NormalizedInstruction(
        program_id=program_id,
        accounts=accounts,
        data=data,
        encoding=encoding,
        instruction_index=instruction_index,
        inner_index=inner_index,
        parent_instruction_index=parent_instruction_index,
        parsed=parsed,
    )


def _loaded_accounts(meta: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    loaded = _mapping_or_attr(meta, "loadedAddresses", "loaded_addresses")
    if loaded is not None:
        writable = _mapping_or_attr(loaded, "writable", default=[])
        readonly = _mapping_or_attr(loaded, "readonly", "readOnly", default=[])
    else:
        writable = _mapping_or_attr(
            meta, "loaded_writable_addresses", "loadedWritableAddresses", default=[]
        )
        readonly = _mapping_or_attr(
            meta, "loaded_readonly_addresses", "loadedReadonlyAddresses", default=[]
        )
    return (
        tuple(_account_text(value) for value in _sequence(writable)),
        tuple(_account_text(value) for value in _sequence(readonly)),
    )


def _inner_instructions(
    meta: Any,
    account_keys: tuple[str, ...],
    *,
    outer_instruction_count: int,
) -> list[NormalizedInstruction]:
    groups = _mapping_or_attr(
        meta, "innerInstructions", "inner_instructions", default=[]
    )
    normalized: list[NormalizedInstruction] = []
    seen_parents: set[int] = set()
    for group in _sequence(groups):
        parent_index = _mapping_or_attr(group, "index")
        if (
            type(parent_index) is not int
            or parent_index < 0
            or parent_index >= outer_instruction_count
        ):
            raise NormalizationError(
                "Inner instruction group has an invalid parent index"
            )
        if parent_index in seen_parents:
            raise NormalizationError(
                f"Duplicate inner instruction group for parent {parent_index}"
            )
        seen_parents.add(parent_index)
        instructions = _mapping_or_attr(group, "instructions", default=[])
        for inner_index, instruction in enumerate(_sequence(instructions)):
            normalized.append(
                _normalize_instruction(
                    instruction,
                    account_keys=account_keys,
                    instruction_index=parent_index,
                    inner_index=inner_index,
                    parent_instruction_index=parent_index,
                )
            )
    return normalized


def _json_transaction_parts(
    tx_wrapper: dict[str, Any],
) -> tuple[
    str,
    str,
    tuple[str, ...],
    list[Any],
]:
    tx_data = tx_wrapper.get("transaction")
    if isinstance(tx_data, (list, tuple)):
        if not tx_data:
            raise NormalizationError("Encoded transaction is empty")
        encoded = tx_data[0]
        encoding = tx_data[1] if len(tx_data) > 1 else "base64"
        raw, used_encoding = _decode_payload([encoded, encoding])
        try:
            transaction = VersionedTransaction.from_bytes(raw)
            transaction.verify_and_hash_message()
        except Exception as exc:
            raise NormalizationError(
                "Encoded transaction is invalid or has an unverifiable signature"
            ) from exc
        signatures = list(transaction.signatures)
        if not signatures:
            raise NormalizationError("Transaction has no signature")
        normalized_signatures = tuple(
            _signature_text(signature, "Encoded transaction")
            for signature in signatures
        )
        if len(set(normalized_signatures)) != len(normalized_signatures):
            raise NormalizationError("Encoded transaction has duplicate signatures")
        static_accounts = tuple(str(key) for key in transaction.message.account_keys)
        if not static_accounts:
            raise NormalizationError("Encoded transaction has no account keys")
        return (
            normalized_signatures[0],
            used_encoding,
            static_accounts,
            list(transaction.message.instructions),
        )

    if not isinstance(tx_data, dict):
        raise NormalizationError("Transaction must be encoded or decoded JSON")
    message = tx_data.get("message")
    if not isinstance(message, dict):
        raise NormalizationError("Decoded transaction is missing its message")
    signatures = tx_data.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise NormalizationError("Decoded transaction has no signature")
    normalized_signatures = tuple(
        _signature_text(signature, "Decoded transaction") for signature in signatures
    )
    if len(set(normalized_signatures)) != len(normalized_signatures):
        raise NormalizationError("Decoded transaction has duplicate signatures")
    account_values = message.get("accountKeys")
    if account_values is None:
        account_values = message.get("account_keys")
    static_accounts = tuple(_account_text(value) for value in _sequence(account_values))
    if not static_accounts:
        raise NormalizationError("Decoded transaction has no account keys")
    instructions = _sequence(message.get("instructions", []))
    encoding = (
        "jsonParsed"
        if any(_mapping_or_attr(ix, "parsed") is not None for ix in instructions)
        else "json"
    )
    return normalized_signatures[0], encoding, static_accounts, instructions


def normalize_block_notification(
    data: dict[str, Any],
    *,
    subscription_ids: set[int] | frozenset[int],
) -> tuple[int, list[dict[str, Any]]] | None:
    """Validate a block notification and return its slot and transactions."""
    if data.get("method") != "blockNotification":
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        raise NotificationRejected("Block notification has no params")
    subscription_id = params.get("subscription")
    if type(subscription_id) is not int or subscription_id not in subscription_ids:
        raise NotificationRejected(
            f"Block notification has unknown subscription {subscription_id!r}"
        )
    result = params.get("result")
    if not isinstance(result, dict):
        raise NotificationRejected("Block notification has no result")
    value = result.get("value")
    if not isinstance(value, dict):
        raise NotificationRejected("Block notification has no value")
    if "err" not in value:
        raise NotificationRejected("Block notification has ambiguous status")
    if value["err"] is not None:
        raise NotificationRejected(
            f"Block notification reports provider error: {value['err']!r}"
        )
    context = result.get("context")
    context_slot = context.get("slot") if isinstance(context, dict) else None
    slot = value.get("slot", context_slot)
    if type(slot) is not int or slot < 0:
        raise NotificationRejected("Block notification has no valid slot")
    block = value.get("block")
    if not isinstance(block, dict):
        raise NotificationRejected("Block notification has no block")
    transactions = block.get("transactions")
    if not isinstance(transactions, list):
        raise NotificationRejected("Block transaction list is malformed")
    return slot, transactions


def normalize_block_transaction(
    tx_wrapper: dict[str, Any],
    *,
    slot: int | None,
    commitment: str | None,
    transaction_index: int,
    source: str = "blocks",
) -> NormalizedTransactionEvent:
    """Normalize one JSON-RPC block transaction, including v0 LUT and CPI data."""
    if not isinstance(tx_wrapper, dict):
        raise NormalizationError("Block transaction wrapper must be an object")
    if slot is not None and (type(slot) is not int or slot < 0):
        raise NormalizationError("Block transaction has an invalid slot")
    if type(transaction_index) is not int or transaction_index < 0:
        raise NormalizationError("Block transaction has an invalid index")
    meta = tx_wrapper.get("meta")
    if not isinstance(meta, dict) or "err" not in meta:
        raise NotificationRejected("Block transaction status is ambiguous")
    transaction_error = meta["err"]

    signature, encoding, static_accounts, raw_instructions = _json_transaction_parts(
        tx_wrapper
    )
    loaded_writable, loaded_readonly = _loaded_accounts(meta)
    _validate_account_key_sets(static_accounts, loaded_writable, loaded_readonly)
    account_keys = (*static_accounts, *loaded_writable, *loaded_readonly)
    instructions = [
        _normalize_instruction(
            instruction,
            account_keys=account_keys,
            instruction_index=index,
        )
        for index, instruction in enumerate(raw_instructions)
    ]
    instructions.extend(
        _inner_instructions(
            meta,
            account_keys,
            outer_instruction_count=len(raw_instructions),
        )
    )
    logs = meta.get("logMessages")
    if logs is None:
        logs = meta.get("log_messages", [])
    if not isinstance(logs, list) or not all(isinstance(log, str) for log in logs):
        raise NormalizationError("Block transaction logs are malformed")
    normalized_logs = tuple(logs)

    return NormalizedTransactionEvent(
        source=source,
        platform=None,
        signature=signature,
        slot=slot,
        commitment=commitment,
        transaction_index=transaction_index,
        static_accounts=static_accounts,
        loaded_writable_accounts=loaded_writable,
        loaded_readonly_accounts=loaded_readonly,
        encoding=encoding,
        transaction_error=transaction_error,
        instructions=tuple(instructions),
        logs=normalized_logs,
        raw=tx_wrapper,
    )


def normalize_logs_notification(
    data: dict[str, Any],
    *,
    subscription_ids: set[int] | frozenset[int],
    commitment: str = "processed",
) -> NormalizedTransactionEvent | None:
    """Normalize a logs notification and reject failed or ambiguous statuses."""
    if data.get("method") != "logsNotification":
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        raise NotificationRejected("Logs notification has no params")
    subscription_id = params.get("subscription")
    if type(subscription_id) is not int or subscription_id not in subscription_ids:
        raise NotificationRejected(
            f"Logs notification has unknown subscription {subscription_id!r}"
        )
    result = params.get("result")
    if not isinstance(result, dict):
        raise NotificationRejected("Logs notification has no result")
    context = result.get("context")
    value = result.get("value")
    if not isinstance(context, dict) or not isinstance(value, dict):
        raise NotificationRejected("Logs notification is missing context or value")
    if "err" not in value:
        raise NotificationRejected("Logs notification has ambiguous transaction status")
    if value["err"] is not None:
        raise NotificationRejected(
            f"Logs notification reports transaction error: {value['err']!r}"
        )
    signature = _signature_text(value.get("signature"), "Logs notification")
    logs = value.get("logs")
    slot = context.get("slot")
    if type(slot) is not int or slot < 0:
        raise NotificationRejected("Logs notification has no valid slot")
    if not isinstance(logs, list) or not all(isinstance(log, str) for log in logs):
        raise NotificationRejected("Logs notification has invalid logs")

    return NormalizedTransactionEvent(
        source="logs",
        platform=None,
        signature=signature,
        slot=slot,
        commitment=commitment,
        transaction_index=None,
        static_accounts=(),
        loaded_writable_accounts=(),
        loaded_readonly_accounts=(),
        encoding="logs",
        transaction_error=None,
        logs=tuple(logs),
        raw=data,
    )


def _protobuf_error(meta: Any) -> Any:
    has_field = getattr(meta, "HasField", None)
    error_present: bool | None = None
    if callable(has_field):
        try:
            error_present = bool(has_field("err"))
            if not error_present:
                return None
        except (ValueError, TypeError):
            error_present = None
    err = _mapping_or_attr(meta, "err")
    if err is None:
        return "<present transaction error without details>" if error_present else None
    payload = _mapping_or_attr(err, "err")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
        if raw:
            return base64.b64encode(raw).decode("ascii")
        return "<present transaction error without details>" if error_present else None
    if payload is None and error_present:
        return "<present transaction error without details>"
    return payload


def normalize_geyser_update(
    update: Any, *, commitment: str = "processed"
) -> NormalizedTransactionEvent | None:
    """Normalize a Yellowstone transaction update without relying on parser layout."""
    has_field = getattr(update, "HasField", None)
    if callable(has_field):
        try:
            has_transaction = bool(has_field("transaction"))
        except (TypeError, ValueError) as exc:
            raise NotificationRejected(
                "Geyser update has an invalid transaction field marker"
            ) from exc
        if not has_transaction:
            return None
    transaction_update = _mapping_or_attr(update, "transaction")
    if transaction_update is None:
        return None
    info = _mapping_or_attr(transaction_update, "transaction")
    if info is None:
        raise NotificationRejected("Geyser update has no transaction info")
    tx = _mapping_or_attr(info, "transaction")
    meta = _mapping_or_attr(info, "meta")
    if tx is None or meta is None:
        raise NotificationRejected("Geyser update is missing transaction or status")
    transaction_error = _protobuf_error(meta)
    message = _mapping_or_attr(tx, "message")
    if message is None:
        raise NotificationRejected("Geyser transaction has no message")

    static_accounts = tuple(
        _account_text(value)
        for value in _sequence(_mapping_or_attr(message, "account_keys", default=[]))
    )
    loaded_writable, loaded_readonly = _loaded_accounts(meta)
    _validate_account_key_sets(static_accounts, loaded_writable, loaded_readonly)
    account_keys = (*static_accounts, *loaded_writable, *loaded_readonly)
    raw_instructions = _sequence(_mapping_or_attr(message, "instructions", default=[]))
    instructions = [
        _normalize_instruction(
            instruction,
            account_keys=account_keys,
            instruction_index=index,
        )
        for index, instruction in enumerate(raw_instructions)
    ]
    instructions.extend(
        _inner_instructions(
            meta,
            account_keys,
            outer_instruction_count=len(raw_instructions),
        )
    )

    signature_value = _mapping_or_attr(info, "signature")
    raw_signatures = _sequence(_mapping_or_attr(tx, "signatures", default=[]))
    signature: str
    if raw_signatures:
        normalized_signatures = tuple(
            _signature_text(value, "Geyser transaction") for value in raw_signatures
        )
        if len(set(normalized_signatures)) != len(normalized_signatures):
            raise NotificationRejected("Geyser transaction has duplicate signatures")
        signature = normalized_signatures[0]
        if signature_value:
            reported_signature = _signature_text(signature_value, "Geyser transaction")
            if reported_signature != signature:
                raise NotificationRejected(
                    "Geyser signature disagrees with transaction signatures"
                )
    else:
        signature = _signature_text(signature_value, "Geyser transaction")

    slot = _mapping_or_attr(transaction_update, "slot")
    if slot is not None and (type(slot) is not int or slot < 0):
        raise NotificationRejected("Geyser transaction has an invalid slot")
    tx_index = _mapping_or_attr(info, "index")
    if tx_index is not None and (type(tx_index) is not int or tx_index < 0):
        raise NotificationRejected("Geyser transaction has an invalid index")
    logs = _sequence(_mapping_or_attr(meta, "log_messages", default=[]))
    if not all(isinstance(log, str) for log in logs):
        raise NotificationRejected("Geyser transaction logs are malformed")
    return NormalizedTransactionEvent(
        source="geyser",
        platform=None,
        signature=signature,
        slot=slot,
        commitment=commitment,
        transaction_index=tx_index,
        static_accounts=static_accounts,
        loaded_writable_accounts=loaded_writable,
        loaded_readonly_accounts=loaded_readonly,
        encoding="protobuf",
        transaction_error=transaction_error,
        instructions=tuple(instructions),
        logs=tuple(logs),
        raw=update,
    )


def normalize_pumpportal_event(
    token_data: dict[str, Any], *, platform: Platform
) -> NormalizedTransactionEvent:
    """Normalize PumpPortal's already-parsed creation event."""
    if not isinstance(token_data, dict):
        raise NotificationRejected("PumpPortal event must be an object")
    if not isinstance(platform, Platform):
        raise NotificationRejected("PumpPortal event has an invalid platform")
    signature = _signature_text(token_data.get("signature"), "PumpPortal event")
    transaction_error = token_data.get(
        "transactionError", token_data.get("error", token_data.get("err"))
    )
    if transaction_error is not None or token_data.get("success") is False:
        raise NotificationRejected(
            f"PumpPortal event reports transaction error: {transaction_error!r}"
        )
    slot = token_data.get("slot")
    transaction_index = token_data.get("transactionIndex")
    if slot is not None and (type(slot) is not int or slot < 0):
        raise NotificationRejected("PumpPortal event has an invalid slot")
    if transaction_index is not None and (
        type(transaction_index) is not int or transaction_index < 0
    ):
        raise NotificationRejected("PumpPortal event has an invalid index")
    return NormalizedTransactionEvent(
        source="pumpportal",
        platform=platform,
        signature=signature,
        slot=slot,
        commitment=token_data.get("commitment"),
        transaction_index=transaction_index,
        static_accounts=(),
        loaded_writable_accounts=(),
        loaded_readonly_accounts=(),
        encoding="parsed",
        transaction_error=None,
        raw=token_data,
    )


def account_key_bytes(event: NormalizedTransactionEvent) -> list[bytes]:
    """Decode normalized effective account keys for platform instruction parsers."""
    try:
        return [bytes(Pubkey.from_string(key)) for key in event.account_keys]
    except ValueError as exc:
        raise NormalizationError("Transaction contains an invalid account key") from exc


def attach_event_context(
    token_info: TokenInfo,
    event: NormalizedTransactionEvent,
    instruction: NormalizedInstruction | None = None,
) -> TokenInfo:
    """Attach lossless monitoring coordinates without changing TokenInfo's API."""
    platform = token_info.platform
    metadata: dict[str, Any] = {
        "source": event.source,
        "platform": platform.value,
        "signature": event.signature,
        "slot": event.slot,
        "commitment": event.commitment,
        "transaction_index": event.transaction_index,
        "static_accounts": list(event.static_accounts),
        "loaded_writable_accounts": list(event.loaded_writable_accounts),
        "loaded_readonly_accounts": list(event.loaded_readonly_accounts),
        "account_keys": list(event.account_keys),
        "encoding": event.encoding,
        "transaction_error": event.transaction_error,
    }
    if instruction is not None:
        metadata.update(
            {
                "instruction_index": instruction.instruction_index,
                "inner_index": instruction.inner_index,
                "parent_instruction_index": instruction.parent_instruction_index,
                "instruction_encoding": instruction.encoding,
                "program_id": instruction.program_id,
                "instruction_accounts": list(instruction.accounts),
            }
        )
    token_info.source = event.source
    token_info.signature = event.signature
    token_info.slot = event.slot
    token_info.commitment = event.commitment
    token_info.transaction_index = event.transaction_index
    if instruction is not None:
        token_info.inner_instruction_index = instruction.inner_index
    token_info.metadata_verified = token_info.metadata_verified or (
        token_info.state_from_event
        and instruction is not None
        and event.transaction_error is None
    )

    additional_data = dict(token_info.additional_data or {})
    additional_data["monitoring"] = metadata
    token_info.additional_data = additional_data
    return token_info

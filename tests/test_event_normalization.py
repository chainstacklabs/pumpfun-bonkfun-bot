from __future__ import annotations

from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature

from interfaces.core import Platform, TokenInfo
from monitoring.event_normalization import (
    NormalizationError,
    NormalizedTransactionEvent,
    NotificationRejected,
    attach_event_context,
    normalize_geyser_update,
)


def test_attach_event_context_populates_first_class_provenance() -> None:
    token = TokenInfo(
        name="Example",
        symbol="EX",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
    )
    event = NormalizedTransactionEvent(
        source="blocks",
        platform=Platform.PUMP_FUN,
        signature="sig",
        slot=7,
        commitment="confirmed",
        transaction_index=2,
        static_accounts=(),
        loaded_writable_accounts=(),
        loaded_readonly_accounts=(),
        encoding="json",
        transaction_error=None,
    )

    attached = attach_event_context(token, event)

    assert attached.source == "blocks"
    assert attached.signature == "sig"
    assert attached.slot == 7
    assert attached.commitment == "confirmed"
    assert attached.transaction_index == 2
    assert attached.metadata_verified is False
    assert attached.additional_data is not None
    assert attached.additional_data["monitoring"]["source"] == "blocks"


class _InvalidFieldUpdate:
    def HasField(self, name: str) -> bool:  # noqa: N802, ARG002
        raise ValueError("field probe failed")


def test_geyser_field_probe_failure_is_rejected() -> None:
    with pytest.raises((NotificationRejected, NormalizationError)):
        normalize_geyser_update(_InvalidFieldUpdate())


def _geyser_update_with_accounts(accounts: object) -> SimpleNamespace:
    keys = [Pubkey.new_unique() for _ in range(4)]
    signature = bytes(Signature.default())
    instruction = SimpleNamespace(
        program_id_index=3,
        accounts=accounts,
        data=b"\x01",
    )
    message = SimpleNamespace(
        account_keys=[bytes(key) for key in keys],
        instructions=[instruction],
    )
    transaction = SimpleNamespace(message=message, signatures=[signature])
    meta = SimpleNamespace(
        loaded_writable_addresses=[],
        loaded_readonly_addresses=[],
        inner_instructions=[],
        log_messages=[],
    )
    info = SimpleNamespace(
        transaction=transaction,
        meta=meta,
        signature=signature,
        index=0,
    )
    update = SimpleNamespace(
        transaction=SimpleNamespace(transaction=info, slot=8),
    )
    update._account_keys = keys
    return update


def test_geyser_compiled_account_bytes_are_u8_indexes() -> None:
    event = normalize_geyser_update(_geyser_update_with_accounts(bytes([0, 2, 1])))

    assert event is not None
    assert event.instructions[0].accounts == (0, 2, 1)


def test_compiled_instruction_resolver_method_does_not_mask_program_index() -> None:
    update = _geyser_update_with_accounts(bytes([0]))
    instruction = update.transaction.transaction.transaction.message.instructions[0]
    instruction.program_id = lambda _keys: Pubkey.new_unique()

    event = normalize_geyser_update(update)

    assert event is not None
    assert event.instructions[0].program_id == str(update._account_keys[3])


def test_geyser_parsed_pubkeys_map_to_effective_key_indexes() -> None:
    update = _geyser_update_with_accounts([])
    update.transaction.transaction.transaction.message.instructions[0].accounts = [
        str(update._account_keys[2]),
        {"pubkey": str(update._account_keys[0])},
    ]

    event = normalize_geyser_update(update)

    assert event is not None
    assert event.instructions[0].accounts == (2, 0)


def test_geyser_compiled_account_index_must_be_in_range() -> None:
    with pytest.raises(NormalizationError, match="exceeds 4 keys"):
        normalize_geyser_update(_geyser_update_with_accounts(bytes([4])))


def test_geyser_parsed_account_must_exist_in_effective_keys() -> None:
    missing = Pubkey.new_unique()

    with pytest.raises(NormalizationError, match="absent from effective keys"):
        normalize_geyser_update(_geyser_update_with_accounts([str(missing)]))

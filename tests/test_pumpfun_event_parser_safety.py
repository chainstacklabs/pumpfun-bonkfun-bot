from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from solders.pubkey import Pubkey

from core.pubkeys import (
    DEFAULT_PUBKEY,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
)
from interfaces.core import Platform
from platforms.pumpfun.address_provider import PumpFunAddresses, PumpFunAddressProvider
from platforms.pumpfun.event_parser import PumpFunEventParser
from platforms.pumpfun.pumpportal_processor import PumpFunPumpPortalProcessor
from utils.idl_manager import get_idl_manager

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "learning-examples"
    / "raw_create_tx_from_gettransaction.json"
)
_EVENT_DISCRIMINATOR = b"event000"
_CREATE_DISCRIMINATOR = b"create00"
_CREATE_V2_DISCRIMINATOR = b"createv2"


class _RecordingIDLParser:
    def __init__(self) -> None:
        self.decode_instruction_calls = 0

    def get_event_discriminators(self) -> dict[str, bytes]:
        return {"CreateEvent": _EVENT_DISCRIMINATOR}

    def get_instruction_discriminators(self) -> dict[str, bytes]:
        return {
            "create": _CREATE_DISCRIMINATOR,
            "create_v2": _CREATE_V2_DISCRIMINATOR,
        }

    def decode_instruction(
        self, instruction_data: bytes, account_keys: list[bytes], accounts: list[int]
    ) -> None:
        self.decode_instruction_calls += 1


class _StaticEventIDLParser(_RecordingIDLParser):
    def __init__(self, fields: dict[str, Any]) -> None:
        super().__init__()
        self._fields = fields

    def decode_event_data(self, event_data: bytes, event_name: str) -> dict[str, Any]:
        return {"event_name": event_name, "fields": self._fields}


class _DecodedInstructionIDLParser(_RecordingIDLParser):
    def __init__(self, **metadata: object) -> None:
        super().__init__()
        self._metadata: dict[str, object] = {
            "name": "safe",
            "symbol": "SAFE",
            "uri": "https://example.invalid/safe.json",
        }
        self._metadata.update(metadata)

    def decode_instruction(
        self, instruction_data: bytes, account_keys: list[bytes], accounts: list[int]
    ) -> dict[str, Any]:
        self.decode_instruction_calls += 1
        return {
            "instruction_name": "create_v2",
            "args": {
                **self._metadata,
                "creator": Pubkey.new_unique(),
                "is_mayhem_mode": False,
                "is_cashback_enabled": False,
            },
        }


def _canonical_event_fields(
    mint: Pubkey, bonding_curve: Pubkey | None = None
) -> dict[str, Any]:
    return {
        "name": "safe",
        "symbol": "SAFE",
        "uri": "https://example.invalid/safe.json",
        "mint": mint,
        "bonding_curve": (
            bonding_curve
            if bonding_curve is not None
            else PumpFunAddressProvider().derive_pool_address(mint)
        ),
        "user": Pubkey.new_unique(),
        "creator": Pubkey.new_unique(),
        "timestamp": 1,
        "virtual_token_reserves": 1,
        "virtual_sol_reserves": 1,
        "real_token_reserves": 0,
        "token_total_supply": 1,
        "token_program": TOKEN_PROGRAM,
        "is_mayhem_mode": False,
        "is_cashback_enabled": False,
        "quote_mint": DEFAULT_PUBKEY,
        "virtual_quote_reserves": 1,
    }


def _pump_event_logs() -> list[str]:
    encoded_event = base64.b64encode(_EVENT_DISCRIMINATOR).decode()
    program = str(PumpFunAddresses.PROGRAM)
    return [
        f"Program {program} invoke [1]",
        "Program log: Instruction: CreateV2",
        f"Program data: {encoded_event}",
        f"Program {program} success",
    ]


def _create_v2_accounts() -> tuple[list[int], list[bytes]]:
    provider = PumpFunAddressProvider()
    mint = Pubkey.new_unique()
    bonding_curve = provider.derive_pool_address(mint)
    associated_bonding_curve = provider.derive_associated_bonding_curve(
        mint, bonding_curve, TOKEN_2022_PROGRAM
    )
    associated_quote_bonding_curve = provider.derive_quote_token_account(
        bonding_curve, WSOL_MINT, TOKEN_PROGRAM
    )
    keys = [Pubkey.new_unique() for _ in range(19)]
    keys[0] = mint
    keys[2] = bonding_curve
    keys[3] = associated_bonding_curve
    keys[5] = Pubkey.new_unique()
    keys[7] = TOKEN_2022_PROGRAM
    keys[16] = WSOL_MINT
    keys[17] = associated_quote_bonding_curve
    keys[18] = TOKEN_PROGRAM
    return list(range(19)), [bytes(key) for key in keys]


def _fixture_logs() -> list[str]:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return fixture["result"]["meta"]["logMessages"]


def _fixture_create_event_log(parser: PumpFunEventParser) -> str:
    discriminator = parser.get_event_discriminators()[0]
    for log in _fixture_logs():
        if not log.startswith("Program data: "):
            continue
        decoded = base64.b64decode(log.split("Program data: ", 1)[1])
        if decoded.startswith(discriminator):
            return log
    raise AssertionError("fixture has no CreateEvent log")


def _real_parser() -> PumpFunEventParser:
    return PumpFunEventParser(get_idl_manager().get_parser(Platform.PUMP_FUN))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "   "),
        ("name", 7),
        ("symbol", ""),
        ("symbol", "   "),
        ("symbol", 7),
    ],
)
def test_pump_token_producers_reject_invalid_name_and_symbol(
    field: str, value: object
) -> None:
    mint = Pubkey.new_unique()
    fields = _canonical_event_fields(mint)
    fields[field] = value
    event_parser = PumpFunEventParser(  # type: ignore[arg-type]
        _StaticEventIDLParser(fields)
    )
    assert (
        event_parser.parse_token_creation_from_logs(
            _pump_event_logs(), signature="invalid-metadata"
        )
        is None
    )

    accounts, account_keys = _create_v2_accounts()
    instruction_parser = PumpFunEventParser(  # type: ignore[arg-type]
        _DecodedInstructionIDLParser(**{field: value})
    )
    assert (
        instruction_parser.parse_token_creation_from_instruction(
            _CREATE_V2_DISCRIMINATOR, accounts, account_keys
        )
        is None
    )

    payload: dict[str, object] = {
        "pool": "pump",
        "name": "safe",
        "symbol": "SAFE",
        "mint": str(Pubkey.new_unique()),
        "bondingCurveKey": str(Pubkey.new_unique()),
        "traderPublicKey": str(Pubkey.new_unique()),
    }
    payload[field] = value
    assert PumpFunPumpPortalProcessor().process_token_data(payload) is None


def test_valid_pump_fixture_retains_provenance_and_derived_accounts() -> None:
    token = _real_parser().parse_token_creation_from_logs(
        _fixture_logs(), signature="fixture"
    )

    assert token is not None
    assert token.state_from_event is True
    assert token.bonding_curve == PumpFunAddressProvider().derive_pool_address(
        token.mint
    )
    assert token.associated_bonding_curve is not None
    assert token.creator_vault is not None


def test_create_event_without_program_invocation_provenance_is_rejected() -> None:
    parser = _real_parser()
    event_log = _fixture_create_event_log(parser)

    token = parser.parse_token_creation_from_logs(
        ["Program log: Instruction: CreateV2", event_log],
        signature="untrusted",
    )

    assert token is None


def test_create_event_under_foreign_program_invocation_is_rejected() -> None:
    parser = _real_parser()
    event_log = _fixture_create_event_log(parser)
    foreign_program = str(Pubkey.new_unique())

    token = parser.parse_token_creation_from_logs(
        [
            f"Program {foreign_program} invoke [1]",
            "Program log: Instruction: CreateV2",
            event_log,
            f"Program {foreign_program} success",
        ],
        signature="foreign-program",
    )

    assert token is None


def test_incomplete_pump_create_event_is_rejected() -> None:
    mint = Pubkey.new_unique()
    fields = _canonical_event_fields(mint)
    fields.pop("quote_mint")
    parser = PumpFunEventParser(_StaticEventIDLParser(fields))  # type: ignore[arg-type]

    token = parser.parse_token_creation_from_logs(
        _pump_event_logs(), signature="incomplete-event"
    )

    assert token is None


def test_create_event_with_noncanonical_bonding_curve_is_rejected() -> None:
    mint = Pubkey.new_unique()
    fields = _canonical_event_fields(mint, bonding_curve=Pubkey.new_unique())
    parser = PumpFunEventParser(_StaticEventIDLParser(fields))  # type: ignore[arg-type]

    token = parser.parse_token_creation_from_logs(
        _pump_event_logs(), signature="wrong-curve"
    )

    assert token is None


def test_instruction_parser_rejects_negative_account_indices_before_decoding() -> None:
    idl_parser = _RecordingIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    accounts = list(range(13))
    accounts[0] = -1

    assert (
        parser.parse_token_creation_from_instruction(
            _CREATE_DISCRIMINATOR, accounts, [bytes(Pubkey.new_unique())] * 13
        )
        is None
    )
    assert idl_parser.decode_instruction_calls == 0


def test_instruction_parser_rejects_malformed_key_lengths_before_decoding() -> None:
    idl_parser = _RecordingIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    account_keys = [bytes(Pubkey.new_unique()) for _ in range(13)]
    account_keys[4] = b"short"

    assert (
        parser.parse_token_creation_from_instruction(
            _CREATE_DISCRIMINATOR, list(range(13)), account_keys
        )
        is None
    )
    assert idl_parser.decode_instruction_calls == 0


@pytest.mark.parametrize("account_count", (17, 18))
def test_create_v2_rejects_partial_remaining_account_group(
    account_count: int,
) -> None:
    idl_parser = _RecordingIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    account_keys = [bytes(Pubkey.new_unique()) for _ in range(account_count)]

    assert (
        parser.parse_token_creation_from_instruction(
            _CREATE_V2_DISCRIMINATOR,
            list(range(account_count)),
            account_keys,
        )
        is None
    )
    assert idl_parser.decode_instruction_calls == 0


@pytest.mark.parametrize("invalid_index", (17, 18))
def test_create_v2_validates_all_quote_remaining_accounts(
    invalid_index: int,
) -> None:
    idl_parser = _DecodedInstructionIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    accounts, account_keys = _create_v2_accounts()
    account_keys[invalid_index] = bytes(Pubkey.new_unique())

    assert (
        parser.parse_token_creation_from_instruction(
            _CREATE_V2_DISCRIMINATOR, accounts, account_keys
        )
        is None
    )


@pytest.mark.parametrize("account_count", (16, 19))
def test_create_v2_accepts_canonical_remaining_account_shapes(
    account_count: int,
) -> None:
    idl_parser = _DecodedInstructionIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    accounts, account_keys = _create_v2_accounts()

    token = parser.parse_token_creation_from_instruction(
        _CREATE_V2_DISCRIMINATOR,
        accounts[:account_count],
        account_keys[:account_count],
    )

    assert token is not None
    assert token.quote_mint == WSOL_MINT
    assert token.quote_token_program_id == TOKEN_PROGRAM


def test_geyser_parser_rejects_negative_program_index_before_instruction_decode() -> (
    None
):
    idl_parser = _RecordingIDLParser()
    parser = PumpFunEventParser(idl_parser)  # type: ignore[arg-type]
    instruction_parse_calls = 0

    def record_instruction_parse(
        instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> None:
        nonlocal instruction_parse_calls
        instruction_parse_calls += 1

    parser.parse_token_creation_from_instruction = record_instruction_parse  # type: ignore[method-assign]
    message = SimpleNamespace(
        account_keys=[Pubkey.new_unique(), PumpFunAddresses.PROGRAM],
        instructions=[
            SimpleNamespace(
                program_id_index=-1,
                data=_CREATE_DISCRIMINATOR,
                accounts=[0] * 13,
            )
        ],
    )
    transaction_info = SimpleNamespace(
        transaction=SimpleNamespace(
            transaction=SimpleNamespace(
                meta=None,
                transaction=SimpleNamespace(message=message),
            )
        )
    )

    assert parser.parse_token_creation_from_geyser(transaction_info) is None
    assert instruction_parse_calls == 0

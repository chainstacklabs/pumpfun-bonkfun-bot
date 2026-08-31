from __future__ import annotations

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo
from monitoring.event_normalization import (
    NormalizedInstruction,
    NormalizedTransactionEvent,
)
from monitoring.parser_dispatch import parse_normalized_event


class _PumpParser:
    def __init__(
        self,
        program_id: Pubkey,
        log_token: TokenInfo,
        instruction_tokens: dict[bytes, TokenInfo],
    ) -> None:
        self._program_id = program_id
        self._log_token = log_token
        self._instruction_tokens = instruction_tokens

    def get_program_id(self) -> Pubkey:
        return self._program_id

    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo:
        return self._log_token

    def parse_token_creation_from_instruction(
        self,
        data: bytes,
        accounts: list[int],
        account_keys: list[bytes],
    ) -> TokenInfo | None:
        return self._instruction_tokens.get(data)


def _pump_token(
    *,
    name: str,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    user: Pubkey,
    token_program_id: Pubkey,
    state_from_event: bool,
    metadata_verified: bool,
) -> TokenInfo:
    return TokenInfo(
        name=name,
        symbol="TKN",
        uri="https://example.test/token.json",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=associated_bonding_curve,
        user=user,
        token_program_id=token_program_id,
        state_from_event=state_from_event,
        metadata_verified=metadata_verified,
    )


def test_pump_log_correlation_keeps_other_unique_create_instructions() -> None:
    program_id = Pubkey.new_unique()
    token_program_id = Pubkey.new_unique()
    user = Pubkey.new_unique()
    first_mint = Pubkey.new_unique()
    first_curve = Pubkey.new_unique()
    first_associated_curve = Pubkey.new_unique()
    second_mint = Pubkey.new_unique()
    second_curve = Pubkey.new_unique()
    second_associated_curve = Pubkey.new_unique()

    log_token = _pump_token(
        name="log token",
        mint=first_mint,
        bonding_curve=first_curve,
        associated_bonding_curve=first_associated_curve,
        user=user,
        token_program_id=token_program_id,
        state_from_event=True,
        metadata_verified=False,
    )
    correlated_instruction_token = _pump_token(
        name="correlated instruction token",
        mint=first_mint,
        bonding_curve=first_curve,
        associated_bonding_curve=first_associated_curve,
        user=user,
        token_program_id=token_program_id,
        state_from_event=False,
        metadata_verified=False,
    )
    extra_instruction_token = _pump_token(
        name="extra instruction token",
        mint=second_mint,
        bonding_curve=second_curve,
        associated_bonding_curve=second_associated_curve,
        user=user,
        token_program_id=token_program_id,
        state_from_event=True,
        metadata_verified=True,
    )
    parser = _PumpParser(
        program_id,
        log_token,
        {
            b"create-first": correlated_instruction_token,
            b"create-second": extra_instruction_token,
        },
    )
    event = NormalizedTransactionEvent(
        source="blocks",
        platform=Platform.PUMP_FUN,
        signature="multi-create",
        slot=42,
        commitment="confirmed",
        transaction_index=3,
        static_accounts=(str(Pubkey.new_unique()),),
        loaded_writable_accounts=(),
        loaded_readonly_accounts=(),
        encoding="base64",
        transaction_error=None,
        instructions=(
            NormalizedInstruction(
                program_id=str(program_id),
                accounts=(),
                data=b"create-first",
                encoding="base64",
                instruction_index=1,
            ),
            NormalizedInstruction(
                program_id=str(program_id),
                accounts=(),
                data=b"create-second",
                encoding="base64",
                instruction_index=4,
            ),
        ),
        logs=("Program log: Instruction: CreateEvent",),
    )

    tokens = parse_normalized_event(event, {Platform.PUMP_FUN: parser})

    assert [token.mint for token in tokens] == [first_mint, second_mint]
    assert len({token.mint for token in tokens}) == 2
    assert tokens[0].name == "log token"
    assert tokens[0].state_from_event is True
    assert tokens[0].metadata_verified is True
    assert tokens[0].additional_data is not None
    assert tokens[0].additional_data["monitoring"]["instruction_index"] == 1
    assert tokens[1].name == "extra instruction token"
    assert tokens[1].state_from_event is False
    assert tokens[1].metadata_verified is False
    assert tokens[1].additional_data is not None
    assert tokens[1].additional_data["monitoring"]["instruction_index"] == 4

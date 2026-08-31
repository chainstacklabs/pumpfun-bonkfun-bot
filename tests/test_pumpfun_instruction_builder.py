from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.instruction_builder import (
    _BUY_V2_ACCOUNTS,
    PumpFunInstructionBuilder,
)

_DISCRIMINATORS = {
    "buy_exact_sol_in": b"legacy__",
    "sell": b"sell____",
    "buy_v2": b"buy_v2__",
    "sell_v2": b"sell_v2_",
}


class _IdlParser:
    def get_instruction_discriminators(self) -> dict[str, bytes]:
        return dict(_DISCRIMINATORS)


def _token() -> TokenInfo:
    return TokenInfo(
        name="Token",
        symbol="TOK",
        uri="https://example.invalid/token.json",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        token_program_id=TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
        quote_token_program_id=TOKEN_PROGRAM,
    )


@pytest.mark.asyncio
async def test_buy_v2_wire_is_exact_output_with_max_quote_cap() -> None:
    token = _token()
    user = Pubkey.new_unique()
    accounts = {name: Pubkey.new_unique() for name, _writable in _BUY_V2_ACCOUNTS}
    accounts.update(
        {
            "base_mint": token.mint,
            "quote_mint": WSOL_MINT,
            "base_token_program": TOKEN_2022_PROGRAM,
            "quote_token_program": TOKEN_PROGRAM,
            "user": user,
        }
    )
    provider = SimpleNamespace(
        get_buy_v2_instruction_accounts=lambda _token, _user: accounts
    )
    builder = PumpFunInstructionBuilder(_IdlParser())

    instructions = await builder.build_buy_instruction(
        token,
        user,
        amount_in=100,
        minimum_amount_out=25,
        address_provider=provider,
    )
    venue_instruction = instructions[-1]

    assert builder.buy_uses_exact_output is True
    assert bytes(venue_instruction.data) == b"buy_v2__" + struct.pack("<QQ", 25, 100)
    assert len(venue_instruction.accounts) == 27


@pytest.mark.asyncio
async def test_legacy_buy_uses_exact_input_discriminator_and_one_byte_option_bool() -> (
    None
):
    token = _token()
    user = Pubkey.new_unique()
    keys = {
        "global",
        "fee",
        "mint",
        "bonding_curve",
        "associated_bonding_curve",
        "user_token_account",
        "user",
        "system_program",
        "token_program",
        "creator_vault",
        "event_authority",
        "program",
        "global_volume_accumulator",
        "user_volume_accumulator",
        "fee_config",
        "fee_program",
        "bonding_curve_v2",
        "breaking_fee_recipient",
    }
    accounts = {name: Pubkey.new_unique() for name in keys}
    accounts.update(
        {
            "mint": token.mint,
            "user": user,
            "token_program": TOKEN_2022_PROGRAM,
        }
    )
    provider = SimpleNamespace(
        get_buy_instruction_accounts=lambda _token, _user: accounts
    )
    builder = PumpFunInstructionBuilder(_IdlParser(), use_legacy_instructions=True)

    instructions = await builder.build_buy_instruction(
        token,
        user,
        amount_in=100,
        minimum_amount_out=25,
        address_provider=provider,
    )
    venue_instruction = instructions[-1]

    assert builder.buy_uses_exact_output is False
    assert bytes(venue_instruction.data) == b"legacy__" + struct.pack(
        "<QQB", 100, 25, 1
    )
    assert len(venue_instruction.accounts) == 18

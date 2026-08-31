from __future__ import annotations

import logging

import pytest
from solders.pubkey import Pubkey

from core.pubkeys import SystemAddresses
from interfaces.core import Platform, TokenInfo
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from platforms.letsbonk.instruction_builder import LetsBonkInstructionBuilder
from platforms.letsbonk.pumpportal_processor import LetsBonkPumpPortalProcessor


class _Discriminators:
    def get_instruction_discriminators(self) -> dict[str, bytes]:
        return {
            "buy_exact_in": b"buyexact",
            "sell_exact_in": b"sellexac",
        }


def test_letsbonk_raw_amounts_allow_zero_minimum_but_not_zero_input() -> None:
    LetsBonkInstructionBuilder._validate_raw_amount(
        0,
        "minimum_amount_out",
        positive=False,
    )

    with pytest.raises(ValueError, match="amount_in"):
        LetsBonkInstructionBuilder._validate_raw_amount(
            0,
            "amount_in",
            positive=True,
        )


def _authoritative_token(provider: LetsBonkAddressProvider) -> TokenInfo:
    mint = Pubkey.new_unique()
    quote_mint = SystemAddresses.SOL_MINT
    return TokenInfo(
        name="Authoritative",
        symbol="AUTH",
        uri="https://example.invalid/token.json",
        mint=mint,
        platform=Platform.LETS_BONK,
        pool_state=provider.derive_pool_address(mint, quote_mint),
        base_vault=provider.derive_base_vault(mint, quote_mint),
        quote_vault=provider.derive_quote_vault(mint, quote_mint),
        global_config=Pubkey.new_unique(),
        platform_config=Pubkey.new_unique(),
        creator=Pubkey.new_unique(),
        token_program_id=SystemAddresses.TOKEN_PROGRAM,
        quote_mint=quote_mint,
        quote_token_program_id=SystemAddresses.TOKEN_PROGRAM,
    )


def test_pumpportal_letsbonk_is_rejected_before_account_derivation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = LetsBonkPumpPortalProcessor()

    class _AccountsMustNotBeGuessed:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(
                f"PumpPortal rejection attempted account lookup: {name}"
            )

    processor.address_provider = _AccountsMustNotBeGuessed()
    payload = {
        "pool": "bonk",
        "name": "Unverified",
        "symbol": "NOPE",
        "mint": str(Pubkey.new_unique()),
        "traderPublicKey": str(Pubkey.new_unique()),
    }

    with caplog.at_level(logging.WARNING):
        assert processor.can_process(payload)
        assert processor.process_token_data(payload) is None

    assert (
        "PumpPortal LetsBonk events lack authoritative LaunchLab pool metadata; "
        "LetsBonk PumpPortal execution is disabled"
    ) in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_consecutive_builds_use_distinct_temporary_wsol_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeds = iter(("0" * 32, "1" * 32, "2" * 32, "3" * 32))

    def next_seed(byte_count: int) -> str:
        assert byte_count == 16
        return next(seeds)

    monkeypatch.setattr(
        "platforms.letsbonk.instruction_builder.secrets.token_hex",
        next_seed,
    )
    provider = LetsBonkAddressProvider()
    token = _authoritative_token(provider)
    user = Pubkey.new_unique()
    builder = LetsBonkInstructionBuilder(_Discriminators())

    first = await builder.build_buy_instruction(token, user, 1_000_000, 1, provider)
    second = await builder.build_buy_instruction(token, user, 1_000_000, 1, provider)

    first_trade = first[3]
    second_trade = second[3]
    first_temporary_wsol = first_trade.accounts[6].pubkey
    second_temporary_wsol = second_trade.accounts[6].pubkey

    assert first_temporary_wsol != second_temporary_wsol
    assert first[-1].accounts[0].pubkey == first_temporary_wsol
    assert second[-1].accounts[0].pubkey == second_temporary_wsol

    third = await builder.build_sell_instruction(token, user, 1, 1, provider)
    fourth = await builder.build_sell_instruction(token, user, 1, 1, provider)
    third_temporary_wsol = third[2].accounts[6].pubkey
    fourth_temporary_wsol = fourth[2].accounts[6].pubkey

    assert (
        len(
            {
                first_temporary_wsol,
                second_temporary_wsol,
                third_temporary_wsol,
                fourth_temporary_wsol,
            }
        )
        == 4
    )
    assert third[-1].accounts[0].pubkey == third_temporary_wsol
    assert fourth[-1].accounts[0].pubkey == fourth_temporary_wsol


def test_priority_fee_accounts_are_only_writable_launchlab_accounts() -> None:
    provider = LetsBonkAddressProvider()
    token = _authoritative_token(provider)
    user = Pubkey.new_unique()
    builder = LetsBonkInstructionBuilder(_Discriminators())
    resolved = provider.get_buy_instruction_accounts(token, user)
    expected = [
        resolved["pool_state"],
        resolved["base_vault"],
        resolved["quote_vault"],
        resolved["platform_fee_vault"],
        resolved["creator_fee_vault"],
    ]

    assert builder.get_required_accounts_for_buy(token, user, provider) == expected
    assert builder.get_required_accounts_for_sell(token, user, provider) == expected
    assert all(isinstance(account, Pubkey) for account in expected)


def test_address_provider_rejects_missing_authoritative_pool_metadata() -> None:
    provider = LetsBonkAddressProvider()
    token = TokenInfo(
        name="Incomplete",
        symbol="NONE",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.LETS_BONK,
    )

    with pytest.raises(ValueError, match="refusing to build guessed accounts"):
        provider.get_buy_instruction_accounts(token, Pubkey.new_unique())


def test_address_provider_rejects_mismatched_authoritative_pool_pda() -> None:
    provider = LetsBonkAddressProvider()
    token = _authoritative_token(provider)
    token.pool_state = Pubkey.new_unique()

    with pytest.raises(ValueError, match="pool_state does not match"):
        provider.get_sell_instruction_accounts(token, Pubkey.new_unique())

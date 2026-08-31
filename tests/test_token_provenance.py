from __future__ import annotations

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo


def test_token_info_carries_detection_provenance_and_unit_metadata() -> None:
    token = TokenInfo(
        name="Example",
        symbol="EX",
        uri="https://example.invalid/token.json",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        source="geyser",
        signature="sig",
        slot=42,
        commitment="confirmed",
        transaction_index=3,
        inner_instruction_index=1,
        base_decimals=6,
        quote_decimals=9,
        metadata_verified=True,
    )

    assert token.source == "geyser"
    assert token.signature == "sig"
    assert token.slot == 42
    assert token.transaction_index == 3
    assert token.inner_instruction_index == 1
    assert token.base_decimals == 6
    assert token.quote_decimals == 9
    assert token.metadata_verified is True

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest
from solders.pubkey import Pubkey

import monitoring.universal_block_listener as block_listener_module
from interfaces.core import Platform, TokenInfo
from monitoring.event_normalization import (
    NormalizedInstruction,
    NormalizedTransactionEvent,
)
from monitoring.universal_block_listener import UniversalBlockListener

_PROGRAM_IDS = {
    Platform.PUMP_FUN: "pump-program",
    Platform.LETS_BONK: "letsbonk-program",
}
_MINTS = {
    Platform.PUMP_FUN: Pubkey.from_bytes(bytes([1]) * 32),
    Platform.LETS_BONK: Pubkey.from_bytes(bytes([2]) * 32),
}


class _Parser:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.instruction_calls: list[bytes] = []

    def get_program_id(self) -> str:
        return _PROGRAM_IDS[self.platform]

    def parse_token_creation_from_logs(self, logs: list[str], signature: str) -> None:
        return None

    def parse_token_creation_from_instruction(
        self,
        data: bytes,
        accounts: list[int],
        account_keys: list[bytes],
    ) -> TokenInfo:
        assert data == self.platform.value.encode()
        self.instruction_calls.append(data)
        return TokenInfo(
            name=self.platform.value,
            symbol=self.platform.value,
            uri="https://offline.invalid/token.json",
            mint=_MINTS[self.platform],
            platform=self.platform,
        )


class _FrameWebSocket:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = deque(json.dumps(frame) for frame in frames)
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def recv(self) -> str:
        return self.frames.popleft()


def _listener(
    monkeypatch: pytest.MonkeyPatch,
    platforms: list[Platform],
) -> tuple[UniversalBlockListener, dict[Platform, _Parser]]:
    parsers: dict[Platform, _Parser] = {}

    def implementations(platform: Platform, _: object) -> SimpleNamespace:
        parser = parsers.setdefault(platform, _Parser(platform))
        return SimpleNamespace(event_parser=parser)

    monkeypatch.setattr(
        block_listener_module,
        "get_platform_implementations",
        implementations,
    )
    return UniversalBlockListener("wss://offline.invalid", platforms), parsers


def _block_notification(subscription_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "blockNotification",
        "params": {
            "subscription": subscription_id,
            "result": {
                "context": {"slot": 77},
                "value": {
                    "err": None,
                    "slot": 77,
                    "block": {
                        "transactions": [
                            {"platform": Platform.PUMP_FUN.value},
                            {"platform": Platform.LETS_BONK.value},
                        ]
                    },
                },
            },
        },
    }


def _normalized_event(
    tx_wrapper: dict[str, Any],
    *,
    slot: int | None,
    commitment: str | None,
    transaction_index: int,
) -> NormalizedTransactionEvent:
    platform = Platform(tx_wrapper["platform"])
    instruction_index = 3 if platform is Platform.PUMP_FUN else 7
    inner_index = 1 if platform is Platform.PUMP_FUN else None
    return NormalizedTransactionEvent(
        source="blocks",
        platform=None,
        signature=f"{platform.value}-signature",
        slot=slot,
        commitment=commitment,
        transaction_index=transaction_index,
        static_accounts=(),
        loaded_writable_accounts=(),
        loaded_readonly_accounts=(),
        encoding="json",
        transaction_error=None,
        instructions=(
            NormalizedInstruction(
                program_id=_PROGRAM_IDS[platform],
                accounts=(),
                data=platform.value.encode(),
                encoding="bytes",
                instruction_index=instruction_index,
                inner_index=inner_index,
            ),
        ),
    )


def _token_with_coordinates(index: int) -> TokenInfo:
    token = TokenInfo(
        name=f"token-{index}",
        symbol=f"T{index}",
        uri="https://offline.invalid/token.json",
        mint=Pubkey.from_bytes(bytes([index]) * 32),
        platform=Platform.PUMP_FUN,
    )
    token.signature = f"signature-{index}"
    token.additional_data = {
        "monitoring": {
            "instruction_index": index,
            "inner_index": None,
        }
    }
    return token


@pytest.mark.asyncio
async def test_subscriptions_deduplicate_platforms_and_preserve_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener, _ = _listener(
        monkeypatch,
        [Platform.PUMP_FUN, Platform.LETS_BONK, Platform.PUMP_FUN],
    )
    websocket = _FrameWebSocket(
        [
            {"jsonrpc": "2.0", "id": 1, "result": 101},
            {"jsonrpc": "2.0", "id": 2, "result": 202},
        ]
    )

    await listener._subscribe_to_programs(websocket)

    requests = [json.loads(frame) for frame in websocket.sent]
    assert [request["id"] for request in requests] == [1, 2]
    assert [
        request["params"][0]["mentionsAccountOrProgram"] for request in requests
    ] == [
        _PROGRAM_IDS[Platform.PUMP_FUN],
        _PROGRAM_IDS[Platform.LETS_BONK],
    ]
    assert listener._subscription_platforms == {
        101: Platform.PUMP_FUN,
        202: Platform.LETS_BONK,
    }


@pytest.mark.asyncio
async def test_full_blocks_route_by_subscription_and_dedupe_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener, parsers = _listener(
        monkeypatch,
        [Platform.PUMP_FUN, Platform.LETS_BONK],
    )
    monkeypatch.setattr(
        block_listener_module,
        "normalize_block_transaction",
        _normalized_event,
    )
    listener._subscription_platforms = {
        101: Platform.PUMP_FUN,
        202: Platform.LETS_BONK,
    }
    listener._pending_frames.extend(
        json.dumps(notification)
        for notification in (
            _block_notification(101),
            _block_notification(202),
            _block_notification(101),
            _block_notification(202),
        )
    )

    deliveries = [await listener._wait_for_token_creation(object()) for _ in range(4)]

    assert [[token.platform for token in delivery] for delivery in deliveries] == [
        [Platform.PUMP_FUN],
        [Platform.LETS_BONK],
        [],
        [],
    ]
    assert [[str(token.mint) for token in delivery] for delivery in deliveries] == [
        [str(_MINTS[Platform.PUMP_FUN])],
        [str(_MINTS[Platform.LETS_BONK])],
        [],
        [],
    ]
    assert parsers[Platform.PUMP_FUN].instruction_calls == [
        Platform.PUMP_FUN.value.encode(),
        Platform.PUMP_FUN.value.encode(),
    ]
    assert parsers[Platform.LETS_BONK].instruction_calls == [
        Platform.LETS_BONK.value.encode(),
        Platform.LETS_BONK.value.encode(),
    ]


@pytest.mark.asyncio
async def test_unknown_block_subscription_is_rejected_before_parser_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener, parsers = _listener(monkeypatch, [Platform.PUMP_FUN])
    monkeypatch.setattr(
        block_listener_module,
        "normalize_block_transaction",
        _normalized_event,
    )
    listener._subscription_platforms = {101: Platform.PUMP_FUN}
    listener._pending_frames.append(json.dumps(_block_notification(999)))

    assert await listener._wait_for_token_creation(object()) == []
    assert parsers[Platform.PUMP_FUN].instruction_calls == []


def test_creation_dedupe_uses_instruction_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener, _ = _listener(monkeypatch, [Platform.PUMP_FUN])
    first = _token_with_coordinates(1)
    second = _token_with_coordinates(1)
    second.additional_data = {
        "monitoring": {
            "instruction_index": 2,
            "inner_index": None,
        }
    }
    third = _token_with_coordinates(1)
    third.additional_data = {
        "monitoring": {
            "instruction_index": 1,
            "inner_index": 2,
        }
    }

    assert listener._deduplicate_creations([first, second, third]) == [
        first,
        second,
        third,
    ]
    assert listener._deduplicate_creations([first]) == []


def test_creation_dedupe_cache_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener, _ = _listener(monkeypatch, [Platform.PUMP_FUN])
    monkeypatch.setattr(block_listener_module, "MAX_RECENT_CREATIONS", 2)
    tokens = [_token_with_coordinates(index) for index in range(1, 4)]

    assert listener._deduplicate_creations(tokens) == tokens
    assert len(listener._recent_creation_order) == 2
    assert len(listener._recent_creation_keys) == 2
    assert listener._deduplicate_creations([tokens[-1]]) == []
    assert listener._deduplicate_creations([tokens[0]]) == [tokens[0]]

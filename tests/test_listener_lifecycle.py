from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

import monitoring.universal_block_listener as block_listener_module
import monitoring.universal_logs_listener as logs_listener_module
import monitoring.universal_pumpportal_listener as pumpportal_listener_module
from interfaces.core import Platform
from monitoring.base_listener import BaseTokenListener
from monitoring.listener_factory import ListenerFactory as TokenListenerFactory
from monitoring.subscription import (
    SubscriptionRejected,
    subscribe_json_rpc,
    subscribe_pumpportal,
)
from monitoring.universal_block_listener import UniversalBlockListener
from monitoring.universal_logs_listener import UniversalLogsListener
from monitoring.universal_pumpportal_listener import UniversalPumpPortalListener


class _StopListener(BaseException):
    pass


class _ConnectionContext:
    def __init__(self, websocket: object) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> object:
        return self.websocket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        return False


class _FrameWebSocket:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = deque(json.dumps(frame) for frame in frames)
        self.sent: list[str] = []
        self.receive_count = 0

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def recv(self) -> str:
        self.receive_count += 1
        return self.frames.popleft()


ListenerFactory = Callable[[], BaseTokenListener]


def _bare_logs_listener() -> BaseTokenListener:
    listener = object.__new__(UniversalLogsListener)
    BaseTokenListener.__init__(listener)
    listener.wss_endpoint = "wss://offline.invalid/logs"
    listener.platform_parsers = {"offline": object()}
    listener._pending_frames = deque(["stale-before-first-handshake"])
    listener._subscription_ids = frozenset()
    return listener


def _bare_block_listener() -> BaseTokenListener:
    listener = object.__new__(UniversalBlockListener)
    BaseTokenListener.__init__(listener)
    listener.wss_endpoint = "wss://offline.invalid/blocks"
    listener.platform_parsers = {"offline": object()}
    listener._pending_frames = deque(["stale-before-first-handshake"])
    listener._subscription_ids = frozenset()
    return listener


def _bare_pumpportal_listener() -> BaseTokenListener:
    listener = object.__new__(UniversalPumpPortalListener)
    BaseTokenListener.__init__(listener)
    listener.pumpportal_url = "wss://offline.invalid/pumpportal"
    listener._pending_frames = deque(["stale-before-first-handshake"])
    listener._subscription_ids = frozenset()
    return listener


def test_factory_rejects_letsbonk_pumpportal_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = False

    class _MustNotConstruct:
        def __init__(self, **_: object) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("unsupported PumpPortal listener was constructed")

    monkeypatch.setattr(
        pumpportal_listener_module,
        "UniversalPumpPortalListener",
        _MustNotConstruct,
    )

    with pytest.raises(
        ValueError,
        match=r"PumpPortal.*does not support.*lets_bonk",
    ):
        TokenListenerFactory.create_listener(
            "pumpportal",
            platforms=[Platform.LETS_BONK],
        )

    assert constructed is False


def test_factory_does_not_advertise_letsbonk_pumpportal_support() -> None:
    assert "pumpportal" not in TokenListenerFactory.get_platform_compatible_listeners(
        Platform.LETS_BONK
    )
    assert TokenListenerFactory.get_pumpportal_supported_platforms() == [
        Platform.PUMP_FUN
    ]


@pytest.mark.parametrize(
    "platforms",
    [
        [Platform.LETS_BONK],
        [Platform.PUMP_FUN, Platform.LETS_BONK],
    ],
)
def test_universal_pumpportal_listener_rejects_unsupported_platforms(
    platforms: list[Platform],
) -> None:
    with pytest.raises(
        ValueError,
        match=r"PumpPortal.*does not support.*lets_bonk",
    ):
        UniversalPumpPortalListener(platforms=platforms)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        {"error": {"message": "provider rejected event"}},
        {"success": False},
        {"status": "failed"},
        {"status": "ERROR"},
        {"status": False},
        {"status": {"Err": {"InstructionError": [0, "Custom"]}}},
        {"status": {"success": False}},
        {"status": {"failed": True}},
        {"status": {"rejected": "provider rejected event"}},
    ],
)
async def test_pumpportal_nested_failure_envelopes_never_reach_processors(
    failure: dict[str, Any],
) -> None:
    listener = _bare_pumpportal_listener()
    listener._pending_frames.clear()
    processor_calls: list[dict[str, Any]] = []

    class _Processor:
        platform = Platform.PUMP_FUN

        def can_process(self, token_data: dict[str, Any]) -> bool:
            processor_calls.append(token_data)
            return True

        def process_token_data(self, token_data: dict[str, Any]) -> None:
            processor_calls.append(token_data)

    listener.pool_to_processors = {"pump": [_Processor()]}
    token_data = {
        "signature": "test-signature",
        "mint": "test-mint",
        "pool": "pump",
        **failure,
    }
    listener._pending_frames.append(
        json.dumps({"method": "newToken", "params": [token_data]})
    )

    assert await listener._wait_for_token_creation(object()) is None
    assert processor_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["success", True, {"Ok": None}, {"success": True}],
)
async def test_pumpportal_success_statuses_reach_the_processor(
    status: object,
) -> None:
    listener = _bare_pumpportal_listener()
    listener._pending_frames.clear()
    processor_calls: list[str] = []

    class _Processor:
        platform = Platform.PUMP_FUN

        def can_process(self, _: dict[str, Any]) -> bool:
            processor_calls.append("can_process")
            return True

        def process_token_data(self, _: dict[str, Any]) -> None:
            processor_calls.append("process_token_data")

    listener.pool_to_processors = {"pump": [_Processor()]}
    listener._pending_frames.append(
        json.dumps(
            {
                "method": "newToken",
                "params": [
                    {
                        "signature": "1" * 64,
                        "mint": "test-mint",
                        "pool": "pump",
                        "status": status,
                    }
                ],
            }
        )
    )

    assert await listener._wait_for_token_creation(object()) is None
    assert processor_calls == ["can_process", "process_token_data"]


@pytest.mark.asyncio
async def test_pumpportal_malformed_json_is_not_swallowed() -> None:
    listener = _bare_pumpportal_listener()
    listener._pending_frames.clear()
    listener._pending_frames.append("not-json")

    with pytest.raises(SubscriptionRejected, match="invalid JSON"):
        await listener._wait_for_token_creation(object())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        ["not-an-object"],
        [{}],
        [{"signature": "1" * 64, "mint": "test-mint"}],
        [{"signature": "1" * 64, "mint": "", "pool": "pump"}],
        [{"signature": "1" * 64, "mint": "test-mint", "pool": 1}],
    ],
)
async def test_pumpportal_malformed_token_envelope_is_not_swallowed(
    params: object,
) -> None:
    listener = _bare_pumpportal_listener()
    listener._pending_frames.clear()
    listener._pending_frames.append(
        json.dumps({"method": "newToken", "params": params})
    )

    with pytest.raises(SubscriptionRejected, match="token object"):
        await listener._wait_for_token_creation(object())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "listener_factory", "subscribe_method", "endpoint"),
    [
        (
            logs_listener_module,
            _bare_logs_listener,
            "_subscribe_to_logs",
            "wss://offline.invalid/logs",
        ),
        (
            block_listener_module,
            _bare_block_listener,
            "_subscribe_to_programs",
            "wss://offline.invalid/blocks",
        ),
        (
            pumpportal_listener_module,
            _bare_pumpportal_listener,
            "_subscribe_to_new_tokens",
            "wss://offline.invalid/pumpportal",
        ),
    ],
)
async def test_listener_resets_only_after_handshake_and_owns_ping_once(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    listener_factory: ListenerFactory,
    subscribe_method: str,
    endpoint: str,
) -> None:
    listener = listener_factory()
    websocket = object()
    connect_urls: list[str] = []
    pending_at_handshake: list[tuple[str | bytes, ...]] = []
    reconnect_attempts: list[int] = []
    ping_started = 0
    ping_finished = 0
    handshake_count = 0

    def connect(url: str, **_: object) -> _ConnectionContext:
        connect_urls.append(url)
        return _ConnectionContext(websocket)

    async def subscribe(actual_websocket: object) -> None:
        nonlocal handshake_count
        assert actual_websocket is websocket
        handshake_count += 1
        pending_at_handshake.append(tuple(listener._pending_frames))
        if handshake_count < 3:
            listener._pending_frames.append(f"stale-from-failure-{handshake_count}")
            raise RuntimeError(f"handshake {handshake_count} failed")

    async def ping_loop(actual_websocket: object) -> None:
        nonlocal ping_started, ping_finished
        assert actual_websocket is websocket
        ping_started += 1
        try:
            await asyncio.Event().wait()
        finally:
            ping_finished += 1

    async def wait_for_token(actual_websocket: object) -> None:
        assert actual_websocket is websocket
        await asyncio.sleep(0)
        raise RuntimeError("subscribed stream disconnected")

    async def wait_before_reconnect(attempt: int, _: BaseException) -> None:
        reconnect_attempts.append(attempt)
        if len(reconnect_attempts) == 3:
            assert ping_finished == 1
            raise _StopListener

    monkeypatch.setattr(module.websockets, "connect", connect)
    monkeypatch.setattr(listener, subscribe_method, subscribe)
    monkeypatch.setattr(listener, "_ping_loop", ping_loop)
    monkeypatch.setattr(listener, "_wait_for_token_creation", wait_for_token)
    monkeypatch.setattr(listener, "wait_before_reconnect", wait_before_reconnect)

    async def callback(_: object) -> None:
        raise AssertionError("No token should be dispatched in this lifecycle test")

    with pytest.raises(_StopListener):
        await listener.listen_for_tokens(callback)

    assert connect_urls == [endpoint, endpoint, endpoint]
    assert pending_at_handshake == [(), (), ()]
    assert reconnect_attempts == [1, 2, 1]
    assert ping_started == 1
    assert ping_finished == 1


@pytest.mark.asyncio
async def test_json_rpc_handshake_rejects_pending_frame_overflow() -> None:
    websocket = _FrameWebSocket(
        [
            {"jsonrpc": "2.0", "method": "logsNotification", "params": {}},
            {"jsonrpc": "2.0", "method": "logsNotification", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": 42},
        ]
    )

    with pytest.raises(SubscriptionRejected, match="buffer limit of 1"):
        await subscribe_json_rpc(
            websocket,
            [{"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe"}],
            timeout=1,
            max_pending_frames=1,
        )

    assert websocket.receive_count == 2


@pytest.mark.asyncio
async def test_pumpportal_handshake_rejects_pending_frame_overflow() -> None:
    websocket = _FrameWebSocket(
        [
            {"method": "newToken", "params": [{"mint": "first"}]},
            {"method": "newToken", "params": [{"mint": "second"}]},
            {"id": 1, "success": True},
        ]
    )

    with pytest.raises(SubscriptionRejected, match="buffer limit of 1"):
        await subscribe_pumpportal(
            websocket,
            request_id=1,
            timeout=1,
            max_pending_frames=1,
        )

    assert websocket.receive_count == 2


@pytest.mark.asyncio
async def test_handshakes_validate_pending_frame_limit_before_io() -> None:
    json_rpc_websocket = _FrameWebSocket([{"jsonrpc": "2.0", "id": 1, "result": 42}])
    with pytest.raises(ValueError, match="positive integer"):
        await subscribe_json_rpc(
            json_rpc_websocket,
            [{"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe"}],
            timeout=1,
            max_pending_frames=0,
        )

    pumpportal_websocket = _FrameWebSocket([{"id": 1, "success": True}])
    with pytest.raises(ValueError, match="positive integer"):
        await subscribe_pumpportal(
            pumpportal_websocket,
            request_id=1,
            timeout=1,
            max_pending_frames=0,
        )

    assert json_rpc_websocket.sent == []
    assert json_rpc_websocket.receive_count == 0
    assert pumpportal_websocket.sent == []
    assert pumpportal_websocket.receive_count == 0

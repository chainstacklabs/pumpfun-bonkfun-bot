"""Bounded, correlated subscription handshakes for monitoring transports."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

MAX_PENDING_HANDSHAKE_FRAMES = 256


class SubscriptionError(ConnectionError):
    """Base class for observable subscription handshake failures."""


class SubscriptionTimeout(SubscriptionError, TimeoutError):
    """Raised when a provider does not acknowledge a subscription in time."""


class SubscriptionRejected(SubscriptionError):
    """Raised when a provider rejects or ambiguously acknowledges a request."""


class SubscriptionCorrelationError(SubscriptionRejected):
    """Raised when a response cannot be correlated to an outstanding request."""


@dataclass(frozen=True, slots=True)
class SubscriptionResult:
    """Confirmed provider subscription IDs plus notifications received in flight."""

    subscription_ids: frozenset[int]
    pending_frames: tuple[str | bytes, ...] = ()


def decode_json_frame(frame: str | bytes) -> dict[str, Any]:
    """Decode a provider frame and require a JSON object."""
    try:
        data = json.loads(frame)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise SubscriptionRejected("Provider returned an invalid JSON frame") from exc
    if not isinstance(data, dict):
        raise SubscriptionRejected("Provider JSON frame must be an object")
    return data


async def _receive_before(websocket: Any, deadline: float) -> str | bytes:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise SubscriptionTimeout("Subscription acknowledgement timed out")
    try:
        return await asyncio.wait_for(websocket.recv(), timeout=remaining)
    except TimeoutError as exc:
        raise SubscriptionTimeout("Subscription acknowledgement timed out") from exc


def _validate_pending_frame_limit(max_pending_frames: int) -> None:
    if (
        isinstance(max_pending_frames, bool)
        or not isinstance(max_pending_frames, int)
        or max_pending_frames < 1
    ):
        raise ValueError("Pending frame buffer limit must be a positive integer")


def _buffer_pending_frame(
    buffered: deque[str | bytes],
    frame: str | bytes,
    *,
    max_pending_frames: int,
) -> None:
    if len(buffered) >= max_pending_frames:
        raise SubscriptionRejected(
            f"Subscription pending frame buffer limit of {max_pending_frames} exceeded"
        )
    buffered.append(frame)


async def subscribe_json_rpc(
    websocket: Any,
    requests: list[dict[str, Any]],
    *,
    timeout: float,
    max_pending_frames: int = MAX_PENDING_HANDSHAKE_FRAMES,
) -> SubscriptionResult:
    """Send JSON-RPC requests and require one successful response for every ID."""
    _validate_pending_frame_limit(max_pending_frames)
    if timeout <= 0:
        raise ValueError("Subscription timeout must be positive")
    if not requests:
        raise ValueError("At least one subscription request is required")

    pending: dict[int, str] = {}
    for request in requests:
        request_id = request.get("id")
        method = request.get("method")
        if type(request_id) is not int or request_id in pending:
            raise ValueError("Subscription request IDs must be unique integers")
        if not isinstance(method, str) or not method.endswith("Subscribe"):
            raise ValueError("Subscription request must name a subscribe method")
        pending[request_id] = method
        await websocket.send(json.dumps(request))

    deadline = asyncio.get_running_loop().time() + timeout
    subscriptions: set[int] = set()
    buffered: deque[str | bytes] = deque()
    all_request_ids = frozenset(pending)

    while pending:
        frame = await _receive_before(websocket, deadline)
        data = decode_json_frame(frame)
        if "id" not in data:
            if "method" in data and "params" in data:
                _buffer_pending_frame(
                    buffered,
                    frame,
                    max_pending_frames=max_pending_frames,
                )
                continue
            raise SubscriptionCorrelationError(
                "Subscription response has no request ID"
            )
        response_id = data["id"]
        if type(response_id) is not int:
            raise SubscriptionCorrelationError(
                f"Subscription response has invalid request ID {response_id!r}"
            )
        if response_id not in pending:
            detail = "duplicate" if response_id in all_request_ids else "unknown"
            raise SubscriptionCorrelationError(
                f"Subscription response has {detail} request ID {response_id!r}"
            )
        method = pending.pop(response_id)
        if data.get("error") is not None:
            raise SubscriptionRejected(
                f"{method} request {response_id} was rejected: {data['error']!r}"
            )
        subscription_id = data.get("result")
        if type(subscription_id) is not int:
            raise SubscriptionRejected(
                f"{method} request {response_id} returned no subscription ID"
            )
        subscriptions.add(subscription_id)

    return SubscriptionResult(
        subscription_ids=frozenset(subscriptions),
        pending_frames=tuple(buffered),
    )


async def subscribe_pumpportal(
    websocket: Any,
    *,
    request_id: int,
    timeout: float,
    max_pending_frames: int = MAX_PENDING_HANDSHAKE_FRAMES,
) -> SubscriptionResult:
    """Require PumpPortal's explicit success acknowledgement for one request."""
    _validate_pending_frame_limit(max_pending_frames)
    request = {
        "id": request_id,
        "method": "subscribeNewToken",
        "params": [],
    }
    await websocket.send(json.dumps(request))
    deadline = asyncio.get_running_loop().time() + timeout
    buffered: deque[str | bytes] = deque()

    while True:
        frame = await _receive_before(websocket, deadline)
        data = decode_json_frame(frame)
        if data.get("method") == "newToken" or {
            "signature",
            "mint",
            "pool",
        }.issubset(data):
            _buffer_pending_frame(
                buffered,
                frame,
                max_pending_frames=max_pending_frames,
            )
            continue
        if "id" in data and data["id"] != request_id:
            raise SubscriptionCorrelationError(
                f"PumpPortal response has unknown request ID {data['id']!r}"
            )
        if data.get("error") is not None or data.get("success") is False:
            raise SubscriptionRejected(
                f"PumpPortal rejected token subscription: {data!r}"
            )

        message = str(data.get("message", "")).lower()
        result = data.get("result")
        acknowledged = (
            data.get("success") is True
            or result is True
            or type(result) is int
            or (
                "subscrib" in message
                and any(word in message for word in ("success", "subscribed"))
            )
        )
        if not acknowledged:
            raise SubscriptionRejected(
                f"Ambiguous PumpPortal subscription response: {data!r}"
            )
        subscription_ids = (
            frozenset({result}) if type(result) is int else frozenset({request_id})
        )
        return SubscriptionResult(
            subscription_ids=subscription_ids,
            pending_frames=tuple(buffered),
        )

"""Structured, redacting logging utilities for the trading bot."""

import json
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_CORRELATION_FIELDS = ("bot_id", "event_id", "intent_id", "signature")
_LOG_CONTEXT: ContextVar[dict[str, str]] = ContextVar("log_context", default={})
_SECRET_DIGESTS: set[bytes] = set()
_LOCK = threading.RLock()
_loggers: dict[str, logging.Logger] = {}
_SENSITIVE_FIELD_NAMES = {
    "privatekey",
    "secretkey",
    "seed",
    "seedphrase",
    "mnemonic",
}
_SENSITIVE_LABEL_RE = re.compile(
    r"(?i)\b(private[\s_-]*key|secret[\s_-]*key|seed(?:[\s_-]*phrase)?|mnemonic)"
    r"([\"']?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\\n\r])*(?:\"|(?=\n|\r|$))"
    r"|'(?:\\.|[^'\\\n\r])*(?:'|(?=\n|\r|$))"
    r"|[^\s,\"'}]+)"
)
_BASE58_TOKEN_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,128}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)


def register_secret(secret: str | bytes | bytearray) -> None:
    """Register a secret digest so its exact value is redacted from logs.

    Only a SHA-256 digest is retained; the logger never stores the secret.
    """
    if isinstance(secret, str):
        secret_bytes = secret.encode("utf-8")
    elif isinstance(secret, (bytes, bytearray)):
        secret_bytes = bytes(secret)
    else:
        raise TypeError("secret must be text or bytes")
    if not secret_bytes:
        raise ValueError("secret cannot be empty")
    with _LOCK:
        _SECRET_DIGESTS.add(sha256(secret_bytes).digest())


def _redact(value: Any) -> str:
    text = str(value)
    text = _SENSITIVE_LABEL_RE.sub(r"\1\2[REDACTED]", text)

    def redact_registered(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digest = sha256(candidate.encode("utf-8")).digest()
        with _LOCK:
            return "[REDACTED]" if digest in _SECRET_DIGESTS else candidate

    return _BASE58_TOKEN_RE.sub(redact_registered, text)


def _sanitize_argument(value: Any) -> Any:
    value_type = type(value)
    if value_type.__name__ == "Keypair" and value_type.__module__.startswith("solders"):
        return "[REDACTED]"
    if value_type.__name__ == "Wallet" and value_type.__module__ == "core.wallet":
        fingerprint = getattr(value, "public_key_fingerprint", None)
        return str(fingerprint) if fingerprint is not None else "[WALLET]"
    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[\s_-]", "", str(key)).lower()
            sanitized[key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_FIELD_NAMES
                else _sanitize_argument(item)
            )
        return sanitized
    if isinstance(value, tuple):
        return tuple(_sanitize_argument(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_argument(item) for item in value]
    return value


class _CorrelationFilter(logging.Filter):
    def __init__(self, default_bot_id: str) -> None:
        super().__init__()
        self._default_bot_id = default_bot_id

    def filter(self, record: logging.LogRecord) -> bool:
        context = _LOG_CONTEXT.get()
        defaults = {
            "bot_id": self._default_bot_id,
            "event_id": "-",
            "intent_id": "-",
            "signature": "-",
        }
        for field in _CORRELATION_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, context.get(field, defaults[field]))
        return True


class _StructuredFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        original_message = record.msg
        original_args = record.args
        try:
            record.msg = _sanitize_argument(record.msg)
            record.args = _sanitize_argument(record.args)
            message = record.getMessage()
        finally:
            record.msg = original_message
            record.args = original_args

        payload: dict[str, str] = {
            "timestamp": f"{timestamp}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "bot_id": _redact(getattr(record, "bot_id", "-")),
            "event_id": _redact(getattr(record, "event_id", "-")),
            "intent_id": _redact(getattr(record, "intent_id", "-")),
            "signature": _redact(getattr(record, "signature", "-")),
            "message": _redact(message),
        }
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return _redact(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get an idempotently configured, root-propagating logger."""
    if not isinstance(name, str) or not name:
        raise ValueError("logger name must be a non-empty string")
    with _LOCK:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        _loggers[name] = logger
        return logger


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Bind bot/event/intent/signature correlation fields for a code block."""
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"unsupported log context fields: {sorted(unknown)}")
    current = _LOG_CONTEXT.get()
    updated = {**current, **{key: str(value) for key, value in fields.items()}}
    token = _LOG_CONTEXT.set(updated)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def setup_file_logging(
    filename: str = "pump_trading.log",
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    bot_id: str | None = None,
) -> None:
    """Attach one structured, rotating file handler for ``filename``."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(backup_count, bool)
        or not isinstance(backup_count, int)
        or backup_count < 1
    ):
        raise ValueError("backup_count must be a positive integer")

    path = Path(filename).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    inferred_bot_id = re.sub(r"_\d{8}_\d{6}$", "", path.stem) or "-"
    effective_bot_id = str(bot_id) if bot_id is not None else inferred_bot_id
    root_logger = logging.getLogger()

    with _LOCK:
        matching_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == path
        ]
        all_managed_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_pumpfun_structured_handler", False)
        ]
        matching_managed_handlers = [
            handler for handler in all_managed_handlers if handler in matching_handlers
        ]
        if matching_managed_handlers:
            keeper = matching_managed_handlers[0]
            keeper.setLevel(level)
            keeper.maxBytes = max_bytes
            keeper.backupCount = backup_count
            keeper.setFormatter(_StructuredFormatter())
            for existing_filter in tuple(keeper.filters):
                if isinstance(existing_filter, _CorrelationFilter):
                    keeper.removeFilter(existing_filter)
            keeper.addFilter(_CorrelationFilter(effective_bot_id))
            for duplicate in tuple(root_logger.handlers):
                if duplicate is keeper:
                    continue
                if duplicate in matching_handlers or duplicate in all_managed_handlers:
                    root_logger.removeHandler(duplicate)
                    duplicate.close()
            if root_logger.level == logging.NOTSET or root_logger.level > level:
                root_logger.setLevel(level)
            return

        for stale_handler in tuple(root_logger.handlers):
            if (
                stale_handler in matching_handlers
                or stale_handler in all_managed_handlers
            ):
                root_logger.removeHandler(stale_handler)
                stale_handler.close()

        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        handler._pumpfun_structured_handler = True  # type: ignore[attr-defined]
        handler.setLevel(level)
        handler.setFormatter(_StructuredFormatter())
        handler.addFilter(_CorrelationFilter(effective_bot_id))
        root_logger.addHandler(handler)
        if root_logger.level == logging.NOTSET or root_logger.level > level:
            root_logger.setLevel(level)

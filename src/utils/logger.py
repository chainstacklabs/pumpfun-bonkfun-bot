"""
Logging utilities for the pump.fun trading bot.

Includes the credential redaction every entry point installs before it logs
anything — see `install_secret_redaction`.
"""

import logging
import os
import re
from urllib.parse import urlsplit

# Global dict to store loggers
_loggers: dict[str, logging.Logger] = {}

# HTTP and RPC clients that log a full request URL at INFO. Silencing them is
# the first line of defence, but only that: solana-py 0.40 swapped httpx for
# httpx2, which renamed the logger and silently reopened the leak this list was
# written to close. `install_secret_redaction` is what actually holds.
_NOISY_HTTP_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "aiohttp.client",
    "urllib3",
    "urllib3.connectionpool",
    "grpc",
    "websockets.client",
)

# Query parameters that carry a credential. Providers differ: Helius uses
# `api-key`, others `apiKey` or `token`. Matched case-insensitively.
_SECRET_QUERY_KEYS = ("api-key", "api_key", "apikey", "token", "auth", "key", "secret")

_SECRET_QUERY = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SECRET_QUERY_KEYS) + r")=[^&\s\"\']+"
)

# A credential sitting in the URL path rather than the query, which is how
# Chainstack and QuickNode endpoints are shaped. A path segment this long and
# this opaque is never a route name.
_SECRET_PATH_SEGMENT = re.compile(r"/[A-Za-z0-9_-]{20,}(?=[/?\s\"\']|$)")

# user:password@host
_URL_USERINFO = re.compile(r"(?<=//)[^/@\s]+(?=@)")

REDACTED = "***"

# Exact values pulled from the environment at install time. Pattern matching
# cannot know that a bare token is a secret; this does.
_literal_secrets: list[str] = []
_redaction_installed = False

# Environment variables whose value must never appear in a log line. The
# endpoints carry their credential inside the URL, so the whole value counts.
_SECRET_ENV_VARS = (
    "SOLANA_PRIVATE_KEY",
    "GEYSER_API_TOKEN",
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "GEYSER_ENDPOINT",
)

# Shortest value worth substring-matching. A one- or two-character variable
# would match half the alphabet and redact the whole log.
_MIN_LITERAL_SECRET_LEN = 8


def redact(text: str) -> str:
    """Strip credentials out of one string.

    Masks, in order: any exact value registered from the environment, URL
    userinfo, secret-bearing query parameters, and long opaque URL path
    segments. Safe to call on a string that holds no secret.

    Args:
        text: The string to clean, typically a log message

    Returns:
        The same string with every credential replaced by `***`
    """
    for secret in _literal_secrets:
        if secret in text:
            text = text.replace(secret, _redact_endpoint(secret))
    text = _URL_USERINFO.sub(REDACTED, text)
    text = _SECRET_QUERY.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _SECRET_PATH_SEGMENT.sub(f"/{REDACTED}", text)


def _redact_endpoint(secret: str) -> str:
    """Replace one known secret, keeping the host when it is a URL.

    A bare `***` in place of an endpoint hides which provider answered, which
    is the one thing a reader needs from that line. The host is not the
    credential, so it stays.

    Args:
        secret: The exact value read from the environment

    Returns:
        `scheme://host/***` for a URL, `***` for anything else
    """
    parts = urlsplit(secret)
    if parts.scheme and parts.hostname:
        return f"{parts.scheme}://{parts.hostname}/{REDACTED}"
    return REDACTED


def silence_http_client_loggers() -> None:
    """Raise the HTTP clients above INFO so they stop logging request URLs."""
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def install_secret_redaction() -> None:
    """Redact credentials from every log record, whoever emits it.

    Wraps the log record factory rather than filtering a logger or a handler.
    A filter only sees records that reach the object it is attached to, so it
    misses a library that logs through its own logger before the bot's handlers
    exist; the factory runs for every record in the process, including ones
    created by code this repo never imports.

    Idempotent, and the environment is read once here so a secret that lands in
    `os.environ` later is still caught by the patterns above.
    """
    global _redaction_installed

    _literal_secrets.clear()
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= _MIN_LITERAL_SECRET_LEN:
            _literal_secrets.append(value)
    # Longest first, so an endpoint is masked before a token that is a prefix
    # of it can replace half of it.
    _literal_secrets.sort(key=len, reverse=True)

    silence_http_client_loggers()

    if _redaction_installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        return _redact_record(record)

    logging.setLogRecordFactory(factory)
    _redaction_installed = True


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Mask any credential in a record, message and arguments together.

    Renders the record the way a handler eventually will, then redacts that.
    Masking `record.msg` and its string arguments separately is not enough:
    httpx2 logs the URL as a `URL` object, not a `str`, so a type check skips
    the one argument that carries the key — the first version of this guard did
    exactly that and the key still reached the terminal. Rendering first means
    the argument's type stops mattering.

    The rendered text replaces the record only when redaction actually changed
    something, so an ordinary line keeps its original message and arguments.

    Args:
        record: The record as the factory built it

    Returns:
        The same record, credentials masked
    """
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a malformed record must not break logging
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return record

    redacted = redact(message)
    if redacted != message:
        record.msg = redacted
        record.args = None
    return record


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get or create a logger with the given name.

    Args:
        name: Logger name, typically __name__
        level: Logging level

    Returns:
        Configured logger
    """
    global _loggers

    if name in _loggers:
        return _loggers[name]

    # Every module in src/ calls this at import, which makes it the one place
    # guaranteed to run before anything logs. A caller that forgets to install
    # redaction still gets it; see install_secret_redaction for why a filter on
    # a handler would not be enough.
    install_secret_redaction()

    logger = logging.getLogger(name)
    logger.setLevel(level)

    _loggers[name] = logger
    return logger


def setup_file_logging(
    filename: str = "pump_trading.log", level: int = logging.INFO
) -> None:
    """Set up file logging for all loggers.

    Args:
        filename: Log file path
        level: Logging level for file handler
    """
    # A file handler at INFO captures the HTTP clients' request URLs into
    # logs/, where they outlive the terminal. Redact before it is attached.
    install_secret_redaction()

    root_logger = logging.getLogger()

    # Check if file handler with same filename already exists
    for handler in root_logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and handler.baseFilename == filename
        ):
            return  # File handler already added

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(filename)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)

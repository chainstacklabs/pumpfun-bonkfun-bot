"""Verify no RPC credential can reach a log line.

The provider endpoints in `.env` carry their API key inside the URL, and the
HTTP clients log a full request URL at INFO. Anything that raises the root
logger to INFO therefore prints the key — `tools/cleanup_accounts.py` did,
for four runs on 2026-09-23, in a terminal and into `logs/`.

That script was already silencing `httpx` and `httpcore` by name. It leaked
anyway, because solana-py 0.40 swapped httpx for httpx2 and the rename took the
guard with it. So naming the clients is a convenience, not the control: these
checks prove the value is masked whichever logger emits it.

Offline machine checks, no network and no funds moved:

  1. `redact` masks a secret-bearing query parameter, URL userinfo, and an
     opaque path segment — the three shapes a provider credential takes.
  2. `redact` masks an exact value registered from the environment, keeping the
     host so a log line still says which provider answered.
  3. A logger this repo has never heard of, logging at INFO, comes out
     redacted — including when the URL is a non-`str` argument, which is how
     httpx2 passes it and how the first version of this guard was defeated.
     This is the check that would have caught the httpx -> httpx2 rename.
  4. Importing `core.client` installs the redaction: every module that logs
     calls `get_logger` at import, and that is where it is installed.
  5. `setup_file_logging` installs it before attaching the handler, so nothing
     reaches `logs/` unredacted.
  6. Every `logging.basicConfig` in `src/` and `tools/` installs it too, so the
     guard does not depend on import order.
  7. No script prints an endpoint environment variable raw; the hostname is the
     most any of them may show.

Usage:
    uv run tests/regression/verify_no_rpc_credentials_logged.py
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from utils.logger import (  # noqa: E402
    REDACTED,
    install_secret_redaction,
    redact,
)

SCAN_ROOTS = ("src", "tools", "cookbook")

# The variables whose values are credentials. Printing one raw is the bug.
SECRET_ENV_VARS = (
    "SOLANA_PRIVATE_KEY",
    "GEYSER_API_TOKEN",
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "GEYSER_ENDPOINT",
)

# A print or f-string that interpolates one of those names directly. Reading
# `.hostname` off it first is allowed and is what the cookbook scripts do.
RAW_ENV_IN_OUTPUT = re.compile(
    r"(?:print|logger\.\w+|logging\.\w+)\([^)]*\{("
    + "|".join(SECRET_ENV_VARS + ("RPC_ENDPOINT", "WSS_ENDPOINT"))
    + r")\}"
)


class _NotAString:
    """Stands in for httpx2's URL object: renders as a URL, is not a `str`."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


class _Capture(logging.Handler):
    """Collect formatted records so a check can read what would be printed."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def check_redact_masks_every_credential_shape() -> bool:
    """The three ways a provider puts a key in a URL."""
    cases = {
        "https://example.com/?api-key=deadbeef0123": "api-key=deadbeef0123",
        "https://example.com/?apiKey=deadbeef0123": "deadbeef0123",
        "wss://user:hunter2@example.com/stream": "user:hunter2",
        "https://example.com/abcdef0123456789abcdef/": "abcdef0123456789abcdef",
    }
    ok = True
    for raw, secret in cases.items():
        cleaned = redact(raw)
        if secret in cleaned or REDACTED not in cleaned:
            print(f"     not masked: {raw} -> {cleaned}")
            ok = False
    return ok


def check_redact_masks_a_registered_value() -> bool:
    """An exact environment value is masked, and the host survives."""
    endpoint = "https://rpc.example.com/9f8e7d6c5b4a39281706"
    os.environ["SOLANA_NODE_RPC_ENDPOINT"] = endpoint
    try:
        install_secret_redaction()
        cleaned = redact(f"connecting to {endpoint}")
    finally:
        del os.environ["SOLANA_NODE_RPC_ENDPOINT"]
        install_secret_redaction()
    if "9f8e7d6c5b4a39281706" in cleaned:
        print(f"     the registered value survived: {cleaned}")
        return False
    if "rpc.example.com" not in cleaned:
        print(f"     the host was masked too, which makes the line useless: {cleaned}")
        return False
    return True


def check_an_unknown_logger_is_redacted() -> bool:
    """The control, and the one that survives a library renaming its logger.

    Silencing clients by name is a list that goes stale. This logs the same
    line httpx2 logs, through a name nothing in this repo mentions, at INFO.
    """
    install_secret_redaction()
    logger = logging.getLogger("some.library.nobody.listed")
    logger.setLevel(logging.INFO)
    capture = _Capture()
    logger.addHandler(capture)
    try:
        logger.info(
            "HTTP Request: POST https://mainnet.example.com/?api-key=deadbeef0123 "
            '"HTTP/2 200 OK"'
        )
        logger.info("endpoint=%s", "https://mainnet.example.com/?api-key=deadbeef0123")
        # The shape that actually leaks. httpx2 passes its URL as a URL object,
        # not a str, so a guard that redacts `record.msg` and then type-checks
        # each argument skips the only one carrying the key. The first version
        # of this guard did that and the key still reached the terminal.
        logger.info(
            'HTTP Request: %s %s "%s"',
            "POST",
            _NotAString("https://mainnet.example.com/?api-key=deadbeef0123"),
            "HTTP/2 200 OK",
        )
    finally:
        logger.removeHandler(capture)
    leaked = [line for line in capture.lines if "deadbeef0123" in line]
    if leaked:
        print(f"     {len(leaked)} of {len(capture.lines)} lines kept the key")
        return False
    if not all("mainnet.example.com" in line for line in capture.lines):
        print("     the host was masked too, which makes the line useless")
        return False
    return len(capture.lines) == 3


def check_importing_src_installs_redaction() -> bool:
    """A fresh interpreter that imports the RPC client is already protected.

    `core.client` is the module that talks to the endpoint, and like every
    module that logs it calls `get_logger` at import, which installs the
    redaction. Run out of process, because this interpreter already has it.
    """
    code = (
        "import sys, logging; sys.path.insert(0, 'src');"
        " import core.client;"
        " log = logging.getLogger('third.party');"
        " log.setLevel(logging.INFO);"
        " import io;"
        " buf = io.StringIO();"
        " h = logging.StreamHandler(buf);"
        " log.addHandler(h);"
        " log.info('GET https://x.example.com/?api-key=deadbeef0123');"
        " print('LEAK' if 'deadbeef0123' in buf.getvalue() else 'CLEAN')"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if "CLEAN" not in result.stdout:
        print(f"     {result.stdout.strip() or result.stderr.strip()[:200]}")
        return False
    return True


def check_file_logging_installs_redaction() -> bool:
    """The other way a root handler appears, and the one that writes to logs/.

    A line printed to the terminal is gone when the scrollback is; a line in
    `logs/` is not, and `logs/` is what gets attached to a bug report.
    """
    source = (PROJECT_ROOT / "src" / "utils" / "logger.py").read_text()
    body = source.split("def setup_file_logging")[1]
    if "install_secret_redaction()" not in body.split("root_logger.addHandler")[0]:
        print("     setup_file_logging attaches a handler before redacting")
        return False
    return True


def check_every_basic_config_installs_redaction() -> bool:
    """`logging.basicConfig` is what turns the leak on; it must turn the guard on too."""
    offenders = []
    for root in ("src", "tools"):
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            source = path.read_text()
            if "logging.basicConfig" not in source:
                continue
            if "install_secret_redaction" not in source:
                offenders.append(path.relative_to(PROJECT_ROOT))
    for path in offenders:
        print(f"     {path} calls basicConfig without install_secret_redaction")
    return not offenders


def check_no_script_prints_a_raw_endpoint() -> bool:
    """Print the hostname if you must, never the value."""
    offenders = []
    for root in SCAN_ROOTS:
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if RAW_ENV_IN_OUTPUT.search(line):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}")
    for hit in offenders:
        print(f"     {hit} interpolates a credential straight into output")
    return not offenders


def main() -> int:
    checks = [
        (
            "redact masks every credential shape",
            check_redact_masks_every_credential_shape,
        ),
        (
            "redact masks a registered value, keeping the host",
            check_redact_masks_a_registered_value,
        ),
        ("an unlisted logger is redacted too", check_an_unknown_logger_is_redacted),
        (
            "importing the RPC client installs the redaction",
            check_importing_src_installs_redaction,
        ),
        ("file logging installs the redaction", check_file_logging_installs_redaction),
        (
            "every basicConfig installs the redaction",
            check_every_basic_config_installs_redaction,
        ),
        ("no script prints a raw endpoint", check_no_script_prints_a_raw_endpoint),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

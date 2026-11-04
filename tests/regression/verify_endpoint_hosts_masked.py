"""Verify a configured endpoint's host never reaches a log line.

Masking replaced an endpoint's whole URL but left its host, and left the host
alone wherever it appeared without the URL. Error text names exactly that:
aiohttp's `Cannot connect to host <host>:443`, gRPC's `errors resolving
<target>`. A traceback was not masked at all, so a gRPC failure printed the
whole Geyser target, token and all, from the exception's `details`.

  A. The host of every endpoint the environment holds (RPC, WSS, Geyser, also
     a bare `host:port` gRPC target) is masked with its port wherever it
     appears, and a masked URL keeps only its scheme.
  B. An endpoint a bot config names directly, not through those variables, is
     masked the same way once `bot_runner.mask_endpoints` has seen the config,
     and so is its Geyser token.
  C. A traceback logged with the record is masked too: an exception whose text
     names the endpoint or its host shows neither.
  D. Nothing that is not a configured endpoint is masked: another host, a
     longer name that contains the host, a single-label host such as
     `localhost`, and ordinary text stay as they are.
  E. gRPC's other target forms (`dns:host:port`, `dns:///host:port`,
     `dns://resolver/host:port`) mask their host, and an IPv6 host is masked
     with its brackets and port (`[2001:db8::7]:8899`).

Usage:
    uv run tests/regression/verify_endpoint_hosts_masked.py
"""

import io
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import bot_runner  # noqa: E402
from utils.logger import install_secret_redaction, redact  # noqa: E402

RPC = "https://acct-1234.solana-mainnet.example-rpc.com/k9f8e7d6c5b4a3928170"
WSS = "wss://acct-1234.solana-mainnet.example-rpc.com/k9f8e7d6c5b4a3928170"
GEYSER = "grpc.acct-1234.example-geyser.io:10000"
RPC_HOST = "acct-1234.solana-mainnet.example-rpc.com"
GEYSER_HOST = "grpc.acct-1234.example-geyser.io"


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _with_env(**values: str) -> None:
    for name in (
        "SOLANA_NODE_RPC_ENDPOINT",
        "SOLANA_NODE_WSS_ENDPOINT",
        "GEYSER_ENDPOINT",
    ):
        os.environ.pop(name, None)
    os.environ.update(values)
    install_secret_redaction()


def check_a_environment_hosts_masked() -> bool:
    problems = []
    _with_env(
        SOLANA_NODE_RPC_ENDPOINT=RPC,
        SOLANA_NODE_WSS_ENDPOINT=WSS,
        GEYSER_ENDPOINT=GEYSER,
    )
    try:
        lines = {
            f"Cannot connect to host {RPC_HOST}:443 ssl:default": "***",
            f"errors resolving {GEYSER}: Misformatted domain name": "***",
            f"Connected to Geyser endpoint: {GEYSER}": "***",
            f"POST {RPC}": "https://***",
            f"subscribing on {WSS}": "wss://***",
            f"host {RPC_HOST.upper()} refused": "***",
        }
        for line, kept in lines.items():
            cleaned = redact(line)
            leaked = [
                part
                for part in (RPC_HOST.lower(), GEYSER_HOST, "10000", "k9f8e7d6")
                if part in cleaned.lower()
            ]
            if leaked or kept not in cleaned:
                problems.append(f"{line!r} -> {cleaned!r}")
    finally:
        _with_env()
    return _report(problems)


def check_b_config_endpoints_masked() -> bool:
    problems = []
    _with_env()
    cfg = {
        "rpc_endpoint": "https://rpc.config-only.example.net/q1w2e3r4t5y6u7i8o9p0",
        "wss_endpoint": "wss://ws.config-only.example.net/q1w2e3r4t5y6u7i8o9p0",
        "geyser": {
            "endpoint": "geyser.config-only.example.net:443",
            "api_token": "tok-config-only-0123456789",
        },
    }
    bot_runner.mask_endpoints(cfg)
    for line in (
        "Cannot connect to host rpc.config-only.example.net:443",
        "Cannot connect to host ws.config-only.example.net:443",
        "errors resolving geyser.config-only.example.net:443",
        "x-token tok-config-only-0123456789",
        f"POST {cfg['rpc_endpoint']}",
    ):
        cleaned = redact(line)
        if "config-only" in cleaned or "q1w2e3" in cleaned:
            problems.append(f"{line!r} -> {cleaned!r}")
    return _report(problems)


def check_c_traceback_masked() -> bool:
    problems = []
    _with_env(GEYSER_ENDPOINT=GEYSER)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("verify.endpoint_hosts")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        details = f'details = "errors resolving {GEYSER}"; host {GEYSER_HOST}'
        try:
            raise ConnectionError(details)  # noqa: TRY301 - the traceback under test
        except ConnectionError:
            logger.exception("gRPC error")
    finally:
        logger.removeHandler(handler)
        _with_env()
    text = stream.getvalue()
    if "Traceback" not in text:
        problems.append("the traceback was not logged at all")
    if GEYSER_HOST in text or "10000" in text:
        problems.append(f"the traceback shows the endpoint: {text[-200:]!r}")
    return _report(problems)


def check_d_other_text_untouched() -> bool:
    problems = []
    _with_env(
        SOLANA_NODE_RPC_ENDPOINT=RPC,
        SOLANA_NODE_WSS_ENDPOINT="ws://localhost:8900",
    )
    bot_runner.mask_endpoints({"geyser": {"endpoint": "geyser:10000"}})
    try:
        for line in (
            "Cannot connect to host api.other-provider.example.org:443",
            f"Cannot connect to host my{RPC_HOST}:443",
            f"Cannot connect to host {RPC_HOST}.au:443",
            "Starting the geyser listener on localhost",
            "Bought 4K9ZHFh6WfBGokv7uuCCPpPUHpMHTnDxaDneWQN7pump at 2.89e-08 SOL",
            "Retrying in 1.2s (attempt 2/3)",
        ):
            cleaned = redact(line)
            if cleaned != line:
                problems.append(f"{line!r} -> {cleaned!r}")
    finally:
        _with_env()
    return _report(problems)


def check_e_target_forms_masked() -> bool:
    problems = []
    for target in (
        f"dns:{GEYSER}",
        f"dns:///{GEYSER}",
        f"dns://8.8.8.8/{GEYSER}",
    ):
        _with_env(GEYSER_ENDPOINT=target)
        try:
            line = f"errors resolving {GEYSER}: Misformatted domain name"
            cleaned = redact(line)
        finally:
            _with_env()
        if GEYSER_HOST in cleaned or "10000" in cleaned:
            problems.append(f"{target}: {line!r} -> {cleaned!r}")
    _with_env(GEYSER_ENDPOINT="[2001:db8::7]:8899")
    try:
        # Another port than the configured one: only the host pattern masks it.
        line = "Cannot connect to host [2001:db8::7]:443 ssl:default"
        cleaned = redact(line)
    finally:
        _with_env()
    if cleaned != "Cannot connect to host *** ssl:default":
        problems.append(f"IPv6: {line!r} -> {cleaned!r}")
    return _report(problems)


def main() -> int:
    """Run every check and report."""
    checks = [
        (
            "A: the hosts of the environment's endpoints are masked",
            check_a_environment_hosts_masked,
        ),
        ("B: endpoints named in a config are masked", check_b_config_endpoints_masked),
        ("C: a logged traceback is masked", check_c_traceback_masked),
        (
            "D: nothing but a configured endpoint is masked",
            check_d_other_text_untouched,
        ),
        (
            "E: gRPC target forms and IPv6 hosts are masked",
            check_e_target_forms_masked,
        ),
    ]
    passed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as e:  # noqa: BLE001 - a crash is a failed check
            print(f"    raised {type(e).__name__}: {e}")
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

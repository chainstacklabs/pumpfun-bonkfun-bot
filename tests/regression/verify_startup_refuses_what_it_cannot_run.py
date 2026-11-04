"""Verify the bot refuses at startup what it cannot run, and says so once.

`bot_runner.py --help` started every enabled bot in `bots/`; a config that failed
validation printed a traceback to stderr only and exited 0; with every bot
disabled the bot printed nothing and exited 0; a bot whose `rpc_endpoint` could
not be reached logged a traceback and kept listening, so every buy would fail;
Ctrl+C ended in a `KeyboardInterrupt` traceback.

  A. `-h` and `--help`, run from a folder holding an enabled bot, print a usage
     text naming `bots/*.yaml`, `logs/`, `trades/` and `positions/` and exit 0;
     any other argument exits 2. Neither loads a config or writes `logs/`.
  B. A config that fails validation logs one line naming the file and the
     cause, no traceback, to the console and to a file in `logs/`; the run
     exits 1.
  C. With every bot disabled the run logs one line saying how many configs it
     found and that all are disabled, and exits 1.
  D. An enabled bot whose `rpc_endpoint` refuses connections logs
     `Bot '<name>' not started: rpc_endpoint is unreachable (connection
     refused)`, no traceback, never starts its trader, names no endpoint and
     exits 1. The same check in process: a node that answers starts the
     trader and `start_bot` reports it ran.
  E. `rpc_unreachable_reason` calls a JSON reply, an error object or HTTP 429
     an answer, and names the cause of anything else in a few words: an HTTP
     4xx (asked once), a 5xx, a non-JSON body, a timeout, a refused connection,
     a URL that is not http(s).
  F. Exit status: `main` exits 0 only when `run_all_bots` reports every bot
     ran, 1 otherwise and 130 on Ctrl+C; `run_all_bots` reports failure for a
     bot not started, a separate process that exited non-zero, or a bad config
     beside a good one (which still starts).
  G. Ctrl+C on a running bot ends with the trader's shutdown line, exit 130
     and no `KeyboardInterrupt` traceback.

Offline: the bot runs against a stand-in JSON-RPC node on 127.0.0.1 and a
closed local port; no funds move.

Usage:
    uv run tests/regression/verify_startup_refuses_what_it_cannot_run.py
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import bot_runner  # noqa: E402
import core.client  # noqa: E402

RUNNER = PROJECT_ROOT / "src" / "bot_runner.py"
LOGS_CONFIG = PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml"
# Kept out of the child's environment: the bot must run on what the test gives it.
ENDPOINT_VARS = (
    "SOLANA_PRIVATE_KEY",
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "GEYSER_ENDPOINT",
    "GEYSER_API_TOKEN",
    "PUMPFUN_PASSWORD",
)


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Node(BaseHTTPRequestHandler):
    """A JSON-RPC stand-in: `behaviour` picks the reply, `hits` counts requests."""

    behaviour = "ok"
    hits = 0

    def do_POST(self) -> None:
        type(self).hits += 1
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, body = {
            "ok": (200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"})),
            "unhealthy": (
                200,
                json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}),
            ),
            "429": (429, "{}"),
            "401": (401, "unauthorized"),
            "500": (500, "oops"),
            "html": (200, "<html>not a node</html>"),
        }.get(self.behaviour, (200, "{}"))
        if self.behaviour == "slow":
            time.sleep(2)
        # A client that gave up on a slow reply has closed the socket.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

    def log_message(self, *_args: object) -> None:
        return None


@contextlib.contextmanager
def _node(behaviour: str = "ok") -> Iterator[str]:
    handler = type("Node", (_Node,), {"behaviour": behaviour, "hits": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def _config(name: str, rpc: str, **overrides: object) -> dict:
    """The committed logs config, enabled, in-process, on the given endpoint."""
    with LOGS_CONFIG.open() as f:
        config = yaml.safe_load(f)
    config.pop("geyser", None)
    config.pop("env_file", None)
    config.update(
        name=name,
        enabled=True,
        separate_process=False,
        rpc_endpoint=rpc,
        wss_endpoint=f"ws://127.0.0.1:{_closed_port()}",
        private_key=base58.b58encode(bytes(Keypair())).decode(),
    )
    config["filters"]["max_token_age"] = 5
    config.update(overrides)
    return config


def _folder(tmp: str, configs: dict[str, dict]) -> Path:
    folder = Path(tempfile.mkdtemp(dir=tmp))
    (folder / "bots").mkdir()
    for stem, config in configs.items():
        (folder / "bots" / f"{stem}.yaml").write_text(yaml.safe_dump(config))
    return folder


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ENDPOINT_VARS}


def _run(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(RUNNER), *args],
        cwd=folder,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _logs(folder: Path) -> str:
    return "".join(p.read_text() for p in sorted((folder / "logs").glob("*.log")))


def check_a_help_starts_nothing(tmp: str) -> bool:
    problems = []
    folder = _folder(tmp, {"live": _config("live", "http://127.0.0.1:1")})
    for args in (["-h"], ["--help"]):
        run = _run(folder, *args)
        if run.returncode != 0:
            problems.append(f"{args}: exit {run.returncode}, expected 0")
        problems += [
            f"{args}: usage does not mention {word}"
            for word in ("bots/*.yaml", "logs/", "trades/", "positions/")
            if word not in run.stdout
        ]
    for args in (["--config", "bots/live.yaml"], ["start"]):
        run = _run(folder, *args)
        if run.returncode != 2:  # noqa: PLR2004 - argparse's usage error
            problems.append(f"{args}: exit {run.returncode}, expected 2")
    if (folder / "logs").exists():
        problems.append("a bot started: logs/ was written")
    return _report(problems)


def check_b_config_errors_are_one_line(tmp: str) -> bool:
    problems = []
    rpc = "http://127.0.0.1:1"
    cases = {
        "logsll": (
            _config("logsll", rpc, platform="launchlab"),
            "Listener type 'logs' is not compatible with platform 'launchlab'",
        ),
        "efmmig": (
            _config(
                "efmmig",
                rpc,
                trade={
                    **_config("x", rpc)["trade"],
                    "extreme_fast_mode": True,
                    "entry": "migration",
                },
            ),
            "trade.extreme_fast_mode does not apply to trade.entry: migration",
        ),
    }
    for stem, (config, cause) in cases.items():
        folder = _folder(tmp, {stem: config})
        run = _run(folder)
        out = run.stdout + run.stderr
        want = f"Could not load bots/{stem}.yaml: {cause}"
        lines = [line for line in out.splitlines() if want in line]
        if len(lines) != 1:
            problems.append(f"{stem}: {len(lines)} console lines with {want!r}")
        if "Traceback" in out:
            problems.append(f"{stem}: a traceback was printed")
        if want not in _logs(folder):
            problems.append(f"{stem}: the line is not in logs/")
        if run.returncode != 1:
            problems.append(f"{stem}: exit {run.returncode}, expected 1")
    return _report(problems)


def check_c_no_enabled_bot_says_so(tmp: str) -> bool:
    problems = []
    rpc = "http://127.0.0.1:1"
    folder = _folder(
        tmp,
        {
            "one": _config("one", rpc, enabled=False),
            "two": _config("two", rpc, enabled=False),
        },
    )
    run = _run(folder)
    out = (run.stdout + run.stderr).strip()
    if "No bot to run in bots/: 2 found, 2 disabled" not in out:
        problems.append(f"no line saying every bot is disabled: {out[-200:]!r}")
    elif len(out.splitlines()) != 1:
        problems.append(f"{len(out.splitlines())} lines, expected one: {out!r}")
    if run.returncode != 1:
        problems.append(f"exit {run.returncode}, expected 1")
    return _report(problems)


class _Trader:
    built: list = []  # noqa: RUF012

    def __init__(self, settings: object) -> None:
        _Trader.built.append(settings)

    async def start(self) -> None:
        return None


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _start_in_process(folder: Path, stem: str) -> tuple[bool, list, list]:
    """The real start_bot with the trader stubbed: result, traders, error lines."""
    original = bot_runner.UniversalTrader
    bot_runner.UniversalTrader = _Trader
    _Trader.built = []
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    cwd = Path.cwd()
    os.chdir(folder)
    try:
        ran = asyncio.run(bot_runner.start_bot(f"bots/{stem}.yaml"))
    finally:
        os.chdir(cwd)
        logging.getLogger().removeHandler(capture)
        bot_runner.UniversalTrader = original
    errors = [r for r in capture.records if r.levelno >= logging.ERROR]
    return ran, _Trader.built, errors


def check_d_unreachable_rpc_refused(tmp: str) -> bool:
    problems = []
    port = _closed_port()
    folder = _folder(tmp, {"badrpc": _config("badrpc", f"http://127.0.0.1:{port}")})
    run = _run(folder)
    out = run.stdout + run.stderr + _logs(folder)
    want = "Bot 'badrpc' not started: rpc_endpoint is unreachable (connection refused)"
    if want not in out:
        problems.append(f"no refusal line: {out[-300:]!r}")
    if "Traceback" in out:
        problems.append("a traceback was printed")
    if "Starting Universal Trader" in out:
        problems.append("the trader started anyway")
    if f"127.0.0.1:{port}" in out:
        problems.append("the endpoint was printed")
    if run.returncode != 1:
        problems.append(f"exit {run.returncode}, expected 1")

    ran, built, errors = _start_in_process(folder, "badrpc")
    if ran or built or len(errors) != 1 or errors[0].exc_info:
        problems.append(
            f"in process, refused node: ran={ran}, traders={len(built)}, "
            f"errors={[r.getMessage() for r in errors]}"
        )
    with _node() as (rpc, _):
        folder = _folder(tmp, {"good": _config("good", rpc)})
        ran, built, errors = _start_in_process(folder, "good")
    if not ran or len(built) != 1 or errors:
        problems.append(
            f"in process, answering node: ran={ran}, traders={len(built)}, "
            f"errors={[r.getMessage() for r in errors]}"
        )
    return _report(problems)


def check_e_probe_names_the_cause(_tmp: str) -> bool:
    problems = []

    def probe(endpoint: str, **kw: object) -> str | None:
        return asyncio.run(core.client.rpc_unreachable_reason(endpoint, **kw))

    for behaviour, want, hits in (
        ("ok", None, 1),
        ("unhealthy", None, 1),
        ("429", None, 1),
        ("401", "HTTP 401", 1),
        ("500", "HTTP 500", 2),
        ("html", "the answer is not JSON", 2),
        ("slow", "no answer within 0.5 s", None),
    ):
        with _node(behaviour) as (rpc, node):
            got = probe(rpc, attempts=2, timeout=0.5)
        if got != want:
            problems.append(f"{behaviour}: {got!r}, expected {want!r}")
        if hits is not None and node.hits != hits:
            problems.append(f"{behaviour}: {node.hits} requests, expected {hits}")
    for endpoint, want in (
        (f"http://127.0.0.1:{_closed_port()}", "connection refused"),
        ("127.0.0.1:8899", "not an http(s) URL"),
        ("ftp://127.0.0.1:8899", "not an http(s) URL"),
    ):
        got = probe(endpoint, attempts=1)
        if got != want:
            problems.append(f"{endpoint}: {got!r}, expected {want!r}")
    return _report(problems)


def _main_exit(outcome: object) -> object:
    """`main`'s exit status when `run_all_bots` returns or raises `outcome`."""

    def run_all() -> bool:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    original = bot_runner.run_all_bots
    bot_runner.run_all_bots = run_all
    try:
        bot_runner.main([])
    except SystemExit as e:
        return e.code
    finally:
        bot_runner.run_all_bots = original
    return "returned"


def _run_all_bots(
    tmp: str, configs: dict[str, dict], exits: dict[str, int], refused: set[str]
) -> tuple[bool, list[str]]:
    """`run_all_bots` with bots stubbed: its result and the bots it started.

    `exits` is a separate process's exit status by bot; a bot in `refused` is
    one `start_bot` did not start.
    """
    started: list[str] = []

    class _Process:
        def __init__(self, target: object, args: tuple, name: str) -> None:
            del target
            self.name = name
            self.stem = Path(args[0]).stem
            self.exitcode = exits.get(self.stem, 0)

        def start(self) -> None:
            started.append(self.stem)

        def join(self) -> None:
            return None

    async def start_bot(config_path: str, _positions: str | None = None) -> bool:
        started.append(Path(config_path).stem)
        return Path(config_path).stem not in refused

    originals = (bot_runner.start_bot, bot_runner.multiprocessing)
    cwd = Path.cwd()
    try:
        bot_runner.start_bot = start_bot
        bot_runner.multiprocessing = SimpleNamespace(Process=_Process)
        os.chdir(_folder(tmp, configs))
        return bot_runner.run_all_bots(), started
    finally:
        os.chdir(cwd)
        bot_runner.start_bot, bot_runner.multiprocessing = originals


def check_f_exit_status(tmp: str) -> bool:
    problems = []
    for outcome, want in ((True, 0), (False, 1), (KeyboardInterrupt(), 130)):
        got = _main_exit(outcome)
        if got != want:
            problems.append(f"main, run_all_bots -> {outcome!r}: {got}, want {want}")

    rpc = "http://127.0.0.1:1"
    good = _config("a", rpc)
    cases = (
        ("all ran", {"a": good, "b": _config("b", rpc)}, {}, set(), True),
        ("one not started", {"a": good}, {}, {"a"}, False),
        (
            "a process exited 1",
            {"a": _config("a", rpc, separate_process=True)},
            {"a": 1},
            set(),
            False,
        ),
        (
            "a bad config beside a good one",
            {"a": good, "b": _config("b", rpc, platform="launchlab")},
            {},
            set(),
            False,
        ),
    )
    for label, configs, exits, refused, want in cases:
        got, started = _run_all_bots(tmp, configs, exits, refused)
        if got is not want:
            problems.append(f"run_all_bots, {label}: {got!r}, expected {want}")
        if "a" not in started:
            problems.append(f"run_all_bots, {label}: bot a was not started")
    return _report(problems)


def check_g_ctrl_c_is_clean(tmp: str) -> bool:
    problems = []
    with _node() as (rpc, _):
        folder = _folder(tmp, {"live": _config("live", rpc)})
        bot = subprocess.Popen(  # noqa: S603
            [sys.executable, str(RUNNER)],
            cwd=folder,
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and bot.poll() is None:
            if "Running in single token mode" in _logs(folder):
                break
            time.sleep(0.2)
        running = bot.poll() is None
        if running:
            bot.send_signal(signal.SIGINT)
        try:
            out, _ = bot.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            bot.kill()
            out, _ = bot.communicate()
            problems.append("the bot did not stop within 60 s of Ctrl+C")
    if not running:
        problems.append(f"the bot never reached its listener: {out[-300:]!r}")
    if "KeyboardInterrupt" in out:
        problems.append("a KeyboardInterrupt traceback was printed")
    if "Universal Trader has shut down" not in out + _logs(folder):
        problems.append("no clean shutdown line")
    if bot.returncode != 130:  # noqa: PLR2004 - the status after SIGINT
        problems.append(f"exit {bot.returncode}, expected 130")
    return _report(problems)


def main() -> int:
    """Run every check and report."""
    checks = [
        (
            "A: help starts nothing; another argument is a usage error",
            check_a_help_starts_nothing,
        ),
        (
            "B: a config error is one line, also in logs/, exit 1",
            check_b_config_errors_are_one_line,
        ),
        (
            "C: no enabled bot says so in one line, exit 1",
            check_c_no_enabled_bot_says_so,
        ),
        (
            "D: an unreachable rpc_endpoint stops the bot before it listens",
            check_d_unreachable_rpc_refused,
        ),
        (
            "E: the RPC probe tells an answer from a failure and names it",
            check_e_probe_names_the_cause,
        ),
        ("F: exit status 0 only when every enabled bot ran", check_f_exit_status),
        ("G: Ctrl+C ends cleanly with exit 130", check_g_ctrl_c_is_clean),
    ]
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for label, check in checks:
            try:
                ok = check(tmp)
            except Exception as e:  # noqa: BLE001 - a crash is a failed check
                print(f"    raised {type(e).__name__}: {e}")
                ok = False
            print(f"{'PASS' if ok else 'FAIL'} {label}")
            passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

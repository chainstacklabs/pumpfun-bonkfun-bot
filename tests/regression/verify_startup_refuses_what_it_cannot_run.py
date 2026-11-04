"""Verify the bot refuses at startup what it cannot run, and says so once.

`bot_runner.py --help` started every enabled bot in `bots/`; a config that failed
validation printed a traceback to stderr only and exited 0; with every bot
disabled the bot printed nothing and exited 0; a bot whose `rpc_endpoint` could
not be reached logged a traceback and kept listening, so every buy would fail;
Ctrl+C ended in a `KeyboardInterrupt` traceback; a trader failing in its own
process printed its traceback raw, endpoint host and all, and a trader that
stopped on an error still exited 0.

  A. `-h` and `--help`, run from a folder holding an enabled bot, print a usage
     text naming `bots/*.yaml`, `logs/`, `trades/` and `positions/` and exit 0;
     any other argument exits 2. Neither loads a config or writes `logs/`.
  B. A config that fails validation logs one line naming the file and the
     cause, no traceback, to the console and to a file in `logs/`; the run
     exits 1.
  C. With every bot disabled the run logs one line saying how many configs it
     found and that all are disabled, and exits 1. A config whose YAML says
     `enabled: false` is skipped unread, so one that could not load writes no
     log.
  D. An enabled bot whose `rpc_endpoint` refuses connections logs
     `Bot '<name>' not started: rpc_endpoint is unreachable (connection
     refused)`, no traceback, never starts its trader, names no endpoint and
     exits 1. The same check in process: a node that answers starts the
     trader and `start_bot` reports it ran.
  E. `rpc_endpoint_problem` calls any JSON-RPC envelope an answer whatever
     the HTTP status (a 503 from a node that is behind, a 400), and HTTP 429
     too; HTTP 401 and 403 are `rejected the request`, asked once; anything
     else is `is unreachable (<cause>)` in a few words: a 5xx or non-JSON body,
     a timeout, a refused connection, too many redirects, a reply that is not
     HTTP, an invalid host name, a URL that is not http(s). No reason holds
     the URL.
  F. Exit status: `main` exits 0 only when `run_all_bots` reports every bot
     ran, 1 otherwise and 130 on Ctrl+C; `run_all_bots` reports failure for a
     bot not started, a separate process that exited non-zero, or a bad config
     beside a good one (which still starts), and success beside a disabled
     config that could not load; `enabled: ${VAR}` is still read through the
     environment.
  G. Ctrl+C on a running bot, in the main process and in its own, sent to the
     whole process group as a terminal does, ends with the trader's shutdown
     line, exit 130 and no `KeyboardInterrupt` traceback.
  H. A trader that raises an error naming its host, the host written only in
     the YAML, logs it once, masked, in the main process and in its own; the
     host reaches neither the console nor `logs/`, and the run exits 1.
  I. A real trader that stops on an error (`Trading stopped due to error`, or
     `Token listening stopped due to error` in yolo mode) makes `start_bot`
     report the bot did not run normally; one whose listener just ends does
     not.
  J. SIGTERM to the process group takes the same path as Ctrl+C, in the main
     process and in its own, also when the bot was started with SIGINT
     ignored, as a shell starts a background job: the trader's shutdown line,
     no `KeyboardInterrupt` traceback, exit 143.

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
from collections.abc import Callable, Iterator
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
from trading import universal_trader  # noqa: E402

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
        behind = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})
        status, body = {
            "ok": (200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"})),
            "unhealthy": (200, behind),
            "503rpc": (503, behind),
            "400rpc": (400, behind),
            "429": (429, "{}"),
            "401": (401, "unauthorized"),
            "403": (403, json.dumps({"jsonrpc": "2.0", "error": {"code": -1}})),
            "500": (500, "oops"),
            "404gateway": (404, json.dumps({"error": "Not Found"})),
            "html": (200, "<html>not a node</html>"),
            "redirect": (307, ""),
        }.get(self.behaviour, (200, "{}"))
        if self.behaviour == "slow":
            time.sleep(2)
        if self.behaviour == "garbage":
            self.close_connection = True
            self.wfile.write(b"NOT HTTP AT ALL\r\n\r\n")
            return
        # A client that gave up on a slow reply has closed the socket.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if self.behaviour == "redirect":
                self.send_header("Location", self.path)
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
            "broken": _config(
                "broken",
                rpc,
                enabled=False,
                platform="launchlab",
                private_key="${T1_NEVER_SET}",
            ),
        },
    )
    run = _run(folder)
    out = (run.stdout + run.stderr).strip()
    if (folder / "logs").exists():
        problems.append("a disabled config was loaded: logs/ was written")
    if "No bot to run in bots/: 3 found, 3 disabled" not in out:
        problems.append(f"no line saying every bot is disabled: {out[-200:]!r}")
    elif len(out.splitlines()) != 1:
        problems.append(f"{len(out.splitlines())} lines, expected one: {out!r}")
    if run.returncode != 1:
        problems.append(f"exit {run.returncode}, expected 1")
    return _report(problems)


class _Trader:
    built: list = []  # noqa: RUF012
    stopped_on_error = False

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


def _start_in_process(
    folder: Path, stem: str, trader: Callable = _Trader
) -> tuple[bool, list, list]:
    """The real start_bot with the trader stubbed: result, traders, error lines."""
    original = bot_runner.UniversalTrader
    bot_runner.UniversalTrader = trader
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
        return asyncio.run(core.client.rpc_endpoint_problem(endpoint, **kw))

    for behaviour, want, hits in (
        ("ok", None, 1),
        ("unhealthy", None, 1),
        ("503rpc", None, 1),
        ("400rpc", None, 1),
        ("429", None, 1),
        ("401", "rejected the request (HTTP 401)", 1),
        ("403", "rejected the request (HTTP 403)", 1),
        ("500", "is unreachable (HTTP 500)", 2),
        ("404gateway", "is unreachable (HTTP 404)", 2),
        ("html", "is unreachable (the answer is not JSON-RPC)", 2),
        ("redirect", "is unreachable (too many redirects)", None),
        ("garbage", "is unreachable (the answer is not HTTP)", 2),
        ("slow", "is unreachable (no answer within 0.5 s)", None),
    ):
        with _node(behaviour) as (rpc, node):
            got = probe(rpc, attempts=2, timeout=0.5)
        if got != want:
            problems.append(f"{behaviour}: {got!r}, expected {want!r}")
        if hits is not None and node.hits != hits:
            problems.append(f"{behaviour}: {node.hits} requests, expected {hits}")
        if got and "127.0.0.1" in got:
            problems.append(f"{behaviour}: the reason names the URL: {got!r}")
    long_label = "a" * 64
    for endpoint, want in (
        (f"http://127.0.0.1:{_closed_port()}", "is unreachable (connection refused)"),
        ("127.0.0.1:8899", "is unreachable (not an http(s) URL)"),
        ("ftp://127.0.0.1:8899", "is unreachable (not an http(s) URL)"),
        (f"http://{long_label}.example.com/", "is unreachable (invalid host name)"),
        ("http://a..example.com/", "is unreachable (invalid host name)"),
    ):
        got = probe(endpoint, attempts=1)
        if got != want:
            problems.append(f"{endpoint[:40]}: {got!r}, expected {want!r}")
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
) -> tuple[bool, list[str], Path]:
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
        folder = _folder(tmp, configs)
        os.chdir(folder)
        return bot_runner.run_all_bots(), started, folder
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
        (
            "a disabled config that cannot load beside a good one",
            {"a": good, "b": _config("b", rpc, enabled=False, platform="launchlab")},
            {},
            set(),
            True,
        ),
        (
            "enabled: ${VAR} set to false, beside a good one",
            {"a": good, "b": _config("b", rpc, enabled="${T1_BOT_ON}")},
            {},
            set(),
            True,
        ),
        (
            "enabled: ${VAR} that is not set, beside a good one",
            {"a": good, "b": _config("b", rpc, enabled="${T1_NEVER_SET}")},
            {},
            set(),
            False,
        ),
    )
    os.environ["T1_BOT_ON"] = "false"
    try:
        for label, configs, exits, refused, want in cases:
            got, started, folder = _run_all_bots(tmp, configs, exits, refused)
            if got is not want:
                problems.append(f"run_all_bots, {label}: {got!r}, expected {want}")
            if "a" not in started:
                problems.append(f"run_all_bots, {label}: bot a was not started")
            enabled = configs.get("b", {}).get("enabled") is True
            if "b" in started and not enabled:
                problems.append(f"run_all_bots, {label}: the disabled bot started")
            if want and (folder / "logs").exists():
                problems.append(f"run_all_bots, {label}: logs/ was written")
    finally:
        os.environ.pop("T1_BOT_ON", None)
    return _report(problems)


# Runs the runner with SIGINT ignored, as a background job starts.
IGNORING_SIGINT = """
import runpy, signal, sys
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _ctrl_c(
    tmp: str,
    *,
    separate_process: bool,
    sig: signal.Signals = signal.SIGINT,
    want: int = 130,
    sigint_ignored: bool = False,
) -> list[str]:
    """Ctrl+C (or `sig`) a running bot through its process group; what went wrong."""
    problems = []
    with _node() as (rpc, _):
        config = _config("live", rpc, separate_process=separate_process)
        folder = _folder(tmp, {"live": config})
        # A shell starts a background job (`&`) with SIGINT ignored.
        command = (
            [sys.executable, "-c", IGNORING_SIGINT, str(RUNNER)]
            if sigint_ignored
            else [sys.executable, str(RUNNER)]
        )
        bot = subprocess.Popen(  # noqa: S603
            command,
            cwd=folder,
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and bot.poll() is None:
            if "Running in single token mode" in _logs(folder):
                break
            time.sleep(0.2)
        running = bot.poll() is None
        if running:
            os.killpg(bot.pid, sig)
        try:
            out, _ = bot.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(bot.pid, signal.SIGKILL)
            out, _ = bot.communicate()
            problems.append(f"the bot did not stop within 60 s of {sig.name}")
    if not running:
        problems.append(f"the bot never reached its listener: {out[-300:]!r}")
    if "KeyboardInterrupt" in out:
        problems.append("a KeyboardInterrupt traceback was printed")
    if "Universal Trader has shut down" not in out + _logs(folder):
        problems.append("no clean shutdown line")
    if bot.returncode != want:
        problems.append(f"exit {bot.returncode}, expected {want}")
    return problems


def check_g_ctrl_c_is_clean(tmp: str) -> bool:
    problems = []
    for separate_process in (False, True):
        mode = "own process" if separate_process else "main process"
        problems += [
            f"{mode}: {p}" for p in _ctrl_c(tmp, separate_process=separate_process)
        ]
    return _report(problems)


def check_j_sigterm_is_clean(tmp: str) -> bool:
    problems = []
    for separate_process in (False, True):
        mode = "own process" if separate_process else "main process"
        for ignored in (False, True):
            found = _ctrl_c(
                tmp,
                separate_process=separate_process,
                sig=signal.SIGTERM,
                want=143,
                sigint_ignored=ignored,
            )
            label = f"{mode}{', SIGINT ignored' if ignored else ''}"
            problems += [f"{label}: {p}" for p in found]
    return _report(problems)


# Runs `main` with a trader that fails naming the configured host. Forked, so a
# separate process inherits the stand-ins.
FAILING_TRADER = """
import multiprocessing, sys
sys.path.insert(0, sys.argv[1])
multiprocessing.set_start_method("fork")
import aiohttp
import bot_runner

class Trader:
    stopped_on_error = False

    def __init__(self, settings):
        self.host = settings.rpc_endpoint.split("/")[2]

    async def start(self):
        raise aiohttp.ClientConnectionError(
            f"Cannot connect to host {self.host}:443 ssl:default"
        )

async def node_answers(_endpoint):
    return None

bot_runner.UniversalTrader = Trader
bot_runner.rpc_endpoint_problem = node_answers
bot_runner.main([])
"""


def check_h_trader_error_is_masked(tmp: str) -> bool:
    problems = []
    host = "acct-77.yaml-only.example.net"
    for separate_process in (False, True):
        mode = "own process" if separate_process else "main process"
        config = _config(
            "failing",
            f"https://{host}/q1w2e3r4t5y6u7i8o9p0",
            separate_process=separate_process,
        )
        folder = _folder(tmp, {"failing": config})
        run = subprocess.run(  # noqa: S603
            [sys.executable, "-c", FAILING_TRADER, str(PROJECT_ROOT / "src")],
            cwd=folder,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        console = run.stdout + run.stderr
        if "yaml-only" in console + _logs(folder):
            problems.append(f"{mode}: the host was printed")
        logged = console.count("ClientConnectionError: Cannot connect to host")
        if logged != 1:
            problems.append(f"{mode}: the error was logged {logged} times, expected 1")
        if run.returncode != 1:
            problems.append(f"{mode}: exit {run.returncode}, expected 1")
    return _report(problems)


class _Client:
    rpc_endpoint = ""

    async def get_health(self) -> str:
        return "ok"

    async def close(self) -> None:
        return None


class _Listener:
    """A listener that ends at once, with an error or without one."""

    def __init__(self, *, fails: bool) -> None:
        self.fails = fails

    async def listen_for_tokens(self, *_args: object, **_kwargs: object) -> None:
        if self.fails:
            raise ConnectionError("the stream ended")  # noqa: TRY003


def _real_trader(*, listener_fails: bool, resume_fails: bool) -> Callable:
    """Builds a real UniversalTrader on stand-in RPC, listener and cleanup."""

    def build(settings: object) -> object:
        originals = (
            universal_trader.SolanaClient,
            universal_trader.ListenerFactory.create_listener,
        )
        universal_trader.SolanaClient = lambda *_a, **_k: _Client()
        universal_trader.ListenerFactory.create_listener = staticmethod(
            lambda **_k: _Listener(fails=listener_fails)
        )
        try:
            trader = universal_trader.UniversalTrader(settings)
        finally:
            universal_trader.SolanaClient = originals[0]
            universal_trader.ListenerFactory.create_listener = staticmethod(
                originals[1]
            )

        async def nothing() -> None:
            return None

        async def unreadable() -> list:
            raise RuntimeError("positions file unreadable")  # noqa: TRY003

        trader._resolve_quote_token_programs = nothing  # noqa: SLF001
        trader._cleanup_resources = nothing  # noqa: SLF001
        if resume_fails:
            trader.positions.resume = unreadable
        return trader

    return build


def check_i_trader_error_stop_fails_the_run(tmp: str) -> bool:
    problems = []
    with _node() as (rpc, _):
        config = _config("yolo", rpc)
        config["filters"]["yolo_mode"] = True
        folder = _folder(tmp, {"yolo": config})
        for label, listener_fails, resume_fails, want in (
            ("Trading stopped due to error", False, True, False),
            ("Token listening stopped due to error", True, False, False),
            ("the listener just ended", False, False, True),
        ):
            trader = _real_trader(
                listener_fails=listener_fails, resume_fails=resume_fails
            )
            ran, _, _ = _start_in_process(folder, "yolo", trader)
            if ran is not want:
                problems.append(f"{label}: start_bot -> {ran!r}, expected {want}")
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
        (
            "H: a trader's error is logged once, masked, in either mode",
            check_h_trader_error_is_masked,
        ),
        (
            "I: a trader stopped by an error fails the run",
            check_i_trader_error_stop_fails_the_run,
        ),
        ("J: SIGTERM stops like Ctrl+C, exit 143", check_j_sigterm_is_clean),
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

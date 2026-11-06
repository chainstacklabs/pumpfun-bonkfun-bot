"""Verify the terminal shows short coloured lines, and the log file keeps every line.

Every console line was the log file's line: the date, the module path and the
level before the message, a 44-character mint or an 88-character signature in
it, all one colour. Most lines wrapped, and a burst of coins hid the buy.

  A. On a terminal a line is `HH:MM:SS  <bot>  <message>`, every bot's message
     starting in the same column: an address is cut to its ends, the SOL mint
     is named, an information line is cut to the window counting a wide
     character (an emoji) as two columns, and a warning or an error is never cut.
  B. By default only events print; `--verbose` prints the detail too. A warning
     prints either way.
  C. Off a terminal every line prints in the log file's format, and `NO_COLOR`
     keeps the short layout without escape codes.
  D. A registered secret is masked on the terminal too, in the message and in a
     traceback.
  E. Each event the colour rules key on is still logged by `src/` in those
     words, so rewording one cannot quietly drop it from the default view.
  F. `pump_bot` prints through it: under a pseudo-terminal its error line has
     the short layout, piped it has the log file's, and `--verbose` parses
     while an unknown flag still exits 2.

Usage:
    uv run tests/regression/verify_console_output.py
"""

import contextlib
import io
import logging
import os
import pty
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _report  # noqa: E402

import bot_runner  # noqa: E402
from utils import console  # noqa: E402
from utils.logger import (  # noqa: E402
    file_handler,
    install_secret_redaction,
    register_secret,
)

MINT = "DGAify7rbk3mcDgkfrHYR6vqS8VbFwWK7uGr3CNMpump"
SOL = "So11111111111111111111111111111111111111112"
WIDTH = 60
USAGE_ERROR = 2
ESC = re.compile(r"\033\[[\d;]*m")

# An event and a detail line of each phase, as `src/` logs them.
EVENTS = [
    "New token detected: JACOBITA (JACOBITA) on pump_fun",
    "New token detected pre-execution: JACOBITA (JACOBITA) on pump_fun",
    "Migration submitted (pre-execution): 4tuYSsHzdPQdfXQRkc8nNeMxSr1piAmrEq3anzrsKGRN -> pool x",
    "Migration done: 4tuYSsHzdPQdfXQRkc8nNeMxSr1piAmrEq3anzrsKGRN -> pool x on pump_fun",
    f"Buying 0.002000 of quote {SOL} worth of JACOBITA on pump_fun...",
    "Successfully bought JACOBITA on pump_fun",
    "Exit condition met for JACOBITA: stop_loss",
    "Successfully exited position: stop_loss (JACOBITA)",
    "Successfully sold GOD (venue: PumpSwap)",
    "Final PnL: -85.03% (-0.001757 SOL from the chain, network fees included)",
    "Skipping token Cow - too old (6.0s since detected > max_token_age 5s)",
    "Token does not match filter 'zz'. Skipping...",
]
DETAIL = [
    f"Queued new token: JACOBITA ({MINT}) on pump_fun",
    "Buying 11899.261600 tokens at 1.68078e-07 SOL per token on pump_fun",
    "Buy transaction confirmed: 27QRseLbABJUXTuDFAnEuEP6o1k4UnFG6NXK9ULk6o3RVcKpSprP3M2yq4v3tvv57hPrfofE46wHLoRZi7VqJjqL",
    "Selling 11899.2616 tokens on pump_fun",
    "Take profit target: 1.93295e-07 SOL",
    "Position PnL: -82.85% (-0.001657 SOL)",
    "Priority fee in microlamports: 200000",
    "Entry: new_launch",
    "Max open positions: 1",
]
# The words each event's colour rule keys on, as they appear in `src/`.
EVENT_WORDS = [
    'f"New token detected{stage}: ',
    'f"Migration {stage}: ',
    '"submitted (pre-execution)" if pool.pre_execution else "done"',
    "of quote",
    "Successfully bought",
    "Exit condition met for",
    "Successfully exited position:",
    "Successfully sold",
    "Final PnL:",
    "Skipping token",
    "Token does not match filter",
]


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _record(
    message: str, level: int = logging.INFO, name: str = "trading.x"
) -> logging.LogRecord:
    return logging.getLogger(name).makeRecord(name, level, "", 0, message, None, None)


def _lines(handler: logging.Handler, records: list[logging.LogRecord]) -> list[str]:
    stream = handler.stream
    for record in records:
        handler.handle(record)
    return stream.getvalue().splitlines()


def check_a_layout() -> bool:
    problems = []
    console.set_bot_name("sniper")
    fmt = console.ConsoleFormatter(colour=False, width=WIDTH)
    line = fmt.format(_record(f"Queued new token: JACOBITA ({MINT}) on pump_fun"))
    if not re.match(r"^\d\d:\d\d:\d\d  sniper {6}Queued", line):
        problems.append(f"layout is not 'HH:MM:SS  bot  message': {line!r}")
    whole = console.ConsoleFormatter(colour=False, width=200).format(
        _record(f"Queued new token: JACOBITA ({MINT}) on pump_fun")
    )
    if MINT in whole or "DGAify…pump" not in whole:
        problems.append(f"the mint is not cut to its ends: {whole!r}")
    if len(line) > WIDTH - 1 or not line.endswith("…"):
        problems.append(
            f"an information line is not cut to the window: {len(line)} columns"
        )
    console.set_bot_name("migration")
    other = fmt.format(_record("Queued new token"))
    console.set_bot_name("sniper")
    if other.index("Queued") != line.index("Queued"):
        problems.append("two bots' messages do not start in the same column")
    wide = fmt.format(_record("New token detected: " + "\U0001f438" * 40))
    columns = sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in wide
    )
    if columns > WIDTH - 1:
        problems.append(f"a line of wide characters takes {columns} columns")
    named = fmt.format(_record(f"Buying 0.002000 of quote {SOL} worth of X"))
    if "quote SOL worth" not in named:
        problems.append(f"the SOL mint is not named: {named!r}")
    long_warning = "x" * 200
    warned = fmt.format(_record(long_warning, logging.WARNING))
    if long_warning not in warned or "WARN" not in warned:
        problems.append("a warning is cut or unmarked")
    erred = fmt.format(_record(long_warning, logging.ERROR))
    if long_warning not in erred or "ERROR" not in erred:
        problems.append("an error is cut or unmarked")
    coloured = console.ConsoleFormatter(colour=True, width=200).format(
        _record(EVENTS[3])
    )
    if not ESC.search(coloured):
        problems.append("an event carries no colour")
    return _report(problems)


def check_b_events_by_default() -> bool:
    problems = []
    records = [_record(m) for m in EVENTS + DETAIL] + [
        _record("a warning", logging.WARNING)
    ]
    quiet = _lines(console.console_handler(_Tty()), records)
    shown = "\n".join(quiet)
    for message in EVENTS:
        if message.split(":")[0].split(" (")[0][:20] not in shown:
            problems.append(f"default view drops an event: {message[:50]}")
    for message in DETAIL:
        if console.shorten(message)[:30] in shown:
            problems.append(f"default view shows a detail line: {message[:50]}")
    if "a warning" not in shown:
        problems.append("default view drops a warning")
    verbose = _lines(console.console_handler(_Tty(), verbose=True), records)
    if len(verbose) != len(records):
        problems.append(f"--verbose prints {len(verbose)} of {len(records)} lines")
    return _report(problems)


def check_c_pipe_and_no_color() -> bool:
    problems = []
    record = _record(EVENTS[3])
    piped = _lines(console.console_handler(io.StringIO()), [record])
    with tempfile.TemporaryDirectory() as folder:
        reference = file_handler(f"{folder}/x.log").formatter.format(record)
    if piped != [reference]:
        problems.append(
            f"piped line differs from the log file's: {piped} vs {reference!r}"
        )
    os.environ["NO_COLOR"] = "1"
    try:
        plain = _lines(console.console_handler(_Tty()), [_record(EVENTS[7])])
    finally:
        del os.environ["NO_COLOR"]
    if not plain or ESC.search(plain[0]) or "sniper" not in plain[0]:
        problems.append(f"NO_COLOR is not the short layout without colour: {plain}")
    return _report(problems)


def check_d_secrets_masked() -> bool:
    problems = []
    secret = "sk-console-verify-0123456789abcdef"  # noqa: S105 - a fake, to be masked
    register_secret(secret)
    install_secret_redaction()
    stream = _Tty()
    handler = console.console_handler(stream, verbose=True)
    log = logging.getLogger("verify.console")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning(f"endpoint {secret} refused")
        try:
            raise RuntimeError(f"connect to {secret} failed")  # noqa: TRY003, TRY301
        except RuntimeError:
            log.exception("send failed")
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    if secret in out:
        problems.append("a registered secret reached the terminal")
    if "RuntimeError" not in out:
        problems.append("the traceback did not print")
    return _report(problems)


def check_e_events_still_logged() -> bool:
    source = "\n".join(p.read_text() for p in (PROJECT_ROOT / "src").rglob("*.py"))
    problems = [
        f"no log line in src/ says {w!r}" for w in EVENT_WORDS if w not in source
    ]
    for message in EVENTS:
        style, detail = console.classify("trading.x", message)
        if detail or (not style and not message.startswith("Final PnL")):
            problems.append(f"not classified as an event: {message[:50]}")
    return _report(problems)


def _pump_bot(args: list[str], folder: str, *, tty: bool) -> tuple[int, str]:
    code = f"import sys; sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r}); import bot_runner; bot_runner.main({args!r})"
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    if not tty:
        run = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            cwd=folder,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        return run.returncode, run.stdout + run.stderr
    leader, follower = pty.openpty()
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=folder,
        stdin=follower,
        stdout=follower,
        stderr=follower,
        env=env,
    )
    os.close(follower)
    chunks = []
    while True:
        try:
            data = os.read(leader, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    os.close(leader)
    return proc.wait(timeout=60), b"".join(chunks).decode(errors="replace")


def check_f_pump_bot_prints_through_it() -> bool:
    problems = []
    with tempfile.TemporaryDirectory() as folder:
        status, out = _pump_bot(["--verbose"], folder, tty=True)
        plain = ESC.sub("", out)
        line = next((x for x in plain.splitlines() if "Bot directory" in x), "")
        if status != 1 or not re.match(r"^\d\d:\d\d:\d\d  ERROR Bot directory", line):
            problems.append(f"terminal run: status {status}, line {line!r}")
        if not ESC.search(out):
            problems.append("terminal run printed no colour")
        status, out = _pump_bot([], folder, tty=False)
        if status != 1 or " - root - ERROR - Bot directory" not in out:
            problems.append(f"piped run: status {status}, not the log file's format")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            bot_runner.parse_args(["--bogus"])
        problems.append("an unknown flag parsed")
    except SystemExit as e:
        if e.code != USAGE_ERROR:
            problems.append(f"an unknown flag exits {e.code}, not 2")
    if not bot_runner.parse_args(["-v"]).verbose:
        problems.append("-v does not set verbose")
    return _report(problems)


def main() -> int:
    checks = [
        (
            "A: a terminal line is short, cut to the window, warnings whole",
            check_a_layout,
        ),
        (
            "B: events only by default, every line with --verbose",
            check_b_events_by_default,
        ),
        (
            "C: piped output keeps the log file's format; NO_COLOR has no colour",
            check_c_pipe_and_no_color,
        ),
        (
            "D: secrets are masked on the terminal, tracebacks included",
            check_d_secrets_masked,
        ),
        (
            "E: the events the colours key on are still logged in those words",
            check_e_events_still_logged,
        ),
        (
            "F: pump_bot prints through it, on a terminal and piped",
            check_f_pump_bot_prints_through_it,
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

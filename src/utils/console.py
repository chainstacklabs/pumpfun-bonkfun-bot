"""How the bot's log reads in a terminal: short, coloured lines.

This changes the terminal only; the log file under `logs/` keeps every line whole.
On a terminal a line is the time, the bot's name and the message, with long
addresses cut to their ends (`71Z638…pump`), known quote mints named (`SOL`), and
an information line cut to the window width. A warning or an error is never cut.

Colour says which phase a line belongs to, brightness how much it matters. By
default only events print (a coin found, a buy, a sale, the profit or loss, a skip,
every warning); `pump_bot --verbose` prints the detail lines too, dimmed. The rules
match on message text, so rewording a message in `_RULES` changes its colour:
`verify_console_output.py` pins the events.

Piped into a file or another program, every line prints in the log file's format.
`NO_COLOR` keeps the short layout without colour.
"""

import logging
import os
import re
import shutil
import sys
import unicodedata
from typing import TextIO

from utils.logger import log_line_formatter, redact

RESET = "\033[0m"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
MAGENTA = "35"
CYAN = "36"


# A colour per bot, picked by name, so two bots in one terminal read apart. Every
# tag is padded to the same width, so messages start in one column.
_BOT_COLOURS = ("94", "95", "96", "93", "92", "91")
_BOT_WIDTH = 10

_KNOWN_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
}
# An address or a signature: 32 to 88 characters of base58.
_BASE58 = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,88}(?![1-9A-HJ-NP-Za-km-z])"
)
_PNL = re.compile(r"(PnL: )([+-]?\d[\d.]*%)( \()([+-]?\d[\d.]* \w+)")

# Loggers whose information lines are all detail.
_QUIET_LOGGERS = (
    "platforms",
    "utils.idl_manager",
    "monitoring.listener_factory",
    "cleanup",
    "core",
)

# Colour says which phase a line belongs to (found, buy, sell, exit), brightness
# says how much it matters: an event is bright, its detail dimmed in the same
# colour. First match wins. Each rule: a regex on the message, its colour, and
# whether the line is detail.
_RULES: tuple[tuple[re.Pattern[str], str, bool], ...] = tuple(
    (re.compile(pattern), style, detail)
    for pattern, style, detail in (
        (r"^Successfully bought", f"{BOLD};{GREEN}", False),
        (r"^Successfully (sold|exited)", f"{BOLD};{MAGENTA}", False),
        (r"^Exit condition met", f"{BOLD};{YELLOW}", False),
        (r"^Final PnL", "", False),
        (r"^Position PnL", DIM, True),
        (r"^(New token detected|Migration (done|submitted))", CYAN, False),
        (r"^Buying \S+ of quote", GREEN, False),
        (
            r"^(Buying|Buy |Actual |Total cost|Fill:|Tokens received|SOL to pool)",
            f"{DIM};{GREEN}",
            True,
        ),
        (
            r"^(Selling|Sell |Sale |Expected|Minimum|Token balance|Reference price)",
            f"{DIM};{MAGENTA}",
            True,
        ),
        (r"^(Found token|Queued|Processing fresh)", f"{DIM};{CYAN}", True),
        (
            r"^(Take profit target|Stop loss target|Current price|Created position|Starting position monitoring)",
            f"{DIM};{YELLOW}",
            True,
        ),
        (r"^(Skipping|Token does not match)", DIM, False),
        (
            r"^(Initialized|Registered platform|Entry:|Match filter:|Creator filter:"
            r"|Marry mode:|YOLO mode:|Exit strategy:|Max |Take profit:|Stop loss:"
            r"|RPC warm-up|Resolved quote mint|Monitoring (platforms|accounts))",
            DIM,
            True,
        ),
        (
            r"^(Waiting|Priority fee|Token information saved|\[Cleanup\]|Closing|Closed)",
            DIM,
            True,
        ),
    )
)

# The bot this process is running: each bot process sets its own.
_current = {"bot": ""}


def set_bot_name(name: str) -> None:
    """Name the bot this process is running, for the tag at the start of each line."""
    _current["bot"] = name


def shorten(text: str) -> str:
    """Cut every address and signature to its ends; name the known quote mints."""

    def cut(match: re.Match[str]) -> str:
        value = match.group(0)
        return _KNOWN_MINTS.get(value) or f"{value[:6]}…{value[-4:]}"

    return _BASE58.sub(cut, text)


def _columns_of(char: str) -> int:
    """Columns a character takes: two for wide ones (most emoji, CJK), else one."""
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit(text: str, columns: int) -> str:
    """`text` cut with `…` to at most `columns` terminal columns."""
    if sum(map(_columns_of, text)) <= columns:
        return text
    used, end = 1, 0
    for end, char in enumerate(text):  # noqa: B007 - `end` is read after the loop
        used += _columns_of(char)
        if used > columns:
            break
    return text[:end] + "…"


def classify(logger: str, message: str) -> tuple[str, bool]:
    """The colour of an information line, and whether it is detail."""
    for pattern, style, detail in _RULES:
        if pattern.search(message):
            return style, detail
    if logger.startswith(_QUIET_LOGGERS):
        return DIM, True
    return "", False


class HideDetail(logging.Filter):
    """Keep the terminal to events: drop information lines that are detail."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not classify(record.name, record.getMessage())[1]


class ConsoleFormatter(logging.Formatter):
    """The terminal's layout: `HH:MM:SS  bot  message`, coloured, cut to the window.

    Args:
        colour: Paint the line; False gives the same layout without escape codes
        width: Columns to cut an information line to; None reads the width of
            the window `stream` writes to on every line, so a resized window
            takes effect at once
        stream: Where the lines go, for that width; sys.stderr when None
    """

    def __init__(
        self,
        *,
        colour: bool = True,
        width: int | None = None,
        stream: TextIO | None = None,
    ) -> None:
        super().__init__()
        self.colour = colour
        self.width = width
        self.stream = stream

    def _columns(self) -> int:
        if self.width:
            return self.width
        try:
            return os.get_terminal_size((self.stream or sys.stderr).fileno()).columns
        except (OSError, ValueError, AttributeError):
            return shutil.get_terminal_size().columns

    def _paint(self, text: str, style: str) -> str:
        return f"\033[{style}m{text}{RESET}" if self.colour and style else text

    def format(self, record: logging.LogRecord) -> str:
        message = shorten(record.getMessage())
        when = self.formatTime(record, "%H:%M:%S")
        bot = _current["bot"][:_BOT_WIDTH].ljust(_BOT_WIDTH) if _current["bot"] else ""
        prefix_width = len(when) + 2 + (len(bot) + 2 if bot else 0)

        badge = ""
        if record.levelno >= logging.ERROR:
            badge, style = "ERROR ", f"{BOLD};{RED}"
        elif record.levelno >= logging.WARNING:
            badge, style = "WARN ", YELLOW
        else:
            style, _ = classify(record.name, message)
            # One column short of the window: a terminal that is handed a line
            # exactly its width may wrap the cursor onto a blank line after it.
            room = self._columns() - prefix_width - 1
            if room > 1:
                message = fit(message, room)

        # The traceback the record factory already masked, never one rendered
        # again from `exc_info`: an exception's text can name an endpoint.
        if record.exc_text:
            message += "\n" + record.exc_text
        elif record.exc_info:
            message += "\n" + redact(self.formatException(record.exc_info))

        line = self._paint(when, DIM) + "  "
        if bot:
            colour = _BOT_COLOURS[sum(map(ord, bot.rstrip())) % len(_BOT_COLOURS)]
            line += self._paint(bot, colour) + "  "
        if badge:
            line += self._paint(badge, f"{BOLD};{style}")
        return line + self._message(message, style)

    def _message(self, message: str, style: str) -> str:
        pnl = _PNL.search(message)
        if not (pnl and self.colour):
            return self._paint(message, style)
        sign = RED if pnl.group(2).startswith("-") else GREEN
        start, end = pnl.span()
        return (
            self._paint(message[:start] + pnl.group(1), style)
            + self._paint(pnl.group(2), f"{BOLD};{sign}")
            + self._paint(pnl.group(3), style)
            + self._paint(pnl.group(4), f"{BOLD};{sign}")
            + self._paint(message[end:], style)
        )


def console_handler(
    stream: TextIO | None = None, *, verbose: bool = False
) -> logging.Handler:
    """The handler `pump_bot` prints through.

    Args:
        stream: Where to print; sys.stderr when None
        verbose: On a terminal, print every line, detail dimmed, rather than
            events only. Off a terminal every line is printed anyway.

    Returns:
        On a terminal, the short coloured layout (no colour under `NO_COLOR`);
        otherwise a log file's line, `<date> <time> - <logger> - <LEVEL> - <message>`
    """
    stream = stream or sys.stderr
    handler = logging.StreamHandler(stream)
    if hasattr(stream, "isatty") and stream.isatty():
        colour = not os.environ.get("NO_COLOR")
        handler.setFormatter(ConsoleFormatter(colour=colour, stream=stream))
        if not verbose:
            handler.addFilter(HideDetail())
    else:
        handler.setFormatter(log_line_formatter())
    return handler

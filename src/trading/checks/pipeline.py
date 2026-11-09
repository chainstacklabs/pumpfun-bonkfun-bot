"""Run the configured pre-buy checks in order, and load them from the bot YAML."""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from trading.checks.base import CheckContext, PreBuyCheck
from utils.logger import get_logger

if TYPE_CHECKING:
    from interfaces.core import TokenInfo

logger = get_logger(__name__)

# Keys of a check entry the pipeline reads; the rest go to the check's __init__.
_ENTRY_KEYS = ("name", "module", "enabled", "required", "budget")


@dataclass(frozen=True)
class ConfiguredCheck:
    """A check as configured.

    Args:
        required: When the check can't decide (it raised, ran past its budget
            or returned something other than None or a str), True refuses the
            coin and False lets it through
        budget: Seconds the check may take; None is no limit
    """

    check: PreBuyCheck
    required: bool = True
    budget: float | None = None


class CheckPipeline:
    """The configured checks, run in order; the first skip wins."""

    def __init__(self, checks: list[ConfiguredCheck], ctx: CheckContext) -> None:
        self.checks = checks
        self.ctx = ctx

    def has(self, *, reads_chain: bool) -> bool:
        """Whether any check of that kind is configured."""
        return any(c.check.reads_chain is reads_chain for c in self.checks)

    async def reason(self, coin: TokenInfo, *, reads_chain: bool) -> str | None:
        """Why a check refuses the coin, as "<check name>: <reason>", or None.

        Runs only the checks whose `reads_chain` matches.
        """
        for configured in self.checks:
            if configured.check.reads_chain is not reads_chain:
                continue
            reason = await self._run(configured, coin)
            if reason is not None:
                return f"{configured.check.name}: {reason}"
        return None

    async def _run(self, configured: ConfiguredCheck, coin: TokenInfo) -> str | None:
        check = configured.check
        try:
            async with asyncio.timeout(configured.budget):
                result = await check.check(coin, self.ctx)
            if result is None or isinstance(result, str):
                return result
            failure = f"returned {type(result).__name__}, not None or a reason"
        except TimeoutError:
            failure = f"ran past its {configured.budget}s budget"
        except Exception as e:  # noqa: BLE001 - a check must never stop the trader
            # The class only: an exception's text can carry an endpoint URL.
            failure = f"failed ({type(e).__name__})"
        if configured.required:
            logger.warning(f"Check {check.name} {failure}; skipping {coin.symbol}")
            return f"could not decide: {failure}"
        logger.warning(f"Check {check.name} {failure}; letting {coin.symbol} through")
        return None


def _class_from_module(path: str) -> type:
    module_name, sep, class_name = path.partition(":")
    if not sep or not module_name or not class_name:
        raise ValueError(f"check module {path!r} must look like 'package.module:Class'")  # noqa: TRY003
    # User checks live beside the bot YAMLs, in the folder pump_bot runs from.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ValueError(f"check module {module_name!r} does not import: {e}") from e  # noqa: TRY003
    try:
        return getattr(module, class_name)
    except AttributeError:
        raise ValueError(f"check module {module_name!r} has no {class_name}") from None  # noqa: TRY003


def load_checks(
    entries: list[dict] | None, builtins: dict[str, type[PreBuyCheck]]
) -> list[ConfiguredCheck]:
    """Build the checks a `filters.checks` list names, leaving out disabled ones.

    Raises:
        ValueError: An entry names no check, both a builtin and a module, an
            unknown builtin, or a module or class that does not load, or its
            options don't fit the check; the message is one line naming it
    """
    loaded = []
    for entry in entries or []:
        if not entry.get("enabled", True):
            continue
        has_name, has_module = "name" in entry, "module" in entry
        if has_name == has_module:
            raise ValueError(f"check {entry!r} needs either name or module")  # noqa: TRY003
        if has_name:
            cls = builtins.get(entry["name"])
            if cls is None:
                raise ValueError(  # noqa: TRY003
                    f"unknown check {entry['name']!r}; built-in checks: "
                    f"{sorted(builtins)}"
                )
        else:
            cls = _class_from_module(entry["module"])
            if not (isinstance(cls, type) and issubclass(cls, PreBuyCheck)):
                raise ValueError(f"{entry['module']} is not a PreBuyCheck subclass")  # noqa: TRY003
        options = {k: v for k, v in entry.items() if k not in _ENTRY_KEYS}
        try:
            check = cls(**options)
        except (TypeError, ValueError) as e:
            raise ValueError(f"check {entry!r}: {e}") from e  # noqa: TRY003
        loaded.append(
            ConfiguredCheck(
                check, required=entry.get("required", True), budget=entry.get("budget")
            )
        )
    return loaded

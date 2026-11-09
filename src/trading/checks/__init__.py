"""Pre-buy checks: the step that accepts or refuses a coin before the buy.

Write your own by subclassing `PreBuyCheck`; `docs/pre-buy-checks.md` shows how.
"""

from trading.checks.base import CheckContext, CheckUnavailable, PreBuyCheck
from trading.checks.pipeline import CheckPipeline, ConfiguredCheck, load_checks

__all__ = [
    "CheckContext",
    "CheckPipeline",
    "CheckUnavailable",
    "ConfiguredCheck",
    "PreBuyCheck",
    "load_checks",
]

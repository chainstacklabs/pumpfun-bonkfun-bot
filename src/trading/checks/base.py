"""What a pre-buy check is: one async method that passes a coin or says why not."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from core.client import SolanaClient
    from interfaces.core import TokenInfo


class CheckUnavailable(Exception):  # noqa: N818
    """A check could not read what it needs, so it cannot decide."""


@dataclass(frozen=True)
class CheckContext:
    """What a check may use besides the coin."""

    client: SolanaClient


class PreBuyCheck(ABC):
    """One check a coin must pass before the bot buys it.

    Options from the check's YAML entry are passed to `__init__` as keyword
    arguments. A check that reads the chain sets `reads_chain = True`: it runs
    just before the buy, after any creation wait, and is never on by default.
    One that reads nothing runs as soon as the coin is detected.
    """

    name: ClassVar[str]
    reads_chain: ClassVar[bool] = False

    @abstractmethod
    async def check(self, coin: TokenInfo, ctx: CheckContext) -> str | None:
        """None to pass the coin, or why it is skipped.

        Raises:
            CheckUnavailable: The data the check needs could not be read; any
                other exception counts the same
        """

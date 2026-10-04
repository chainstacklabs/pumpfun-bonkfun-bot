"""letsbonk.fun: Raydium LaunchLab pools under letsbonk.fun's platform config."""

from typing import Final

from solders.pubkey import Pubkey

from interfaces.core import Platform
from platforms.launchlab import (
    LaunchLabAddressProvider,
    LaunchLabCurveManager,
    LaunchLabEventParser,
    LaunchLabInstructionBuilder,
)

PLATFORM_CONFIGS: Final[frozenset[Pubkey]] = frozenset(
    {Pubkey.from_string("5thqcDwKp5QQ8US4XRMoseGeGbmLKMmoKZmS6zHrQAsA")}
)


class LetsBonkAddressProvider(LaunchLabAddressProvider):
    """letsbonk.fun addresses."""

    PLATFORM_CONFIGS = PLATFORM_CONFIGS

    @property
    def platform(self) -> Platform:
        """letsbonk.fun."""
        return Platform.LETS_BONK


class LetsBonkInstructionBuilder(LaunchLabInstructionBuilder):
    """letsbonk.fun trades."""

    @property
    def platform(self) -> Platform:
        """letsbonk.fun."""
        return Platform.LETS_BONK


class LetsBonkCurveManager(LaunchLabCurveManager):
    """letsbonk.fun pool reads."""

    @property
    def platform(self) -> Platform:
        """letsbonk.fun."""
        return Platform.LETS_BONK


class LetsBonkEventParser(LaunchLabEventParser):
    """letsbonk.fun launches."""

    PLATFORM_CONFIGS = PLATFORM_CONFIGS

    @property
    def platform(self) -> Platform:
        """letsbonk.fun."""
        return Platform.LETS_BONK


__all__ = [
    "LetsBonkAddressProvider",
    "LetsBonkCurveManager",
    "LetsBonkEventParser",
    "LetsBonkInstructionBuilder",
]

"""StonkFun: Raydium LaunchLab pools under StonkFun's two platform configs."""

from interfaces.core import Platform
from platforms.launchlab import (
    LaunchLabAddressProvider,
    LaunchLabCurveManager,
    LaunchLabEventParser,
    LaunchLabInstructionBuilder,
)
from platforms.stonkfun.addresses import PLATFORM_CONFIGS


class StonkFunAddressProvider(LaunchLabAddressProvider):
    """StonkFun addresses."""

    PLATFORM_CONFIGS = PLATFORM_CONFIGS

    @property
    def platform(self) -> Platform:
        """StonkFun."""
        return Platform.STONK_FUN


class StonkFunInstructionBuilder(LaunchLabInstructionBuilder):
    """StonkFun trades."""

    @property
    def platform(self) -> Platform:
        """StonkFun."""
        return Platform.STONK_FUN


class StonkFunCurveManager(LaunchLabCurveManager):
    """StonkFun pool reads."""

    @property
    def platform(self) -> Platform:
        """StonkFun."""
        return Platform.STONK_FUN


class StonkFunEventParser(LaunchLabEventParser):
    """StonkFun launches."""

    PLATFORM_CONFIGS = PLATFORM_CONFIGS

    @property
    def platform(self) -> Platform:
        """StonkFun."""
        return Platform.STONK_FUN


__all__ = [
    "StonkFunAddressProvider",
    "StonkFunCurveManager",
    "StonkFunEventParser",
    "StonkFunInstructionBuilder",
]

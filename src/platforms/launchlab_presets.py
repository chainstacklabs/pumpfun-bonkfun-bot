"""Which LaunchLab platform configs each LaunchLab platform covers."""

from typing import Final

from solders.pubkey import Pubkey

from interfaces.core import Platform
from platforms.letsbonk import PLATFORM_CONFIGS as LETSBONK_CONFIGS
from platforms.stonkfun.addresses import PLATFORM_CONFIGS as STONKFUN_CONFIGS

# None covers every platform config on the program.
LAUNCHLAB_PLATFORMS: Final[dict[Platform, frozenset[Pubkey] | None]] = {
    Platform.LAUNCHLAB: None,
    Platform.LETS_BONK: LETSBONK_CONFIGS,
    Platform.STONK_FUN: STONKFUN_CONFIGS,
}


def resolve_platform_configs(
    platform: Platform, configured: list[str] | None
) -> frozenset[Pubkey] | None:
    """Turn a bot's `launchlab.platform_configs` into the configs it listens on.

    Returns:
        The configured set, or None when the setting is absent: the platform
        then covers what `LAUNCHLAB_PLATFORMS` says

    Raises:
        ValueError: If the setting is set on a platform that is not LaunchLab,
            is not a non-empty list of addresses, or names a config a preset
            does not cover
    """
    if configured is None:
        return None
    if platform not in LAUNCHLAB_PLATFORMS:
        raise ValueError(  # noqa: TRY003
            f"launchlab.platform_configs does not apply to platform '{platform.value}'"
        )
    if not isinstance(configured, list) or not configured:
        raise ValueError(  # noqa: TRY003
            "launchlab.platform_configs must be a non-empty list of addresses"
        )
    try:
        narrowed = frozenset(Pubkey.from_string(str(c)) for c in configured)
    except ValueError as e:
        raise ValueError(f"launchlab.platform_configs: {e}") from e  # noqa: TRY003
    covered = LAUNCHLAB_PLATFORMS[platform]
    if covered is not None and not narrowed <= covered:
        outside = sorted(str(c) for c in narrowed - covered)
        raise ValueError(  # noqa: TRY003
            f"platform '{platform.value}' does not cover {outside}; "
            f"use platform 'launchlab' for other launchpads"
        )
    return narrowed


def traded_platform_configs(
    platform: Platform, configured: list[str] | None
) -> frozenset[Pubkey] | None:
    """The platform configs a bot trades: its setting, else what its platform covers.

    None for every config on the program, and for pump.fun, which has none.

    Raises:
        ValueError: As `resolve_platform_configs`
    """
    narrowed = resolve_platform_configs(platform, configured)
    return narrowed if narrowed is not None else LAUNCHLAB_PLATFORMS.get(platform)

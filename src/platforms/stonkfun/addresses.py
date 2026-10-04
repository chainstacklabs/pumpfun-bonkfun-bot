"""StonkFun's LaunchLab platform configs."""

from typing import Final

from solders.pubkey import Pubkey

# Standard coins carry no transfer fee; reward coins are Token-2022 mints with a
# 1% or 3% transfer fee, paid out to holders.
STANDARD_PLATFORM_CONFIG: Final[Pubkey] = Pubkey.from_string(
    "4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"
)
REWARD_PLATFORM_CONFIG: Final[Pubkey] = Pubkey.from_string(
    "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"
)
PLATFORM_CONFIGS: Final[frozenset[Pubkey]] = frozenset(
    {STANDARD_PLATFORM_CONFIG, REWARD_PLATFORM_CONFIG}
)

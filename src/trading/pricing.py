"""A coin's price where it trades now: its curve, or the pool it graduated to."""

from interfaces.core import CurveGraduatedError, Market, TokenInfo
from platforms import PlatformImplementations


async def read_price(
    token_info: TokenInfo, implementations: PlatformImplementations
) -> tuple[float, Market]:
    """The price, in whole quote units per whole token, and the venue that set it.

    0.0 for a pool with nothing in it.

    Raises:
        ValueError: A graduated coin's pool can't be priced yet (it lands a
            moment after graduation), or the platform has no pool to price
    """
    try:
        curve = implementations.curve_market
        return await curve.get_price(token_info), curve
    except CurveGraduatedError:
        market = implementations.graduated_market
        if market is None:
            raise ValueError(  # noqa: TRY003
                f"{token_info.symbol} has graduated and "
                f"{token_info.platform.value} has no post-graduation market"
            ) from None
        return await market.get_price(token_info), market

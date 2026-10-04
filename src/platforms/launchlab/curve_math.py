"""Raydium LaunchLab curve arithmetic: prices, fees and Token-2022 transfer fees.

All amounts are raw integer units, with the program's own rounding: fees round
up, curve outputs round down. These reproduce `simulateTransaction` to the unit.
"""

import struct

# Fee rates on GlobalConfig and PlatformConfig are parts per million.
FEE_RATE_DENOMINATOR = 1_000_000
BASIS_POINTS = 10_000

# Token-2022 mint layout: 82-byte base mint, padding to 165, one account-type
# byte, then type-length-value extensions.
_EXTENSIONS_OFFSET = 166
_TRANSFER_FEE_CONFIG = 1
# TransferFeeConfig: two authorities and a withheld amount (72 bytes), then the
# older and newer fees, each (epoch u64, maximum_fee u64, basis_points u16).
_OLDER_FEE_OFFSET = 72
_NEWER_FEE_OFFSET = 90


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def mint_transfer_fee(mint_data: bytes) -> tuple[int, int] | None:
    """A Token-2022 mint's transfer fee, as (basis points, maximum fee raw).

    Returns the higher of the scheduled older and newer fees. A rate change only
    takes effect two epochs after it is set, and taking the higher one avoids an
    epoch read while never underestimating what a transfer loses.

    Returns:
        None when the mint has no TransferFeeConfig extension — then a transfer
        loses nothing and there is nothing withheld to harvest.
    """
    offset = _EXTENSIONS_OFFSET
    while offset + 4 <= len(mint_data):
        kind, length = struct.unpack_from("<HH", mint_data, offset)
        if kind == 0 and length == 0:
            break
        if kind == _TRANSFER_FEE_CONFIG:
            body = mint_data[offset + 4 : offset + 4 + length]
            _, older_max, older_bps = struct.unpack_from(
                "<QQH", body, _OLDER_FEE_OFFSET
            )
            _, newer_max, newer_bps = struct.unpack_from(
                "<QQH", body, _NEWER_FEE_OFFSET
            )
            return max((older_bps, older_max), (newer_bps, newer_max))
        offset += 4 + length
    return None


def withheld_fee(amount: int, transfer_fee_bps: int, maximum_fee: int) -> int:
    """What Token-2022 withholds from a transfer of `amount`, rounded up and capped."""
    if not transfer_fee_bps:
        return 0
    return min(_ceil_div(amount * transfer_fee_bps, BASIS_POINTS), maximum_fee)


def spot_price_raw(pool: dict) -> float:
    """Raw quote units per raw base unit at the current point on the curve.

    The virtual reserves are fixed when the pool is created; the real reserves
    record every trade since. Pricing off the virtual reserves alone returns the
    launch price forever.
    """
    base_reserve = pool["virtual_base"] - pool["real_base"]
    quote_reserve = pool["virtual_quote"] + pool["real_quote"]
    if base_reserve <= 0 or quote_reserve <= 0:
        return 0.0
    return quote_reserve / base_reserve


def buy_amount_out(
    pool: dict,
    amount_in: int,
    fee_rate: int,
    transfer_fee: tuple[int, int] = (0, 0),
) -> int:
    """Raw base tokens delivered to the buyer for exactly `amount_in` raw quote.

    The curve fee comes off the input first, the rest trades on the curve, and
    the transfer fee is withheld on delivery.

    Args:
        fee_rate: Total curve fee, parts per million (trade + platform + creator)
        transfer_fee: (basis points, maximum fee raw) of the base mint
    """
    net_in = amount_in - _ceil_div(amount_in * fee_rate, FEE_RATE_DENOMINATOR)
    base_reserve = pool["virtual_base"] - pool["real_base"]
    quote_reserve = pool["virtual_quote"] + pool["real_quote"]
    out = base_reserve * net_in // (quote_reserve + net_in)
    return out - withheld_fee(out, *transfer_fee)


def sell_amount_out(
    pool: dict,
    amount_in: int,
    fee_rate: int,
    transfer_fee: tuple[int, int] = (0, 0),
) -> int:
    """Raw quote paid to the seller for sending `amount_in` raw base tokens.

    The transfer fee is withheld on the way into the pool, so the curve trades
    on what arrives, and the curve fee then comes off the quote side.
    """
    arrived = amount_in - withheld_fee(amount_in, *transfer_fee)
    base_reserve = pool["virtual_base"] - pool["real_base"]
    quote_reserve = pool["virtual_quote"] + pool["real_quote"]
    gross = quote_reserve * arrived // (base_reserve + arrived)
    return gross - _ceil_div(gross * fee_rate, FEE_RATE_DENOMINATOR)

"""Everything one trader is configured with, built by `bot_runner` from a bot YAML."""

from dataclasses import dataclass, field

from interfaces.core import Platform

# Default for trade.max_exit_sell_attempts. The seller's own max_retries covers
# submission only, so a revert is retried in the monitor loop, where the price is
# re-read first. Bounded so a token that keeps reverting cannot pin the bot.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3

#: What the bot buys: coins as they launch, on their curve, or coins whose curve
#: has just migrated, in their new AMM pool.
ENTRIES = ("new_launch", "migration")


def entry_error(entry: str, *, extreme_fast_mode: bool) -> str | None:
    """Why `trade.entry` cannot run as configured, or None when it can."""
    if entry not in ENTRIES:
        return f"trade.entry must be one of {list(ENTRIES)}, not {entry!r}"
    if entry == "migration" and extreme_fast_mode:
        return (
            "trade.extreme_fast_mode does not apply to trade.entry: migration. "
            "A migrated coin is bought in its AMM pool, sized from the pool's "
            "reserves, which extreme_fast_mode never reads. Set "
            "extreme_fast_mode: false."
        )
    return None


@dataclass(frozen=True)
class TraderSettings:
    """One bot's trading configuration.

    Amounts are in whole quote units, times in seconds, slippages and
    percentages as fractions (0.25 is 25%). The endpoints carry their API key
    and are left out of the repr, as are the key and the geyser token.
    """

    rpc_endpoint: str = field(repr=False)
    wss_endpoint: str = field(repr=False)
    private_key: str = field(repr=False)
    buy_amount: float
    buy_slippage: float
    sell_slippage: float

    platform: Platform | str = Platform.PUMP_FUN
    # "new_launch" buys coins as they launch; "migration" buys them in the pool
    # their curve migrates to.
    entry: str = "new_launch"
    # LaunchLab platform config addresses to listen on; None is all the
    # platform covers.
    platform_configs: list[str] | None = None

    listener_type: str = "logs"
    geyser_endpoint: str | None = field(default=None, repr=False)
    geyser_api_token: str | None = field(default=None, repr=False)
    geyser_auth_type: str = "x-token"

    extreme_fast_mode: bool = False
    extreme_fast_token_amount: int = 30
    curve_refresh_budget: float = 2.0
    trust_create_event: bool = True

    # Keys are mint addresses or the aliases "sol"/"usdc"; SOL always falls back
    # to buy_amount.
    quote_amounts: dict[str, float] | None = None
    # None takes every quote asset that has a buy amount.
    allowed_quote_mints: list[str] | None = None
    # None takes every coin; a LaunchLab reward coin's transfer fee is paid on
    # the buy and again on the sell.
    max_transfer_fee_bps: int | None = None

    exit_strategy: str = "time_based"
    take_profit_percentage: float | None = None
    stop_loss_percentage: float | None = None
    max_hold_time: int | None = None
    price_check_interval: int = 10
    max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS
    # Positions whose exit runs at once; the bot takes no new coin while this
    # many are open. Positions under `manual` or `marry_mode` are not counted.
    max_open_positions: int = 1
    # SOL the wallet must hold for the bot to buy; below it, open positions
    # still run to their exit. None: no floor.
    min_wallet_balance: float | None = None

    enable_dynamic_priority_fee: bool = False
    enable_fixed_priority_fee: bool = True
    fixed_priority_fee: int = 200_000
    extra_priority_fee: float = 0.0
    hard_cap_prior_fee: int = 200_000

    max_retries: int = 3
    wait_time_after_creation: int = 15
    wait_time_after_buy: int = 15
    wait_time_before_new_token: int = 15
    max_token_age: int | float = 0.001
    # What `max_token_age` counts from: "detection" (when the listener saw the
    # coin) or "chain" (the slot its create landed in).
    age_from: str = "detection"
    token_wait_timeout: int = 30

    cleanup_mode: str = "disabled"
    cleanup_force_close_with_burn: bool = False
    cleanup_with_priority_fee: bool = True

    match_string: str | None = None
    bro_address: str | None = None
    marry_mode: bool = False
    yolo_mode: bool = False

    # "buy", "sell" and "account_data_size" overrides; empty uses the venue's.
    compute_units: dict | None = None
    max_rps: float = 25.0

    # Where open positions are kept across restarts; None keeps them in memory
    # only. bot_runner sets `positions/<bot name>.json`.
    positions_file: str | None = None

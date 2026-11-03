import asyncio
import logging
import multiprocessing
import sys
from datetime import datetime
from pathlib import Path

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        logging.info("Using winloop event loop policy for improved performance")
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logging.info("Using uvloop event loop policy for improved performance")
except ImportError:
    logging.info(
        "Using standard asyncio event loop (install uvloop/winloop for better performance)"
    )

from config_loader import (
    get_platform_from_config,
    load_bot_config,
    print_config_summary,
    validate_platform_listener_combination,
)
from core.keystore import KeystoreError, keystore_private_key, password_from_env
from core.wallet import Wallet
from interfaces.core import Platform
from trading.settings import DEFAULT_MAX_EXIT_SELL_ATTEMPTS, TraderSettings
from trading.universal_trader import UniversalTrader
from utils.logger import (
    get_logger,
    install_secret_redaction,
    register_secret,
    setup_file_logging,
)

logger = get_logger(__name__)


def setup_logging(bot_name: str):
    """Set up logging to file for a specific bot instance."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"{bot_name}_{timestamp}.log"

    setup_file_logging(str(log_filename))


def build_trader_settings(cfg: dict, platform: Platform) -> TraderSettings:
    """Turn a loaded bot config into the trader's settings, filling in defaults."""
    return TraderSettings(
        # Connection settings
        rpc_endpoint=cfg["rpc_endpoint"],
        wss_endpoint=cfg["wss_endpoint"],
        private_key=cfg["private_key"],
        # Platform configuration - pass platform enum directly
        platform=platform,
        platform_configs=(cfg.get("launchlab") or {}).get("platform_configs"),
        # Trade parameters
        entry=cfg["trade"].get("entry", "new_launch"),
        buy_amount=cfg["trade"]["buy_amount"],
        buy_slippage=cfg["trade"]["buy_slippage"],
        sell_slippage=cfg["trade"]["sell_slippage"],
        # Extreme fast mode settings
        extreme_fast_mode=cfg["trade"].get("extreme_fast_mode", False),
        extreme_fast_token_amount=cfg["trade"].get("extreme_fast_token_amount", 30),
        curve_refresh_budget=cfg["trade"].get("curve_refresh_budget", 2.0),
        trust_create_event=cfg["trade"].get("trust_create_event", True),
        # Quote asset configuration (pump.fun non-SOL pairs)
        quote_amounts=cfg["trade"].get("quote_amounts"),
        allowed_quote_mints=cfg["filters"].get("allowed_quote_mints"),
        max_transfer_fee_bps=cfg["filters"].get("max_transfer_fee_bps"),
        # Exit strategy configuration
        exit_strategy=cfg["trade"].get("exit_strategy", "time_based"),
        take_profit_percentage=cfg["trade"].get("take_profit_percentage"),
        stop_loss_percentage=cfg["trade"].get("stop_loss_percentage"),
        max_hold_time=cfg["trade"].get("max_hold_time"),
        price_check_interval=cfg["trade"].get("price_check_interval", 10),
        max_exit_sell_attempts=cfg["trade"].get(
            "max_exit_sell_attempts", DEFAULT_MAX_EXIT_SELL_ATTEMPTS
        ),
        max_open_positions=cfg["trade"].get("max_open_positions", 1),
        # Listener configuration
        listener_type=cfg["filters"]["listener_type"],
        # Geyser configuration (if applicable)
        geyser_endpoint=cfg.get("geyser", {}).get("endpoint"),
        geyser_api_token=cfg.get("geyser", {}).get("api_token"),
        geyser_auth_type=cfg.get("geyser", {}).get("auth_type", "x-token"),
        # Priority fee configuration
        enable_dynamic_priority_fee=cfg.get("priority_fees", {}).get(
            "enable_dynamic", False
        ),
        enable_fixed_priority_fee=cfg.get("priority_fees", {}).get(
            "enable_fixed", True
        ),
        fixed_priority_fee=cfg.get("priority_fees", {}).get("fixed_amount", 500000),
        extra_priority_fee=cfg.get("priority_fees", {}).get("extra_percentage", 0.0),
        hard_cap_prior_fee=cfg.get("priority_fees", {}).get("hard_cap", 500000),
        # Retry and timeout settings
        max_retries=cfg.get("retries", {}).get("max_attempts", 10),
        wait_time_after_creation=cfg.get("retries", {}).get("wait_after_creation", 15),
        wait_time_after_buy=cfg.get("retries", {}).get("wait_after_buy", 15),
        wait_time_before_new_token=cfg.get("retries", {}).get(
            "wait_before_new_token", 15
        ),
        max_token_age=cfg.get("filters", {}).get("max_token_age", 0.001),
        token_wait_timeout=cfg.get("timing", {}).get("token_wait_timeout", 120),
        # Cleanup settings
        cleanup_mode=cfg.get("cleanup", {}).get("mode", "disabled"),
        cleanup_force_close_with_burn=cfg.get("cleanup", {}).get(
            "force_close_with_burn", False
        ),
        cleanup_with_priority_fee=cfg.get("cleanup", {}).get(
            "with_priority_fee", False
        ),
        # Trading filters
        match_string=cfg["filters"].get("match_string"),
        bro_address=cfg["filters"].get("bro_address"),
        marry_mode=cfg["filters"].get("marry_mode", False),
        yolo_mode=cfg["filters"].get("yolo_mode", False),
        # Compute unit configuration
        compute_units=cfg.get("compute_units", {}),
        # Node provider configuration
        max_rps=cfg.get("node", {}).get("max_rps", 25),
        # Open positions, resumed on the next start
        positions_file=f"positions/{cfg['name']}.json" if cfg.get("name") else None,
    )


def unlock_keystore(cfg: dict) -> None:
    """Decrypt the config's `keystore` into `cfg["private_key"]`.

    The password is `PUMPFUN_PASSWORD`, read after `load_bot_config` loaded the
    bot's `env_file`. The decrypted key is masked in every later log line.

    Raises:
        KeystoreError: The keystore cannot be unlocked; the message says why
    """
    path = Path(cfg["keystore"])
    private_key = keystore_private_key(path, password_from_env())
    register_secret(private_key)
    cfg["private_key"] = private_key
    logger.info(f"Wallet {Wallet(private_key).pubkey} unlocked from keystore {path}")


async def start_bot(config_path: str):
    """Start a trading bot with the configuration from the specified path."""
    cfg = load_bot_config(config_path)
    setup_logging(cfg["name"])
    print_config_summary(cfg)

    # Get and validate platform from configuration
    try:
        platform = get_platform_from_config(cfg)
        logging.info(f"Detected platform: {platform.value}")
    except ValueError as e:
        logging.exception(f"Platform configuration error: {e}")
        return

    # Validate platform support
    try:
        from platforms import platform_factory

        if not platform_factory.registry.is_platform_supported(platform):
            logging.error(
                f"Platform {platform.value} is not supported. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
            )
            return
    except Exception as e:
        logging.exception(f"Could not validate platform support: {e}")
        return

    # Validate listener compatibility
    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        from config_loader import get_supported_listeners_for_platform

        supported = get_supported_listeners_for_platform(platform)
        logging.error(
            f"Listener '{listener_type}' is not compatible with platform '{platform.value}'. Supported listeners: {supported}"
        )
        return

    if "keystore" in cfg:
        try:
            unlock_keystore(cfg)
        except KeystoreError as e:
            logger.error(f"Bot '{cfg['name']}' not started: {e}")  # noqa: TRY400
            return

    # Initialize universal trader with platform-specific configuration
    try:
        trader = UniversalTrader(build_trader_settings(cfg, platform))

        await trader.start()

    except Exception as e:
        logging.exception(f"Failed to initialize or start trader: {e}")
        raise


def run_bot_process(config_path):
    asyncio.run(start_bot(config_path))


def _drop_duplicate_names(bot_files: list[Path]) -> list[Path]:
    """The files to start, without an enabled bot whose name an earlier one uses.

    The name keys a bot's log and positions files: two bots under one name
    would overwrite each other's open positions. A file that does not load is
    kept, for `run_all_bots` to report.
    """
    names_in_use: dict[str, Path] = {}
    kept = []
    for file in bot_files:
        try:
            cfg = load_bot_config(str(file))
        except Exception:  # noqa: BLE001
            kept.append(file)
            continue
        name = cfg.get("name", file.stem)
        if cfg.get("enabled", True):
            if name in names_in_use:
                logging.error(
                    f"Skipping bot '{name}' in {file}: {names_in_use[name]} "
                    f"already uses that name, and bot names must be unique"
                )
                continue
            names_in_use[name] = file
        kept.append(file)
    return kept


def run_all_bots():
    """Run all bots defined in YAML files in the 'bots' directory."""
    bot_dir = Path("bots")
    if not bot_dir.exists():
        logging.error(f"Bot directory '{bot_dir}' not found")
        return

    bot_files = sorted(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error(f"No bot configuration files found in '{bot_dir}'")
        return

    logging.info(f"Found {len(bot_files)} bot configuration files")

    bot_files = _drop_duplicate_names(bot_files)
    processes = []
    skipped_bots = 0

    for file in bot_files:
        try:
            cfg = load_bot_config(str(file))
            bot_name = cfg.get("name", file.stem)

            # Skip bots with enabled=False
            if not cfg.get("enabled", True):
                logging.info(f"Skipping disabled bot '{bot_name}'")
                skipped_bots += 1
                continue

            # Validate platform configuration
            try:
                platform = get_platform_from_config(cfg)

                # Check platform support
                from platforms import platform_factory

                if not platform_factory.registry.is_platform_supported(platform):
                    logging.error(
                        f"Platform {platform.value} is not supported for bot '{bot_name}'. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
                    )
                    skipped_bots += 1
                    continue

                # Validate listener compatibility
                listener_type = cfg["filters"]["listener_type"]
                if not validate_platform_listener_combination(platform, listener_type):
                    from config_loader import get_supported_listeners_for_platform

                    supported = get_supported_listeners_for_platform(platform)
                    logging.error(
                        f"Listener '{listener_type}' is not compatible with platform '{platform.value}' for bot '{bot_name}'. Supported listeners: {supported}"
                    )
                    skipped_bots += 1
                    continue

            except Exception as e:
                logging.exception(
                    f"Invalid platform configuration for bot '{bot_name}': {e}. Skipping..."
                )
                skipped_bots += 1
                continue

            # Start bot in separate process or main process
            if cfg.get("separate_process", False):
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in separate process"
                )
                p = multiprocessing.Process(
                    target=run_bot_process, args=(str(file),), name=f"bot-{bot_name}"
                )
                p.start()
                processes.append(p)
            else:
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in main process"
                )
                asyncio.run(start_bot(str(file)))

        except Exception as e:
            logging.exception(f"Failed to start bot from {file}: {e}")
            skipped_bots += 1

    logging.info(
        f"Started {len(bot_files) - skipped_bots} bots, skipped {skipped_bots} disabled/invalid bots"
    )

    # Wait for all processes to complete
    for p in processes:
        p.join()
        logging.info(f"Process {p.name} completed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # Turning the root logger up to INFO is what makes the HTTP clients start
    # printing request URLs, and those URLs carry the provider API key. Redact
    # before the first line is emitted, not after.
    install_secret_redaction()

    # Log supported platforms and listeners
    try:
        from platforms import platform_factory

        supported_platforms = platform_factory.get_supported_platforms()
        logging.info(f"Supported platforms: {[p.value for p in supported_platforms]}")

        # Log listener compatibility for each platform
        from config_loader import get_supported_listeners_for_platform

        for platform in supported_platforms:
            listeners = get_supported_listeners_for_platform(platform)
            logging.info(f"Platform {platform.value} supports listeners: {listeners}")

    except Exception as e:
        logging.warning(f"Could not load platform information: {e}")

    run_all_bots()


if __name__ == "__main__":
    main()

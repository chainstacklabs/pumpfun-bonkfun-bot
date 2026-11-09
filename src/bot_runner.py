import argparse
import asyncio
import logging
import multiprocessing
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

import base58
from solders.keypair import Keypair

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
    disabled_in_yaml,
    get_platform_from_config,
    get_supported_listeners_for_platform,
    load_bot_config,
    print_config_summary,
    validate_platform_listener_combination,
)
from core.client import rpc_endpoint_problem
from core.keystore import KeystoreError, unlock_from_env
from interfaces.core import Platform
from trading.position_store import holds_positions
from trading.settings import DEFAULT_MAX_EXIT_SELL_ATTEMPTS, TraderSettings
from trading.universal_trader import UniversalTrader
from utils import console
from utils.logger import (
    file_handler,
    get_logger,
    install_secret_redaction,
    register_endpoint,
    register_secret,
    setup_file_logging,
)

logger = get_logger(__name__)

# Exit status after Ctrl+C, as a shell reports a process ended by SIGINT.
EXIT_INTERRUPTED = 130
# Exit status after SIGTERM, as a shell reports a process ended by it.
EXIT_TERMINATED = 143
# Holds SIGTERM once received, so the exit status can say which signal it was.
_terminated: set[int] = set()
# The bot this process is running right now, for the SIGTERM handler to cancel.
_running: list[asyncio.Task] = []
# Bots this process started in their own processes, for SIGTERM to reach.
_children: list[multiprocessing.Process] = []


def _stop_on_sigterm() -> None:
    """Make SIGTERM stop the bots the way Ctrl+C does.

    The handler cancels the running bot, as `asyncio.run` does on Ctrl+C: its
    trader stops its positions and cleans up, then the process exits 143. It
    does not go through SIGINT, which a shell ignores in a job it starts in the
    background. SIGTERM is passed on to every bot in its own process, so one
    sent to the main process alone stops them all; only the first SIGTERM a
    process gets acts, later ones are ignored. With no bot running here (the
    main process waiting for its bot processes) it raises `KeyboardInterrupt`,
    and multiprocessing then waits for the bot processes to finish stopping.
    """

    def on_sigterm(signum: int, _frame: object) -> None:
        if _terminated:
            return  # already stopping; SIGKILL is the way to force it
        _terminated.add(signum)
        for child in _children:
            if child.is_alive() and child.pid is not None:
                os.kill(child.pid, signal.SIGTERM)
        bot = _running[-1] if _running else None
        if bot is not None and not bot.done():
            bot.get_loop().call_soon_threadsafe(bot.cancel)
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)


def _run_bot(config_path: str, positions_file: str | None) -> bool:
    """`asyncio.run(start_bot(...))`, cancellable by SIGTERM.

    Raises:
        KeyboardInterrupt: Ctrl+C or SIGTERM stopped the bot
    """

    async def run() -> bool:
        _running.append(asyncio.current_task())
        try:
            return await start_bot(config_path, positions_file)
        finally:
            _running.pop()

    try:
        return asyncio.run(run())
    except asyncio.CancelledError:
        if _terminated:
            raise KeyboardInterrupt from None
        raise


def _interrupted_status() -> int:
    """143 when SIGTERM stopped the process, 130 for Ctrl+C."""
    return EXIT_TERMINATED if _terminated else EXIT_INTERRUPTED


USAGE = """\
Run every bot configured in bots/*.yaml under the current folder.
A bot whose YAML says `enabled: false` is skipped. --config <file> runs only
the bot in that file, also one that says `enabled: false`.

Each bot writes its log to logs/, its trades to trades/ and its open
positions to positions/, all under the current folder.

On a terminal each line is short and coloured, and only events print: a coin
found, a buy, a sale, its profit or loss, a skip, a warning. --verbose prints
every line, the detail dimmed. logs/ always gets every line, in full.

Exit status: 0 when every enabled bot started and finished without an error,
1 when a config could not be loaded, a bot could not start or stopped on an
error, or no bot is enabled, 130 when stopped with Ctrl+C, 143 when stopped
with SIGTERM. Both stop the bots the same way."""


def _log_file(stem: str) -> str:
    """`logs/<stem>_<timestamp>.log`, creating `logs/`."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(log_dir / f"{stem}_{timestamp}.log")


def setup_logging(bot_name: str) -> logging.Handler | None:
    """Set up logging to file for a specific bot instance.

    Returns:
        The bot's file handler, for `start_bot` to remove when the bot ends
    """
    return setup_file_logging(_log_file(bot_name))


def mask_endpoints(cfg: dict) -> None:
    """Mask the config's endpoints, their hosts and the Geyser token in logs.

    The environment variables the configs ship with are masked already; this
    covers an endpoint written into the YAML or read from another variable.
    """
    geyser = cfg.get("geyser") or {}
    for endpoint in (cfg.get("rpc_endpoint"), cfg.get("wss_endpoint")):
        if isinstance(endpoint, str):
            register_endpoint(endpoint)
    if isinstance(geyser.get("endpoint"), str):
        register_endpoint(geyser["endpoint"])
    if isinstance(geyser.get("api_token"), str):
        register_secret(geyser["api_token"])


def positions_file_for(name: str | None, config_file: str | None = None) -> str | None:
    """`positions/<name>.json`, or `<name>@<config_file>.json`; None without a name.

    Characters other than letters, digits, `.`, `_` and `-` become `_`, so a
    name such as `../x` stays a single file inside `positions/`, and no bot's
    own file can be another's `<name>@<config_file>.json`.
    """
    if not name:
        return None
    parts = [name] if config_file is None else [name, config_file]
    stem = "@".join(re.sub(r"[^A-Za-z0-9._-]", "_", str(part)) for part in parts)
    return f"positions/{stem}.json"


def build_trader_settings(
    cfg: dict, platform: Platform, positions_file: str | None = None
) -> TraderSettings:
    """Turn a loaded bot config into the trader's settings, filling in defaults.

    `positions_file` overrides the one named after the bot.
    """
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
        min_wallet_balance=cfg["trade"].get("min_wallet_balance"),
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
        # On unless explicitly false: a missing key or null sends closes with it.
        cleanup_with_priority_fee=cfg.get("cleanup", {}).get("with_priority_fee")
        is not False,
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
        positions_file=positions_file or positions_file_for(cfg.get("name")),
    )


def unlock_keystore(cfg: dict) -> None:
    """Decrypt the config's `keystore` into `cfg["private_key"]`.

    The password is `KEYSTORE_PASSWORD`, else `PUMPFUN_PASSWORD`, read after
    `load_bot_config` loaded the bot's `env_file`; both are tried, since every
    bot's `env_file` loads into one environment. The decrypted key is masked in
    every later log line.

    Raises:
        KeystoreError: The keystore cannot be unlocked; the message says why
    """
    path = Path(cfg["keystore"])
    keypair, variable = unlock_from_env(path)
    private_key = base58.b58encode(bytes(keypair)).decode()
    register_secret(private_key)
    cfg["private_key"] = private_key
    logger.info(
        f"Wallet {keypair.pubkey()} unlocked from keystore {path} "
        f"(password from {variable})"
    )


def log_private_key_wallet(cfg: dict) -> None:
    """Log the address of the wallet a `private_key` config trades from.

    A key that does not decode is left to the trader, which refuses it.
    """
    try:
        pubkey = Keypair.from_base58_string(cfg["private_key"]).pubkey()
    except (ValueError, TypeError, KeyError):
        return
    logger.info(f"Wallet {pubkey} from private_key")


async def start_bot(config_path: str, positions_file: str | None = None) -> bool:
    """Start the bot configured in `config_path` and run it until it ends.

    A bot that cannot run (config, platform, keystore, `rpc_endpoint`) logs one
    line saying why and is not started. Nothing raises out of here but a
    cancellation: an error is logged, masked, and reported as False.

    Returns:
        True when the bot started and ended normally; False when it was not
        started or its trader stopped on an error
    """
    try:
        cfg = load_bot_config(config_path)
    except Exception as e:  # noqa: BLE001 - any cause gets the same one line
        logger.error(f"Could not load {config_path}: {_one_line(e)}")  # noqa: TRY400
        return False
    mask_endpoints(cfg)
    console.set_bot_name(cfg["name"])
    handler = setup_logging(cfg["name"])
    try:
        return await _start_loaded(cfg, positions_file)
    finally:
        # Bots in the main process run one after another: the next one's lines
        # go to its own file, not this one's too.
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


async def _start_loaded(cfg: dict, positions_file: str | None) -> bool:
    """`start_bot` once the config has loaded and the bot's log file is set up."""
    print_config_summary(cfg)

    try:
        platform = _startable_platform(cfg)
    except Exception as e:  # noqa: BLE001 - any cause gets the same one line
        logger.error(f"Bot '{cfg['name']}' not started: {_one_line(e)}")  # noqa: TRY400
        return False

    if "keystore" in cfg:
        try:
            unlock_keystore(cfg)
        except KeystoreError as e:
            logger.error(f"Bot '{cfg['name']}' not started: {e}")  # noqa: TRY400
            return False
    else:
        log_private_key_wallet(cfg)

    # Checked here, before the trader starts listening: a bot that cannot reach
    # its node would detect coins and fail every buy.
    problem = await rpc_endpoint_problem(cfg["rpc_endpoint"])
    if problem:
        logger.error(f"Bot '{cfg['name']}' not started: rpc_endpoint {problem}")
        return False

    # Initialize universal trader with platform-specific configuration
    try:
        trader = UniversalTrader(build_trader_settings(cfg, platform, positions_file))

        await trader.start()

    except Exception as e:
        # Logged here, masked, and not re-raised: in its own process the error
        # would reach multiprocessing, which prints it raw.
        logger.exception(f"Failed to initialize or start trader: {e}")
        return False
    return not trader.stopped_on_error


def run_bot_process(config_path, positions_file: str | None = None):
    """A bot's own process: exit 1 unless it ran normally, 130 on Ctrl+C.

    143 on SIGTERM. Nothing may escape: multiprocessing prints an uncaught
    error unmasked.
    """
    # A forked bot process starts with copies of its parent's lists.
    _children.clear()
    _terminated.clear()
    _stop_on_sigterm()
    try:
        ran = _run_bot(config_path, positions_file)
    except KeyboardInterrupt:
        sys.exit(_interrupted_status())
    except Exception:
        # Logged masked: multiprocessing would print it raw.
        logger.exception(f"Bot process for {config_path} stopped on an error")
        sys.exit(1)
    if not ran:
        sys.exit(1)


def _one_line(error: Exception) -> str:
    """An exception's message on one line, or its type when it has none."""
    return " ".join(str(error).split()) or type(error).__name__


def _startable_platform(cfg: dict) -> Platform:
    """The bot's platform, checked to be supported and to take its listener.

    Raises:
        ValueError: The platform is unknown, unsupported or lacks the listener
    """
    platform = get_platform_from_config(cfg)

    from platforms import platform_factory

    if not platform_factory.registry.is_platform_supported(platform):
        available = [p.value for p in platform_factory.get_supported_platforms()]
        msg = f"platform {platform.value} is not supported; available: {available}"
        raise ValueError(msg)

    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        supported = get_supported_listeners_for_platform(platform)
        msg = (
            f"listener '{listener_type}' is not compatible with platform "
            f"'{platform.value}'; supported: {supported}"
        )
        raise ValueError(msg)
    return platform


def _log_config_error(file: Path, error: Exception) -> None:
    """Log one line naming a config that cannot run, also into `logs/`.

    The bot has no log file of its own yet, so the line gets one named after
    the config file: `logs/<file stem>_<timestamp>.log`.
    """
    handler = file_handler(_log_file(file.stem))
    logger.addHandler(handler)
    try:
        logger.error(f"Could not load {file}: {_one_line(error)}")
    finally:
        logger.removeHandler(handler)
        handler.close()


def _shared_positions_files(bots: list[tuple[Path, dict]]) -> dict[Path, str]:
    """A positions file of its own for each starting bot whose name another uses.

    The name keys a bot's positions file, and two bots writing one file would
    overwrite each other's open positions. Files are compared ignoring case, as
    on macOS and Windows. Of the bots sharing a file, the one whose config file
    sorts first keeps it; each other still starts, with its config file's name
    added: `positions/<name>@<config file>.json`, then `-2`, `-3`, ... if that
    is taken too.
    """
    taken: dict[str, Path] = {}  # casefolded positions file -> config file
    own = {}
    for file, cfg in sorted(bots, key=lambda bot: str(bot[0])):
        name = cfg.get("name")
        plain = positions_file_for(name)
        if plain is None:
            continue
        if plain.casefold() not in taken:
            taken[plain.casefold()] = file
            continue
        path, n = positions_file_for(name, file.stem), 1
        while path.casefold() in taken:
            n += 1
            path = positions_file_for(name, f"{file.stem}-{n}")
        taken[path.casefold()] = file
        own[file] = path
        logging.warning(
            f"Bot '{name}' in {file} would share {plain} with "
            f"{taken[plain.casefold()]}; its open positions are kept in {path} instead"
        )
        if holds_positions(plain):
            logging.warning(
                f"{plain} holds open positions that bot '{name}' in {file} does "
                f"not manage: it keeps its own in {path}"
            )
    return own


def run_all_bots(config: str | None = None) -> bool:
    """Run all bots defined in YAML files in the 'bots' directory.

    Args:
        config: Run only the bot in this file, whatever its `enabled` says

    Returns:
        True only when every config loaded and every enabled bot, at least
        one, started and ended normally
    """
    bot_dir = Path("bots")
    if config is not None:
        bot_files = [Path(config)]
        if not bot_files[0].is_file():
            logger.error(f"Config file {config} not found")
            return False
    elif not bot_dir.exists():
        logging.error(f"Bot directory '{bot_dir}' not found")
        return False
    else:
        bot_files = sorted(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error(f"No bot configuration files found in '{bot_dir}'")
        return False

    logging.info(f"Found {len(bot_files)} bot configuration files")

    processes = []
    skipped_bots = 0
    disabled = 0
    all_ok = True

    # Check every file first, so bots sharing a name are known before any starts.
    startable: list[tuple[Path, dict, Platform]] = []
    for file in bot_files:
        if config is None and disabled_in_yaml(str(file)):
            skipped_bots += 1
            disabled += 1
            continue
        try:
            cfg = load_bot_config(str(file))
            mask_endpoints(cfg)
            bot_name = cfg.get("name", file.stem)

            # Skip bots with enabled=False
            if config is None and not cfg.get("enabled", True):
                logging.info(f"Skipping disabled bot '{bot_name}'")
                skipped_bots += 1
                disabled += 1
                continue

            startable.append((file, cfg, _startable_platform(cfg)))

        except Exception as e:  # noqa: BLE001 - any cause gets the same one line
            _log_config_error(file, e)
            skipped_bots += 1
            all_ok = False

    if not startable:
        invalid = len(bot_files) - disabled
        logger.error(
            f"No bot to run in {config or f'{bot_dir}/'}: {len(bot_files)} found, "
            f"{disabled} disabled"
            + (f", {invalid} could not be loaded" if invalid else "")
        )
        return False

    positions_files = _shared_positions_files([(f, cfg) for f, cfg, _ in startable])

    for file, cfg, platform in startable:
        bot_name = cfg.get("name", file.stem)
        positions_file = positions_files.get(file)
        try:
            # Start bot in separate process or main process
            if cfg.get("separate_process", False):
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in separate process"
                )
                p = multiprocessing.Process(
                    target=run_bot_process,
                    args=(str(file), positions_file),
                    name=f"bot-{bot_name}",
                )
                p.start()
                processes.append(p)
                _children.append(p)
            else:
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in main process"
                )
                if not _run_bot(str(file), positions_file):
                    all_ok = False

        except Exception as e:
            logging.exception(f"Failed to start bot from {file}: {e}")
            skipped_bots += 1
            all_ok = False

    logging.info(
        f"Launched {len(bot_files) - skipped_bots} bots, skipped {skipped_bots} disabled/invalid bots"
    )

    # Wait for all processes to complete
    for p in processes:
        p.join()
        logging.info(f"Process {p.name} completed")
        if p.exitcode != 0:
            all_ok = False
    return all_ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command line: `-h`/`--help` prints `USAGE` and exits 0.

    Any argument other than help, `--verbose` and `--config FILE` is a usage
    error (exit 2), so a mistyped flag never starts a bot.
    """
    parser = argparse.ArgumentParser(
        description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print every log line on the terminal, not only events",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        help="run only the bot configured in FILE",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run every enabled bot; exit 0 only when all of them ran normally."""
    args = parse_args(argv)
    _stop_on_sigterm()
    # `force`: a line logged at import (the event loop policy above) has
    # already given the root logger Python's default handler. The root stays
    # at WARNING: the bot's modules log INFO through their own loggers
    # (`get_logger`), while the runner's own INFO lines and the HTTP clients'
    # request URLs, which carry the provider API key, stay off the terminal.
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[console.console_handler(verbose=args.verbose)],
        force=True,
    )
    # Redact before the first line is emitted, not after.
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

    try:
        all_ok = run_all_bots(args.config)
    except KeyboardInterrupt:
        # The trader already shut down cleanly; a traceback would add nothing.
        sys.exit(_interrupted_status())
    except Exception:
        # Logged masked: Python would print it raw.
        logger.exception("The bot runner stopped on an error")
        all_ok = False
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

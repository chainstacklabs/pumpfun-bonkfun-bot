import argparse
import asyncio
import logging
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
from core.execution_policy import (
    ExecutionBlocked,
    ExecutionMode,
    ExecutionPolicy,
)
from utils.logger import setup_file_logging

PROCESS_POLL_INTERVAL_SECONDS = 0.2
PROCESS_SHUTDOWN_GRACE_SECONDS = 5.0
PROCESS_TERMINATE_GRACE_SECONDS = 2.0
PROCESS_KILL_GRACE_SECONDS = 1.0


def setup_logging(bot_name: str) -> None:
    """Set up logging to file for a specific bot instance."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"{bot_name}_{timestamp}.log"

    setup_file_logging(str(log_filename))


def build_execution_policy(
    config: dict,
    *,
    authorize_live: bool = False,
) -> ExecutionPolicy:
    """Build a policy and require an explicit runtime grant for live mode."""
    policy = ExecutionPolicy.from_config(config)
    if policy.mode is ExecutionMode.LIVE and not authorize_live:
        raise ExecutionBlocked(
            "Live execution requires explicit runtime authorization "
            "with --authorize-live"
        )
    if authorize_live:
        policy = policy.authorize_live()
    return policy


async def start_bot(
    config_path: str | Path,
    *,
    authorize_live: bool = False,
) -> None:
    """Start one validated bot, failing before signer creation when unauthorized."""
    cfg = load_bot_config(config_path)
    if not cfg["enabled"]:
        raise RuntimeError(f"Bot '{cfg['name']}' is disabled")
    policy = build_execution_policy(cfg, authorize_live=authorize_live)
    setup_logging(cfg["name"])
    print_config_summary(cfg)

    platform = get_platform_from_config(cfg)
    logging.info("Detected platform: %s", platform.value)

    from platforms import platform_factory

    if not platform_factory.registry.is_platform_supported(platform):
        raise ValueError(
            f"Platform {platform.value} is not supported. Available platforms: "
            f"{[p.value for p in platform_factory.get_supported_platforms()]}"
        )

    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        from config_loader import get_supported_listeners_for_platform

        supported = get_supported_listeners_for_platform(platform)
        raise ValueError(
            f"Listener '{listener_type}' is not compatible with platform "
            f"'{platform.value}'. Supported listeners: {supported}"
        )

    # Initialize universal trader with platform-specific configuration
    from trading.universal_trader import (
        DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        UniversalTrader,
    )

    try:
        trader = UniversalTrader(
            # Connection settings
            rpc_endpoint=cfg["rpc_endpoint"],
            wss_endpoint=cfg["wss_endpoint"],
            private_key=cfg["private_key"],
            # Platform configuration - pass platform enum directly
            platform=platform,
            # Trade parameters
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
            # Exit strategy configuration
            exit_strategy=cfg["trade"].get("exit_strategy", "time_based"),
            take_profit_percentage=cfg["trade"].get("take_profit_percentage"),
            stop_loss_percentage=cfg["trade"].get("stop_loss_percentage"),
            max_hold_time=cfg["trade"].get("max_hold_time"),
            price_check_interval=cfg["trade"].get("price_check_interval", 10),
            max_exit_sell_attempts=cfg["trade"].get(
                "max_exit_sell_attempts", DEFAULT_MAX_EXIT_SELL_ATTEMPTS
            ),
            # Listener configuration
            listener_type=cfg["filters"]["listener_type"],
            # Geyser configuration (if applicable)
            geyser_endpoint=cfg.get("geyser", {}).get("endpoint"),
            geyser_api_token=cfg.get("geyser", {}).get("api_token"),
            geyser_auth_type=cfg.get("geyser", {}).get("auth_type", "x-token"),
            # PumpPortal configuration (if applicable)
            pumpportal_url=cfg.get("pumpportal", {}).get(
                "url", "wss://pumpportal.fun/api/data"
            ),
            # Priority fee configuration
            enable_dynamic_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_dynamic", False
            ),
            enable_fixed_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_fixed", True
            ),
            fixed_priority_fee=cfg.get("priority_fees", {}).get("fixed_amount", 500000),
            extra_priority_fee=cfg.get("priority_fees", {}).get(
                "extra_percentage", 0.0
            ),
            hard_cap_prior_fee=cfg.get("priority_fees", {}).get("hard_cap", 500000),
            # Retry and timeout settings
            max_retries=cfg.get("retries", {}).get("max_attempts", 1),
            wait_time_after_creation=cfg.get("retries", {}).get(
                "wait_after_creation", 15
            ),
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
            execution_policy=policy,
        )

        await trader.start()

    except Exception as e:
        logging.exception(f"Failed to initialize or start trader: {e}")
        raise


async def _run_bot_process(
    config_path: str | Path,
    *,
    authorize_live: bool = False,
) -> int | None:
    """Run one bot and translate child signals into cancellable shutdown."""
    shutdown_signal: list[int | None] = [None]
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Bot process has no active asyncio task")
    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        if shutdown_signal[0] is None:
            shutdown_signal[0] = signum
        loop.call_soon_threadsafe(task.cancel)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        await start_bot(config_path, authorize_live=authorize_live)
    except asyncio.CancelledError:
        if shutdown_signal[0] is None:
            raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return shutdown_signal[0]


def run_bot_process(config_path: str | Path) -> None:
    """Run a bot in a child process with nonzero signal termination status."""
    shutdown_signal = asyncio.run(_run_bot_process(config_path))
    if shutdown_signal is not None:
        raise SystemExit(128 + shutdown_signal)


def _alive_processes(
    processes: list[tuple[multiprocessing.Process, str]],
) -> list[tuple[multiprocessing.Process, str]]:
    return [(process, name) for process, name in processes if process.is_alive()]


def _join_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    timeout: float,
) -> list[tuple[multiprocessing.Process, str]]:
    """Join children only in bounded increments and return any survivors."""
    deadline = time.monotonic() + max(timeout, 0.0)
    alive = _alive_processes(processes)
    while alive:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for process, _ in alive:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            process.join(timeout=min(PROCESS_POLL_INTERVAL_SECONDS, remaining))
        alive = _alive_processes(processes)

    for process, _ in processes:
        if not process.is_alive():
            process.join(timeout=0)
    return _alive_processes(processes)


def _forward_signal(
    processes: list[tuple[multiprocessing.Process, str]],
    signum: int,
) -> None:
    for process, bot_name in _alive_processes(processes):
        pid = process.pid
        if pid is None:
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        except OSError:
            logging.exception(
                "Failed to propagate signal %s to process %s for bot '%s'",
                signum,
                process.name,
                bot_name,
            )


def _stop_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    signum: int,
) -> list[tuple[multiprocessing.Process, str]]:
    """Request shutdown, then terminate and kill any stubborn children."""
    _forward_signal(processes, signum)
    alive = _join_processes(processes, PROCESS_SHUTDOWN_GRACE_SECONDS)
    for process, bot_name in alive:
        logging.warning(
            "Terminating unresponsive process %s for bot '%s'",
            process.name,
            bot_name,
        )
        try:
            process.terminate()
        except ProcessLookupError:
            continue

    alive = _join_processes(processes, PROCESS_TERMINATE_GRACE_SECONDS)
    for process, bot_name in alive:
        logging.error(
            "Killing unresponsive process %s for bot '%s'",
            process.name,
            bot_name,
        )
        try:
            process.kill()
        except ProcessLookupError:
            continue

    return _join_processes(processes, PROCESS_KILL_GRACE_SECONDS)


@contextmanager
def _supervisor_signal_handlers(
    processes: list[tuple[multiprocessing.Process, str]],
) -> Iterator[list[int | None]]:
    shutdown_signal: list[int | None] = [None]
    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        if shutdown_signal[0] is None:
            shutdown_signal[0] = signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        yield shutdown_signal
    finally:
        alive = _alive_processes(processes)
        if alive:
            cleanup_signal = shutdown_signal[0] or signal.SIGTERM
            survivors = _stop_processes(processes, cleanup_signal)
            for process, bot_name in survivors:
                logging.critical(
                    "Process %s for bot '%s' survived kill escalation",
                    process.name,
                    bot_name,
                )
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _supervise_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    shutdown_signal: list[int | None],
) -> int:
    """Watch children and fail closed on signals or any child failure."""
    observed: set[int] = set()
    child_failed = False

    while True:
        for process, bot_name in processes:
            if process.exitcode is None or id(process) in observed:
                continue
            observed.add(id(process))
            logging.info(
                "Process %s for bot '%s' exited with status %s",
                process.name,
                bot_name,
                process.exitcode,
            )
            if process.exitcode != 0:
                child_failed = True

        alive = _alive_processes(processes)
        if shutdown_signal[0] is not None or child_failed:
            signum = shutdown_signal[0] or signal.SIGTERM
            survivors = _stop_processes(processes, signum)
            for process, bot_name in survivors:
                logging.critical(
                    "Process %s for bot '%s' survived kill escalation",
                    process.name,
                    bot_name,
                )
            break
        if not alive:
            break

        _join_processes(processes, PROCESS_POLL_INTERVAL_SECONDS)

    if shutdown_signal[0] is not None:
        return 128 + shutdown_signal[0]
    return 1 if child_failed else 0


def run_all_bots() -> int:
    """Run enabled non-live bots and return a process-style exit status."""
    bot_dir = Path("bots")
    if not bot_dir.exists():
        logging.error("Bot directory '%s' not found", bot_dir)
        return 1

    bot_files = sorted(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error("No bot configuration files found in '%s'", bot_dir)
        return 1

    logging.info("Found %d bot configuration files", len(bot_files))
    processes: list[tuple[multiprocessing.Process, str]] = []
    disabled_count = 0
    started_count = 0
    failure_count = 0
    supervisor_status = 0

    with _supervisor_signal_handlers(processes) as shutdown_signal:
        for config_file in bot_files:
            if shutdown_signal[0] is not None:
                break
            try:
                cfg = load_bot_config(config_file)
                bot_name = cfg["name"]
                if not cfg["enabled"]:
                    logging.info("Skipping disabled bot '%s'", bot_name)
                    disabled_count += 1
                    continue

                # Bulk startup intentionally has no live-authorization path. A live
                # bot must be selected explicitly with --config --authorize-live.
                build_execution_policy(cfg, authorize_live=False)
                platform = get_platform_from_config(cfg)

                if cfg.get("separate_process", False):
                    process = multiprocessing.Process(
                        target=run_bot_process,
                        args=(config_file,),
                        name=f"bot-{bot_name}",
                    )
                    process.start()
                    processes.append((process, bot_name))
                    started_count += 1
                    logging.info(
                        "Started bot '%s' (%s) in process %s",
                        bot_name,
                        platform.value,
                        process.name,
                    )
                else:
                    logging.info(
                        "Starting bot '%s' (%s) in the main process",
                        bot_name,
                        platform.value,
                    )
                    main_signal = asyncio.run(_run_bot_process(config_file))
                    if main_signal is not None:
                        shutdown_signal[0] = main_signal
                        break
                    started_count += 1
            except Exception:
                failure_count += 1
                logging.exception("Failed to start bot from %s", config_file)

        supervisor_status = _supervise_processes(processes, shutdown_signal)
        if supervisor_status != 0:
            failure_count += 1

    logging.info(
        "Bot run summary: started=%d disabled=%d failed=%d",
        started_count,
        disabled_count,
        failure_count,
    )
    if supervisor_status >= 128:
        return supervisor_status
    return 1 if failure_count else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the safe runner command line."""
    parser = argparse.ArgumentParser(description="Run configured trading bots")
    parser.add_argument(
        "--config",
        type=Path,
        help="Run exactly one bot configuration instead of scanning bots/",
    )
    parser.add_argument(
        "--authorize-live",
        action="store_true",
        help=(
            "Explicitly authorize live transaction submission for the single "
            "configuration selected with --config"
        ),
    )
    args = parser.parse_args(argv)
    if args.authorize_live and args.config is None:
        parser.error("--authorize-live requires an explicit --config path")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = parse_args(argv)

    try:
        if args.config is not None:
            shutdown_signal = asyncio.run(
                _run_bot_process(
                    args.config,
                    authorize_live=args.authorize_live,
                )
            )
            return 128 + shutdown_signal if shutdown_signal is not None else 0
        return run_all_bots()
    except Exception:
        logging.exception("Bot runner stopped before successful startup")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

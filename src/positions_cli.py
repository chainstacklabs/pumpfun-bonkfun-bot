"""`pump_bot_positions`: see what a bot holds, or sell all of it.

    pump_bot_positions status <bot yaml> [--positions FILE]
    pump_bot_positions sell-all <bot yaml> [--positions FILE] [--yes]

The bot YAML gives the RPC endpoint, the wallet, the sell slippage, the
priority fee and the positions file (`positions/<name>.json`, from the folder
`pump_bot` runs in). `status` only reads, so it runs while the bot does.
`sell-all` refuses to run while the bot or another `sell-all` uses the file.
It sells through the bot's own seller, closes each emptied account, and burns
nothing: what it can't sell stays in the wallet and the file, marked given up.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from bot_runner import (
    _one_line,
    build_trader_settings,
    mask_endpoints,
    positions_file_for,
    unlock_keystore,
)
from cleanup.manager import AccountCleanupManager
from config_loader import get_platform_from_config, load_bot_config
from core.client import SolanaClient
from core.keystore import KeystoreError
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    normalize_quote_mint,
    quote_symbol,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from platforms import get_platform_implementations
from trading import trade_log
from trading.platform_aware import PlatformAwareSeller
from trading.position_store import PositionStore, StoredPosition
from trading.pricing import read_price
from trading.run_lock import RunLock
from utils import console
from utils.logger import get_logger, install_secret_redaction

logger = get_logger(__name__)


@dataclass
class Tools:
    """What the commands read and sell with, built from the bot YAML."""

    client: Any
    wallet: Any
    seller: Any
    implementations: Any
    cleanup: Any


def resolve_positions_file(config_path: Path, cfg: dict, override: str | None) -> Path:
    """The positions file the bot in `config_path` uses.

    Raises:
        SystemExit: Another YAML in the same folder has the same name, ignoring
            case, so which file this bot gets depends on how bots are started
    """
    if override:
        return Path(override)
    name = str(cfg.get("name", ""))
    for other in sorted(config_path.parent.glob("*.yaml")):
        if other.resolve() == config_path.resolve():
            continue
        try:
            other_name = (yaml.safe_load(other.read_text()) or {}).get("name")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if str(other_name).casefold() == name.casefold():
            sys.exit(
                f"Another bot in {config_path.parent} ({other.name}) is named "
                f"{name}; pass --positions <file>"
            )
    return Path(positions_file_for(name))


def _ours(stored: StoredPosition, tools: Tools) -> bool:
    return stored.wallet is None or stored.wallet == str(tools.wallet.pubkey)


def _flag(stored: StoredPosition, tools: Tools) -> str:
    if not _ours(stored, tools):
        return "other wallet"
    if stored.pending is not None:
        return "pending"
    if stored.sold:
        return "sold, closing"
    if stored.given_up:
        return f"given up: {stored.given_up}"
    return ""


async def _priced(stored: StoredPosition, tools: Tools) -> tuple[float, str] | None:
    """The coin's price and venue now; None when it can't be read."""
    try:
        price, market = await read_price(stored.token_info, tools.implementations)
    except Exception:  # noqa: BLE001 - shown as n/a; the error may name the endpoint
        return None
    if price <= 0:
        return None
    return price, market.name


async def status(positions: Path, tools: Tools, out: TextIO) -> int:
    """Print each position in the file with its value now; reads only.

    Returns:
        0, or 1 when the file can't be read
    """
    try:
        stored = PositionStore.peek(positions)
    except (OSError, ValueError, KeyError, TypeError):
        print(f"Positions file {positions} can't be read", file=out)
        return 1
    if not stored:
        print(f"No positions in {positions}", file=out)
        return 0
    print(
        f"{'symbol':<10} {'mint':<44} {'venue':<14} {'held':>14} {'entry':>12} "
        f"{'now':>12} {'unrealised':>22}  flag",
        file=out,
    )
    for position in stored:
        coin, buy = position.token_info, position.buy_result
        label = quote_symbol(normalize_quote_mint(coin.quote_mint))
        priced = await _priced(position, tools)
        entry = f"{buy.price:.4e}" if buy.price else "n/a"
        if priced is None:
            venue, now, pnl = "n/a", "n/a", "n/a"
        else:
            price, venue = priced
            now = f"{price:.4e}"
            if buy.price:
                change = (price - buy.price) * buy.amount
                pnl = f"{change:+.6f} {label} {price / buy.price - 1:+.0%}"
            else:
                pnl = "n/a"
        print(
            f"{coin.symbol[:10]:<10} {coin.mint!s:<44} {venue:<14} "
            f"{buy.amount:>14.2f} {entry:>12} {now:>12} {pnl:>22}  "
            f"{_flag(position, tools)}",
            file=out,
        )
    return 0


async def _sell_one(
    position: StoredPosition, store: PositionStore, tools: Tools, out: TextIO
) -> tuple[bool, bool]:
    """Sell one position; (all of it gone from the wallet, its account to close)."""
    coin = position.token_info
    label = f"{coin.symbol} ({coin.mint})"
    account = tools.wallet.get_associated_token_address(
        coin.mint, coin.token_program_id
    )
    try:
        held = await tools.client.get_token_account_balance(
            account, commitment="confirmed"
        )
    except Exception as e:  # noqa: BLE001 - the message may carry the endpoint
        print(f"{label}: balance unreadable ({type(e).__name__}); kept", file=out)
        return False, False
    if held == 0:
        print(f"{label}: the wallet holds none; dropped from the file", file=out)
        store.remove(coin.mint)
        return True, False
    priced = await _priced(position, tools)
    if priced is None:
        store.mark_given_up(coin.mint, "sell-all could not price it")
        print(f"{label}: no price to sell it at; kept", file=out)
        return False, False
    price, _venue = priced
    try:
        quote_mint = normalize_quote_mint(coin.quote_mint)
        get_account = getattr(tools.client, "get_account_info", None)
        if get_account is not None:
            await resolve_quote_token_program(quote_mint, get_account)
        result = await tools.seller.execute(
            coin,
            token_amount=held / 10**TOKEN_DECIMALS,
            token_price=price,
            token_amount_raw=held,
        )
    except Exception as e:  # noqa: BLE001 - the message may carry the endpoint
        result = None
        reason = type(e).__name__
    else:
        # The reason, not the message: a message can quote an RPC error.
        reason = result.failure_reason.value if result.failure_reason else "failed"
    if result is None or not result.success:
        store.mark_given_up(coin.mint, "sell-all could not sell it")
        print(f"{label}: sale failed ({reason}); kept", file=out)
        return False, False
    sold = result.amount_raw if result.amount_raw is not None else held
    trade_log.log_trade(
        "sell",
        coin,
        result.price,
        result.amount,
        result.tx_signature,
        venue=result.venue,
    )
    left = held - sold
    if left > 0:
        store.reduce(coin.mint, left / 10**TOKEN_DECIMALS, left, sold=True)
        store.mark_given_up(coin.mint, "sell-all could not sell all of it")
        print(
            f"{label}: sold {sold / 10**TOKEN_DECIMALS:g}, the curve could not "
            f"pay for the rest",
            file=out,
        )
        return False, False
    store.remove(coin.mint)
    print(
        f"{label}: sold {sold / 10**TOKEN_DECIMALS:g} ({result.tx_signature})", file=out
    )
    return True, True


def _print_left(positions: Path, mints: set, tools: Tools, out: TextIO) -> None:
    print("Left in the wallet:", file=out)
    for position in PositionStore.peek(positions):
        coin = position.token_info
        if coin.mint in mints:
            print(
                f"  {coin.symbol} ({coin.mint}): {position.buy_result.amount:g} "
                f"{_flag(position, tools) or 'kept'}",
                file=out,
            )


async def _sell_listed(
    store: PositionStore,
    tools: Tools,
    *,
    yes: bool,
    ask: Callable[[str], str],
    out: TextIO,
) -> int:
    stored = store.load()
    to_sell = [
        p for p in stored if _ours(p, tools) and p.pending is None and not p.sold
    ]
    left = {p.token_info.mint for p in stored if p not in to_sell}
    if not to_sell:
        print(f"Nothing to sell in {store.path}", file=out)
        return 1 if left else 0
    for position in to_sell:
        coin = position.token_info
        print(
            f"  {coin.symbol} ({coin.mint}): {position.buy_result.amount:g}", file=out
        )
    answer = "y" if yes else ask(f"Sell all {len(to_sell)} positions? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("Nothing sold", file=out)
        return 0
    closes = []
    for position in to_sell:
        gone, close = await _sell_one(position, store, tools, out)
        if not gone:
            left.add(position.token_info.mint)
        if close:
            closes.append(position.token_info)
    # One settle wait before the first close covers the sales before it.
    for i, coin in enumerate(closes):
        await tools.cleanup.cleanup_ata(coin.mint, coin.token_program_id, settle=i == 0)
    if left:
        _print_left(store.path, left, tools, out)
        return 1
    return 0


async def sell_all(
    positions: Path,
    tools: Tools,
    *,
    yes: bool,
    ask: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
) -> int:
    """Sell every position in the file this wallet holds; burns nothing.

    Returns:
        0 when nothing is left to sell (or the answer was no), else 1
    """
    lock = RunLock(positions)
    if not lock.acquire():
        print(f"Positions file {positions} is in use; stop the bot first", file=out)
        return 1
    try:
        return await _sell_listed(
            PositionStore(positions), tools, yes=yes, ask=ask, out=out
        )
    finally:
        lock.release()


async def _tools(cfg: dict) -> Tools:
    settings = build_trader_settings(cfg, get_platform_from_config(cfg))
    client = SolanaClient(settings.rpc_endpoint, max_rps=settings.max_rps)
    wallet = Wallet(settings.private_key)
    fees = PriorityFeeManager(
        client=client,
        enable_dynamic_fee=settings.enable_dynamic_priority_fee,
        enable_fixed_fee=settings.enable_fixed_priority_fee,
        fixed_fee=settings.fixed_priority_fee,
        extra_fee=settings.extra_priority_fee,
        hard_cap=settings.hard_cap_prior_fee,
    )
    return Tools(
        client=client,
        wallet=wallet,
        seller=PlatformAwareSeller(
            client,
            wallet,
            fees,
            settings.sell_slippage,
            settings.max_retries,
            compute_units=settings.compute_units or {},
        ),
        implementations=get_platform_implementations(
            get_platform_from_config(cfg), client
        ),
        cleanup=AccountCleanupManager(
            client, wallet, fees, settings.cleanup_with_priority_fee, force_burn=False
        ),
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pump_bot_positions",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("status", "list the positions and what they are worth now"),
        ("sell-all", "sell every position this wallet holds; burns nothing"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("config", help="the bot's YAML file")
        command.add_argument(
            "--positions", metavar="FILE", help="the positions file, if not the bot's"
        )
        if name == "sell-all":
            command.add_argument(
                "--yes", action="store_true", help="sell without asking first"
            )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    try:
        cfg = load_bot_config(str(config_path))
    except Exception as e:  # noqa: BLE001 - any cause gets one line
        # Logged, not printed: the log redacts endpoints and keys.
        logger.error(f"Could not load {config_path}: {_one_line(e)}")  # noqa: TRY400
        return 1
    mask_endpoints(cfg)
    positions = resolve_positions_file(config_path, cfg, args.positions)
    if "keystore" in cfg:
        try:
            unlock_keystore(cfg)
        except KeystoreError as e:
            logger.error(f"Could not unlock the wallet: {e}")  # noqa: TRY400
            return 1
    tools = await _tools(cfg)
    try:
        if args.command == "status":
            return await status(positions, tools, sys.stdout)
        return await sell_all(positions, tools, yes=args.yes)
    finally:
        await tools.client.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, handlers=[console.console_handler()], force=True
    )
    install_secret_redaction()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

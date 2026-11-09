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
import dataclasses
import functools
import logging
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

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
from core.keystore import KeystoreError, keystore_pubkey
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    SystemAddresses,
    normalize_quote_mint,
    quote_symbol,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from interfaces.core import (
    ConfirmationStatus,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from platforms import get_platform_implementations
from trading import trade_log, unresolved
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareSeller, landed_sale, read_deltas
from trading.pnl import CostBasis, Realised, SalePnl, cost_basis, pnl_text, sale_pnl
from trading.position_store import PositionStore, StoredPosition
from trading.pricing import read_price
from trading.run_lock import RunLock
from trading.unresolved import SALE, Earlier, Unresolved
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
    #: The bot's platform; positions bought on another are left alone.
    platform: Platform = Platform.PUMP_FUN


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


def _other_platform(stored: StoredPosition, tools: Tools) -> bool:
    return stored.token_info.platform != tools.platform


def _flag(stored: StoredPosition, tools: Tools) -> str:
    if not _ours(stored, tools):
        return "other wallet"
    if _other_platform(stored, tools):
        return f"other platform ({stored.token_info.platform.value})"
    if stored.pending is not None:
        return "pending"
    if stored.sold:
        return "sold, closing"
    if stored.given_up:
        return f"given up: {stored.given_up}"
    return ""


async def _priced(stored: StoredPosition, tools: Tools) -> tuple[float, str] | None:
    """The coin's price and venue now; None when it can't be read.

    The quote mint is resolved first: pricing needs its decimals.
    """
    try:
        await resolve_quote_token_program(
            normalize_quote_mint(stored.token_info.quote_mint),
            functools.partial(tools.client.get_account_info, commitment="confirmed"),
        )
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
        priced = (
            None if _other_platform(position, tools) else await _priced(position, tools)
        )
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


#: Seconds between the reads that must agree before a zero balance is believed,
#: and between looks at an unresolved trade.
_RECHECK_SECONDS = 1.0
#: Reads of a zero balance, all of which must find nothing.
_ZERO_READS = 3


def _fresh_from(position: StoredPosition) -> int | None:
    """The slot a balance read must have reached: the position's last trade's."""
    if position.sold:
        return position.sold_slot
    deltas = position.buy_result.deltas
    return deltas.slot if deltas is not None else None


async def _read_held(
    coin: TokenInfo, tools: Tools, *, min_slot: int | None = None
) -> tuple[int, bool, Pubkey]:
    """Raw amount in the wallet's account, whether it exists, and its program.

    Read at "confirmed", from a node at least at `min_slot` when given: a node
    behind it fails the read, which keeps the record. An account that does not
    exist (closed after a sale by hand) holds none. A record with no token
    program takes the mint's owner. Zero is read again, `_ZERO_READS` times in
    all, since a node behind a fresh buy shows none; every read must agree.

    Raises:
        Exception: A read failed; nothing can be said
    """
    at_slot = {} if min_slot is None else {"min_context_slot": min_slot}
    program = coin.token_program_id
    if program is None:
        mint = await tools.client.get_account_info(
            coin.mint, commitment="confirmed", **at_slot
        )
        program = mint.owner
    account = tools.wallet.get_associated_token_address(coin.mint, program)
    for read in range(_ZERO_READS):
        if read:
            await asyncio.sleep(_RECHECK_SECONDS)
        try:
            info = await tools.client.get_account_info(
                account, commitment="confirmed", **at_slot
            )
        except ValueError:
            exists, held = False, 0
        else:
            # SPL token account: mint (32), owner (32), amount (u64).
            exists, held = True, struct.unpack_from("<Q", bytes(info.data), 64)[0]
        if held:
            return held, exists, program
    return 0, exists, program


def _realised(
    position: StoredPosition, sale: TradeResult, price: float, quote_mint: Pubkey
) -> tuple[Realised, SalePnl]:
    """The position's realised PnL with this sale added, and the sale's own.

    The stored total is copied, not changed, so an estimate can be replaced.
    """
    buy = position.buy_result
    cost = cost_basis(buy, quote_mint) or CostBasis(
        buy.price or price, from_chain=False
    )
    pnl = sale_pnl(cost, sale, quote_mint, price)
    realised = (
        dataclasses.replace(position.realised) if position.realised else Realised()
    )
    realised.add(pnl)
    return realised, pnl


async def _settle_earlier(
    position: StoredPosition, store: PositionStore, tools: Tools, out: TextIO
) -> StoredPosition | None:
    """Resolve a sale or burn of the position left unresolved, before selling.

    Returns:
        The position to go on with, net of a sale that landed; None when
        nothing more is done with it this run
    """
    trade = position.unresolved
    if trade is None:
        return position
    coin, buy = position.token_info, position.buy_result
    label = f"{coin.symbol} ({coin.mint})"
    earlier, sold, slot = await unresolved.resolve(
        tools.client,
        tools.wallet.pubkey,
        coin,
        trade,
        looks=unresolved.LOOKS_BEFORE_GIVING_UP,
        pause=_RECHECK_SECONDS,
    )
    if earlier in (Earlier.UNKNOWN, Earlier.UNREADABLE):
        print(f"{label}: its last {trade.kind} {earlier.value}; kept", file=out)
        return None
    store.clear_unresolved(coin.mint)
    if earlier is Earlier.NOT_LANDED:
        return dataclasses.replace(position, unresolved=None)
    if earlier is Earlier.BURNED:
        store.remove(coin.mint)
        print(f"{label}: its burn landed; dropped from the file", file=out)
        return None
    recorded = buy.amount_raw
    if recorded is None:
        recorded = round(buy.amount * 10**TOKEN_DECIMALS)
    left = recorded - sold
    print(
        f"{label}: its last sale landed, {sold / 10**TOKEN_DECIMALS:g} sold", file=out
    )
    if left <= 0:
        store.mark_sold(coin.mint, slot=slot)
        return dataclasses.replace(position, unresolved=None, sold=True, sold_slot=slot)
    store.reduce(coin.mint, left / 10**TOKEN_DECIMALS, left, sold=True)
    return dataclasses.replace(
        position,
        unresolved=None,
        buy_result=dataclasses.replace(
            buy, amount=left / 10**TOKEN_DECIMALS, amount_raw=left
        ),
    )


def _nothing_to_sell(  # noqa: PLR0913
    position: StoredPosition,
    store: PositionStore,
    *,
    held: int,
    exists: bool,
    program: Pubkey,
    out: TextIO,
) -> Pubkey | None:
    """Settle a position with nothing to sell; the program of an account to close."""
    coin = position.token_info
    if held and position.sold:
        # Sold already; what is there now came some other way.
        store.remove(coin.mint)
        return None
    if not exists:
        print(
            f"{coin.symbol} ({coin.mint}): the wallet holds none; dropped from the "
            f"file",
            file=out,
        )
        store.remove(coin.mint)
        return None
    # Empty: kept, marked sold, until its account is closed.
    store.mark_sold(coin.mint, slot=position.sold_slot)
    return program


async def _sell_one(
    position: StoredPosition, store: PositionStore, tools: Tools, out: TextIO
) -> tuple[bool, Pubkey | None]:
    """Sell one position.

    Returns:
        Whether nothing of it is left to sell, and the token program of an
        account left to close (None when there is none)
    """
    coin = position.token_info
    label = f"{coin.symbol} ({coin.mint})"
    settled = await _settle_earlier(position, store, tools, out)
    if settled is None:
        # Gone from the file when its burn landed; else kept for a later run.
        return str(coin.mint) not in store.mints(), None
    position = settled
    try:
        balance, exists, program = await _read_held(
            coin, tools, min_slot=_fresh_from(position)
        )
    except Exception as e:  # noqa: BLE001 - the message may carry the endpoint
        print(f"{label}: balance unreadable ({type(e).__name__}); kept", file=out)
        return False, None
    if position.sold or balance == 0:
        return True, _nothing_to_sell(
            position, store, held=balance, exists=exists, program=program, out=out
        )
    # Never more than the record: coins bought by hand stay, as with the bot.
    recorded = position.buy_result.amount_raw
    if recorded is None:
        recorded = round(position.buy_result.amount * 10**TOKEN_DECIMALS)
    held = min(balance, recorded)
    priced = await _priced(position, tools)
    if priced is None:
        store.mark_given_up(coin.mint, "sell-all could not price it")
        print(f"{label}: no price to sell it at; kept", file=out)
        return False, None
    price, _venue = priced
    try:
        result = await tools.seller.execute(
            coin,
            token_amount=held / 10**TOKEN_DECIMALS,
            token_price=price,
            token_amount_raw=held,
        )
    except Exception as e:  # noqa: BLE001 - the message may carry the endpoint
        result, reason = None, type(e).__name__
    else:
        # The reason, not the message: a message can quote an RPC error.
        reason = result.failure_reason.value if result.failure_reason else "failed"
        if (
            result.failure_reason is TradeFailureReason.UNCONFIRMED
            and result.tx_signature
        ):
            result, reason = await _resolved(
                tools,
                result,
                held=held,
                label=label,
                mint=coin.mint,
                store=store,
                out=out,
            )
            if result is None and reason == _STILL_UNKNOWN:
                return False, None
    gone, closes = await _settle(
        position,
        store,
        result,
        reason=reason,
        held=held,
        balance=balance,
        price=price,
        tools=tools,
        out=out,
    )
    return gone, program if closes else None


#: `_resolved`'s reason for a sale it could not resolve.
_STILL_UNKNOWN = "still unknown"


async def _resolved(  # noqa: PLR0913
    tools: Tools,
    result: TradeResult,
    *,
    held: int,
    label: str,
    mint: Pubkey,
    store: PositionStore,
    out: TextIO,
) -> tuple[TradeResult | None, str]:
    """An unconfirmed sale, once resolved: landed, failed, or still unknown.

    Still unknown, it is recorded with its signature and expiry height, and the
    next run, or the bot, resolves it before the coin moves again.
    """
    trade = Unresolved(SALE, str(result.tx_signature), result.last_valid_block_height)
    status = await unresolved.look_up(
        tools.client,
        trade,
        looks=unresolved.LOOKS_BEFORE_GIVING_UP,
        pause=_RECHECK_SECONDS,
    )
    if status is ConfirmationStatus.SUCCESS:
        return dataclasses.replace(
            result,
            success=True,
            failure_reason=None,
            amount_raw=result.amount_raw or held,
            amount=(result.amount_raw or held) / 10**TOKEN_DECIMALS,
        ), ""
    if status is ConfirmationStatus.UNCONFIRMED:
        store.mark_unresolved(mint, trade)
        store.mark_given_up(mint, "its last sale is unresolved")
        print(
            f"{label}: its sale {result.tx_signature} is still unknown; kept", file=out
        )
        return None, _STILL_UNKNOWN
    return None, "it did not land"


async def _settle(  # noqa: PLR0913
    position: StoredPosition,
    store: PositionStore,
    result: TradeResult | None,
    *,
    reason: str,
    held: int,
    balance: int,
    price: float,
    tools: Tools,
    out: TextIO,
) -> tuple[bool, bool]:
    """Record what one sale did.

    The file is written first, PnL included as an estimate; then the sale is
    read back, its PnL replaced with the chain's, and reported. A cancellation
    or a failed print after the sale landed loses nothing but the report.

    Args:
        held: Raw tokens the sale was asked to sell
        balance: Raw tokens the wallet held, coins bought by hand included

    Returns:
        Whether nothing of the position is left, and whether its account is
        to be closed (not when coins bought by hand remain in it)
    """
    coin = position.token_info
    label = f"{coin.symbol} ({coin.mint})"
    quote_mint = normalize_quote_mint(coin.quote_mint)
    if result is not None and result.curve_short:
        store.mark_given_up(coin.mint, "the curve can't pay for it now")
        print(f"{label}: the curve can't pay for it now; kept", file=out)
        return False, False
    if result is None or not result.success:
        store.mark_given_up(coin.mint, "sell-all could not sell it")
        print(f"{label}: sale failed ({reason}); kept", file=out)
        return False, False
    sold = result.amount_raw if result.amount_raw is not None else held
    left = held - sold
    extra = left <= 0 and balance > sold
    if left > 0:
        store.reduce(coin.mint, left / 10**TOKEN_DECIMALS, left, sold=True)
        store.mark_given_up(coin.mint, "sell-all could not sell all of it")
        store.set_realised(
            coin.mint,
            _realised(
                position, dataclasses.replace(result, deltas=None), price, quote_mint
            )[0],
        )
    elif extra:
        # Coins bought by hand remain: the account stays open and is not the
        # bot's to close, so the record goes now.
        store.remove(coin.mint)
    else:
        # Kept, marked sold, until its account is closed.
        store.mark_sold(coin.mint)
    # Read back like the bot's own sales, so the PnL is the chain's.
    deltas = await read_deltas(
        tools.client, result.tx_signature, tools.wallet.pubkey, coin
    )
    sale = landed_sale(coin, result.tx_signature, sold, price, deltas)
    sale.venue = result.venue
    realised, pnl = _realised(position, sale, price, quote_mint)
    if left > 0:
        store.set_realised(coin.mint, realised)
    elif not extra and deltas is not None and deltas.slot is not None:
        store.mark_sold(coin.mint, slot=deltas.slot)
    trade_log.log_trade(
        "sell",
        coin,
        sale.price,
        sale.amount,
        sale.tx_signature,
        deltas=sale.deltas,
        venue=sale.venue,
    )
    quote_label = quote_symbol(quote_mint)
    pnl_line = f"  {pnl_text('PnL', pnl, quote_label)}"
    if left > 0:
        print(
            f"{label}: sold {sold / 10**TOKEN_DECIMALS:g}, the curve could not "
            f"pay for the rest\n{pnl_line}",
            file=out,
        )
        return False, False
    print(
        f"{label}: sold {sold / 10**TOKEN_DECIMALS:g} ({result.tx_signature})\n"
        f"{pnl_line}{realised.suffix(quote_label)}",
        file=out,
    )
    return True, not extra


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
        p
        for p in stored
        if _ours(p, tools) and not _other_platform(p, tools) and p.pending is None
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
        gone, program = await _sell_one(position, store, tools, out)
        if not gone:
            left.add(position.token_info.mint)
        if program is not None:
            closes.append((position.token_info, program))
    # One settle wait before the first close covers the sales before it. A
    # record goes only once its account is closed; else the next run retries.
    for i, (coin, program) in enumerate(closes):
        if await tools.cleanup.cleanup_ata(coin.mint, program, settle=i == 0):
            store.remove(coin.mint)
        else:
            left.add(coin.mint)
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


class _Watcher:
    """A wallet known by its public key only: enough to read, not to sign."""

    def __init__(self, pubkey: Pubkey) -> None:
        self.pubkey = pubkey

    def get_associated_token_address(
        self, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        return get_associated_token_address(
            self.pubkey, mint, token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
        )


def _pubkey(cfg: dict) -> Pubkey:
    """The bot's wallet address, without unlocking a keystore.

    Raises:
        ValueError: The config names no readable wallet
    """
    if "keystore" in cfg:
        pubkey = keystore_pubkey(Path(cfg["keystore"]))
        if pubkey is None:
            raise ValueError(f"keystore {cfg['keystore']} names no public key")  # noqa: TRY003
        return Pubkey.from_string(pubkey)
    return Keypair.from_base58_string(cfg["private_key"]).pubkey()


async def _tools(cfg: dict, *, selling: bool = True) -> Tools:
    """What the commands need; `selling` False builds no signer or seller.

    With `selling`, a keystore must be unlocked first (`private_key` set).
    """
    platform = get_platform_from_config(cfg)
    if not selling:
        cfg = {**cfg, "private_key": cfg.get("private_key") or "unused"}
    settings = build_trader_settings(cfg, platform)
    client = SolanaClient(settings.rpc_endpoint, max_rps=settings.max_rps)
    implementations = get_platform_implementations(platform, client)
    if not selling:
        return Tools(
            client=client,
            wallet=_Watcher(_pubkey(cfg)),
            seller=None,
            implementations=implementations,
            cleanup=None,
            platform=platform,
        )
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
        implementations=implementations,
        cleanup=AccountCleanupManager(
            client, wallet, fees, settings.cleanup_with_priority_fee, force_burn=False
        ),
        platform=platform,
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
    selling = args.command == "sell-all"
    if selling and "keystore" in cfg:
        try:
            unlock_keystore(cfg)
        except KeystoreError as e:
            logger.error(f"Could not unlock the wallet: {e}")  # noqa: TRY400
            return 1
    try:
        tools = await _tools(cfg, selling=selling)
    except Exception as e:  # noqa: BLE001 - logged, so the endpoint is redacted
        logger.error(f"Could not set up: {_one_line(e)}")  # noqa: TRY400
        return 1
    try:
        if args.command == "status":
            return await status(positions, tools, sys.stdout)
        return await sell_all(positions, tools, yes=args.yes)
    except Exception as e:  # noqa: BLE001 - logged, so the endpoint is redacted
        logger.error(f"{args.command} stopped: {_one_line(e)}")  # noqa: TRY400
        return 1
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

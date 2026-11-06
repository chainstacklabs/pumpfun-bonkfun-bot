"""Open positions on disk, so a restarted bot picks them up again.

One JSON file per bot, `positions/<bot name>.json`, relative to the working
directory like `logs/` and `trades/`. A position is added with a `pending`
object when its buy is sent, rewritten without it once the buy is resolved as
landed, and removed once it is sold. The file
is rewritten whole on every change through a temporary file, so a crash leaves
either the old or the new version.
"""

import dataclasses
import json
import math
import os
import typing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from solders.pubkey import Pubkey

from core.trade_deltas import TradeDeltas
from interfaces.core import Platform, TokenInfo
from trading.base import PendingBuy, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)

_VERSION = 1


def _is_pubkey_field(field: dataclasses.Field) -> bool:
    return field.type is Pubkey or Pubkey in typing.get_args(field.type)


def encode_token_info(token_info: TokenInfo) -> dict:
    """Every `TokenInfo` field as JSON: pubkeys as base58, the platform by value."""
    encoded = {}
    for field in dataclasses.fields(TokenInfo):
        value = getattr(token_info, field.name)
        if isinstance(value, Pubkey):
            value = str(value)
        elif isinstance(value, Platform):
            value = value.value
        encoded[field.name] = value
    return encoded


def decode_token_info(encoded: dict) -> TokenInfo:
    """Rebuild a `TokenInfo` from `encode_token_info`; unknown keys are ignored.

    Raises:
        KeyError, TypeError, ValueError: If a required field is missing or a
            pubkey or platform does not parse
    """
    values = {}
    for field in dataclasses.fields(TokenInfo):
        if field.name not in encoded:
            continue
        value = encoded[field.name]
        if value is not None and _is_pubkey_field(field):
            value = Pubkey.from_string(value)
        elif field.name == "platform":
            value = Platform(value)
        values[field.name] = value
    return TokenInfo(**values)


def _check_buy(buy: dict) -> None:
    """Raise TypeError unless the stored buy's amounts are usable numbers.

    `amount` is a finite number, `price` one or null (the fill was never read:
    the position takes the first price it reads, as it does for a price of zero
    or below), `amount_raw` a whole number of raw units or null. A hand-edited
    `"1.0"` would otherwise fail later, outside the per-record guard, and stop
    the bot on every start.
    """

    def real(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    raw = buy.get("amount_raw")
    raw_ok = raw is None or (
        isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0
    )
    price_ok = buy["price"] is None or real(buy["price"])
    if not (real(buy["amount"]) and price_ok and raw_ok):
        raise TypeError


def _coin_of(record: dict, mint: str) -> TokenInfo:
    """The record's coin; ValueError unless it is the one `mint` names.

    `remove` and `reduce` find a record by its coin's mint.
    """
    token_info = decode_token_info(record["token_info"])
    if str(token_info.mint) != mint:
        raise ValueError(f"record names {mint}, its coin is {token_info.mint}")  # noqa: TRY003
    return token_info


def _wallet_of(record: dict) -> str | None:
    """The record's wallet, None if it names none; TypeError if not a string."""
    wallet = record.get("wallet")
    if wallet is not None and not isinstance(wallet, str):
        raise TypeError
    return wallet


def _whole(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _count(value: object) -> bool:
    """A whole number that is not negative, or None."""
    return _whole(value) and (value is None or value >= 0)


def _pending_of(record: dict) -> PendingBuy | None:
    """The record's unresolved buy, None for a landed one; TypeError if malformed."""
    pending = record.get("pending")
    if pending is None:
        return None
    if not isinstance(pending, dict):
        raise TypeError
    found = PendingBuy(
        max_amount_raw=pending.get("max_amount_raw"),
        quote_destination=pending.get("quote_destination"),
        spend_raw=pending.get("spend_raw"),
        last_valid_block_height=pending.get("last_valid_block_height"),
        # Only a label for the trade log: one that is not a name is dropped
        # rather than making the record unresumable.
        venue=venue if isinstance(venue := pending.get("venue"), str) else None,
    )
    destination = found.quote_destination
    if not (
        _count(found.max_amount_raw)
        and _count(found.spend_raw)
        and _count(found.last_valid_block_height)
        and (destination is None or isinstance(destination, str))
    ):
        raise TypeError
    return found


def _deltas_of(buy: dict) -> TradeDeltas | None:
    """The buy's transaction deltas, None if not recorded; TypeError if malformed."""
    deltas = buy.get("deltas")
    if deltas is None:
        return None
    if not isinstance(deltas, dict):
        raise TypeError
    found = TradeDeltas(**deltas)
    nullable = {"quote_raw", "venue_quote_raw"}
    for name, value in dataclasses.asdict(found).items():
        if not _whole(value) or (value is None and name not in nullable):
            raise TypeError
    return found


def _records_in(data: object) -> list:
    """The `positions` list of a parsed file; KeyError or TypeError without one."""
    records = data["positions"]
    if not isinstance(records, list):
        raise TypeError
    return records


def holds_positions(path: Path | str) -> bool:
    """Whether a positions file exists and is not empty, read only.

    A file that cannot be read counts as holding positions.
    """
    try:
        return bool(json.loads(Path(path).read_text(encoding="utf-8"))["positions"])
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return True


@dataclass(frozen=True)
class StoredPosition:
    """One position read back from the file."""

    token_info: TokenInfo
    #: The buy as it landed: price, amount, amount_raw and signature. After a
    #: partial sale the amounts are what is left.
    buy_result: TradeResult
    #: When the buy landed, naive UTC like `Position.entry_time`.
    entry_time: datetime
    #: The wallet that bought it, base58; None in a record written before
    #: positions named their wallet.
    wallet: str | None = None
    #: Set while the buy was sent but its outcome is not resolved:
    #: `buy_result` then holds the signature and the expected amount and price.
    pending: PendingBuy | None = None


def _decode(mint: str, record: dict) -> StoredPosition:
    """One record as a `StoredPosition`.

    Raises:
        ValueError, KeyError, TypeError, AttributeError: It does not decode
    """
    buy = record["buy"]
    _check_buy(buy)
    return StoredPosition(
        token_info=_coin_of(record, mint),
        buy_result=TradeResult(
            success=True,
            platform=Platform(record["token_info"]["platform"]),
            tx_signature=buy.get("tx_signature"),
            amount=buy["amount"],
            amount_raw=buy.get("amount_raw"),
            # A price of zero would divide every PnL by zero: unread instead.
            price=buy["price"] if buy["price"] is None or buy["price"] > 0 else None,
            deltas=_deltas_of(buy),
        ),
        entry_time=datetime.fromisoformat(record["entry_time"]),
        wallet=_wallet_of(record),
        pending=_pending_of(record),
    )


class PositionStore:
    """The positions file of one bot."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Records by mint, as written; one that does not decode is kept as is.
        self._records: dict[str, dict] = {}
        # Records with no usable mint to key them by, or a mint an earlier one
        # holds: kept as written, not monitored.
        self._unkeyed: list = []
        # False once an unreadable file could not be moved aside: writing
        # would replace it, and the positions in it with it.
        self._writable = True

    def _free_aside_path(self) -> Path:
        aside = self.path.with_name(self.path.name + ".unreadable")
        n = 0
        while aside.exists():
            n += 1
            aside = self.path.with_name(f"{self.path.name}.unreadable.{n}")
        return aside

    def load(self) -> list[StoredPosition]:
        """Read the file and return the positions in it.

        A file that cannot be read (not valid JSON or UTF-8, or no read
        permission) is renamed to `<name>.unreadable` (then
        `<name>.unreadable.1`, `.2`, ... so no earlier one is overwritten) and
        logged, so the next write cannot overwrite what it held. If it cannot be
        moved either, this store writes nothing this run. A record that does not
        decode, has no usable `mint` (a string), names a mint an earlier record
        already holds, or whose `mint` is not its coin's, is logged and kept in
        the file, but not returned.
        """
        self._records = {}
        self._unkeyed = []
        try:
            records = _records_in(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, KeyError, TypeError):
            self._set_aside()
            return []
        for record in records:
            try:
                mint = record["mint"]
                if not isinstance(mint, str):
                    raise TypeError  # noqa: TRY301
            except (KeyError, TypeError):
                self._unkeyed.append(record)
                logger.exception(
                    f"A position in {self.path} has no usable mint; "
                    f"left in the file, not monitored: {record!r}"
                )
                continue
            if mint in self._records:
                # The first keeps the mint, so the choice survives every write.
                self._unkeyed.append(record)
                logger.error(
                    f"A second position for {mint} in {self.path}; left in the "
                    f"file, not monitored: {record!r}"
                )
                continue
            self._records[mint] = record

        positions = []
        for mint, record in self._records.items():
            try:
                positions.append(_decode(mint, record))
            except (ValueError, KeyError, TypeError, AttributeError):
                logger.exception(
                    f"Position for {mint} in {self.path} does not decode; "
                    f"left in the file, not monitored"
                )
        return positions

    def _set_aside(self) -> None:
        try:
            aside = self._free_aside_path()
            self.path.replace(aside)
        except OSError:
            self._writable = False
            logger.exception(
                f"Positions file {self.path} is unreadable and could not be moved "
                f"aside; positions in it are not monitored, and new ones are not "
                f"written to disk this run"
            )
            return
        logger.exception(
            f"Positions file {self.path} is unreadable; moved to {aside}. "
            f"Positions in it are not monitored."
        )

    def mints(self) -> set[str]:
        """Every mint in the file as last loaded or written, decodable or not."""
        return set(self._records)

    def pending(self, mint: Pubkey) -> StoredPosition | None:
        """The coin's record if its buy is still unresolved and it decodes."""
        if str(mint) not in self.pending_mints():
            return None
        try:
            return _decode(str(mint), self._records[str(mint)])
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def pending_mints(self) -> set[str]:
        """Mints whose buy was sent and never confirmed."""
        return {
            mint
            for mint, record in self._records.items()
            if isinstance(record, dict) and isinstance(record.get("pending"), dict)
        }

    def add(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime,
        wallet: Pubkey | None = None,
        *,
        pending: PendingBuy | None = None,
    ) -> None:
        """Record a position whose buy just landed, or was just sent.

        Args:
            wallet: The wallet holding the coin; only that wallet resumes it
            pending: Set while the buy was sent and its outcome is not
                resolved; the record replaces any earlier one for the coin
                either way
        """
        mint = str(token_info.mint)
        record = {
            "mint": mint,
            "symbol": token_info.symbol,
            "wallet": str(wallet) if wallet is not None else None,
            "entry_time": entry_time.isoformat(),
            "buy": {
                "price": buy_result.price,
                "amount": buy_result.amount,
                "amount_raw": buy_result.amount_raw,
                "tx_signature": (
                    str(buy_result.tx_signature) if buy_result.tx_signature else None
                ),
            },
            "token_info": encode_token_info(token_info),
        }
        if buy_result.deltas is not None:
            # The buy's cost, so a resumed position's PnL counts from it.
            record["buy"]["deltas"] = dataclasses.asdict(buy_result.deltas)
        if pending is not None:
            # Only on a pending record, so a landed one is written as before.
            record["pending"] = dataclasses.asdict(pending)
        self._records[mint] = record
        self._write()

    def reduce(self, mint: Pubkey, amount: float, amount_raw: int) -> None:
        """Keep only what is left of a position after part of it sold.

        Args:
            amount: Whole tokens still held
            amount_raw: The same in raw token units
        """
        record = self._records.get(str(mint))
        if record is None:
            return
        record["buy"]["amount"] = amount
        record["buy"]["amount_raw"] = amount_raw
        self._write()

    def set_price(self, mint: Pubkey, price: float) -> None:
        """Record the entry price of a position bought at an unknown price."""
        record = self._records.get(str(mint))
        if record is None:
            return
        record["buy"]["price"] = price
        self._write()

    def remove(self, mint: Pubkey) -> None:
        """Drop a position that has been sold; a mint not in the file is a no-op."""
        if self._records.pop(str(mint), None) is not None:
            self._write()

    def _write(self) -> None:
        """Replace the file with the current records. A failed write is logged."""
        if not self._writable:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "version": _VERSION,
            "positions": [*self._records.values(), *self._unkeyed],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        except OSError:
            logger.exception(f"Failed to write positions file {self.path}")

"""Open positions on disk, so a restarted bot picks them up again.

One JSON file per bot, `positions/<bot name>.json`, relative to the working
directory like `logs/` and `trades/`. A position is added when its buy lands and
removed once it is sold. The file is rewritten whole on every change through a
temporary file, so a crash leaves either the old or the new version.
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

from interfaces.core import Platform, TokenInfo
from trading.base import TradeResult
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

    `amount` and `price` are finite numbers, `amount_raw` a whole number of raw
    units or null. A hand-edited `"1.0"` would otherwise fail later, outside
    the per-record guard, and stop the bot on every start.
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
    if not (real(buy["amount"]) and real(buy["price"]) and raw_ok):
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


class PositionStore:
    """The positions file of one bot."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Records by mint, as written; one that does not decode is kept as is.
        self._records: dict[str, dict] = {}
        # Records with no usable mint to key them by, kept as written.
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
        decode, has no usable `mint`, or whose `mint` is not its coin's, is
        logged and kept in the file, but not returned.
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
                self._records[record["mint"]] = record
            except (KeyError, TypeError):
                self._unkeyed.append(record)
                logger.exception(
                    f"A position in {self.path} has no usable mint; "
                    f"left in the file, not monitored: {record!r}"
                )

        positions = []
        for mint, record in self._records.items():
            try:
                buy = record["buy"]
                _check_buy(buy)
                token_info = _coin_of(record, mint)
                wallet = _wallet_of(record)
                positions.append(
                    StoredPosition(
                        token_info=token_info,
                        buy_result=TradeResult(
                            success=True,
                            platform=Platform(record["token_info"]["platform"]),
                            tx_signature=buy.get("tx_signature"),
                            amount=buy["amount"],
                            amount_raw=buy.get("amount_raw"),
                            price=buy["price"],
                        ),
                        entry_time=datetime.fromisoformat(record["entry_time"]),
                        wallet=wallet,
                    )
                )
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

    def add(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        entry_time: datetime,
        wallet: Pubkey | None = None,
    ) -> None:
        """Record a position whose buy just landed.

        Args:
            wallet: The wallet holding the coin; only that wallet resumes it
        """
        mint = str(token_info.mint)
        self._records[mint] = {
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

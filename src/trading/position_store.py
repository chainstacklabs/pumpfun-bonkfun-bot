"""Open positions on disk, so a restarted bot picks them up again.

One JSON file per bot, `positions/<bot name>.json`, relative to the working
directory like `logs/` and `trades/`. A position is added when its buy lands and
removed once it is sold. The file is rewritten whole on every change through a
temporary file, so a crash leaves either the old or the new version.
"""

import dataclasses
import json
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


@dataclass(frozen=True)
class StoredPosition:
    """One position read back from the file."""

    token_info: TokenInfo
    #: The buy as it landed: price, amount, amount_raw and signature.
    buy_result: TradeResult
    #: When the buy landed, naive UTC like `Position.entry_time`.
    entry_time: datetime


class PositionStore:
    """The positions file of one bot."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Records by mint, as written; one that does not decode is kept as is.
        self._records: dict[str, dict] = {}

    def _free_aside_path(self) -> Path:
        aside = self.path.with_name(self.path.name + ".unreadable")
        n = 0
        while aside.exists():
            n += 1
            aside = self.path.with_name(f"{self.path.name}.unreadable.{n}")
        return aside

    def load(self) -> list[StoredPosition]:
        """Read the file and return the positions in it.

        A file that is not valid JSON is renamed to `<name>.unreadable` (then
        `<name>.unreadable.1`, `.2`, ... so no earlier one is overwritten) and
        logged, so the next write cannot overwrite what it held. A record that
        does not decode is logged and kept in the file, but not returned.
        """
        self._records = {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            records = json.loads(text)["positions"]
            self._records = {record["mint"]: record for record in records}
        except (ValueError, KeyError, TypeError):
            aside = self._free_aside_path()
            logger.exception(
                f"Positions file {self.path} is unreadable; moved to {aside}. "
                f"Positions in it are not monitored."
            )
            self.path.replace(aside)
            return []

        positions = []
        for mint, record in self._records.items():
            try:
                buy = record["buy"]
                positions.append(
                    StoredPosition(
                        token_info=decode_token_info(record["token_info"]),
                        buy_result=TradeResult(
                            success=True,
                            platform=Platform(record["token_info"]["platform"]),
                            tx_signature=buy.get("tx_signature"),
                            amount=buy["amount"],
                            amount_raw=buy.get("amount_raw"),
                            price=buy["price"],
                        ),
                        entry_time=datetime.fromisoformat(record["entry_time"]),
                    )
                )
            except (ValueError, KeyError, TypeError):
                logger.exception(
                    f"Position for {mint} in {self.path} does not decode; "
                    f"left in the file, not monitored"
                )
        return positions

    def add(
        self, token_info: TokenInfo, buy_result: TradeResult, entry_time: datetime
    ) -> None:
        """Record a position whose buy just landed."""
        mint = str(token_info.mint)
        self._records[mint] = {
            "mint": mint,
            "symbol": token_info.symbol,
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

    def remove(self, mint: Pubkey) -> None:
        """Drop a position that has been sold; a mint not in the file is a no-op."""
        if self._records.pop(str(mint), None) is not None:
            self._write()

    def _write(self) -> None:
        """Replace the file with the current records. A failed write is logged."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": _VERSION, "positions": list(self._records.values())}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        except OSError:
            logger.exception(f"Failed to write positions file {self.path}")

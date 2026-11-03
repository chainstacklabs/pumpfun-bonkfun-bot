"""What the bot writes under `trades/`: one file per detected coin and a trade log."""

import json
from datetime import datetime
from pathlib import Path

from interfaces.core import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)


def save_token_info(token_info: TokenInfo) -> None:
    """Write the coin's details to `trades/<mint>.txt`."""
    try:
        trades_dir = Path("trades")
        trades_dir.mkdir(exist_ok=True)
        file_path = trades_dir / f"{token_info.mint}.txt"

        # Convert to dictionary for saving - platform-agnostic
        token_dict = {
            "name": token_info.name,
            "symbol": token_info.symbol,
            "uri": token_info.uri,
            "mint": str(token_info.mint),
            "platform": token_info.platform.value,
            "user": str(token_info.user) if token_info.user else None,
            "creator": str(token_info.creator) if token_info.creator else None,
            "creation_timestamp": token_info.creation_timestamp,
        }

        # Add platform-specific fields only if they exist
        platform_fields = {
            "bonding_curve": token_info.bonding_curve,
            "associated_bonding_curve": token_info.associated_bonding_curve,
            "creator_vault": token_info.creator_vault,
            "pool_state": token_info.pool_state,
            "base_vault": token_info.base_vault,
            "quote_vault": token_info.quote_vault,
        }

        for field_name, field_value in platform_fields.items():
            if field_value is not None:
                token_dict[field_name] = str(field_value)

        file_path.write_text(json.dumps(token_dict, indent=2))

        logger.info(f"Token information saved to {file_path}")
    except OSError:
        logger.exception("Failed to save token information")


def log_trade(
    action: str,
    token_info: TokenInfo,
    price: float,
    amount: float,
    tx_hash: str | None,
) -> None:
    """Append one buy or sell to `trades/trades.log` as a JSON line."""
    try:
        trades_dir = Path("trades")
        trades_dir.mkdir(exist_ok=True)

        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "action": action,
            "platform": token_info.platform.value,
            "token_address": str(token_info.mint),
            "symbol": token_info.symbol,
            "price": price,
            "amount": amount,
            "tx_hash": str(tx_hash) if tx_hash else None,
        }

        log_file_path = trades_dir / "trades.log"
        with log_file_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(log_entry) + "\n")
    except OSError:
        logger.exception("Failed to log trade information")

"""What the bot writes under `trades/`: one file per detected coin and a trade log."""

import json
from datetime import datetime
from pathlib import Path

from core import session
from core.pubkeys import normalize_quote_mint, quote_symbol, quote_units_per_token
from core.trade_deltas import TradeDeltas
from interfaces.core import TokenInfo
from trading.pnl import SalePnl, lamports_to_sol
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
            "platform_config": token_info.platform_config,
        }

        for field_name, field_value in platform_fields.items():
            if field_value is not None:
                token_dict[field_name] = str(field_value)

        file_path.write_text(json.dumps(token_dict, indent=2))

        logger.info(f"Token information saved to {file_path}")
    except OSError:
        logger.exception("Failed to save token information")


def log_trade(  # noqa: PLR0913
    action: str,
    token_info: TokenInfo,
    price: float,
    amount: float,
    tx_hash: str | None,
    *,
    deltas: TradeDeltas | None = None,
    pnl: SalePnl | None = None,
    venue: str | None = None,
) -> None:
    """Append one buy or sell to `trades/trades.log` as a JSON line.

    `platform` is where the coin launched; `venue` is where this trade went:
    "bonding curve", or the pool the coin graduated to ("PumpSwap", "Raydium
    CPMM"). It is null when not known, as for a buy sent before a restart by
    a version that did not record it.

    `price` is a buy's fill, protocol fees left out (what exits measure from),
    and what a sale paid per token, protocol fees taken off. With `deltas` the
    row also says what the transaction did to the wallet: `quote_amount`
    (negative when spent, protocol fees included, network fee left out), the network
    fee, and rent put into or back from the wallet's token accounts, in SOL.
    `from_chain` is false when the transaction was not read: then `price` and
    `amount` are estimates and those fields are null. A sell row with `pnl`
    carries that sale's realised PnL. `bot` names the bot that traded, null
    outside a bot's session. Every row also counts towards the session summary.
    """
    try:
        trades_dir = Path("trades")
        trades_dir.mkdir(exist_ok=True)

        quote_mint = normalize_quote_mint(token_info.quote_mint)
        chain = _chain_fields(deltas, quote_mint)
        session.current().add_trade(
            action,
            network_fee_sol=chain["network_fee_sol"],
            rent_deposit_sol=chain["rent_deposit_sol"],
            rent_refund_sol=chain["rent_refund_sol"],
            quote_label=quote_symbol(quote_mint),
            pnl=pnl.pnl if pnl is not None else None,
            pnl_from_chain=pnl.from_chain if pnl is not None else True,
        )
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "bot": session.current().bot,
            "action": action,
            "platform": token_info.platform.value,
            "venue": venue,
            "token_address": str(token_info.mint),
            "symbol": token_info.symbol,
            "price": price,
            "amount": amount,
            "tx_hash": str(tx_hash) if tx_hash else None,
            "quote_mint": str(quote_mint),
            **chain,
        }
        if pnl is not None:
            log_entry["pnl"] = pnl.pnl
            log_entry["pnl_pct"] = pnl.pct
            log_entry["pnl_from_chain"] = pnl.from_chain
            if not pnl.sol_quote:
                log_entry["pnl_sol_costs"] = pnl.sol_costs
        # LaunchLab: which launchpad the coin trades on.
        if token_info.platform_config is not None:
            log_entry["platform_config"] = str(token_info.platform_config)

        log_file_path = trades_dir / "trades.log"
        with log_file_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(log_entry) + "\n")
    except OSError:
        logger.exception("Failed to log trade information")


def _chain_fields(deltas: TradeDeltas | None, quote_mint: object) -> dict:
    """The row's amounts from the transaction; nulls when it was not read."""
    try:
        unit = quote_units_per_token(quote_mint) if deltas is not None else None
    except ValueError:
        unit = None
    if deltas is None:
        return {
            "from_chain": False,
            "amount_raw": None,
            "quote_amount": None,
            "network_fee_sol": None,
            "rent_deposit_sol": None,
            "rent_refund_sol": None,
            "other_sol": None,
        }
    return {
        "from_chain": True,
        "amount_raw": abs(deltas.tokens_raw),
        # Negative when spent, positive when received.
        "quote_amount": (
            deltas.quote_raw / unit
            if deltas.quote_raw is not None and unit is not None
            else None
        ),
        "network_fee_sol": lamports_to_sol(deltas.network_fee_lamports),
        "rent_deposit_sol": lamports_to_sol(deltas.rent_deposited_lamports),
        "rent_refund_sol": lamports_to_sol(deltas.rent_refunded_lamports),
        # Non-SOL quote only: other SOL the trade moved, negative when spent.
        "other_sol": lamports_to_sol(deltas.other_lamports),
    }

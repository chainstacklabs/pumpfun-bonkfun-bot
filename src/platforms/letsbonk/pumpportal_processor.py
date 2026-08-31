"""
LetsBonk-specific PumpPortal event processor.
File: src/platforms/letsbonk/pumpportal_processor.py
"""

from interfaces.core import Platform, TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)


class LetsBonkPumpPortalProcessor:
    """PumpPortal boundary for LetsBonk tokens.

    PumpPortal's LetsBonk notification does not carry the authoritative
    LaunchLab pool/config/vault accounts required to validate an executable
    token. The synchronous processor cannot safely enrich those accounts from
    chain state, so it rejects this source instead of deriving guessed values.
    """

    REJECTION_REASON = (
        "PumpPortal LetsBonk events lack authoritative LaunchLab pool metadata; "
        "LetsBonk PumpPortal execution is disabled"
    )

    @property
    def platform(self) -> Platform:
        """Get the platform this processor handles."""
        return Platform.LETS_BONK

    @property
    def supported_pool_names(self) -> list[str]:
        """Keep routing bonk events here so rejection remains observable."""
        return ["bonk"]

    def can_process(self, token_data: dict) -> bool:
        """Return whether this is a LetsBonk PumpPortal notification."""
        pool = token_data.get("pool", "")
        return isinstance(pool, str) and pool.lower() in self.supported_pool_names

    def process_token_data(self, token_data: dict) -> TokenInfo | None:
        """Reject PumpPortal LetsBonk data before any account derivation."""
        logger.warning(
            "Rejected LetsBonk PumpPortal token %s: %s",
            token_data.get("mint", "<unknown>"),
            self.REJECTION_REASON,
        )
        return None

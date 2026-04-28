"""
LetsBonk-specific PumpPortal event processor.
File: src/platforms/letsbonk/pumpportal_processor.py
"""

from solders.pubkey import Pubkey

from core.pubkeys import SystemAddresses
from interfaces.core import Platform, TokenInfo
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from utils.logger import get_logger

logger = get_logger(__name__)


class LetsBonkPumpPortalProcessor:
    """PumpPortal processor for LetsBonk tokens."""

    def __init__(self):
        """Initialize the processor with address provider."""
        self.address_provider = LetsBonkAddressProvider()

    @property
    def platform(self) -> Platform:
        """Get the platform this processor handles."""
        return Platform.LETS_BONK

    @property
    def supported_pool_names(self) -> list[str]:
        """Get the pool names this processor supports from PumpPortal."""
        return ["bonk"]  # PumpPortal pool name for LetsBonk/bonk pools

    def can_process(self, token_data: dict) -> bool:
        """Check if this processor can handle the given token data.

        Args:
            token_data: Token data from PumpPortal

        Returns:
            True if this processor can handle the token data
        """
        pool = token_data.get("pool", "").lower()
        return pool in self.supported_pool_names

    def process_token_data(self, token_data: dict) -> TokenInfo | None:
        """Process LetsBonk token data from PumpPortal.

        Args:
            token_data: Token data from PumpPortal WebSocket

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            # Extract required fields for LetsBonk
            name = token_data.get("name", "")
            symbol = token_data.get("symbol", "")
            mint_str = token_data.get("mint")
            creator_str = token_data.get("traderPublicKey")
            uri = token_data.get("uri", "")

            # Note: LetsBonk tokens from PumpPortal might have different field mappings
            # This would need to be adjusted based on actual PumpPortal data for LetsBonk tokens

            if not all([name, symbol, mint_str, creator_str]):
                logger.warning(
                    "Missing required fields in PumpPortal LetsBonk token data"
                )
                return None

            # Convert string addresses to Pubkey objects
            mint = Pubkey.from_string(mint_str)
            user = Pubkey.from_string(creator_str)
            creator = user

            # Derive LetsBonk-specific addresses
            pool_state = self.address_provider.derive_pool_address(mint)

            # For LetsBonk, vault addresses might need to be derived differently
            # or provided in the PumpPortal data. For now, we'll derive them
            # using the standard pattern, but this might need adjustment
            additional_accounts = self.address_provider.get_additional_accounts(
                # Create a minimal TokenInfo to get additional accounts
                TokenInfo(
                    name=name,
                    symbol=symbol,
                    uri=uri,
                    mint=mint,
                    platform=Platform.LETS_BONK,
                    pool_state=pool_state,
                    user=user,
                    creator=creator,
                    base_vault=None,  # Will be filled from additional_accounts
                    quote_vault=None,  # Will be filled from additional_accounts
                )
            )

            # Extract vault addresses if available
            base_vault = additional_accounts.get("base_vault")
            quote_vault = additional_accounts.get("quote_vault")

            # If vaults aren't available from additional_accounts,
            # we might need to derive them or leave them None
            # and let the trading logic handle the derivation

            return TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=mint,
                platform=Platform.LETS_BONK,
                pool_state=pool_state,
                base_vault=base_vault,
                quote_vault=quote_vault,
                user=user,
                creator=creator,
                # PumpPortal data does not carry the token program. LetsBonk
                # tokens are predominantly regular SPL (Token), not Token-2022.
                # Default here so the ATA-create ix uses the correct program;
                # without this the universal builder defaults to Token-2022 and
                # the buy fails with IncorrectProgramId on GetAccountDataSize.
                token_program_id=SystemAddresses.TOKEN_PROGRAM,
            )

        except Exception:
            logger.exception("Failed to process PumpPortal LetsBonk token data")
            return None

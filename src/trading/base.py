"""Base interfaces for trading operations, with platform support."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

# Import from interfaces to avoid duplication
from interfaces.core import Platform, TokenInfo, TradeFailureReason


@dataclass
class TradeResult:
    """Enhanced result of a trading operation with platform support."""

    success: bool
    platform: Platform = Platform.PUMP_FUN  # Add platform tracking
    tx_signature: str | None = None
    error_message: str | None = None
    amount: float | None = None
    price: float | None = None
    # Why a failed trade failed, when the caller has to decide whether
    # resubmitting is safe. None on success, and on failures that predate the
    # distinction — treat a missing reason as "unknown", not as "reverted".
    failure_reason: TradeFailureReason | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization.

        Returns:
            Dictionary representation of the trade result
        """
        return {
            "success": self.success,
            "platform": self.platform.value,
            "tx_signature": self.tx_signature,
            "error_message": self.error_message,
            "amount": self.amount,
            "price": self.price,
            "failure_reason": (
                self.failure_reason.value if self.failure_reason else None
            ),
        }


class Trader(ABC):
    """Enhanced base interface for trading operations with platform support."""

    @abstractmethod
    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute trading operation.

        Args:
            token_info: Enhanced token information with platform support

        Returns:
            TradeResult with operation outcome including platform info
        """
        pass

    def _get_relevant_accounts(self, token_info: TokenInfo) -> list[Pubkey]:
        """Get the list of accounts relevant for calculating the priority fee.

        This is now platform-agnostic and should be overridden by platform-specific traders.

        Args:
            token_info: Enhanced token information

        Returns:
            List of relevant accounts (basic implementation)
        """
        # Basic implementation - platform-specific traders should override this
        accounts = [token_info.mint]

        if token_info.bonding_curve:
            accounts.append(token_info.bonding_curve)

        if token_info.pool_state:  # For other platforms
            accounts.append(token_info.pool_state)

        return accounts


# Legacy TokenInfo for backward compatibility (keep pump.fun specific)
@dataclass
class TokenInfo_Legacy:
    """Legacy token information structure for backward compatibility."""

    name: str
    symbol: str
    uri: str
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    user: Pubkey
    creator: Pubkey
    creator_vault: Pubkey

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenInfo_Legacy":
        """Create TokenInfo from dictionary.

        Args:
            data: Dictionary with token data

        Returns:
            TokenInfo_Legacy instance
        """
        return cls(
            name=data["name"],
            symbol=data["symbol"],
            uri=data["uri"],
            mint=Pubkey.from_string(data["mint"]),
            bonding_curve=Pubkey.from_string(data["bondingCurve"]),
            associated_bonding_curve=Pubkey.from_string(data["associatedBondingCurve"]),
            user=Pubkey.from_string(data["user"]),
            creator=Pubkey.from_string(data["creator"]),
            creator_vault=Pubkey.from_string(data["creator_vault"]),
        )

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "name": self.name,
            "symbol": self.symbol,
            "uri": self.uri,
            "mint": str(self.mint),
            "bondingCurve": str(self.bonding_curve),
            "associatedBondingCurve": str(self.associated_bonding_curve),
            "user": str(self.user),
            "creator": str(self.creator),
            "creatorVault": str(self.creator_vault),
        }


def upgrade_token_info(legacy_token_info: TokenInfo_Legacy) -> TokenInfo:
    """Convert legacy TokenInfo to enhanced TokenInfo.

    This function allows existing code that creates legacy TokenInfo objects
    to be upgraded to the new enhanced format.

    Returns:
        Enhanced TokenInfo with platform information
    """
    return TokenInfo(
        name=legacy_token_info.name,
        symbol=legacy_token_info.symbol,
        uri=legacy_token_info.uri,
        mint=legacy_token_info.mint,
        platform=Platform.PUMP_FUN,  # Default to pump.fun for legacy tokens
        bonding_curve=legacy_token_info.bonding_curve,
        associated_bonding_curve=legacy_token_info.associated_bonding_curve,
        user=legacy_token_info.user,
        creator=legacy_token_info.creator,
        creator_vault=legacy_token_info.creator_vault,
    )


def create_legacy_token_info(enhanced_token_info: TokenInfo) -> TokenInfo_Legacy:
    """Convert enhanced TokenInfo back to legacy TokenInfo if needed.

    This function allows the enhanced TokenInfo to be used with existing
    code that expects the legacy format.

    Raises:
        ValueError: If enhanced TokenInfo doesn't have required pump.fun fields
    """
    if enhanced_token_info.platform != Platform.PUMP_FUN:
        raise ValueError("Can only convert pump.fun tokens to legacy format")

    if not all(
        [
            enhanced_token_info.bonding_curve,
            enhanced_token_info.associated_bonding_curve,
            enhanced_token_info.creator_vault,
        ]
    ):
        raise ValueError("Enhanced TokenInfo missing required pump.fun fields")

    return TokenInfo_Legacy(
        name=enhanced_token_info.name,
        symbol=enhanced_token_info.symbol,
        uri=enhanced_token_info.uri,
        mint=enhanced_token_info.mint,
        bonding_curve=enhanced_token_info.bonding_curve,
        associated_bonding_curve=enhanced_token_info.associated_bonding_curve,
        user=enhanced_token_info.user or enhanced_token_info.creator,
        creator=enhanced_token_info.creator or enhanced_token_info.user,
        creator_vault=enhanced_token_info.creator_vault,
    )


def create_pump_fun_token_info(
    name: str,
    symbol: str,
    uri: str,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    user: Pubkey,
    creator: Pubkey | None = None,
    creator_vault: Pubkey | None = None,
    **kwargs,
) -> TokenInfo:
    """Build a pump.fun TokenInfo with the platform field already set.

    Args:
        name: Token name
        symbol: Token symbol
        uri: Token metadata URI
        mint: Token mint address
        user: User/trader address
        creator: Creator address (defaults to user if not provided)
        creator_vault: Creator vault address (will be derived if not provided)
        **kwargs: Additional fields for TokenInfo

    Returns:
        Enhanced TokenInfo configured for pump.fun
    """
    # Derive creator vault if not provided (import here to avoid circular imports)
    if creator_vault is None and creator:
        # We can't import PumpAddresses here, so this will need to be handled elsewhere
        # For now, leave it as None and let the platform implementation handle it
        pass

    return TokenInfo(
        name=name,
        symbol=symbol,
        uri=uri,
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=associated_bonding_curve,
        user=user,
        creator=creator or user,
        creator_vault=creator_vault,
        **kwargs,
    )


def create_lets_bonk_token_info(
    name: str,
    symbol: str,
    uri: str,
    mint: Pubkey,
    pool_state: Pubkey,
    base_vault: Pubkey,
    quote_vault: Pubkey,
    user: Pubkey,
    creator: Pubkey | None = None,
    **kwargs,
) -> TokenInfo:
    """Build a letsbonk.fun TokenInfo with the platform field already set.

    Args:
        name: Token name
        symbol: Token symbol
        uri: Token metadata URI
        mint: Token mint address
        base_vault: Base token vault address
        quote_vault: Quote token vault address
        user: User/trader address
        creator: Creator address (defaults to user if not provided)
        **kwargs: Additional fields for TokenInfo

    Returns:
        Enhanced TokenInfo configured for LetsBonk
    """
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
        creator=creator or user,
        **kwargs,
    )


def is_pump_fun_token(token_info: TokenInfo) -> bool:
    """Check if a TokenInfo is for pump.fun platform.

    Args:
        token_info: Token information to check

    Returns:
        True if token is for pump.fun platform
    """
    return token_info.platform == Platform.PUMP_FUN


def is_lets_bonk_token(token_info: TokenInfo) -> bool:
    """Check if a TokenInfo is for LetsBonk platform.

    Args:
        token_info: Token information to check

    Returns:
        True if token is for LetsBonk platform
    """
    return token_info.platform == Platform.LETS_BONK


def get_platform_specific_fields(token_info: TokenInfo) -> dict[str, Any]:
    """Get platform-specific fields from TokenInfo.

    Returns:
        Dictionary of platform-specific fields
    """
    if token_info.platform == Platform.PUMP_FUN:
        return {
            "bonding_curve": token_info.bonding_curve,
            "associated_bonding_curve": token_info.associated_bonding_curve,
            "creator_vault": token_info.creator_vault,
        }
    elif token_info.platform == Platform.LETS_BONK:
        return {
            "pool_state": token_info.pool_state,
            "base_vault": token_info.base_vault,
            "quote_vault": token_info.quote_vault,
        }
    else:
        return {}


def validate_token_info(token_info: TokenInfo) -> bool:
    """Validate that TokenInfo has required fields for its platform.

    Args:
        token_info: Token information to validate

    Returns:
        True if TokenInfo is valid for its platform
    """
    # Check common required fields
    if not all(
        [
            token_info.name,
            token_info.symbol,
            token_info.mint,
            token_info.platform,
        ]
    ):
        return False

    # Check platform-specific required fields
    if token_info.platform == Platform.PUMP_FUN:
        return all(
            [
                token_info.bonding_curve,
                token_info.associated_bonding_curve,
            ]
        )
    elif token_info.platform == Platform.LETS_BONK:
        return all(
            [
                token_info.pool_state,
                token_info.base_vault,
                token_info.quote_vault,
            ]
        )

    return True


# Backward compatibility exports
__all__ = [
    "Platform",  # Platform enum
    "TokenInfo",  # Enhanced TokenInfo (main export)
    "TokenInfo_Legacy",  # Legacy TokenInfo for compatibility
    "TradeResult",  # Enhanced TradeResult
    "Trader",  # Enhanced Trader base class
    "create_legacy_token_info",
    "create_lets_bonk_token_info",
    "create_pump_fun_token_info",
    "get_platform_specific_fields",
    "is_lets_bonk_token",
    "is_pump_fun_token",
    "upgrade_token_info",
    "validate_token_info",
]

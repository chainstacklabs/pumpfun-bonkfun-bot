"""
Core interfaces for multi-platform trading bot architecture.

This module defines the abstract base classes that each trading platform
must implement to enable unified trading operations across different protocols.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from solders.instruction import Instruction
from solders.pubkey import Pubkey


class Platform(Enum):
    """Supported trading platforms."""

    PUMP_FUN = "pump_fun"
    LETS_BONK = "lets_bonk"


@dataclass
class TokenInfo:
    """Enhanced token information with platform support."""

    # Core token data
    name: str
    symbol: str
    uri: str
    mint: Pubkey

    # Platform-specific fields
    platform: Platform
    bonding_curve: Pubkey | None = None  # pump.fun specific
    associated_bonding_curve: Pubkey | None = None  # pump.fun specific
    pool_state: Pubkey | None = None  # LetsBonk specific
    base_vault: Pubkey | None = None  # LetsBonk specific
    quote_vault: Pubkey | None = None  # LetsBonk specific
    global_config: Pubkey | None = None  # LetsBonk specific
    platform_config: Pubkey | None = None  # LetsBonk specific

    # Common fields
    user: Pubkey | None = None
    creator: Pubkey | None = None
    creator_vault: Pubkey | None = None

    # Metadata
    creation_timestamp: float | None = None
    additional_data: dict[str, Any] | None = None


class AddressProvider(ABC):
    """Abstract interface for platform-specific address management."""

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        pass

    @property
    @abstractmethod
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform."""
        pass

    @abstractmethod
    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for this platform.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        pass

    @abstractmethod
    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the pool/curve address for trading pair.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint address (if applicable)

        Returns:
            Pool/curve address for the trading pair
        """
        pass

    @abstractmethod
    def derive_user_token_account(self, user: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive user's token account address.

        Args:
            user: User's wallet address
            mint: Token mint address

        Returns:
            User's token account address
        """
        pass

    @abstractmethod
    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get platform-specific additional accounts needed for trading.

        Args:
            token_info: Token information

        Returns:
            Dictionary of additional account addresses
        """
        pass


class InstructionBuilder(ABC):
    """Abstract interface for building platform-specific trading instructions."""

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        pass

    @abstractmethod
    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for the platform.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of quote tokens to spend
            minimum_amount_out: Minimum base tokens expected
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        pass

    @abstractmethod
    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for the platform.

        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of base tokens to sell
            minimum_amount_out: Minimum quote tokens expected
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        pass

    @abstractmethod
    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for buy operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        pass

    @abstractmethod
    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for sell operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        pass

    @abstractmethod
    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for buy operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for buy operations
        """
        pass

    @abstractmethod
    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for sell operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for sell operations
        """
        pass


class CurveManager(ABC):
    """Abstract interface for platform-specific price calculations and pool state management."""

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Get the platform this manager serves."""
        pass

    @abstractmethod
    async def get_pool_state(self, pool_address: Pubkey) -> dict[str, Any]:
        """Get the current state of a trading pool/curve.

        Args:
            pool_address: Address of the pool/curve

        Returns:
            Dictionary containing pool state data
        """
        pass

    @abstractmethod
    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Calculate current token price from pool state.

        Args:
            pool_address: Address of the pool/curve

        Returns:
            Current token price in quote token units
        """
        pass

    @abstractmethod
    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected tokens received for a buy operation.

        Args:
            pool_address: Address of the pool/curve
            amount_in: Amount of quote tokens to spend

        Returns:
            Expected amount of base tokens to receive
        """
        pass

    @abstractmethod
    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected quote tokens received for a sell operation.

        Args:
            pool_address: Address of the pool/curve
            amount_in: Amount of base tokens to sell

        Returns:
            Expected amount of quote tokens to receive
        """
        pass

    @abstractmethod
    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Get current pool reserves.

        Args:
            pool_address: Address of the pool/curve

        Returns:
            Tuple of (base_reserves, quote_reserves)
        """
        pass


class EventParser(ABC):
    """Abstract interface for parsing platform-specific token creation events."""

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Get the platform this parser serves."""
        pass

    @abstractmethod
    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo | None:
        """Parse token creation from transaction logs.

        Args:
            logs: List of log strings from transaction
            signature: Transaction signature

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        pass

    @abstractmethod
    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        """Parse token creation from instruction data.

        Args:
            instruction_data: Raw instruction data
            accounts: List of account indices
            account_keys: List of account public keys

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        pass

    @abstractmethod
    def parse_token_creation_from_geyser(
        self, transaction_info: Any
    ) -> TokenInfo | None:
        """Parse token creation from Geyser transaction data.

        Args:
            transaction_info: Geyser transaction information

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        pass

    @abstractmethod
    def parse_token_creation_from_block(
        self, block_data: dict[str, Any]
    ) -> TokenInfo | None:
        """Parse token creation from block data.

        Args:
            block_data: Block data containing transactions

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        pass

    @abstractmethod
    def get_program_id(self) -> Pubkey:
        """Get the program ID this parser monitors.

        Returns:
            Program ID for event filtering
        """
        pass

    @abstractmethod
    def get_instruction_discriminators(self) -> list[bytes]:
        """Get instruction discriminators for token creation.

        Returns:
            List of discriminator bytes to match
        """
        pass

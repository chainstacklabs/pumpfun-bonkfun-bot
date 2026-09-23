"""Abstract base classes each trading platform implements."""

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


class ConfirmationStatus(Enum):
    """Outcome of asking the RPC what happened to a submitted transaction.

    "Did not succeed" is two answers, and conflating them fires a
    non-idempotent retry at a trade that already went through:

    - `REVERTED` is a fact: the transaction landed and the program errored.
    - `UNCONFIRMED` is an absence of information — the lookup ran out of budget,
      which says nothing about whether the transaction landed.

    Retrying is right for the first and wrong for the second.
    """

    SUCCESS = "success"
    REVERTED = "reverted"
    UNCONFIRMED = "unconfirmed"


class TradeFailureReason(Enum):
    """Why a trade did not complete, as far as the trader can tell.

    Mirrors :class:`ConfirmationStatus` and adds the case that never reached an
    RPC at all, so a caller can tell "the chain rejected it" from "we never found
    out" from "it never left the building".
    """

    REVERTED = "reverted"
    UNCONFIRMED = "unconfirmed"
    SUBMIT_FAILED = "submit_failed"


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
    token_program_id: Pubkey | None = None  # Token or Token2022 program
    is_mayhem_mode: bool = False  # pump.fun mayhem mode flag

    # pump.fun cashback coin flag. create_v2 rejects new cashback coins with
    # 6082 CashbackDeprecated, but existing ones keep trading and accruing, so
    # every cashback code path reading this flag stays live.
    is_cashback_coin: bool = False

    # Holder-reward coins: the creator fee is set aside for holders instead of a
    # creator wallet, and BondingCurve.creator holds a pump.fun address. Trading
    # is identical — creator_vault derivation and the buy_v2/sell_v2 account
    # lists are unchanged — so this is informational and available for filtering.
    # creator_fee_bps is non-zero only on custom-pair coins; SOL- and USDC-paired
    # coins use the standard fee schedule (bps 0).
    is_holder_reward: bool = False
    creator_fee_bps: int = 0

    # Quote asset (pump.fun v2 instructions). SOL-paired coins carry
    # Pubkey::default() on-chain; normalize_quote_mint() maps that to wrapped
    # SOL, which is what buy_v2/sell_v2 expect to be passed.
    quote_mint: Pubkey | None = None
    quote_token_program_id: Pubkey | None = None
    virtual_quote_reserves: int | None = None

    # True when creator, mayhem/cashback flags and quote_mint were read from the
    # on-chain CreateEvent, letting extreme_fast_mode skip the pre-buy curve
    # refresh — zero RPC calls between detection and submission. Listeners that
    # guess any of these, or read them from user-supplied instruction args, must
    # leave it False.
    state_from_event: bool = False

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
        """
        pass

    @abstractmethod
    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get platform-specific additional accounts needed for trading.

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

    def get_creation_filter_accounts(self) -> list[Pubkey]:
        """Get accounts to subscribe on when only creations are wanted.

        A subscription filtered on the program id sees every transaction the
        program handles, which is overwhelmingly trades. Where a platform has
        an account that only its creation instructions touch, naming it here
        moves that filtering server-side. The pre-execution shreds listener
        needs this: the unfiltered firehose makes a Python consumer lag, and a
        lagging deshred stream is terminated by the server rather than slowed.

        Defaults to the program id, which is correct but unselective.

        Returns:
            Accounts a creation transaction is guaranteed to mention
        """
        return [self.get_program_id()]

    @abstractmethod
    def get_instruction_discriminators(self) -> list[bytes]:
        """Get instruction discriminators for token creation.

        Returns:
            List of discriminator bytes to match
        """
        pass

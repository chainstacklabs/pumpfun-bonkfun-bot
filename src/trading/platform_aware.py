"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS
from core.wallet import Wallet
from interfaces.core import AddressProvider, Platform, TokenInfo
from platforms import get_platform_implementations
from trading.base import Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)


class PlatformAwareBuyer(Trader):
    """Platform-aware token buyer that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 5,
        extreme_fast_token_amount: int = 0,
        extreme_fast_mode: bool = False,
        compute_units: dict | None = None,
    ):
        """Initialize platform-aware token buyer."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.compute_units = compute_units or {}

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Execute buy operation using platform-specific implementations."""
        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Convert amount to lamports
            amount_lamports = int(self.amount * LAMPORTS_PER_SOL)

            if self.extreme_fast_mode:
                # Skip the wait and directly calculate the amount
                token_amount = self.extreme_fast_token_amount
                token_price_sol = self.amount / token_amount if token_amount > 0 else 0
            else:
                # Get pool address based on platform using platform-agnostic method
                pool_address = self._get_pool_address(token_info, address_provider)

                # Regular behavior with RPC call
                # Fetch pool state to get price and mayhem mode status
                pool_state = await curve_manager.get_pool_state(pool_address)
                token_price_sol = pool_state.get("price_per_token")

                # Validate price_per_token is present and positive
                if token_price_sol is None or token_price_sol <= 0:
                    raise ValueError(
                        f"Invalid price_per_token: {token_price_sol} for pool {pool_address} "
                        f"(mint: {token_info.mint}) - cannot execute buy with zero/invalid price"
                    )

                # Set is_mayhem_mode from bonding curve state
                token_info.is_mayhem_mode = pool_state.get("is_mayhem_mode", False)
                token_amount = self.amount / token_price_sol

            # Calculate minimum token amount with slippage
            minimum_token_amount = token_amount * (1 - self.slippage)
            minimum_token_amount_raw = int(minimum_token_amount * 10**TOKEN_DECIMALS)

            # Calculate maximum SOL to spend with slippage
            max_amount_lamports = int(amount_lamports * (1 + self.slippage))

            # Build buy instructions using platform-specific builder
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                max_amount_lamports,  # amount_in (SOL)
                minimum_token_amount_raw,  # minimum_amount_out (tokens)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )

            logger.info(
                f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} SOL per token on {token_info.platform.value}"
            )
            logger.info(
                f"Total cost: {self.amount:.6f} SOL (max: {max_amount_lamports / LAMPORTS_PER_SOL:.6f} SOL)"
            )

            # Send transaction
            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    priority_accounts
                ),
                compute_unit_limit=instruction_builder.get_buy_compute_unit_limit(
                    self._get_cu_override("buy", token_info.platform)
                ),
                account_data_size_limit=self._get_cu_override(
                    "account_data_size", token_info.platform
                ),
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Buy transaction confirmed: {tx_signature}")

                # Fetch actual tokens and SOL spent from transaction
                # Uses preBalances/postBalances to get exact amounts
                sol_destination = self._get_sol_destination(
                    token_info, address_provider
                )
                tokens_raw, sol_spent = await self.client.get_buy_transaction_details(
                    str(tx_signature), token_info.mint, sol_destination
                )

                if tokens_raw is not None and sol_spent is not None:
                    actual_amount = tokens_raw / 10**TOKEN_DECIMALS
                    actual_price = (sol_spent / LAMPORTS_PER_SOL) / actual_amount
                    logger.info(
                        f"Actual tokens received: {actual_amount:.6f} "
                        f"(expected: {token_amount:.6f})"
                    )
                    logger.info(
                        f"Actual SOL spent: {sol_spent / LAMPORTS_PER_SOL:.10f} SOL"
                    )
                    logger.info(f"Actual price: {actual_price:.10f} SOL/token")
                    token_amount = actual_amount
                    token_price_sol = actual_price
                else:
                    raise ValueError(
                        f"Failed to parse transaction details: tokens={tokens_raw}, "
                        f"sol_spent={sol_spent}"
                    )

                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_amount,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.exception("Buy operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _get_sol_destination(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the address where SOL is sent during a buy transaction.

        For pump.fun: SOL goes to the bonding curve
        For letsbonk: SOL goes to the quote_vault (WSOL vault)

        Args:
            token_info: Token information
            address_provider: Platform-specific address provider

        Returns:
            Address where SOL is transferred during buy

        Raises:
            NotImplementedError: If platform SOL destination is not implemented
        """
        if token_info.platform == Platform.PUMP_FUN:
            # For pump.fun, SOL goes directly to bonding curve
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
            return address_provider.derive_pool_address(token_info.mint)
        elif token_info.platform == Platform.LETS_BONK:
            # For letsbonk, SOL goes to quote_vault (WSOL vault)
            if hasattr(token_info, "quote_vault") and token_info.quote_vault:
                return token_info.quote_vault
            # Derive quote_vault if not available
            return address_provider.derive_quote_vault(token_info.mint)

        raise NotImplementedError(
            f"SOL destination not implemented for platform {token_info.platform.value}. "
            f"Add platform-specific logic to _get_sol_destination() to specify where "
            f"SOL is transferred during buy transactions for this platform."
        )

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)


class PlatformAwareSeller(Trader):
    """Platform-aware token seller that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        slippage: float = 0.25,
        max_retries: int = 5,
        compute_units: dict | None = None,
    ):
        """Initialize platform-aware token seller."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.compute_units = compute_units or {}

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        """Execute sell operation using platform-specific implementations.

        Args:
            token_info: Token information for the sell operation
            token_amount: Token amount to sell (from buy result). Required to avoid
                         RPC balance query delays.
            token_price: Token price in SOL (from buy result). Required to avoid
                        RPC pool state query delays.

        Returns:
            TradeResult with operation outcome

        Raises:
            ValueError: If required parameters are not provided
        """
        if token_amount is None:
            raise ValueError(
                "token_amount is required for sell operation. "
                "Pass the amount from buy result to avoid RPC delays."
            )
        if token_price is None or token_price <= 0:
            raise ValueError(
                "token_price is required for sell operation and must be positive. "
                "Pass the price from buy result to avoid RPC delays."
            )

        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder

            # Use pre-known amount and price (no RPC delay)
            token_balance_decimal = token_amount
            token_balance = int(token_amount * 10**TOKEN_DECIMALS)
            token_price_sol = token_price

            logger.info(f"Token balance: {token_balance_decimal:.6f}")
            logger.info(f"Price per Token (from buy): {token_price_sol:.8f} SOL")

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="No tokens to sell",
                )

            # Calculate expected SOL output with slippage protection
            expected_sol_output = token_balance_decimal * token_price_sol
            min_sol_output = max(
                1,
                int((expected_sol_output * (1 - self.slippage)) * LAMPORTS_PER_SOL),
            )
            logger.info(
                f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
            )
            logger.info(f"Expected SOL output: {expected_sol_output:.10f} SOL")
            logger.info(
                f"Minimum SOL output (with {self.slippage * 100:.1f}% slippage): "
                f"{min_sol_output / LAMPORTS_PER_SOL:.10f} SOL ({min_sol_output} lamports)"
            )

            # Build sell instructions using platform-specific builder
            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,  # amount_in (tokens)
                min_sol_output,  # minimum_amount_out (SOL)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_sell(
                token_info, self.wallet.pubkey, address_provider
            )

            # Send transaction
            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    priority_accounts
                ),
                compute_unit_limit=instruction_builder.get_sell_compute_unit_limit(
                    self._get_cu_override("sell", token_info.platform)
                ),
                account_data_size_limit=self._get_cu_override(
                    "account_data_size", token_info.platform
                ),
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Sell transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_balance_decimal,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.exception("Sell operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)

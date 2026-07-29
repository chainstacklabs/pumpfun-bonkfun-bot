"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

import asyncio
from time import monotonic

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    SystemAddresses,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
)
from core.wallet import Wallet
from interfaces.core import AddressProvider, Platform, TokenInfo
from platforms import get_platform_implementations
from trading.base import Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)


def _quote_symbol(quote_mint: Pubkey) -> str:
    """Human-readable label for a quote mint, for logging only.

    Args:
        quote_mint: Quote mint address

    Returns:
        "SOL" for wrapped SOL, otherwise a truncated mint address
    """
    if is_sol_paired(quote_mint):
        return "SOL"
    mint_str = str(quote_mint)
    return f"{mint_str[:4]}..{mint_str[-4:]}"


async def _read_pool_state_with_retry(
    curve_manager: object,
    pool_address: Pubkey,
    mint: Pubkey | None = None,
    budget_seconds: float = 2.0,
    delay_seconds: float = 0.15,
) -> tuple[dict, Pubkey | None]:
    """Read bonding curve state, retrying within a time budget on a lagging node.

    A freshly created curve may not be visible at `confirmed` yet, and a node
    can momentarily serve a slot that predates it — both surface as "account
    not found". Reading at `processed` and retrying costs a handful of RPC
    calls, which is far cheaper than trading on stale account data. Issue #170
    measured individual reads on a load-balanced endpoint lagging several
    seconds behind a fast listener, hence a time budget rather than a fixed
    attempt count.

    When `mint` is given and the curve manager supports it, the curve and the
    mint are read in one slot-consistent batch so the mint's owning token
    program comes back for free (pumpportal listeners can only guess it).

    Args:
        curve_manager: Platform curve manager
        pool_address: Bonding curve / pool address
        mint: Optional token mint to read alongside the curve
        budget_seconds: Total time to keep retrying before giving up
        delay_seconds: Pause between attempts

    Returns:
        Tuple of (decoded pool state, token program id or None if unknown)

    Raises:
        Exception: The last read error if every attempt fails
    """
    batch_read = mint is not None and hasattr(
        curve_manager, "get_pool_state_and_token_program"
    )
    deadline = monotonic() + budget_seconds
    last_error: Exception | None = None
    while True:
        try:
            if batch_read:
                result = await curve_manager.get_pool_state_and_token_program(
                    pool_address, mint, commitment="processed"
                )
            else:
                state = await curve_manager.get_pool_state(
                    pool_address, commitment="processed"
                )
                result = (state, None)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if monotonic() + delay_seconds > deadline:
                break
            await asyncio.sleep(delay_seconds)
        else:
            return result

    raise last_error or RuntimeError("pool_state unavailable after retries")


def _refresh_quote_mint(token_info: TokenInfo, pool_state: dict) -> Pubkey:
    """Sync token_info's quote asset from freshly-read curve state.

    Listeners do not all carry quote_mint (pumpportal carries none of the
    per-coin flags), and the curve is authoritative, so prefer its value.

    Args:
        token_info: Token information, mutated in place
        pool_state: Decoded bonding curve state

    Returns:
        The resolved quote mint
    """
    quote_mint = normalize_quote_mint(
        pool_state.get("quote_mint", token_info.quote_mint)
    )
    token_info.quote_mint = quote_mint
    return quote_mint


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
        quote_amounts: dict[Pubkey, float] | None = None,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
    ):
        """Initialize platform-aware token buyer.

        Args:
            client: Solana RPC client
            wallet: Trading wallet
            priority_fee_manager: Priority fee strategy
            amount: Amount of SOL to spend per buy on SOL-paired coins
            slippage: Acceptable price deviation
            max_retries: Transaction submission attempts
            extreme_fast_token_amount: Tokens to buy when skipping price checks
            extreme_fast_mode: Skip curve stabilization and price check
            compute_units: Optional CU overrides
            quote_amounts: Per-quote-mint spend amounts in whole quote units,
                for coins paired against something other than SOL. A coin whose
                quote mint is absent from this map is skipped rather than
                traded with a SOL-denominated amount.
            curve_refresh_budget: Seconds to keep retrying the pre-buy curve
                read before skipping the token. A buy built without fresh curve
                state guesses fee_recipient/creator_vault and tends to revert
                on-chain (issue #170), so skipping beats racing.
            trust_create_event: Skip the pre-buy curve read entirely for
                TokenInfo marked state_from_event (creator/flags/quote_mint
                read from the on-chain CreateEvent) — extreme_fast_mode then
                makes zero RPC calls between detection and submission. Set
                False to force the refresh for every listener.
        """
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.compute_units = compute_units or {}
        self.curve_refresh_budget = curve_refresh_budget
        self.trust_create_event = trust_create_event
        # SOL-paired coins always use `amount`; other quotes need an explicit
        # per-mint amount because 0.0001 USDC and 0.0001 SOL are not comparable.
        self.quote_amounts: dict[Pubkey, float] = {
            WSOL_MINT: amount,
            **(quote_amounts or {}),
        }

    def _resolve_quote_amount(self, quote_mint: Pubkey) -> float | None:
        """Get the configured spend amount for a quote mint.

        Args:
            quote_mint: Normalized quote mint

        Returns:
            Amount in whole quote units, or None if this quote is not configured
        """
        return self.quote_amounts.get(quote_mint)

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

            # Quote asset is resolved from the curve below; start from whatever
            # the listener gave us so extreme_fast_mode has a usable default.
            quote_mint = normalize_quote_mint(token_info.quote_mint)

            if self.extreme_fast_mode:
                # Zero-RPC hot path — the point of extreme_fast_mode. When the
                # CreateEvent already carried the canonical creator, the
                # mayhem/cashback flags and quote_mint, nothing sits between
                # detection and submission. Otherwise (pumpportal, old-format
                # events) refresh from chain or skip.
                if not self._can_skip_refresh(token_info):
                    skip_reason = await self._refresh_curve_state(
                        token_info, address_provider, curve_manager
                    )
                    if skip_reason is not None:
                        return TradeResult(
                            success=False,
                            platform=token_info.platform,
                            error_message=skip_reason,
                        )
                    quote_mint = normalize_quote_mint(token_info.quote_mint)
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

                # Set mayhem-mode and cashback flags from bonding-curve state
                # so the instruction builder picks the correct fee_recipient and
                # account-list shape (cashback sells use 17 accounts, non-cashback 16).
                token_info.is_mayhem_mode = pool_state.get("is_mayhem_mode", False)
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)

            # A coin paired against a quote asset we have no configured amount
            # for cannot be traded — spending `amount` of it would be a
            # different order of magnitude entirely.
            quote_amount = self._resolve_quote_amount(quote_mint)
            if quote_amount is None:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=(
                        f"No configured buy amount for quote mint {quote_mint}; "
                        f"set trade.quote_amounts for this mint to trade it"
                    ),
                )

            quote_unit = quote_units_per_token(quote_mint)
            quote_label = _quote_symbol(quote_mint)

            # Both branches need the resolved quote amount to finish sizing the
            # trade: extreme_fast_mode fixes the token count and back-derives an
            # implied price, while the regular path fixes the spend and derives
            # the token count from the curve price.
            if self.extreme_fast_mode:
                token_amount = self.extreme_fast_token_amount
                token_price_sol = quote_amount / token_amount if token_amount > 0 else 0
            else:
                token_amount = quote_amount / token_price_sol

            # Calculate minimum token amount with slippage
            minimum_token_amount = token_amount * (1 - self.slippage)
            minimum_token_amount_raw = int(minimum_token_amount * 10**TOKEN_DECIMALS)

            # Calculate maximum quote to spend with slippage, in the quote
            # mint's own raw units (lamports for SOL, 1e-6 for USDC).
            max_quote_amount_raw = int(quote_amount * quote_unit * (1 + self.slippage))

            # Build buy instructions using platform-specific builder
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                max_quote_amount_raw,  # amount_in (raw quote units)
                minimum_token_amount_raw,  # minimum_amount_out (tokens)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )

            logger.info(
                f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} "
                f"{quote_label} per token on {token_info.platform.value}"
            )
            logger.info(
                f"Total cost: {quote_amount:.6f} {quote_label} "
                f"(max: {max_quote_amount_raw / quote_unit:.6f} {quote_label})"
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
                tokens_raw, quote_spent = await self.client.get_buy_transaction_details(
                    str(tx_signature),
                    token_info.mint,
                    sol_destination,
                    quote_mint=quote_mint,
                )

                if tokens_raw is not None and quote_spent is not None:
                    actual_amount = tokens_raw / 10**TOKEN_DECIMALS
                    actual_price = (quote_spent / quote_unit) / actual_amount
                    logger.info(
                        f"Actual tokens received: {actual_amount:.6f} "
                        f"(expected: {token_amount:.6f})"
                    )
                    logger.info(
                        f"Actual {quote_label} spent: "
                        f"{quote_spent / quote_unit:.10f} {quote_label}"
                    )
                    logger.info(
                        f"Actual price: {actual_price:.10f} {quote_label}/token"
                    )
                    token_amount = actual_amount
                    token_price_sol = actual_price
                else:
                    raise ValueError(
                        f"Failed to parse transaction details: tokens={tokens_raw}, "
                        f"quote_spent={quote_spent} (tx: {tx_signature}). "
                        f"The transaction may have failed on-chain — check explorer."
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

    def _can_skip_refresh(self, token_info: TokenInfo) -> bool:
        """Whether the pre-buy curve read can be skipped entirely.

        True when the listener read creator, mayhem/cashback and quote_mint
        from the on-chain CreateEvent (canonical at create time), keeping
        extreme_fast_mode at zero RPC calls between detection and submission.

        Args:
            token_info: Token information from the listener

        Returns:
            True if the buy can be built from token_info as-is
        """
        return (
            self.trust_create_event
            and token_info.state_from_event
            and token_info.quote_mint is not None
        )

    async def _refresh_curve_state(
        self,
        token_info: TokenInfo,
        address_provider: AddressProvider,
        curve_manager: object,
    ) -> str | None:
        """Refresh mayhem/cashback/creator/quote_mint/token program from chain.

        Listeners that guess these (pumpportal carries none of them) produce
        buys the program rejects with NotAuthorized (0x1770) / ConstraintSeeds
        (0x7d6) when fee_recipient or creator_vault is wrong. PumpPortal also
        notifies before the BC account is readable on a lagging node, so the
        read retries within curve_refresh_budget.

        Args:
            token_info: Token information, mutated in place on success
            address_provider: Platform address provider
            curve_manager: Platform curve manager

        Returns:
            None on success; on failure a reason to skip the buy — a buy built
            from listener-guessed defaults tends to revert on-chain
            (issue #170: 0x1770 / 0x7d6 / pool 3012), which still costs the fee
        """
        try:
            pool_address = self._get_pool_address(token_info, address_provider)
            # Geyser/logs fire on processed, so the BC is typically readable in
            # the same slot; pumpportal occasionally races the on-chain commit,
            # hence the retries.
            pool_state, fresh_token_program = await _read_pool_state_with_retry(
                curve_manager,
                pool_address,
                mint=token_info.mint,
                budget_seconds=self.curve_refresh_budget,
            )
        except Exception as e:  # noqa: BLE001
            return (
                f"Curve state unreadable within {self.curve_refresh_budget:.1f}s "
                f"({e}); skipping buy rather than submitting with guessed accounts"
            )

        token_info.is_mayhem_mode = pool_state.get(
            "is_mayhem_mode", token_info.is_mayhem_mode
        )
        token_info.is_cashback_coin = pool_state.get(
            "is_cashback_coin", token_info.is_cashback_coin
        )
        # The quote asset decides which balance we spend and how amounts are
        # scaled, so it must come from the curve rather than a listener guess.
        _refresh_quote_mint(token_info, pool_state)
        fresh_creator = pool_state.get("creator")
        if fresh_creator and hasattr(address_provider, "derive_creator_vault"):
            new_creator = (
                Pubkey.from_string(fresh_creator)
                if isinstance(fresh_creator, str)
                else fresh_creator
            )
            token_info.creator = new_creator
            token_info.creator_vault = address_provider.derive_creator_vault(
                new_creator
            )
        self._apply_token_program(token_info, fresh_token_program, address_provider)
        return None

    def _apply_token_program(
        self,
        token_info: TokenInfo,
        token_program: Pubkey | None,
        address_provider: AddressProvider,
    ) -> None:
        """Correct a listener-guessed token program from the mint's real owner.

        PumpPortal payloads carry no token program, so the processor defaults
        to Token-2022; a legacy-`create` coin is SPL Token and the ATA-create
        instruction then fails with IncorrectProgramId. The associated bonding
        curve is an ordinary ATA, so it must be re-derived under the corrected
        program too.

        Args:
            token_info: Token information, mutated in place
            token_program: Owner of the mint account, or None if unknown
            address_provider: Platform address provider for ATA derivation
        """
        known_programs = (
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        )
        if token_program is None or token_program not in known_programs:
            return
        if token_info.token_program_id == token_program:
            return
        logger.info(
            f"Correcting token program for {token_info.mint}: "
            f"{token_info.token_program_id} -> {token_program}"
        )
        token_info.token_program_id = token_program
        if token_info.bonding_curve and hasattr(
            address_provider, "derive_associated_bonding_curve"
        ):
            token_info.associated_bonding_curve = (
                address_provider.derive_associated_bonding_curve(
                    token_info.mint, token_info.bonding_curve, token_program
                )
            )

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
            curve_manager = implementations.curve_manager

            # Fall back to the listener's quote asset if the refresh below fails.
            quote_mint = normalize_quote_mint(token_info.quote_mint)

            # Refresh mayhem-mode and cashback flags from curve state.
            # The sell account list is 16 (non-cashback) vs 17 (cashback), and
            # fee_recipient differs in mayhem mode — both can change between
            # buy and sell, so re-read from chain instead of trusting create-time
            # flags carried in token_info.
            try:
                pool_address = self._get_pool_address(token_info, address_provider)
                # Retry rather than reading once at `confirmed`: a node serving a
                # slightly stale slot reports the curve as missing, and silently
                # falling back to create-time values risks a wrong creator_vault
                # (ConstraintSeeds 0x7d6) or wrong mayhem fee_recipient.
                pool_state, _ = await _read_pool_state_with_retry(
                    curve_manager, pool_address
                )
                token_info.is_mayhem_mode = pool_state.get(
                    "is_mayhem_mode", token_info.is_mayhem_mode
                )
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)
                # Refresh creator/creator_vault from current BC state. Post
                # 2026-04-28 the program may delegate BC.creator to a PFEE-owned
                # PDA after the initial creator buy, so the create-time vault
                # cached on token_info goes stale before the sell lands. Failing
                # to refresh manifests as ConstraintSeeds (0x7d6) on Sell.
                fresh_creator = pool_state.get("creator")
                if fresh_creator:
                    from solders.pubkey import Pubkey as _Pubkey

                    new_creator = (
                        _Pubkey.from_string(fresh_creator)
                        if isinstance(fresh_creator, str)
                        else fresh_creator
                    )
                    token_info.creator = new_creator
                    token_info.creator_vault = address_provider.derive_creator_vault(
                        new_creator
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Could not refresh curve flags before sell ({e}); "
                    f"using token_info values is_mayhem_mode={token_info.is_mayhem_mode}, "
                    f"is_cashback_coin={token_info.is_cashback_coin}"
                )

            quote_unit = quote_units_per_token(quote_mint)
            quote_label = _quote_symbol(quote_mint)

            # Use pre-known amount and price (no RPC delay)
            token_balance_decimal = token_amount
            token_balance = int(token_amount * 10**TOKEN_DECIMALS)
            token_price_sol = token_price

            logger.info(f"Token balance: {token_balance_decimal:.6f}")
            logger.info(
                f"Price per Token (from buy): {token_price_sol:.8f} {quote_label}"
            )

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="No tokens to sell",
                )

            # Calculate expected quote output with slippage protection, in the
            # quote mint's raw units.
            expected_quote_output = token_balance_decimal * token_price_sol
            min_quote_output = max(
                1,
                int((expected_quote_output * (1 - self.slippage)) * quote_unit),
            )
            logger.info(
                f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
            )
            logger.info(
                f"Expected {quote_label} output: {expected_quote_output:.10f} {quote_label}"
            )
            logger.info(
                f"Minimum {quote_label} output (with {self.slippage * 100:.1f}% slippage): "
                f"{min_quote_output / quote_unit:.10f} {quote_label} "
                f"({min_quote_output} raw units)"
            )

            # Build sell instructions using platform-specific builder
            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,  # amount_in (tokens)
                min_quote_output,  # minimum_amount_out (raw quote units)
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

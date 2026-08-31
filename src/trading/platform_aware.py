"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

import asyncio
from decimal import ROUND_DOWN, Decimal
from math import isfinite
from time import monotonic

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from core.client import (
    SolanaClient,
    TransactionStatus,
    TransactionSubmissionUnknown,
)
from core.execution_policy import ExecutionMode
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    SystemAddresses,
    get_quote_asset,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
)
from core.quote_engine import minimum_output_with_slippage
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


def _additional_native_buy_recipients(
    platform: Platform,
    instructions: list[Instruction],
    address_provider: AddressProvider,
    primary_destination: Pubkey,
) -> tuple[Pubkey, ...]:
    """Extract every native-SOL fee recipient from the built venue instruction."""
    if not instructions:
        return ()
    program_id = getattr(address_provider, "program_id", None)
    if not isinstance(program_id, Pubkey):
        raise ValueError("Address provider has no canonical program id")
    venue_instructions = [
        instruction
        for instruction in instructions
        if instruction.program_id == program_id
    ]
    if len(venue_instructions) != 1:
        raise ValueError(
            "Expected exactly one venue instruction for receipt accounting"
        )
    accounts = list(venue_instructions[0].accounts)
    if platform is Platform.PUMP_FUN:
        if len(accounts) == 27:
            primary_index = 10
            additional_indexes = (6, 8, 16)
        elif len(accounts) == 18:
            primary_index = 3
            additional_indexes = (1, 9, 17)
        else:
            raise ValueError(f"Unsupported Pump.fun buy account count: {len(accounts)}")
    elif platform is Platform.LETS_BONK:
        if len(accounts) < 17:
            raise ValueError(f"Unsupported LetsBonk buy account count: {len(accounts)}")
        primary_index = 8
        additional_indexes = (16, *((17,) if len(accounts) > 17 else ()))
    else:
        raise ValueError(
            f"Unsupported platform for native receipt accounting: {platform}"
        )

    if accounts[primary_index].pubkey != primary_destination:
        raise ValueError("Built venue instruction disagrees with its SOL destination")
    recipients = tuple(accounts[index].pubkey for index in additional_indexes)
    if any(not isinstance(recipient, Pubkey) for recipient in recipients):
        raise ValueError("Built venue instruction contains an invalid SOL recipient")
    if primary_destination in recipients or len(set(recipients)) != len(recipients):
        raise ValueError("Built venue instruction contains duplicate SOL recipients")
    return recipients


async def _exact_native_receipt_destinations(
    client: SolanaClient,
    signature: str,
    local_destinations: tuple[Pubkey, ...],
) -> tuple[Pubkey, ...]:
    """Prefer the destination set bound to the exact durable wire transaction."""
    reader = getattr(client, "get_submission_receipt_destinations", None)
    if callable(reader):
        durable_destinations = await reader(signature)
        if durable_destinations is not None:
            if (
                not durable_destinations
                or any(
                    not isinstance(destination, Pubkey)
                    for destination in durable_destinations
                )
                or len(set(durable_destinations)) != len(durable_destinations)
            ):
                raise ValueError("Durable native receipt destinations are invalid")
            return durable_destinations
        if getattr(client, "ledger", None) is not None:
            raise ValueError(
                "Submitted transaction has no durable native receipt context"
            )
    if not local_destinations:
        raise ValueError("Native buy has no receipt destinations")
    return local_destinations


def _to_raw_units(amount: float, unit: int, field_name: str) -> int:
    """Convert a presentation amount to raw units without binary-float drift."""
    decimal_amount = Decimal(str(amount))
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    if isinstance(unit, bool) or not isinstance(unit, int) or unit <= 0:
        raise ValueError("unit must be a positive integer")
    raw_amount = int((decimal_amount * unit).to_integral_value(rounding=ROUND_DOWN))
    if raw_amount <= 0:
        raise ValueError(f"{field_name} is below one raw unit")
    return raw_amount


def _slippage_bps(slippage: float) -> int:
    """Validate and convert a fractional slippage value into basis points."""
    decimal_slippage = Decimal(str(slippage))
    if (
        not decimal_slippage.is_finite()
        or decimal_slippage < 0
        or decimal_slippage >= 1
    ):
        raise ValueError("slippage must be finite and between 0 (inclusive) and 1")
    return int((decimal_slippage * 10_000).to_integral_value(rounding=ROUND_DOWN))


def _base_unit(token_info: TokenInfo) -> int:
    """Return the authoritative base-token unit for execution accounting."""
    decimals = token_info.base_decimals
    if decimals is None and token_info.platform is Platform.PUMP_FUN:
        decimals = TOKEN_DECIMALS
    if (
        isinstance(decimals, bool)
        or not isinstance(decimals, int)
        or not 0 <= decimals <= 18
    ):
        raise ValueError(
            f"Unsupported or unknown base decimals for {token_info.mint}: {decimals}"
        )
    token_info.base_decimals = decimals
    return 10**decimals


def _require_executable_quote_contract(
    token_info: TokenInfo, client: SolanaClient
) -> None:
    """Reject live Pump.fun execution until its dynamic fees are sourced."""
    policy = getattr(client, "execution_policy", None)
    if (
        token_info.platform is Platform.PUMP_FUN
        and policy is not None
        and policy.mode is ExecutionMode.LIVE
    ):
        raise RuntimeError(
            "Pump.fun dynamic protocol and creator fees are unavailable; "
            "refusing to derive an executable minimum from a pre-fee quote"
        )


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
    """Sync and validate quote-asset metadata from authoritative pool state."""
    asset = get_quote_asset(pool_state.get("quote_mint", token_info.quote_mint))
    state_program = pool_state.get("quote_token_program")
    if state_program is not None and state_program != asset.token_program:
        raise ValueError(
            "Pool quote token program does not match the registered quote mint"
        )
    if token_info.quote_token_program_id not in (None, asset.token_program):
        raise ValueError(
            "Listener quote token program conflicts with authoritative quote metadata"
        )
    state_decimals = pool_state.get("quote_decimals")
    if state_decimals is not None and (
        type(state_decimals) is not int or state_decimals != asset.decimals
    ):
        raise ValueError("Pool quote decimals do not match the registered quote mint")

    token_info.quote_mint = asset.mint
    token_info.quote_token_program_id = asset.token_program
    token_info.quote_decimals = asset.decimals
    return asset.mint


def _record_executable_state(token_info: TokenInfo, pool_state: dict) -> None:
    """Require current venue state to remain executable before building a tx."""
    if not isinstance(pool_state, dict):
        raise ValueError("Pool state must be a mapping")
    if token_info.platform is Platform.PUMP_FUN:
        if pool_state.get("complete") is not False:
            raise ValueError(
                "Pump.fun bonding curve is complete or missing its completion flag"
            )
        token_info.curve_complete = False
        token_info.pool_tradeable = True
        token_info.pool_status = "funding"
        return

    if token_info.platform is Platform.LETS_BONK:
        if pool_state.get("is_tradeable") is not True:
            raise ValueError("LetsBonk pool is not tradeable")
        status_name = pool_state.get("status_name")
        status = pool_state.get("status")
        if not isinstance(status_name, str):
            status_name = getattr(status, "name", "").lower()
        if status_name != "funding":
            raise ValueError(
                f"LetsBonk pool status is not FUNDING: {status_name or status!r}"
            )
        token_info.curve_complete = False
        token_info.pool_tradeable = True
        token_info.pool_status = status_name
        return

    raise ValueError(f"Unsupported trading platform: {token_info.platform!r}")


def _require_recorded_executable_state(token_info: TokenInfo) -> None:
    """Validate state carried by a no-RPC creation-event execution path."""
    if token_info.platform is Platform.PUMP_FUN:
        if token_info.curve_complete is not False:
            raise ValueError(
                "Pump.fun extreme-fast buy lacks authoritative incomplete-curve state"
            )
        return
    raise ValueError(
        "Extreme-fast execution requires a fresh authoritative pool read for "
        f"{token_info.platform.value}"
    )


def _apply_token_program(
    token_info: TokenInfo,
    token_program: Pubkey | None,
    address_provider: AddressProvider,
) -> None:
    """Apply authoritative token-program metadata and validate mint-bound PDAs."""
    known_programs = (
        SystemAddresses.TOKEN_PROGRAM,
        SystemAddresses.TOKEN_2022_PROGRAM,
    )
    resolved_program = token_program or token_info.token_program_id
    if resolved_program not in known_programs:
        raise ValueError(
            f"Unsupported or unknown token program for {token_info.mint}: "
            f"{resolved_program}"
        )

    if (
        token_info.platform is Platform.PUMP_FUN
        and getattr(address_provider, "platform", None) is Platform.PUMP_FUN
    ):
        expected_curve = address_provider.derive_pool_address(token_info.mint)
        if (
            token_info.bonding_curve is not None
            and token_info.bonding_curve != expected_curve
        ):
            raise ValueError("Pump.fun bonding curve does not match the token mint")
        token_info.bonding_curve = expected_curve
        expected_associated_curve = address_provider.derive_associated_bonding_curve(
            token_info.mint, expected_curve, resolved_program
        )
        if (
            token_info.associated_bonding_curve is not None
            and token_info.associated_bonding_curve != expected_associated_curve
            and token_info.metadata_verified
        ):
            raise ValueError(
                "Verified Pump.fun associated bonding curve does not match the mint"
            )
        token_info.associated_bonding_curve = expected_associated_curve
        if token_info.creator is not None:
            expected_creator_vault = address_provider.derive_creator_vault(
                token_info.creator
            )
            if (
                token_info.creator_vault is not None
                and token_info.creator_vault != expected_creator_vault
            ):
                raise ValueError("Pump.fun creator vault does not match the creator")
            token_info.creator_vault = expected_creator_vault

    if token_info.token_program_id != resolved_program:
        logger.info(
            "Correcting token program for %s: %s -> %s",
            token_info.mint,
            token_info.token_program_id,
            resolved_program,
        )
        token_info.token_program_id = resolved_program


def _require_pool_pubkey(pool_state: dict, field_name: str) -> Pubkey:
    """Return a required authoritative pool pubkey."""
    value = pool_state.get(field_name)
    if not isinstance(value, Pubkey):
        raise ValueError(f"Pool state has invalid {field_name}")
    return value


def _sync_letsbonk_execution_metadata(
    token_info: TokenInfo,
    pool_state: dict,
    address_provider: AddressProvider,
) -> None:
    """Replace listener/journal LaunchLab accounts with authoritative state."""
    if token_info.platform is not Platform.LETS_BONK:
        return

    base_mint = _require_pool_pubkey(pool_state, "base_mint")
    quote_mint = _require_pool_pubkey(pool_state, "quote_mint")
    pool_address = _require_pool_pubkey(pool_state, "pool_address")
    base_token_program = _require_pool_pubkey(pool_state, "base_token_program")
    quote_token_program = _require_pool_pubkey(pool_state, "quote_token_program")
    if base_mint != token_info.mint:
        raise ValueError("LaunchLab pool base mint does not match the token")
    if quote_mint != token_info.quote_mint:
        raise ValueError("LaunchLab pool quote mint does not match refreshed metadata")
    if base_token_program != token_info.token_program_id:
        raise ValueError("LaunchLab base token program is inconsistent")
    if quote_token_program != token_info.quote_token_program_id:
        raise ValueError("LaunchLab quote token program is inconsistent")

    expected_pool = address_provider.derive_pool_address(base_mint, quote_mint)
    if pool_address != expected_pool:
        raise ValueError("LaunchLab pool address is not mint-bound")

    token_info.pool_state = pool_address
    token_info.base_vault = _require_pool_pubkey(pool_state, "base_vault")
    token_info.quote_vault = _require_pool_pubkey(pool_state, "quote_vault")
    token_info.global_config = _require_pool_pubkey(pool_state, "global_config")
    token_info.platform_config = _require_pool_pubkey(pool_state, "platform_config")
    token_info.creator = _require_pool_pubkey(pool_state, "creator")


class PlatformAwareBuyer(Trader):
    """Platform-aware token buyer that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 1,
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
            max_retries: Safety assertion; must be one because newly signed
                retries are disabled
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
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries != 1
        ):
            raise ValueError(
                "newly signed transaction retries are disabled; max_retries must be 1"
            )
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.slippage_bps = _slippage_bps(slippage)
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

    async def _read_pretrade_balance(self, token_info: TokenInfo) -> int | None:
        """Capture a cleanup ownership baseline when the token program is known."""
        if token_info.token_program_id is None:
            return None
        ata = self.wallet.get_associated_token_address(
            token_info.mint, token_info.token_program_id
        )
        try:
            await self.client.get_account_info(ata)
        except ValueError:
            return 0
        except Exception as exc:
            logger.warning(
                f"Could not record pre-buy ATA baseline for {token_info.mint}: {exc}"
            )
            return None
        return await self.client.get_token_account_balance(ata)

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Execute buy operation using platform-specific implementations."""
        token_amount: float | None = None
        token_price_sol: float | None = None
        expected_token_amount_raw: int | None = None
        max_quote_amount_raw: int | None = None
        account_balance_baseline_raw: int | None = None
        submitted_signature: str | None = None
        quote_mint: Pubkey | None = None
        zero_rpc_event_path = False
        native_receipt_destinations: tuple[Pubkey, ...] = ()
        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager
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
                else:
                    _require_recorded_executable_state(token_info)
                    zero_rpc_event_path = True
                    quote_mint = normalize_quote_mint(token_info.quote_mint)
                    pool_address = self._get_pool_address(token_info, address_provider)
            else:
                # Get pool address based on platform using platform-agnostic method
                pool_address = self._get_pool_address(token_info, address_provider)

                pool_state, fresh_token_program = await _read_pool_state_with_retry(
                    curve_manager,
                    pool_address,
                    mint=token_info.mint,
                    budget_seconds=self.curve_refresh_budget,
                )
                _record_executable_state(token_info, pool_state)
                token_info.base_decimals = pool_state.get(
                    "base_decimals", token_info.base_decimals
                )
                token_info.quote_decimals = pool_state.get(
                    "quote_decimals", token_info.quote_decimals
                )
                token_price_sol = pool_state.get("price_per_token")
                if token_price_sol is None or token_price_sol <= 0:
                    raise ValueError(
                        f"Invalid price_per_token: {token_price_sol} for "
                        f"pool {pool_address} (mint: {token_info.mint})"
                    )
                token_info.is_mayhem_mode = pool_state.get("is_mayhem_mode", False)
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)
                fresh_creator = pool_state.get("creator")
                derive_creator_vault = getattr(
                    address_provider, "derive_creator_vault", None
                )
                if fresh_creator and callable(derive_creator_vault):
                    new_creator = (
                        Pubkey.from_string(fresh_creator)
                        if isinstance(fresh_creator, str)
                        else fresh_creator
                    )
                    token_info.creator = new_creator
                    token_info.creator_vault = derive_creator_vault(new_creator)
                _apply_token_program(token_info, fresh_token_program, address_provider)
                _sync_letsbonk_execution_metadata(
                    token_info, pool_state, address_provider
                )

            if quote_mint is None:
                quote_mint = normalize_quote_mint(token_info.quote_mint)

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
            quote_amount_raw = _to_raw_units(quote_amount, quote_unit, "quote amount")
            _require_executable_quote_contract(token_info, self.client)

            # Use the venue's nonlinear exact-in quote whenever a curve read is
            # available. A marginal spot price is not a safe execution floor.
            if self.extreme_fast_mode:
                expected_token_amount_raw = _to_raw_units(
                    self.extreme_fast_token_amount,
                    _base_unit(token_info),
                    "extreme fast token amount",
                )
            else:
                expected_token_amount_raw = (
                    await curve_manager.calculate_buy_amount_out(
                        pool_address, quote_amount_raw
                    )
                )
                if expected_token_amount_raw <= 0:
                    raise ValueError("Platform buy quote returned no tokens")

            minimum_token_amount_raw = minimum_output_with_slippage(
                expected_token_amount_raw, self.slippage_bps
            )
            token_unit = _base_unit(token_info)
            token_amount = expected_token_amount_raw / token_unit
            token_price_sol = quote_amount / token_amount
            max_quote_amount_raw = (
                quote_amount_raw * (10_000 + self.slippage_bps) + 9_999
            ) // 10_000
            builder_quote_amount_raw = (
                quote_amount_raw
                if token_info.platform is Platform.LETS_BONK
                else max_quote_amount_raw
            )
            buy_amount_argument = (
                expected_token_amount_raw
                if getattr(instruction_builder, "buy_uses_exact_output", False)
                else minimum_token_amount_raw
            )
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                builder_quote_amount_raw,
                buy_amount_argument,
                address_provider,
            )
            if is_sol_paired(quote_mint):
                primary_destination = self._get_sol_destination(
                    token_info, address_provider
                )
                native_receipt_destinations = (
                    primary_destination,
                    *_additional_native_buy_recipients(
                        token_info.platform,
                        instructions,
                        address_provider,
                        primary_destination,
                    ),
                )
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )
            priority_fee = await self.priority_fee_manager.calculate_priority_fee(
                priority_accounts
            )
            if not zero_rpc_event_path:
                account_balance_baseline_raw = await self._read_pretrade_balance(
                    token_info
                )

            logger.info(
                f"Buying {token_amount:.6f} tokens at average quote "
                f"{token_price_sol:.8f} {quote_label} per token on "
                f"{token_info.platform.value}"
            )
            logger.info(
                f"Total cost: {quote_amount:.6f} {quote_label} "
                f"(max: {max_quote_amount_raw / quote_unit:.6f} {quote_label})"
            )

            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=priority_fee,
                compute_unit_limit=instruction_builder.get_buy_compute_unit_limit(
                    self._get_cu_override("buy", token_info.platform)
                ),
                account_data_size_limit=self._get_cu_override(
                    "account_data_size", token_info.platform
                ),
                quote_amount_raw=max_quote_amount_raw,
                intent_id=f"buy:{token_info.platform.value}:{token_info.mint}",
                receipt_destinations=(
                    tuple(
                        str(destination) for destination in native_receipt_destinations
                    )
                    if native_receipt_destinations
                    else None
                ),
            )
            signature = str(tx_signature)
            submitted_signature = signature
            outcome = await self.client.confirm_transaction_outcome(signature)

            if outcome.status is TransactionStatus.SUCCESS:
                logger.info(f"Buy transaction confirmed: {signature}")
                if is_sol_paired(quote_mint):
                    exact_destinations = await _exact_native_receipt_destinations(
                        self.client,
                        signature,
                        native_receipt_destinations,
                    )
                    sol_destination = exact_destinations[0]
                    native_quote_destinations = exact_destinations[1:]
                else:
                    sol_destination = self._get_sol_destination(
                        token_info, address_provider
                    )
                    native_quote_destinations = ()
                tokens_raw, quote_spent = await self.client.get_buy_transaction_details(
                    signature,
                    token_info.mint,
                    sol_destination,
                    quote_mint=quote_mint,
                    quote_destinations=list(native_quote_destinations),
                )
                if (
                    isinstance(tokens_raw, bool)
                    or not isinstance(tokens_raw, int)
                    or tokens_raw <= 0
                    or isinstance(quote_spent, bool)
                    or not isinstance(quote_spent, int)
                    or quote_spent <= 0
                ):
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=signature,
                        error_message=(
                            "Buy confirmed but receipt accounting is unresolved: "
                            f"tokens={tokens_raw}, quote_spent={quote_spent}"
                        ),
                        account_balance_baseline_raw=account_balance_baseline_raw,
                        slot=outcome.slot,
                        status=TransactionStatus.UNKNOWN.value,
                    )
                token_unit = _base_unit(token_info)
                actual_amount = tokens_raw / token_unit
                actual_price = (quote_spent / quote_unit) / actual_amount
                logger.info(
                    f"Actual tokens received: {actual_amount:.6f} "
                    f"(quoted: {token_amount:.6f})"
                )
                logger.info(
                    f"Actual {quote_label} spent: "
                    f"{quote_spent / quote_unit:.10f} {quote_label}"
                )
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=signature,
                    amount=actual_amount,
                    price=actual_price,
                    amount_raw=tokens_raw,
                    quote_amount_raw=quote_spent,
                    account_balance_baseline_raw=account_balance_baseline_raw,
                    slot=outcome.slot,
                    status=outcome.status.value,
                )

            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=signature,
                error_message=outcome.error
                or f"Buy transaction outcome: {outcome.status.value}",
                amount_raw=expected_token_amount_raw,
                quote_amount_raw=max_quote_amount_raw,
                account_balance_baseline_raw=account_balance_baseline_raw,
                slot=outcome.slot,
                status=outcome.status.value,
            )

        except TransactionSubmissionUnknown as exc:
            logger.warning(
                "Buy submission outcome is unresolved for %s: %s",
                token_info.mint,
                exc,
            )
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=exc.signature,
                error_message=str(exc),
                amount=token_amount,
                price=token_price_sol,
                amount_raw=expected_token_amount_raw,
                quote_amount_raw=max_quote_amount_raw,
                account_balance_baseline_raw=account_balance_baseline_raw,
                status=TransactionStatus.UNKNOWN.value,
            )
        except Exception as e:
            logger.exception("Buy operation failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=submitted_signature,
                error_message=str(e),
                amount=token_amount,
                price=token_price_sol,
                amount_raw=expected_token_amount_raw,
                quote_amount_raw=max_quote_amount_raw,
                account_balance_baseline_raw=account_balance_baseline_raw,
                status=(
                    TransactionStatus.UNKNOWN.value
                    if submitted_signature is not None
                    else None
                ),
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Resolve a mint-bound pool address without trusting listener input."""
        if token_info.platform is Platform.PUMP_FUN:
            derived = address_provider.derive_pool_address(token_info.mint)
            supplied = token_info.bonding_curve
            if (
                getattr(address_provider, "platform", None) is Platform.PUMP_FUN
                and supplied is not None
                and supplied != derived
            ):
                raise ValueError("Pump.fun bonding curve does not match the token mint")
            token_info.bonding_curve = derived
            return derived
        if token_info.platform is Platform.LETS_BONK:
            if not isinstance(token_info.quote_mint, Pubkey):
                raise ValueError(
                    "LetsBonk pool resolution requires quote mint metadata"
                )
            quote_mint = get_quote_asset(token_info.quote_mint).mint
            derived = address_provider.derive_pool_address(token_info.mint, quote_mint)
            supplied = token_info.pool_state
            if supplied is not None and supplied != derived:
                raise ValueError(
                    "LetsBonk pool does not match the base/quote mint pair"
                )
            token_info.pool_state = derived
            return derived
        raise ValueError(f"Unsupported trading platform: {token_info.platform!r}")

    def _can_skip_refresh(self, token_info: TokenInfo) -> bool:
        """Whether the pre-buy curve read can be skipped entirely.

        True when the listener read creator, mayhem/cashback, quote mint, and
        quote token program from the on-chain CreateEvent (canonical at create
        time), keeping extreme_fast_mode at zero RPC calls between detection
        and submission.

        Args:
            token_info: Token information from the listener

        Returns:
            True if the buy can be built from token_info as-is
        """
        if token_info.quote_mint is None:
            return False
        try:
            expected_quote_program = get_quote_asset(
                token_info.quote_mint
            ).token_program
        except ValueError:
            return False
        return (
            token_info.platform is Platform.PUMP_FUN
            and self.trust_create_event
            and token_info.state_from_event
            and token_info.curve_complete is False
            and token_info.quote_token_program_id == expected_quote_program
            and token_info.token_program_id
            in {
                SystemAddresses.TOKEN_PROGRAM,
                SystemAddresses.TOKEN_2022_PROGRAM,
            }
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
            _record_executable_state(token_info, pool_state)
        except (TypeError, ValueError) as exc:
            return f"Pool is not executable ({exc}); skipping buy"
        except Exception as exc:  # noqa: BLE001
            return (
                f"Curve state unreadable within {self.curve_refresh_budget:.1f}s "
                f"({exc}); skipping buy rather than submitting with guessed accounts"
            )

        token_info.is_mayhem_mode = pool_state.get(
            "is_mayhem_mode", token_info.is_mayhem_mode
        )
        token_info.is_cashback_coin = pool_state.get(
            "is_cashback_coin", token_info.is_cashback_coin
        )
        token_info.base_decimals = pool_state.get(
            "base_decimals", token_info.base_decimals
        )
        token_info.quote_decimals = pool_state.get(
            "quote_decimals", token_info.quote_decimals
        )
        # The quote asset decides which balance we spend and how amounts are
        # scaled, so it must come from the curve rather than a listener guess.
        _refresh_quote_mint(token_info, pool_state)
        fresh_creator = pool_state.get("creator")
        derive_creator_vault = getattr(address_provider, "derive_creator_vault", None)
        if fresh_creator and callable(derive_creator_vault):
            new_creator = (
                Pubkey.from_string(fresh_creator)
                if isinstance(fresh_creator, str)
                else fresh_creator
            )
            token_info.creator = new_creator
            token_info.creator_vault = derive_creator_vault(new_creator)
        _apply_token_program(token_info, fresh_token_program, address_provider)
        _sync_letsbonk_execution_metadata(token_info, pool_state, address_provider)
        return None

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
        max_retries: int = 1,
        compute_units: dict | None = None,
    ):
        """Initialize platform-aware token seller."""
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries != 1
        ):
            raise ValueError(
                "newly signed transaction retries are disabled; max_retries must be 1"
            )
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.slippage_bps = _slippage_bps(slippage)
        self.max_retries = max_retries
        self.compute_units = compute_units or {}

    async def execute(
        self,
        token_info: TokenInfo,
        token_amount: float | None,
        token_price: float | None,
        *,
        token_amount_raw: int | None = None,
        intent_id: str | None = None,
    ) -> TradeResult:
        """Execute sell operation using platform-specific implementations.

        Args:
            token_info: Token information for the sell operation
            token_amount: Token amount to sell (from buy result). Required to avoid
                         RPC balance query delays.
            token_price: Reference price in the quote asset that the slippage
                        floor is computed from. Required rather than read here,
                        to avoid RPC pool state query delays — pass the freshest
                        price the caller has. A stale price that is above the
                        market sets a floor the pool cannot pay and the sell
                        reverts (pump.fun 6003 TooLittleSolReceived).

        Returns:
            TradeResult with operation outcome

        Raises:
            ValueError: If required parameters are not provided
        """
        if token_amount_raw is None and token_amount is None:
            raise ValueError("token_amount or token_amount_raw is required")
        if token_amount_raw is not None and (
            isinstance(token_amount_raw, bool)
            or not isinstance(token_amount_raw, int)
            or token_amount_raw <= 0
        ):
            raise ValueError("token_amount_raw must be a positive integer")
        if token_price is not None and (
            isinstance(token_price, bool)
            or not isinstance(token_price, (int, float))
            or not isfinite(token_price)
            or token_price <= 0
        ):
            raise ValueError("token_price must be finite and positive when supplied")

        token_balance: int | None = None
        token_balance_decimal: float | None = None
        quoted_average_price: float | None = None
        expected_quote_output_raw: int | None = None
        submitted_signature: str | None = None
        try:
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Fall back to the listener's quote asset if the refresh below fails.
            quote_mint = normalize_quote_mint(token_info.quote_mint)

            # Refresh all execution metadata immediately before a sell.
            pool_address = self._get_pool_address(token_info, address_provider)
            try:
                pool_state, fresh_token_program = await _read_pool_state_with_retry(
                    curve_manager,
                    pool_address,
                    mint=token_info.mint,
                )
                _record_executable_state(token_info, pool_state)
                token_info.is_mayhem_mode = pool_state.get(
                    "is_mayhem_mode", token_info.is_mayhem_mode
                )
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                token_info.base_decimals = pool_state.get(
                    "base_decimals", token_info.base_decimals
                )
                token_info.quote_decimals = pool_state.get(
                    "quote_decimals", token_info.quote_decimals
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)
                fresh_creator = pool_state.get("creator")
                if fresh_creator:
                    new_creator = (
                        Pubkey.from_string(fresh_creator)
                        if isinstance(fresh_creator, str)
                        else fresh_creator
                    )
                    if not isinstance(new_creator, Pubkey):
                        raise ValueError("Pool state returned an invalid creator")
                    token_info.creator = new_creator
                derive_creator_vault = getattr(
                    address_provider, "derive_creator_vault", None
                )
                if token_info.platform is Platform.PUMP_FUN:
                    if token_info.creator is None:
                        raise RuntimeError("Pump.fun sell requires creator metadata")
                    if not callable(derive_creator_vault):
                        raise RuntimeError(
                            "Pump.fun sell requires creator-vault derivation "
                            "from its address provider"
                        )
                    token_info.creator_vault = derive_creator_vault(token_info.creator)
                elif fresh_creator and callable(derive_creator_vault):
                    token_info.creator_vault = derive_creator_vault(token_info.creator)
                _apply_token_program(token_info, fresh_token_program, address_provider)
                _sync_letsbonk_execution_metadata(
                    token_info, pool_state, address_provider
                )
            except Exception as exc:
                raise RuntimeError(
                    "Could not refresh authoritative protocol metadata before "
                    f"sell: {exc}"
                ) from exc

            quote_unit = quote_units_per_token(quote_mint)
            quote_label = _quote_symbol(quote_mint)
            if token_amount_raw is None:
                token_balance = _to_raw_units(
                    token_amount, _base_unit(token_info), "token amount"
                )
            else:
                token_balance = token_amount_raw
            token_balance_decimal = token_balance / _base_unit(token_info)
            _require_executable_quote_contract(token_info, self.client)

            quote_method = getattr(curve_manager, "calculate_sell_amount_out", None)
            if quote_method is not None:
                expected_quote_output_raw = await quote_method(
                    pool_address, token_balance
                )
            elif token_price is not None:
                expected_quote_output_raw = _to_raw_units(
                    token_balance_decimal * token_price,
                    quote_unit,
                    "expected quote output",
                )
            else:
                raise ValueError(
                    "Platform has no exact sell quote and no reference price was supplied"
                )
            if expected_quote_output_raw <= 0:
                raise ValueError("Platform sell quote returned no output")
            min_quote_output = minimum_output_with_slippage(
                expected_quote_output_raw, self.slippage_bps
            )
            expected_quote_output = expected_quote_output_raw / quote_unit
            quoted_average_price = expected_quote_output / token_balance_decimal

            logger.info(
                f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
            )
            logger.info(
                f"Nonlinear quote output: {expected_quote_output:.10f} {quote_label}"
            )
            logger.info(
                f"Minimum {quote_label} output (with "
                f"{self.slippage * 100:.1f}% slippage): "
                f"{min_quote_output / quote_unit:.10f} {quote_label} "
                f"({min_quote_output} raw units)"
            )

            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,
                min_quote_output,
                address_provider,
            )
            priority_accounts = instruction_builder.get_required_accounts_for_sell(
                token_info, self.wallet.pubkey, address_provider
            )
            priority_fee = await self.priority_fee_manager.calculate_priority_fee(
                priority_accounts
            )
            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=priority_fee,
                compute_unit_limit=instruction_builder.get_sell_compute_unit_limit(
                    self._get_cu_override("sell", token_info.platform)
                ),
                account_data_size_limit=self._get_cu_override(
                    "account_data_size", token_info.platform
                ),
                quote_amount_raw=0,
                intent_id=intent_id
                or (
                    f"sell:{token_info.platform.value}:{token_info.mint}:"
                    f"{token_balance}"
                ),
            )
            signature = str(tx_signature)
            submitted_signature = signature
            outcome = await self.client.confirm_transaction_outcome(signature)
            if outcome.status is TransactionStatus.SUCCESS:
                logger.info(f"Sell transaction confirmed: {signature}")
                quote_received_raw = await self.client.get_sell_transaction_details(
                    signature,
                    quote_mint,
                    self.wallet.pubkey,
                )
                if (
                    isinstance(quote_received_raw, bool)
                    or not isinstance(quote_received_raw, int)
                    or quote_received_raw <= 0
                ):
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=signature,
                        error_message=(
                            "Sell confirmed but receipt accounting is unresolved: "
                            f"quote_received={quote_received_raw}"
                        ),
                        amount=token_balance_decimal,
                        amount_raw=token_balance,
                        slot=outcome.slot,
                        status=TransactionStatus.UNKNOWN.value,
                    )
                actual_price = (quote_received_raw / quote_unit) / (
                    token_balance_decimal or 1
                )
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=signature,
                    amount=token_balance_decimal,
                    price=actual_price,
                    amount_raw=token_balance,
                    quote_amount_raw=quote_received_raw,
                    slot=outcome.slot,
                    status=outcome.status.value,
                )
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=signature,
                error_message=outcome.error
                or f"Sell transaction outcome: {outcome.status.value}",
                amount=token_balance_decimal,
                price=quoted_average_price,
                amount_raw=token_balance,
                slot=outcome.slot,
                status=outcome.status.value,
            )

        except TransactionSubmissionUnknown as exc:
            logger.warning(
                "Sell submission outcome is unresolved for %s: %s",
                token_info.mint,
                exc,
            )
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=exc.signature,
                error_message=str(exc),
                amount=token_balance_decimal,
                price=quoted_average_price,
                amount_raw=token_balance,
                status=TransactionStatus.UNKNOWN.value,
            )
        except Exception as e:
            logger.exception("Sell operation failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                tx_signature=submitted_signature,
                error_message=str(e),
                amount=token_balance_decimal,
                price=quoted_average_price,
                amount_raw=token_balance,
                status=(
                    TransactionStatus.UNKNOWN.value
                    if submitted_signature is not None
                    else None
                ),
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Resolve a mint-bound pool address without trusting listener input."""
        if token_info.platform is Platform.PUMP_FUN:
            derived = address_provider.derive_pool_address(token_info.mint)
            supplied = token_info.bonding_curve
            if (
                getattr(address_provider, "platform", None) is Platform.PUMP_FUN
                and supplied is not None
                and supplied != derived
            ):
                raise ValueError("Pump.fun bonding curve does not match the token mint")
            token_info.bonding_curve = derived
            return derived
        if token_info.platform is Platform.LETS_BONK:
            if not isinstance(token_info.quote_mint, Pubkey):
                raise ValueError(
                    "LetsBonk pool resolution requires quote mint metadata"
                )
            quote_mint = get_quote_asset(token_info.quote_mint).mint
            derived = address_provider.derive_pool_address(token_info.mint, quote_mint)
            supplied = token_info.pool_state
            if supplied is not None and supplied != derived:
                raise ValueError(
                    "LetsBonk pool does not match the base/quote mint pair"
                )
            token_info.pool_state = derived
            return derived
        raise ValueError(f"Unsupported trading platform: {token_info.platform!r}")

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

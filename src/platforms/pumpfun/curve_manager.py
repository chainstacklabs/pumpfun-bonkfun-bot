"""
Pump.Fun implementation of CurveManager interface.

This module handles pump.fun-specific bonding curve operations
by implementing the CurveManager interface using IDL-based decoding.
"""

from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import (
    LAMPORTS_PER_SOL,
    QUOTE_TOKEN_PROGRAMS,
    TOKEN_DECIMALS,
    SystemAddresses,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
)
from interfaces.core import CurveManager, Platform
from platforms.pumpfun.address_provider import PumpFunAddresses
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

_BONDING_CURVE_DISCRIMINATOR = bytes((23, 183, 248, 55, 96, 216, 172, 96))
# 8-byte discriminator + five u64 + bool + pubkey + two bools + quote pubkey.
_BONDING_CURVE_MIN_ACCOUNT_SIZE = 115
_SUPPORTED_TOKEN_PROGRAMS = frozenset(
    (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM)
)


def _coerce_pubkey(value: object) -> Pubkey | None:
    """Coerce an IDL pubkey field without guessing a default."""
    if isinstance(value, Pubkey):
        return value
    if isinstance(value, str):
        try:
            return Pubkey.from_string(value)
        except ValueError:
            return None
    if isinstance(value, bytes | bytearray) and len(value) == 32:
        return Pubkey.from_bytes(bytes(value))
    return None


def _require_raw_u64(value: object, name: str, *, positive: bool = False) -> int:
    """Return a validated raw u64 or raise."""
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 0xFFFF_FFFF_FFFF_FFFF
    ):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{name} must be a {qualifier}raw u64 integer")
    return value


class PumpFunCurveManager(CurveManager):
    """Pump.Fun implementation of CurveManager interface using IDL-based decoding."""

    def __init__(self, client: SolanaClient, idl_parser: IDLParser):
        """Initialize pump.fun curve manager with injected IDL parser.

        Args:
            client: Solana RPC client
            idl_parser: Pre-loaded IDL parser for pump.fun platform
        """
        self.client = client
        self._idl_parser = idl_parser

        logger.info("Pump.Fun curve manager initialized with injected IDL parser")

    @property
    def platform(self) -> Platform:
        """Get the platform this manager serves."""
        return Platform.PUMP_FUN

    @staticmethod
    def _validated_curve_data(account: Any, pool_address: Pubkey) -> bytes:
        """Validate account provenance and fixed-layout prefix before decoding."""
        if account is None:
            raise ValueError(f"Bonding curve account {pool_address} not found")
        if getattr(account, "owner", None) != PumpFunAddresses.PROGRAM:
            raise ValueError(
                f"Bonding curve account {pool_address} has an unexpected owner"
            )
        data = getattr(account, "data", None)
        if not isinstance(data, bytes | bytearray):
            raise ValueError(f"No data in bonding curve account {pool_address}")
        raw_data = bytes(data)
        if len(raw_data) < _BONDING_CURVE_MIN_ACCOUNT_SIZE:
            raise ValueError(
                f"Bonding curve account {pool_address} is too short: "
                f"{len(raw_data)}/{_BONDING_CURVE_MIN_ACCOUNT_SIZE}"
            )
        if raw_data[:8] != _BONDING_CURVE_DISCRIMINATOR:
            raise ValueError(
                f"Bonding curve account {pool_address} has an invalid discriminator"
            )
        return raw_data

    async def get_pool_state(
        self, pool_address: Pubkey, commitment: str | None = None
    ) -> dict[str, Any]:
        """Get the current state of a pump.fun bonding curve.

        Args:
            pool_address: Address of the bonding curve
            commitment: Optional commitment override. Pass "processed" right
                after a geyser/logs event so the BC is readable in the same
                slot it was created (default: confirmed, ~1-2 slot lag).

        Returns:
            Dictionary containing bonding curve state data
        """
        try:
            account = await self.client.get_account_info(
                pool_address, commitment=commitment
            )
            account_data = self._validated_curve_data(account, pool_address)
            curve_state_data = self._decode_curve_state_with_idl(account_data)

            return curve_state_data

        except Exception as e:
            logger.exception("Failed to get curve state")
            raise ValueError(f"Invalid bonding curve state: {e!s}")

    async def get_pool_state_and_token_program(
        self, pool_address: Pubkey, mint: Pubkey, commitment: str | None = None
    ) -> tuple[dict[str, Any], Pubkey | None]:
        """Read curve state and the mint's owning token program together.

        One getMultipleAccounts round trip, so both values come from the same
        node and slot. Listeners that don't carry the token program (pumpportal
        guesses Token-2022) can be corrected from the mint account's owner
        without a second, possibly inconsistent read (issue #170).

        Args:
            pool_address: Address of the bonding curve
            mint: Token mint whose owner identifies the token program
            commitment: Optional commitment override (see get_pool_state)

        Returns:
            Tuple of (decoded curve state, token program id or None if the
            mint account was not readable)

        Raises:
            ValueError: If the bonding curve account is missing or undecodable
        """
        if (
            pool_address
            != Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)], PumpFunAddresses.PROGRAM
            )[0]
        ):
            raise ValueError("Bonding curve address does not match the mint")
        try:
            curve_account, mint_account = await self.client.get_multiple_accounts(
                [pool_address, mint], commitment=commitment
            )
        except Exception as e:
            logger.exception("Failed to read curve and mint accounts")
            raise ValueError(f"Invalid bonding curve state: {e!s}") from e  # noqa: TRY003

        curve_data = self._validated_curve_data(curve_account, pool_address)
        curve_state_data = self._decode_curve_state_with_idl(curve_data)
        if mint_account is None:
            return curve_state_data, None

        token_program = getattr(mint_account, "owner", None)
        if token_program not in _SUPPORTED_TOKEN_PROGRAMS:
            raise ValueError(f"Mint account {mint} has an unsupported owner")
        return curve_state_data, token_program

    @staticmethod
    def _require_incomplete_curve(state: dict[str, Any]) -> None:
        """Reject migrated curves before deriving any executable quote."""
        if state.get("complete") is not False:
            raise ValueError("Pump.fun bonding curve is complete or malformed")

    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Calculate current token price from bonding curve state.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            Current token price denominated in the curve's quote asset
            (SOL for SOL-paired coins, USDC for USDC-paired coins)
        """
        pool_state = await self.get_pool_state(pool_address)

        if pool_state["virtual_token_reserves"] <= 0:
            return 0.0

        # _decode_curve_state_with_idl already scales by the quote mint's
        # decimals, so don't re-derive the price with a hardcoded 1e9 here.
        return pool_state["price_per_token"]

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate a pre-fee token-output upper bound in raw units.

        The dynamic protocol and creator fee schedule lives outside the bonding
        curve account. This helper therefore does not pretend a zero-fee quote
        is an executable minimum; callers must apply a sourced fee schedule and
        slippage before instruction construction.
        """
        amount_in = _require_raw_u64(amount_in, "amount_in", positive=True)
        pool_state = await self.get_pool_state(pool_address)
        self._require_incomplete_curve(pool_state)
        virtual_token_reserves = _require_raw_u64(
            pool_state["virtual_token_reserves"],
            "virtual_token_reserves",
            positive=True,
        )
        virtual_quote_reserves = _require_raw_u64(
            pool_state["virtual_quote_reserves"],
            "virtual_quote_reserves",
            positive=True,
        )
        real_token_reserves = _require_raw_u64(
            pool_state["real_token_reserves"], "real_token_reserves"
        )

        tokens_out = (amount_in * virtual_token_reserves) // (
            virtual_quote_reserves + amount_in
        )
        return min(tokens_out, real_token_reserves)

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate a pre-fee quote-output upper bound in raw units.

        Output is capped by the real quote reserves available on the curve.
        Dynamic protocol and creator fees must be sourced separately before
        deriving an executable minimum output.
        """
        amount_in = _require_raw_u64(amount_in, "amount_in", positive=True)
        pool_state = await self.get_pool_state(pool_address)
        self._require_incomplete_curve(pool_state)
        virtual_token_reserves = _require_raw_u64(
            pool_state["virtual_token_reserves"],
            "virtual_token_reserves",
            positive=True,
        )
        virtual_quote_reserves = _require_raw_u64(
            pool_state["virtual_quote_reserves"],
            "virtual_quote_reserves",
            positive=True,
        )
        real_quote_reserves = _require_raw_u64(
            pool_state["real_quote_reserves"], "real_quote_reserves"
        )

        quote_out = (amount_in * virtual_quote_reserves) // (
            virtual_token_reserves + amount_in
        )
        return min(quote_out, real_quote_reserves)

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Get current bonding curve reserves.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            Tuple of (token_reserves, sol_reserves) in raw units
        """
        pool_state = await self.get_pool_state(pool_address)
        return (
            pool_state["virtual_token_reserves"],
            pool_state["virtual_sol_reserves"],
        )

    def _decode_curve_state_with_idl(self, data: bytes) -> dict[str, Any]:
        """Decode bonding curve state data using injected IDL parser.

        Args:
            data: Raw account data

        Returns:
            Dictionary with decoded bonding curve state

        Raises:
            ValueError: If IDL parsing fails
        """
        if not isinstance(data, bytes | bytearray):
            raise ValueError("Bonding curve data must be bytes")
        raw_data = bytes(data)
        if len(raw_data) < _BONDING_CURVE_MIN_ACCOUNT_SIZE:
            raise ValueError(
                f"Bonding curve data is too short: "
                f"{len(raw_data)}/{_BONDING_CURVE_MIN_ACCOUNT_SIZE}"
            )
        if raw_data[:8] != _BONDING_CURVE_DISCRIMINATOR:
            raise ValueError("Invalid BondingCurve account discriminator")

        decoded_curve_state = self._idl_parser.decode_account_data(
            raw_data, "BondingCurve", skip_discriminator=True
        )
        if not decoded_curve_state:
            raise ValueError("Failed to decode bonding curve state with IDL parser")

        required_fields = {
            "virtual_token_reserves",
            "virtual_quote_reserves",
            "real_token_reserves",
            "real_quote_reserves",
            "token_total_supply",
            "complete",
            "creator",
            "is_mayhem_mode",
            "is_cashback_coin",
            "quote_mint",
        }
        missing_fields = required_fields.difference(decoded_curve_state)
        if missing_fields:
            raise ValueError(
                f"BondingCurve state is missing fields: {sorted(missing_fields)}"
            )

        virtual_token_reserves = _require_raw_u64(
            decoded_curve_state["virtual_token_reserves"],
            "virtual_token_reserves",
            positive=True,
        )
        virtual_quote_reserves = _require_raw_u64(
            decoded_curve_state["virtual_quote_reserves"],
            "virtual_quote_reserves",
            positive=True,
        )
        real_token_reserves = _require_raw_u64(
            decoded_curve_state["real_token_reserves"], "real_token_reserves"
        )
        real_quote_reserves = _require_raw_u64(
            decoded_curve_state["real_quote_reserves"], "real_quote_reserves"
        )
        token_total_supply = _require_raw_u64(
            decoded_curve_state["token_total_supply"],
            "token_total_supply",
            positive=True,
        )
        if real_token_reserves > virtual_token_reserves:
            raise ValueError("real_token_reserves exceeds virtual_token_reserves")
        if real_quote_reserves > virtual_quote_reserves:
            raise ValueError("real_quote_reserves exceeds virtual_quote_reserves")
        if real_token_reserves > token_total_supply:
            raise ValueError("real_token_reserves exceeds token_total_supply")

        complete = decoded_curve_state["complete"]
        is_mayhem_mode = decoded_curve_state["is_mayhem_mode"]
        is_cashback_coin = decoded_curve_state["is_cashback_coin"]
        if not all(
            isinstance(value, bool)
            for value in (complete, is_mayhem_mode, is_cashback_coin)
        ):
            raise ValueError("BondingCurve flag fields must be booleans")

        creator = _coerce_pubkey(decoded_curve_state["creator"])
        if creator is None:
            raise ValueError("BondingCurve creator is not a valid pubkey")
        raw_quote_mint = _coerce_pubkey(decoded_curve_state["quote_mint"])
        if raw_quote_mint is None:
            raise ValueError("BondingCurve quote_mint is not a valid pubkey")
        quote_mint = normalize_quote_mint(raw_quote_mint)
        if quote_mint not in QUOTE_TOKEN_PROGRAMS:
            raise ValueError(f"Unsupported BondingCurve quote mint: {quote_mint}")
        quote_unit = quote_units_per_token(quote_mint)

        curve_data = {
            "virtual_token_reserves": virtual_token_reserves,
            "virtual_quote_reserves": virtual_quote_reserves,
            "real_token_reserves": real_token_reserves,
            "real_quote_reserves": real_quote_reserves,
            "token_total_supply": token_total_supply,
            "complete": complete,
            "creator": creator,
            "is_mayhem_mode": is_mayhem_mode,
            "is_cashback_coin": is_cashback_coin,
            "quote_mint": quote_mint,
            "is_sol_paired": is_sol_paired(quote_mint),
        }

        # Back-compat field names remain aliases only; all calculations above
        # use the quote-asset-neutral current IDL names.
        curve_data["virtual_sol_reserves"] = virtual_quote_reserves
        curve_data["real_sol_reserves"] = real_quote_reserves

        curve_data["price_per_token"] = (
            (virtual_quote_reserves / virtual_token_reserves)
            * (10**TOKEN_DECIMALS)
            / quote_unit
        )
        curve_data["token_reserves_decimal"] = (
            virtual_token_reserves / 10**TOKEN_DECIMALS
        )
        curve_data["quote_reserves_decimal"] = virtual_quote_reserves / quote_unit
        curve_data["sol_reserves_decimal"] = curve_data["quote_reserves_decimal"]

        logger.debug(
            f"Decoded curve state: virtual_token_reserves={virtual_token_reserves}, "
            f"virtual_quote_reserves={virtual_quote_reserves}, "
            f"quote_mint={quote_mint}, "
            f"price={curve_data['price_per_token']:.8f} quote/token"
        )
        return curve_data

    # Compatibility aliases retained for callers that used the older helper
    # names. Inputs and outputs are now raw integers; decimal conversion belongs
    # only at presentation boundaries.
    async def calculate_expected_tokens(
        self, pool_address: Pubkey, quote_amount_raw: int
    ) -> int:
        """Return the raw token-output upper bound for raw quote input."""
        return await self.calculate_buy_amount_out(pool_address, quote_amount_raw)

    async def calculate_expected_sol(
        self, pool_address: Pubkey, token_amount_raw: int
    ) -> int:
        """Return the raw quote-output upper bound for raw token input."""
        return await self.calculate_sell_amount_out(pool_address, token_amount_raw)

    async def is_curve_complete(self, pool_address: Pubkey) -> bool:
        """Check if the bonding curve is complete (migrated to Raydium).

        Args:
            pool_address: Address of the bonding curve

        Returns:
            True if curve is complete, False otherwise
        """
        pool_state = await self.get_pool_state(pool_address)
        return pool_state.get("complete", False)

    async def get_curve_progress(self, pool_address: Pubkey) -> dict[str, Any]:
        """Get bonding curve completion progress information.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            Dictionary with progress information
        """
        pool_state = await self.get_pool_state(pool_address)

        # Calculate progress based on SOL raised vs target
        # This is approximate since the exact target isn't stored in the curve state
        sol_raised = pool_state["real_sol_reserves"] / LAMPORTS_PER_SOL

        # Estimate progress based on typical pump.fun graduation requirements
        # (This could be made more accurate with additional on-chain data)
        estimated_target_sol = 85.0  # Typical pump.fun graduation target
        progress_percentage = min((sol_raised / estimated_target_sol) * 100, 100.0)

        return {
            "complete": pool_state.get("complete", False),
            "sol_raised": sol_raised,
            "estimated_target_sol": estimated_target_sol,
            "progress_percentage": progress_percentage,
            "tokens_available": pool_state["virtual_token_reserves"]
            / 10**TOKEN_DECIMALS,
            "market_cap_sol": sol_raised,  # Approximate market cap
        }

    def validate_curve_state_structure(self, pool_address: Pubkey) -> bool:
        """Validate that the curve state structure matches IDL expectations.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            True if structure is valid, False otherwise
        """
        try:
            # This would be used during development/testing to ensure
            # the IDL parsing is working correctly
            pool_state = self.get_pool_state(pool_address)

            required_fields = [
                "virtual_token_reserves",
                "virtual_sol_reserves",
                "real_token_reserves",
                "real_sol_reserves",
                "token_total_supply",
                "complete",
            ]

            for field in required_fields:
                if field not in pool_state:
                    logger.error(f"Missing required field: {field}")
                    return False

                if field != "complete" and not isinstance(pool_state[field], int):
                    logger.error(
                        f"Field {field} is not an integer: {type(pool_state[field])}"
                    )
                    return False

            return True

        except Exception:
            logger.exception("Curve state validation failed")
            return False

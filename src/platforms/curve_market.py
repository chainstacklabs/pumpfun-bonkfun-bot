"""A platform's bonding curve, behind the `Market` interface."""

from typing import Any

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from interfaces.core import (
    AddressProvider,
    CurveManager,
    InstructionBuilder,
    Market,
    TokenInfo,
)


class CurveMarket(Market):
    """Trades on a bonding curve through the platform's builder and curve manager."""

    def __init__(
        self,
        address_provider: AddressProvider,
        instruction_builder: InstructionBuilder,
        curve_manager: CurveManager,
    ) -> None:
        self.address_provider = address_provider
        self.instruction_builder = instruction_builder
        self.curve_manager = curve_manager

    @property
    def spends_exact_amount_in(self) -> bool:
        """Whether the platform's buy spends exactly `amount_in`."""
        return getattr(self.instruction_builder, "spends_exact_amount_in", False)

    def pool_address(self, token_info: TokenInfo) -> Pubkey:
        """The coin's curve: from the listener when it gave one, else derived."""
        if token_info.bonding_curve:
            return token_info.bonding_curve
        if token_info.pool_state:
            return token_info.pool_state
        return self.address_provider.derive_pool_address(token_info.mint)

    async def get_market_state(self, token_info: TokenInfo) -> dict[str, Any]:
        """The curve manager's decoded pool state for the coin's curve."""
        return await self.curve_manager.get_pool_state(self.pool_address(token_info))

    async def get_price(self, token_info: TokenInfo) -> float:
        """Price off the curve.

        Returns:
            Whole quote units per whole token; 0.0 for a curve with no virtual
            token reserves

        Raises:
            CurveGraduatedError: If the curve has graduated; the coin now
                trades in the platform's graduated market
        """
        return await self.curve_manager.calculate_price(self.pool_address(token_info))

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
    ) -> list[Instruction]:
        """Build the platform's curve buy."""
        return await self.instruction_builder.build_buy_instruction(
            token_info, user, amount_in, minimum_amount_out, self.address_provider
        )

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
    ) -> list[Instruction]:
        """Build the platform's curve sell."""
        return await self.instruction_builder.build_sell_instruction(
            token_info, user, amount_in, minimum_amount_out, self.address_provider
        )

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey
    ) -> list[Pubkey]:
        """Accounts the curve buy locks."""
        return self.instruction_builder.get_required_accounts_for_buy(
            token_info, user, self.address_provider
        )

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey
    ) -> list[Pubkey]:
        """Accounts the curve sell locks."""
        return self.instruction_builder.get_required_accounts_for_sell(
            token_info, user, self.address_provider
        )

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for the curve buy."""
        return self.instruction_builder.get_buy_compute_unit_limit(config_override)

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for the curve sell."""
        return self.instruction_builder.get_sell_compute_unit_limit(config_override)

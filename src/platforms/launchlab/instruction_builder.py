"""Raydium LaunchLab buy_exact_in / sell_exact_in instructions, for any quote mint."""

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from core.quote_account import quote_settlement_account
from interfaces.core import InstructionBuilder, TokenInfo
from platforms.launchlab.address_provider import LaunchLabAddressProvider
from utils.idl_parser import IDLParser

# A front end may charge its own share fee on a trade; the bot charges none.
SHARE_FEE_RATE = 0

# The IDL's 15 named accounts, in order. Three more follow, read positionally by
# the program: the system program and the two fee vaults.
_SWAP_ACCOUNTS: list[tuple[str, bool]] = [
    ("payer", True),
    ("authority", False),
    ("global_config", False),
    ("platform_config", False),
    ("pool_state", True),
    ("user_base_token", True),
    ("user_quote_token", True),
    ("base_vault", True),
    ("quote_vault", True),
    ("base_token_mint", False),
    ("quote_token_mint", False),
    ("base_token_program", False),
    ("quote_token_program", False),
    ("event_authority", False),
    ("program", False),
    ("system_program", False),
    ("platform_fee_vault", True),
    ("creator_fee_vault", True),
]


class LaunchLabInstructionBuilder(InstructionBuilder):
    """Builds LaunchLab trades. Subclasses only name their platform.

    A SOL-quoted pool settles through a throwaway wrapped-SOL account created,
    used and closed inside the transaction. Any other quote is paid from, and
    paid into, the wallet's own account for that mint; the bot never acquires a
    quote asset, so a buy of a non-SOL coin needs the balance already there.
    """

    def __init__(self, idl_parser: IDLParser) -> None:
        discriminators = idl_parser.get_instruction_discriminators()
        self._buy_exact_in = discriminators["buy_exact_in"]
        self._sell_exact_in = discriminators["sell_exact_in"]

    @property
    def spends_exact_amount_in(self) -> bool:
        """buy_exact_in spends all of amount_in."""
        return True

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: LaunchLabAddressProvider,
    ) -> list[Instruction]:
        """Build a buy that spends exactly `amount_in` raw quote units.

        Args:
            amount_in: Raw quote units to spend, all of them
            minimum_amount_out: Raw base tokens that must arrive, after the
                transfer fee on a reward coin
        """
        accounts = address_provider.get_buy_instruction_accounts(token_info, user)
        instructions = [
            create_idempotent_associated_token_account(
                user, user, token_info.mint, accounts["base_token_program"]
            )
        ]
        quote_account, open_ixs, close_ixs = self._quote_account(
            accounts, user, funding=amount_in
        )
        instructions += open_ixs
        instructions.append(
            self._swap(
                self._buy_exact_in,
                accounts,
                quote_account,
                amount_in,
                minimum_amount_out,
            )
        )
        return instructions + close_ixs

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: LaunchLabAddressProvider,
    ) -> list[Instruction]:
        """Build a sell of exactly `amount_in` raw base tokens.

        Args:
            amount_in: Raw base tokens to send
            minimum_amount_out: Raw quote units that must be paid out
        """
        accounts = address_provider.get_sell_instruction_accounts(token_info, user)
        quote_account, open_ixs, close_ixs = self._quote_account(
            accounts, user, funding=0
        )
        return [
            *open_ixs,
            self._swap(
                self._sell_exact_in,
                accounts,
                quote_account,
                amount_in,
                minimum_amount_out,
            ),
            *close_ixs,
        ]

    def _quote_account(
        self, accounts: dict[str, Pubkey], user: Pubkey, funding: int
    ) -> tuple[Pubkey, list[Instruction], list[Instruction]]:
        return quote_settlement_account(
            user, accounts["quote_token_mint"], accounts["quote_token_program"], funding
        )

    def _swap(
        self,
        discriminator: bytes,
        accounts: dict[str, Pubkey],
        quote_account: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
    ) -> Instruction:
        resolved = {**accounts, "user_quote_token": quote_account}
        metas = [
            AccountMeta(resolved[name], is_signer=name == "payer", is_writable=writable)
            for name, writable in _SWAP_ACCOUNTS
        ]
        data = discriminator + struct.pack(
            "<QQQ", amount_in, minimum_amount_out, SHARE_FEE_RATE
        )
        return Instruction(accounts["program"], data, metas)

    def get_required_accounts_for_buy(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        address_provider: LaunchLabAddressProvider,
    ) -> list[Pubkey]:
        """Writable accounts a buy locks, for priority-fee estimation."""
        accounts = address_provider.get_buy_instruction_accounts(token_info, user)
        return [
            accounts[name]
            for name in ("pool_state", "base_vault", "quote_vault", "program")
        ]

    def get_required_accounts_for_sell(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        address_provider: LaunchLabAddressProvider,
    ) -> list[Pubkey]:
        """Writable accounts a sell locks, for priority-fee estimation."""
        accounts = address_provider.get_sell_instruction_accounts(token_info, user)
        return [
            accounts[name]
            for name in ("pool_state", "base_vault", "quote_vault", "program")
        ]

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a buy; simulated buys use about 70,000."""
        return config_override if config_override is not None else 150_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a sell; simulated sells use about 60,000."""
        return config_override if config_override is not None else 150_000

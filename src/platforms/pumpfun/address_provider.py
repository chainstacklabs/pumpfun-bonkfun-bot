"""
Pump.Fun implementation of AddressProvider interface.

This module provides all pump.fun-specific addresses and PDA derivations
by implementing the AddressProvider interface.
"""

import secrets
from dataclasses import dataclass
from typing import ClassVar, Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses, normalize_quote_mint, quote_token_program
from interfaces.core import AddressProvider, Platform, TokenInfo


@dataclass
class PumpFunAddresses:
    """Pump.fun program addresses."""

    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    )
    GLOBAL: Final[Pubkey] = Pubkey.from_string(
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    )
    EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
    )
    FEE: Final[Pubkey] = Pubkey.from_string(
        "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
    )
    # Mayhem mode fee recipient (hardcoded to avoid RPC calls)
    # To check if this address is up-to-date, fetch Global account data at offset 483
    # from the pump.fun Global account: 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
    MAYHEM_FEE: Final[Pubkey] = Pubkey.from_string(
        "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"
    )
    LIQUIDITY_MIGRATOR: Final[Pubkey] = Pubkey.from_string(
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
    )
    FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    )
    # 8 normal fee recipients — use one as fee_recipient for non-mayhem coins.
    # See FEE_RECIPIENTS.md in the pump-fun public docs repository.
    NORMAL_FEE_RECIPIENTS: ClassVar[list[Pubkey]] = [
        Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"),
        Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"),
        Pubkey.from_string("7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX"),
        Pubkey.from_string("9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz"),
        Pubkey.from_string("AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY"),
        Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"),
        Pubkey.from_string("FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz"),
        Pubkey.from_string("G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP"),
    ]
    # 8 reserved fee recipients — use one as fee_recipient for mayhem coins.
    RESERVED_FEE_RECIPIENTS: ClassVar[list[Pubkey]] = [
        Pubkey.from_string("GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"),
        Pubkey.from_string("4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6"),
        Pubkey.from_string("8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR"),
        Pubkey.from_string("4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH"),
        Pubkey.from_string("8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6"),
        Pubkey.from_string("Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk"),
        Pubkey.from_string("463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq"),
        Pubkey.from_string("6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA"),
    ]
    # 8 buyback fee recipients — one is required on every buy/sell, for every
    # coin. On the legacy buy/sell these are appended (mutable) after
    # bonding-curve-v2; on buy_v2/sell_v2 they are the buyback_fee_recipient
    # account. Introduced by the 2026-04-28 program upgrade.
    # See FEE_RECIPIENTS.md in the pump-fun public docs repository.
    BUYBACK_FEE_RECIPIENTS: ClassVar[list[Pubkey]] = [
        Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
        Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
        Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
        Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
        Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
        Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
        Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
        Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
    ]
    # Back-compat alias for the pre-v2 naming used by the legacy buy/sell path.
    BREAKING_FEE_RECIPIENTS: ClassVar[list[Pubkey]] = BUYBACK_FEE_RECIPIENTS

    @staticmethod
    def pick_buyback_fee_recipient() -> Pubkey:
        """Pick one of the 8 buyback fee recipients at random.

        Spreads load across recipients per pump.fun's recommendation.
        """
        return secrets.choice(PumpFunAddresses.BUYBACK_FEE_RECIPIENTS)

    @staticmethod
    def pick_breaking_fee_recipient() -> Pubkey:
        """Deprecated alias for :meth:`pick_buyback_fee_recipient`."""
        return PumpFunAddresses.pick_buyback_fee_recipient()

    @staticmethod
    def find_sharing_config(base_mint: Pubkey) -> Pubkey:
        """Derive the creator-fee sharing config PDA for a coin.

        Mandatory on buy_v2/sell_v2. Lives under the pump fees program, not
        the pump program.

        Args:
            base_mint: Base token mint address

        Returns:
            Pubkey of the derived sharing config account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"sharing-config", bytes(base_mint)],
            PumpFunAddresses.FEE_PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_global_volume_accumulator() -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for the global volume accumulator.

        Returns:
            Pubkey of the derived global volume accumulator account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for a user's volume accumulator.

        Args:
            user: Pubkey of the user account

        Returns:
            Pubkey of the derived user volume accumulator account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_bonding_curve_v2(mint: Pubkey) -> Pubkey:
        """Derive the bonding curve v2 PDA for a token mint.

        Args:
            mint: Token mint address

        Returns:
            Pubkey of the derived bonding curve v2 account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"bonding-curve-v2", bytes(mint)],
            PumpFunAddresses.PROGRAM,
        )
        return derived_address

    @staticmethod
    def find_fee_config() -> Pubkey:
        """
        Derive the Program Derived Address (PDA) for the fee config.

        Returns:
            Pubkey of the derived fee config account
        """
        derived_address, _ = Pubkey.find_program_address(
            [b"fee_config", bytes(PumpFunAddresses.PROGRAM)],
            PumpFunAddresses.FEE_PROGRAM,
        )
        return derived_address


class PumpFunAddressProvider(AddressProvider):
    """Pump.Fun implementation of AddressProvider interface."""

    @property
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        return Platform.PUMP_FUN

    @property
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform."""
        return PumpFunAddresses.PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for pump.fun.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        # Get system addresses from the single source of truth
        system_addresses = SystemAddresses.get_all_system_addresses()

        # Add pump.fun specific addresses
        pumpfun_addresses = {
            # Pump.fun specific addresses
            "program": PumpFunAddresses.PROGRAM,
            "global": PumpFunAddresses.GLOBAL,
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "fee": PumpFunAddresses.FEE,
            "liquidity_migrator": PumpFunAddresses.LIQUIDITY_MIGRATOR,
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
        }

        # Combine system and platform-specific addresses
        return {**system_addresses, **pumpfun_addresses}

    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the bonding curve address for a token.

        For pump.fun, this is the bonding curve PDA derived from the mint.

        Args:
            base_mint: Token mint address
            quote_mint: Not used for pump.fun (SOL is always the quote)

        Returns:
            Bonding curve address
        """
        bonding_curve, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(base_mint)], PumpFunAddresses.PROGRAM
        )
        return bonding_curve

    def derive_user_token_account(
        self, user: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive user's associated token account address.

        Args:
            user: User's wallet address
            mint: Token mint address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            User's associated token account address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
        return get_associated_token_address(user, mint, token_program_id)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get pump.fun-specific additional accounts needed for trading.

        Args:
            token_info: Token information

        Returns:
            Dictionary of additional account addresses
        """
        accounts = {}

        # Add bonding curve if available
        if token_info.bonding_curve:
            accounts["bonding_curve"] = token_info.bonding_curve

        # Add associated bonding curve if available
        if token_info.associated_bonding_curve:
            accounts["associated_bonding_curve"] = token_info.associated_bonding_curve

        # Add creator vault if available
        if token_info.creator_vault:
            accounts["creator_vault"] = token_info.creator_vault

        # Derive associated bonding curve if not provided
        if not token_info.associated_bonding_curve and token_info.bonding_curve:
            accounts["associated_bonding_curve"] = self.derive_associated_bonding_curve(
                token_info.mint, token_info.bonding_curve, token_info.token_program_id
            )

        # Derive creator vault if not provided but creator is available
        if not token_info.creator_vault and token_info.creator:
            accounts["creator_vault"] = self.derive_creator_vault(token_info.creator)

        return accounts

    def derive_associated_bonding_curve(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        token_program_id: Pubkey | None = None,
    ) -> Pubkey:
        """Derive the associated bonding curve (ATA of bonding curve for the token).

        Args:
            mint: Token mint address
            bonding_curve: Bonding curve address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            Associated bonding curve address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

        derived_address, _ = Pubkey.find_program_address(
            [
                bytes(bonding_curve),
                bytes(token_program_id),
                bytes(mint),
            ],
            SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        )
        return derived_address

    def derive_creator_vault(self, creator: Pubkey) -> Pubkey:
        """Derive the creator vault address.

        Args:
            creator: Creator address

        Returns:
            Creator vault address
        """
        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)], PumpFunAddresses.PROGRAM
        )
        return creator_vault

    def derive_global_volume_accumulator(self) -> Pubkey:
        """Derive the global volume accumulator PDA.

        Returns:
            Global volume accumulator address
        """
        return PumpFunAddresses.find_global_volume_accumulator()

    def derive_user_volume_accumulator(self, user: Pubkey) -> Pubkey:
        """Derive the user volume accumulator PDA.

        Args:
            user: User address

        Returns:
            User volume accumulator address
        """
        return PumpFunAddresses.find_user_volume_accumulator(user)

    def derive_bonding_curve_v2(self, mint: Pubkey) -> Pubkey:
        """Derive the bonding curve v2 PDA for a token mint.

        Args:
            mint: Token mint address

        Returns:
            Bonding curve v2 address
        """
        return PumpFunAddresses.find_bonding_curve_v2(mint)

    def derive_fee_config(self) -> Pubkey:
        """Derive the fee config PDA.

        Returns:
            Fee config address
        """
        return PumpFunAddresses.find_fee_config()

    def derive_sharing_config(self, base_mint: Pubkey) -> Pubkey:
        """Derive the creator-fee sharing config PDA for a coin.

        Args:
            base_mint: Base token mint address

        Returns:
            Sharing config address
        """
        return PumpFunAddresses.find_sharing_config(base_mint)

    def resolve_quote(self, token_info: TokenInfo) -> tuple[Pubkey, Pubkey]:
        """Resolve the quote mint and its token program for a coin.

        SOL-paired coins store Pubkey::default() in bonding_curve.quote_mint
        but must pass wrapped SOL to the v2 instructions.

        Args:
            token_info: Token information

        Returns:
            Tuple of (quote_mint, quote_token_program)
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        quote_program = token_info.quote_token_program_id or quote_token_program(
            quote_mint
        )
        return quote_mint, quote_program

    def derive_quote_token_account(
        self, owner: Pubkey, quote_mint: Pubkey, quote_token_program_id: Pubkey
    ) -> Pubkey:
        """Derive an associated token account for the quote mint.

        Args:
            owner: Account that owns the ATA (may be a PDA)
            quote_mint: Quote mint address
            quote_token_program_id: Token program owning the quote mint

        Returns:
            Associated token account address
        """
        return get_associated_token_address(owner, quote_mint, quote_token_program_id)

    def _get_v2_common_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Build the account set shared by buy_v2 and sell_v2.

        Both instructions take the same 26 accounts; buy_v2 additionally takes
        global_volume_accumulator. All accounts are mandatory — there are no
        optional or conditional accounts on the v2 interface, regardless of
        mayhem/cashback/quote-mint combination.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses keyed by IDL account name
        """
        additional_accounts = self.get_additional_accounts(token_info)

        base_mint = token_info.mint
        base_token_program = (
            token_info.token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
        )
        quote_mint, quote_program = self.resolve_quote(token_info)

        bonding_curve = additional_accounts.get(
            "bonding_curve", token_info.bonding_curve
        )
        creator_vault = additional_accounts.get(
            "creator_vault", token_info.creator_vault
        )
        fee_recipient = self.get_fee_recipient(token_info)
        buyback_fee_recipient = PumpFunAddresses.pick_buyback_fee_recipient()
        user_volume_accumulator = self.derive_user_volume_accumulator(user)

        return {
            "global": PumpFunAddresses.GLOBAL,
            "base_mint": base_mint,
            "quote_mint": quote_mint,
            "base_token_program": base_token_program,
            "quote_token_program": quote_program,
            "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
            "fee_recipient": fee_recipient,
            "associated_quote_fee_recipient": self.derive_quote_token_account(
                fee_recipient, quote_mint, quote_program
            ),
            "buyback_fee_recipient": buyback_fee_recipient,
            "associated_quote_buyback_fee_recipient": self.derive_quote_token_account(
                buyback_fee_recipient, quote_mint, quote_program
            ),
            "bonding_curve": bonding_curve,
            "associated_base_bonding_curve": additional_accounts.get(
                "associated_bonding_curve", token_info.associated_bonding_curve
            ),
            "associated_quote_bonding_curve": self.derive_quote_token_account(
                bonding_curve, quote_mint, quote_program
            ),
            "user": user,
            "associated_base_user": self.derive_user_token_account(
                user, base_mint, base_token_program
            ),
            "associated_quote_user": self.derive_quote_token_account(
                user, quote_mint, quote_program
            ),
            "creator_vault": creator_vault,
            "associated_creator_vault": self.derive_quote_token_account(
                creator_vault, quote_mint, quote_program
            ),
            "sharing_config": self.derive_sharing_config(base_mint),
            "user_volume_accumulator": user_volume_accumulator,
            "associated_user_volume_accumulator": self.derive_quote_token_account(
                user_volume_accumulator, quote_mint, quote_program
            ),
            "fee_config": self.derive_fee_config(),
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "program": PumpFunAddresses.PROGRAM,
        }

    def get_buy_v2_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all 27 accounts needed for a buy_v2 instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for the buy_v2 instruction
        """
        accounts = self._get_v2_common_accounts(token_info, user)
        accounts["global_volume_accumulator"] = self.derive_global_volume_accumulator()
        return accounts

    def get_sell_v2_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all 26 accounts needed for a sell_v2 instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for the sell_v2 instruction
        """
        return self._get_v2_common_accounts(token_info, user)

    def get_fee_recipient(self, token_info: TokenInfo) -> Pubkey:
        """Get the correct fee recipient based on mayhem mode.

        Args:
            token_info: Token information with is_mayhem_mode flag

        Returns:
            Fee recipient address (mayhem or standard)
        """
        if token_info.is_mayhem_mode:
            return PumpFunAddresses.MAYHEM_FEE
        return PumpFunAddresses.FEE

    def get_buy_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a buy instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for buy instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_PROGRAM
        )

        # Determine fee recipient based on mayhem mode
        fee_recipient = self.get_fee_recipient(token_info)

        return {
            "global": PumpFunAddresses.GLOBAL,
            "fee": fee_recipient,
            "mint": token_info.mint,
            "bonding_curve": additional_accounts.get(
                "bonding_curve", token_info.bonding_curve
            ),
            "associated_bonding_curve": additional_accounts.get(
                "associated_bonding_curve", token_info.associated_bonding_curve
            ),
            "user_token_account": self.derive_user_token_account(
                user, token_info.mint, token_program_id
            ),
            "user": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "token_program": token_program_id,
            "creator_vault": additional_accounts.get(
                "creator_vault", token_info.creator_vault
            ),
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "program": PumpFunAddresses.PROGRAM,
            "global_volume_accumulator": self.derive_global_volume_accumulator(),
            "user_volume_accumulator": self.derive_user_volume_accumulator(user),
            "fee_config": self.derive_fee_config(),
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
            "bonding_curve_v2": self.derive_bonding_curve_v2(token_info.mint),
            "breaking_fee_recipient": PumpFunAddresses.pick_breaking_fee_recipient(),
        }

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a sell instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for sell instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_PROGRAM
        )

        # Determine fee recipient based on mayhem mode
        fee_recipient = self.get_fee_recipient(token_info)

        return {
            "global": PumpFunAddresses.GLOBAL,
            "fee": fee_recipient,
            "mint": token_info.mint,
            "bonding_curve": additional_accounts.get(
                "bonding_curve", token_info.bonding_curve
            ),
            "associated_bonding_curve": additional_accounts.get(
                "associated_bonding_curve", token_info.associated_bonding_curve
            ),
            "user_token_account": self.derive_user_token_account(
                user, token_info.mint, token_program_id
            ),
            "user": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "creator_vault": additional_accounts.get(
                "creator_vault", token_info.creator_vault
            ),
            "token_program": token_program_id,
            "event_authority": PumpFunAddresses.EVENT_AUTHORITY,
            "program": PumpFunAddresses.PROGRAM,
            "fee_config": self.derive_fee_config(),
            "fee_program": PumpFunAddresses.FEE_PROGRAM,
            "bonding_curve_v2": self.derive_bonding_curve_v2(token_info.mint),
            "user_volume_accumulator": self.derive_user_volume_accumulator(user),
            "breaking_fee_recipient": PumpFunAddresses.pick_breaking_fee_recipient(),
        }

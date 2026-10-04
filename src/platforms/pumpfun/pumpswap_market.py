"""PumpSwap, where a pump.fun coin trades once its bonding curve completes."""

import random
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.client import SolanaClient
from core.pubkeys import (
    TOKEN_DECIMALS,
    SystemAddresses,
    normalize_quote_mint,
    quote_units_per_token,
)
from core.quote_account import quote_settlement_account
from interfaces.core import AddressProvider, GraduatedMarket, TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddresses
from utils.idl_parser import IDLParser

PUMP_AMM_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
)
PUMP_FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
)


def _amm_pda(*seeds: bytes) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), PUMP_AMM_PROGRAM)[0]


_PUMP_SWAP_IDL = Path(__file__).resolve().parents[3] / "idl" / "pump_swap_idl.json"

GLOBAL_CONFIG: Final[Pubkey] = _amm_pda(b"global_config")
EVENT_AUTHORITY: Final[Pubkey] = _amm_pda(b"__event_authority")
FEE_CONFIG: Final[Pubkey] = Pubkey.find_program_address(
    [b"fee_config", bytes(PUMP_AMM_PROGRAM)], PUMP_FEE_PROGRAM
)[0]

# Pool, after the discriminator: pool_bump u8, index u16, creator, base_mint,
# quote_mint, lp_mint, the two pool token accounts, lp_supply u64, coin_creator,
# is_mayhem_mode, is_cashback_coin, virtual_quote_reserves i128.
_POOL_BASE_ACCOUNT_OFFSET = 139
_POOL_QUOTE_ACCOUNT_OFFSET = 171
_POOL_COIN_CREATOR_OFFSET = 211
_POOL_MAYHEM_OFFSET = 243
_POOL_CASHBACK_OFFSET = 244
_POOL_VIRTUAL_QUOTE_OFFSET = 245

# GlobalConfig, after the discriminator: admin, lp/protocol fee bps (u64 each),
# disable_flags u8, protocol_fee_recipients[8], coin_creator_fee bps u64, two
# authorities, reserved_fee_recipient, mayhem flag, reserved_fee_recipients[7],
# cashback flag, buyback_fee_recipients[8].
_CONFIG_LP_FEE_OFFSET = 40
_CONFIG_PROTOCOL_FEE_OFFSET = 48
_CONFIG_PROTOCOL_RECIPIENTS_OFFSET = 57
_CONFIG_CREATOR_FEE_OFFSET = 313
_CONFIG_RESERVED_RECIPIENT_OFFSET = 385
_CONFIG_BUYBACK_RECIPIENTS_OFFSET = 643
_BASIS_POINTS = 10_000
_TOKEN_AMOUNT_OFFSET = 64


@dataclass(frozen=True)
class PumpSwapPool:
    """A canonical PumpSwap pool: the one a pump.fun curve migrated into."""

    address: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_account: Pubkey
    quote_account: Pubkey
    base_program: Pubkey
    quote_program: Pubkey
    coin_creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool


def canonical_pool_address(base_mint: Pubkey, quote_mint: Pubkey) -> Pubkey:
    """The pool a pump.fun migration creates: index 0, owned by the curve's authority."""
    pool_authority = Pubkey.find_program_address(
        [b"pool-authority", bytes(base_mint)], PumpFunAddresses.PROGRAM
    )[0]
    return _amm_pda(
        b"pool",
        struct.pack("<H", 0),
        bytes(pool_authority),
        bytes(base_mint),
        bytes(quote_mint),
    )


def _pubkeys(data: bytes, offset: int, count: int) -> list[Pubkey]:
    return [
        Pubkey.from_bytes(data[offset + 32 * i : offset + 32 * (i + 1)])
        for i in range(count)
    ]


class PumpSwapMarket(GraduatedMarket):
    """Sells a graduated pump.fun coin into its canonical PumpSwap pool.

    The pool is derived from the mint, so finding it costs no search. The
    accounts follow PumpSwap's `sell`: the IDL's 21, then the cashback pair on a
    cashback pool, `pool-v2` on a pool with a coin creator — which every
    canonical pool has — and a buyback recipient with its quote account last,
    which the program reads from the end of the list.
    """

    def __init__(self, client: SolanaClient) -> None:
        self.client = client
        self._sell_discriminator = IDLParser(
            str(_PUMP_SWAP_IDL)
        ).get_instruction_discriminators()["sell"]
        self._config: bytes | None = None
        self._pools: dict[Pubkey, PumpSwapPool] = {}

    async def _global_config(self) -> bytes:
        if self._config is None:
            self._config = (await self.client.get_account_info(GLOBAL_CONFIG)).data
        return self._config

    async def _resolve(self, token_info: TokenInfo) -> tuple[PumpSwapPool, bytes]:
        """The pool, and its current account data. Raises ValueError if it does not exist."""
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        address = canonical_pool_address(token_info.mint, quote_mint)
        pool_data = (await self.client.get_account_info(address)).data
        pool = self._pools.get(token_info.mint)
        if pool is None:
            base_account, quote_account = (
                Pubkey.from_bytes(pool_data[o : o + 32])
                for o in (_POOL_BASE_ACCOUNT_OFFSET, _POOL_QUOTE_ACCOUNT_OFFSET)
            )
            vaults = await self.client.get_multiple_accounts(
                [base_account, quote_account]
            )
            pool = PumpSwapPool(
                address=address,
                base_mint=token_info.mint,
                quote_mint=quote_mint,
                base_account=base_account,
                quote_account=quote_account,
                base_program=vaults[0].owner,
                quote_program=vaults[1].owner,
                coin_creator=Pubkey.from_bytes(
                    pool_data[
                        _POOL_COIN_CREATOR_OFFSET : _POOL_COIN_CREATOR_OFFSET + 32
                    ]
                ),
                is_mayhem_mode=bool(pool_data[_POOL_MAYHEM_OFFSET]),
                is_cashback_coin=bool(pool_data[_POOL_CASHBACK_OFFSET]),
            )
            self._pools[token_info.mint] = pool
        return pool, pool_data

    async def get_market_state(self, token_info: TokenInfo) -> dict[str, Any]:
        """Price from the pool's balances plus its virtual quote reserves.

        Raises:
            ValueError: If the pool does not exist yet
        """
        try:
            pool, pool_data = await self._resolve(token_info)
        except ValueError as e:
            raise ValueError(f"No PumpSwap pool for {token_info.mint} yet: {e}") from e  # noqa: TRY003
        base_vault, quote_vault = await self.client.get_multiple_accounts(
            [pool.base_account, pool.quote_account]
        )
        base_reserve = struct.unpack_from("<Q", base_vault.data, _TOKEN_AMOUNT_OFFSET)[
            0
        ]
        virtual_quote = int.from_bytes(
            pool_data[_POOL_VIRTUAL_QUOTE_OFFSET : _POOL_VIRTUAL_QUOTE_OFFSET + 16],
            "little",
            signed=True,
        )
        quote_reserve = (
            struct.unpack_from("<Q", quote_vault.data, _TOKEN_AMOUNT_OFFSET)[0]
            + virtual_quote
        )
        config = await self._global_config()
        fee_bps = sum(
            struct.unpack_from("<Q", config, offset)[0]
            for offset in (
                _CONFIG_LP_FEE_OFFSET,
                _CONFIG_PROTOCOL_FEE_OFFSET,
                _CONFIG_CREATOR_FEE_OFFSET,
            )
        )
        return {
            "price_per_token": (quote_reserve / quote_units_per_token(pool.quote_mint))
            / (base_reserve / 10**TOKEN_DECIMALS)
            if base_reserve > 0
            else 0.0,
            "fee_fraction": fee_bps / _BASIS_POINTS,
            "pool": pool.address,
        }

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,  # noqa: ARG002
    ) -> list[Instruction]:
        """Build PumpSwap `sell` for `amount_in` raw tokens."""
        pool, _ = await self._resolve(token_info)
        config = await self._global_config()
        quote_mint, quote_program = pool.quote_mint, pool.quote_program

        if pool.is_mayhem_mode:
            fee_recipient = _pubkeys(config, _CONFIG_RESERVED_RECIPIENT_OFFSET, 1)[0]
        else:
            fee_recipient = random.choice(
                _pubkeys(config, _CONFIG_PROTOCOL_RECIPIENTS_OFFSET, 8)
            )
        buyback_recipient = random.choice(
            _pubkeys(config, _CONFIG_BUYBACK_RECIPIENTS_OFFSET, 8)
        )
        creator_vault = _amm_pda(b"creator_vault", bytes(pool.coin_creator))
        user_quote, open_ixs, close_ixs = quote_settlement_account(
            user, quote_mint, quote_program
        )

        def ata(owner: Pubkey) -> Pubkey:
            return get_associated_token_address(owner, quote_mint, quote_program)

        accounts = [
            AccountMeta(pool.address, is_signer=False, is_writable=True),
            AccountMeta(user, is_signer=True, is_writable=True),
            AccountMeta(GLOBAL_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(pool.base_mint, is_signer=False, is_writable=False),
            AccountMeta(quote_mint, is_signer=False, is_writable=False),
            AccountMeta(
                get_associated_token_address(user, pool.base_mint, pool.base_program),
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(user_quote, is_signer=False, is_writable=True),
            AccountMeta(pool.base_account, is_signer=False, is_writable=True),
            AccountMeta(pool.quote_account, is_signer=False, is_writable=True),
            AccountMeta(fee_recipient, is_signer=False, is_writable=False),
            AccountMeta(ata(fee_recipient), is_signer=False, is_writable=True),
            AccountMeta(pool.base_program, is_signer=False, is_writable=False),
            AccountMeta(quote_program, is_signer=False, is_writable=False),
            AccountMeta(
                SystemAddresses.SYSTEM_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(PUMP_AMM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(ata(creator_vault), is_signer=False, is_writable=True),
            AccountMeta(creator_vault, is_signer=False, is_writable=False),
            AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]
        if pool.is_cashback_coin:
            accumulator = _amm_pda(b"user_volume_accumulator", bytes(user))
            accounts += [
                AccountMeta(ata(accumulator), is_signer=False, is_writable=True),
                AccountMeta(accumulator, is_signer=False, is_writable=True),
            ]
        if pool.coin_creator != Pubkey.default():
            accounts.append(
                AccountMeta(
                    _amm_pda(b"pool-v2", bytes(pool.base_mint)),
                    is_signer=False,
                    is_writable=False,
                )
            )
        accounts += [
            AccountMeta(buyback_recipient, is_signer=False, is_writable=False),
            AccountMeta(ata(buyback_recipient), is_signer=False, is_writable=True),
        ]
        data = self._sell_discriminator + struct.pack(
            "<QQ", amount_in, minimum_amount_out
        )
        return [*open_ixs, Instruction(PUMP_AMM_PROGRAM, data, accounts), *close_ixs]

    def get_required_accounts_for_sell(
        self,
        token_info: TokenInfo,
        user: Pubkey,  # noqa: ARG002
        address_provider: AddressProvider,  # noqa: ARG002
    ) -> list[Pubkey]:
        """The pool and its token accounts, once the pool has been resolved."""
        pool = self._pools.get(token_info.mint)
        if pool is None:
            return [PUMP_AMM_PROGRAM]
        return [pool.address, pool.base_account, pool.quote_account, PUMP_AMM_PROGRAM]

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a PumpSwap sell."""
        return config_override if config_override is not None else 200_000

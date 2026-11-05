"""PumpSwap, where a pump.fun coin trades once its bonding curve completes."""

import random
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

from core.client import SolanaClient
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    SystemAddresses,
    normalize_quote_mint,
    quote_units_per_token,
)
from core.quote_account import quote_settlement_account, wrapped_sol_ata
from interfaces.core import Market, TokenInfo
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


PUMP_SWAP_IDL = Path(__file__).resolve().parents[3] / "idl" / "pump_swap_idl.json"

GLOBAL_CONFIG: Final[Pubkey] = _amm_pda(b"global_config")
EVENT_AUTHORITY: Final[Pubkey] = _amm_pda(b"__event_authority")
GLOBAL_VOLUME_ACCUMULATOR: Final[Pubkey] = _amm_pda(b"global_volume_accumulator")
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
# buy_exact_quote_in's last arg, track_volume: an OptionBool, sent as a bare bool.
_TRACK_VOLUME = b"\x01"
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


class PumpSwapMarket(Market):
    """Buys and sells a graduated pump.fun coin in its canonical PumpSwap pool.

    The pool is derived from the mint, so finding it costs no search. The
    accounts follow PumpSwap's swaps: the IDL's 23 (buy) or 21 (sell), then the
    user's cashback account(s) on a cashback pool, `pool-v2` on a pool with a
    coin creator — which every canonical pool has — and a buyback recipient
    with its quote account last, which the program reads from the end of the
    list.
    """

    name = "PumpSwap"

    def __init__(self, client: SolanaClient) -> None:
        self.client = client
        discriminators = IDLParser(str(PUMP_SWAP_IDL)).get_instruction_discriminators()
        self._buy_discriminator = discriminators["buy_exact_quote_in"]
        self._sell_discriminator = discriminators["sell"]
        self._config: bytes | None = None
        self._pools: dict[Pubkey, PumpSwapPool] = {}

    @property
    def program_id(self) -> Pubkey:
        """PumpSwap (pump-amm)."""
        return PUMP_AMM_PROGRAM

    @property
    def spends_exact_amount_in(self) -> bool:
        """A buy is `buy_exact_quote_in`: it spends all of `amount_in`."""
        return True

    async def _global_config(self) -> bytes:
        if self._config is None:
            self._config = (await self.client.get_account_info(GLOBAL_CONFIG)).data
        return self._config

    async def _resolve(self, token_info: TokenInfo) -> tuple[PumpSwapPool, bytes]:
        """The pool, and its current account data. Raises ValueError if it does not exist.

        The coin creator and the mayhem and cashback flags are taken from that
        data every time: fee sharing can move the coin creator while a
        position is held, and a swap naming the old one's vault reverts.
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        address = canonical_pool_address(token_info.mint, quote_mint)
        pool_data = (await self.client.get_account_info(address)).data
        flags = {
            "coin_creator": Pubkey.from_bytes(
                pool_data[_POOL_COIN_CREATOR_OFFSET : _POOL_COIN_CREATOR_OFFSET + 32]
            ),
            "is_mayhem_mode": bool(pool_data[_POOL_MAYHEM_OFFSET]),
            "is_cashback_coin": bool(pool_data[_POOL_CASHBACK_OFFSET]),
        }
        pool = self._pools.get(token_info.mint)
        if pool is not None:
            pool = replace(pool, **flags)
        else:
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
                **flags,
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

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
    ) -> list[Instruction]:
        """Build PumpSwap `buy_exact_quote_in`, spending exactly `amount_in`.

        `amount_in` includes the pool's fees. A SOL pool is paid through the
        wallet's WSOL ATA, funded with `amount_in` and closed in the same
        transaction: the throwaway account the sell uses does not fit beside a
        buy's account list. Any other quote is paid from the wallet's ATA, which
        must already hold it.
        """
        pool, _ = await self._resolve(token_info)
        if pool.quote_mint == WSOL_MINT:
            user_quote, open_ixs, close_ixs = wrapped_sol_ata(user, amount_in)
        else:
            user_quote, open_ixs, close_ixs = quote_settlement_account(
                user, pool.quote_mint, pool.quote_program
            )
        create_base_ata = create_idempotent_associated_token_account(
            user, user, pool.base_mint, pool.base_program
        )
        accounts = await self._swap_accounts(pool, user, user_quote, is_buy=True)
        data = (
            self._buy_discriminator
            + struct.pack("<QQ", amount_in, minimum_amount_out)
            + _TRACK_VOLUME
        )
        swap = Instruction(PUMP_AMM_PROGRAM, data, accounts)
        return [*open_ixs, create_base_ata, swap, *close_ixs]

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
    ) -> list[Instruction]:
        """Build PumpSwap `sell` for `amount_in` raw tokens."""
        pool, _ = await self._resolve(token_info)
        user_quote, open_ixs, close_ixs = quote_settlement_account(
            user, pool.quote_mint, pool.quote_program
        )
        accounts = await self._swap_accounts(pool, user, user_quote, is_buy=False)
        data = self._sell_discriminator + struct.pack(
            "<QQ", amount_in, minimum_amount_out
        )
        return [*open_ixs, Instruction(PUMP_AMM_PROGRAM, data, accounts), *close_ixs]

    async def _swap_accounts(
        self, pool: PumpSwapPool, user: Pubkey, user_quote: Pubkey, *, is_buy: bool
    ) -> list[AccountMeta]:
        """The swap's accounts, in the order the program reads them.

        A buy adds the volume accumulators after the creator vault, and on a
        cashback pool sends only the accumulator's quote account where a sell
        also sends the accumulator itself.
        """
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
        accumulator = _amm_pda(b"user_volume_accumulator", bytes(user))

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
        ]
        if is_buy:
            accounts += [
                AccountMeta(
                    GLOBAL_VOLUME_ACCUMULATOR, is_signer=False, is_writable=False
                ),
                AccountMeta(accumulator, is_signer=False, is_writable=True),
            ]
        accounts += [
            AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]
        if pool.is_cashback_coin:
            accounts.append(
                AccountMeta(ata(accumulator), is_signer=False, is_writable=True)
            )
            if not is_buy:
                accounts.append(
                    AccountMeta(accumulator, is_signer=False, is_writable=True)
                )
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
        return accounts

    def get_required_accounts_for_buy(
        self,
        token_info: TokenInfo,
        user: Pubkey,
    ) -> list[Pubkey]:
        """The pool and its token accounts, as for a sell."""
        return self.get_required_accounts_for_sell(token_info, user)

    def get_required_accounts_for_sell(
        self,
        token_info: TokenInfo,
        user: Pubkey,  # noqa: ARG002
    ) -> list[Pubkey]:
        """The pool and its token accounts, once the pool has been resolved."""
        pool = self._pools.get(token_info.mint)
        if pool is None:
            return [PUMP_AMM_PROGRAM]
        return [pool.address, pool.base_account, pool.quote_account, PUMP_AMM_PROGRAM]

    def quote_destination(self, token_info: TokenInfo) -> Pubkey | None:
        """The pool's quote account, once the pool has been resolved."""
        pool = self._pools.get(token_info.mint)
        return pool.quote_account if pool else None

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a PumpSwap buy.

        More than a sell: the buy also creates the base-token ATA and, on the
        wallet's first PumpSwap buy, its volume accumulator.
        """
        return config_override if config_override is not None else 250_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a PumpSwap sell."""
        return config_override if config_override is not None else 200_000

"""Raydium CPMM, where a LaunchLab pool migrates when its curve graduates."""

import hashlib
import struct
from dataclasses import dataclass
from typing import Any, Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.client import SolanaClient
from core.pubkeys import normalize_quote_mint
from core.quote_account import quote_settlement_account
from interfaces.core import AddressProvider, GraduatedMarket, TokenInfo

CPMM_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
)
_AUTHORITY: Final[Pubkey] = Pubkey.find_program_address(
    [b"vault_and_lp_mint_auth_seed"], CPMM_PROGRAM
)[0]
_SWAP_BASE_INPUT: Final[bytes] = hashlib.sha256(b"global:swap_base_input").digest()[:8]

FEE_RATE_DENOMINATOR = 1_000_000

# PlatformConfig.cpswap_config: discriminator, epoch, two wallets, three u64
# scales, fee_rate, name[64], web[256], img[256].
_PLATFORM_CPSWAP_CONFIG_OFFSET = 8 + 8 + 32 + 32 + 3 * 8 + 8 + 64 + 256 + 256
# LaunchLab PoolState.migrate_type: discriminator, epoch, auth_bump, status and
# the two decimals bytes. 0 migrates to AMM v4, 1 to CPMM.
_LAUNCHLAB_MIGRATE_TYPE_OFFSET = 8 + 8 + 4
_MIGRATE_TO_AMM_V4 = 0
# LaunchLab PoolState.platform_config: discriminator, epoch, five u8 flags, ten
# u64 amounts, a five-u64 vesting schedule, global_config.
_LAUNCHLAB_PLATFORM_CONFIG_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8 + 32

# CPMM PoolState: discriminator, then ten pubkeys, five u8, seven u64, two u8,
# six bytes of padding and two u64 creator-fee counters.
_POOL_KEYS = (
    "amm_config",
    "pool_creator",
    "token_0_vault",
    "token_1_vault",
    "lp_mint",
    "token_0_mint",
    "token_1_mint",
    "token_0_program",
    "token_1_program",
    "observation_key",
)
_POOL_DECIMALS_OFFSET = 8 + 10 * 32 + 3
_POOL_FEES_OFFSET = 8 + 10 * 32 + 5 + 8
_POOL_CREATOR_FEE_FLAG_OFFSET = 8 + 10 * 32 + 5 + 7 * 8 + 1
_POOL_CREATOR_FEES_OFFSET = 8 + 10 * 32 + 5 + 7 * 8 + 2 + 6
# AmmConfig: discriminator, bump, disable_create_pool, index u16, trade_fee_rate,
# three more u64, two owners, creator_fee_rate.
_CONFIG_TRADE_FEE_OFFSET = 8 + 1 + 1 + 2
_CONFIG_CREATOR_FEE_OFFSET = _CONFIG_TRADE_FEE_OFFSET + 4 * 8 + 2 * 32
# SPL token account amount.
_TOKEN_AMOUNT_OFFSET = 64


@dataclass(frozen=True)
class CpmmPool:
    """One CPMM pool, oriented around the coin being sold."""

    address: Pubkey
    amm_config: Pubkey
    observation: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    base_program: Pubkey
    quote_program: Pubkey
    base_decimals: int
    quote_decimals: int
    base_is_token_0: bool


def cpmm_pool_address(amm_config: Pubkey, mint_a: Pubkey, mint_b: Pubkey) -> Pubkey:
    """CPMM orders a pair's mints by their raw bytes before deriving the pool."""
    token_0, token_1 = sorted([mint_a, mint_b], key=bytes)
    return Pubkey.find_program_address(
        [b"pool", bytes(amm_config), bytes(token_0), bytes(token_1)], CPMM_PROGRAM
    )[0]


def decode_pool(address: Pubkey, data: bytes, base_mint: Pubkey) -> CpmmPool:
    """Decode a CPMM PoolState, oriented so `base` is `base_mint`."""
    keys = {
        name: Pubkey.from_bytes(data[8 + 32 * i : 8 + 32 * (i + 1)])
        for i, name in enumerate(_POOL_KEYS)
    }
    if base_mint not in (keys["token_0_mint"], keys["token_1_mint"]):
        raise ValueError(f"CPMM pool {address} does not trade {base_mint}")  # noqa: TRY003
    base = 0 if keys["token_0_mint"] == base_mint else 1
    quote = 1 - base

    def pick(field: str, index: int) -> Pubkey:
        return keys[f"token_{index}_{field}"]

    decimals = data[_POOL_DECIMALS_OFFSET : _POOL_DECIMALS_OFFSET + 2]
    return CpmmPool(
        address=address,
        amm_config=keys["amm_config"],
        observation=keys["observation_key"],
        base_mint=base_mint,
        quote_mint=pick("mint", quote),
        base_vault=pick("vault", base),
        quote_vault=pick("vault", quote),
        base_program=pick("program", base),
        quote_program=pick("program", quote),
        base_decimals=decimals[base],
        quote_decimals=decimals[quote],
        base_is_token_0=base == 0,
    )


def accrued_fees(data: bytes, base_is_token_0: bool) -> tuple[int, int]:
    """Fees sitting in the vaults that are not reserves, as (base, quote) raw.

    The vaults hold the protocol, fund and creator fees until they are claimed;
    the curve trades on the vault balance less these.
    """
    p0, p1, f0, f1 = struct.unpack_from("<4Q", data, _POOL_FEES_OFFSET)
    c0, c1 = struct.unpack_from("<2Q", data, _POOL_CREATOR_FEES_OFFSET)
    token_0, token_1 = p0 + f0 + c0, p1 + f1 + c1
    return (token_0, token_1) if base_is_token_0 else (token_1, token_0)


class CpmmMarket(GraduatedMarket):
    """Sells a graduated LaunchLab coin into its Raydium CPMM pool.

    The pool is derived, not searched for: CPMM keys it by the platform's
    `cpswap_config` and the two mints. Platform configs and AMM configs are
    shared by many pools and read once.
    """

    def __init__(self, client: SolanaClient) -> None:
        self.client = client
        self._cpswap_configs: dict[Pubkey, Pubkey] = {}
        self._pools: dict[Pubkey, CpmmPool] = {}

    async def _platform_config(self, token_info: TokenInfo) -> Pubkey:
        if token_info.platform_config:
            return token_info.platform_config
        pool = await self.client.get_account_info(token_info.pool_state)
        offset = _LAUNCHLAB_PLATFORM_CONFIG_OFFSET
        return Pubkey.from_bytes(pool.data[offset : offset + 32])

    async def _resolve(self, token_info: TokenInfo) -> CpmmPool:
        """Find and decode the coin's CPMM pool, cached per mint."""
        cached = self._pools.get(token_info.mint)
        if cached:
            return cached
        platform_config = await self._platform_config(token_info)
        amm_config = self._cpswap_configs.get(platform_config)
        if amm_config is None:
            config = await self.client.get_account_info(platform_config)
            offset = _PLATFORM_CPSWAP_CONFIG_OFFSET
            amm_config = Pubkey.from_bytes(config.data[offset : offset + 32])
            self._cpswap_configs[platform_config] = amm_config
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        address = cpmm_pool_address(amm_config, token_info.mint, quote_mint)
        account = await self.client.get_account_info(address)
        pool = decode_pool(address, account.data, token_info.mint)
        self._pools[token_info.mint] = pool
        return pool

    async def _migrated_to_amm_v4(self, token_info: TokenInfo) -> bool:
        """Whether the LaunchLab pool was set to migrate to AMM v4, not CPMM."""
        if not token_info.pool_state:
            return False
        try:
            pool = await self.client.get_account_info(token_info.pool_state)
        except ValueError:
            return False
        return pool.data[_LAUNCHLAB_MIGRATE_TYPE_OFFSET] == _MIGRATE_TO_AMM_V4

    async def get_market_state(self, token_info: TokenInfo) -> dict[str, Any]:
        """Price from the vault balances less accrued fees; fees from the AMM config.

        Raises:
            ValueError: If the CPMM pool does not exist yet
        """
        try:
            pool = await self._resolve(token_info)
        except ValueError as e:
            if await self._migrated_to_amm_v4(token_info):
                raise ValueError(  # noqa: TRY003
                    f"{token_info.mint} migrated to Raydium AMM v4, which the bot "
                    f"cannot sell into"
                ) from e
            raise ValueError(  # noqa: TRY003
                f"No CPMM pool for {token_info.mint} yet: {e}"
            ) from e
        accounts = await self.client.get_multiple_accounts(
            [pool.address, pool.amm_config, pool.base_vault, pool.quote_vault]
        )
        if any(account is None for account in accounts):
            raise ValueError(f"CPMM pool {pool.address} accounts unreadable")  # noqa: TRY003
        pool_account, config, base_vault, quote_vault = accounts
        base_fees, quote_fees = accrued_fees(pool_account.data, pool.base_is_token_0)
        base_reserve = (
            struct.unpack_from("<Q", base_vault.data, _TOKEN_AMOUNT_OFFSET)[0]
            - base_fees
        )
        quote_reserve = (
            struct.unpack_from("<Q", quote_vault.data, _TOKEN_AMOUNT_OFFSET)[0]
            - quote_fees
        )
        trade_fee = struct.unpack_from("<Q", config.data, _CONFIG_TRADE_FEE_OFFSET)[0]
        creator_fee = (
            struct.unpack_from("<Q", config.data, _CONFIG_CREATOR_FEE_OFFSET)[0]
            if pool_account.data[_POOL_CREATOR_FEE_FLAG_OFFSET]
            else 0
        )
        return {
            "price_per_token": (quote_reserve / 10**pool.quote_decimals)
            / (base_reserve / 10**pool.base_decimals)
            if base_reserve > 0
            else 0.0,
            "fee_fraction": (trade_fee + creator_fee) / FEE_RATE_DENOMINATOR,
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
        """Build swap_base_input selling `amount_in` raw tokens for the quote."""
        pool = await self._resolve(token_info)
        user_base = get_associated_token_address(
            user, pool.base_mint, pool.base_program
        )
        quote_account, open_ixs, close_ixs = quote_settlement_account(
            user, pool.quote_mint, pool.quote_program
        )
        accounts = [
            AccountMeta(user, is_signer=True, is_writable=True),
            AccountMeta(_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pool.amm_config, is_signer=False, is_writable=False),
            AccountMeta(pool.address, is_signer=False, is_writable=True),
            AccountMeta(user_base, is_signer=False, is_writable=True),
            AccountMeta(quote_account, is_signer=False, is_writable=True),
            AccountMeta(pool.base_vault, is_signer=False, is_writable=True),
            AccountMeta(pool.quote_vault, is_signer=False, is_writable=True),
            AccountMeta(pool.base_program, is_signer=False, is_writable=False),
            AccountMeta(pool.quote_program, is_signer=False, is_writable=False),
            AccountMeta(pool.base_mint, is_signer=False, is_writable=False),
            AccountMeta(pool.quote_mint, is_signer=False, is_writable=False),
            AccountMeta(pool.observation, is_signer=False, is_writable=True),
        ]
        data = _SWAP_BASE_INPUT + struct.pack("<QQ", amount_in, minimum_amount_out)
        return [*open_ixs, Instruction(CPMM_PROGRAM, data, accounts), *close_ixs]

    def get_required_accounts_for_sell(
        self,
        token_info: TokenInfo,
        user: Pubkey,  # noqa: ARG002
        address_provider: AddressProvider,  # noqa: ARG002
    ) -> list[Pubkey]:
        """The pool and its vaults, once the pool has been resolved."""
        pool = self._pools.get(token_info.mint)
        if pool is None:
            return [CPMM_PROGRAM]
        return [pool.address, pool.base_vault, pool.quote_vault, CPMM_PROGRAM]

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Compute units for a CPMM sell."""
        return config_override if config_override is not None else 150_000

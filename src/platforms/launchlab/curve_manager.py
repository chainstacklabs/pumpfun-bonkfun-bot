"""Raydium LaunchLab pool reads: state, price, curve fee and transfer fee."""

import struct
from time import monotonic
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import CurveManager
from platforms.launchlab.curve_math import (
    FEE_RATE_DENOMINATOR,
    buy_amount_out,
    mint_transfer_fee,
    sell_amount_out,
    spot_price_raw,
)
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

# GlobalConfig: discriminator, epoch u64, curve_type u8, index u16, migrate_fee u64.
_GLOBAL_TRADE_FEE_RATE_OFFSET = 8 + 8 + 1 + 2 + 8
# PlatformConfig: discriminator, epoch, two wallets, three u64 scales, fee_rate;
# then name[64], web[256], img[256], cpswap_config, creator_fee_rate.
_PLATFORM_FEE_RATE_OFFSET = 8 + 8 + 32 + 32 + 3 * 8
_PLATFORM_CREATOR_FEE_RATE_OFFSET = _PLATFORM_FEE_RATE_OFFSET + 8 + 64 + 256 + 256 + 32

# Fee rates are read live by the program on every trade, and a platform admin
# can change them, so a cached rate is re-read after this long.
_FEE_RATE_TTL_SECONDS = 300.0

_PUBKEY_FIELDS = (
    "global_config",
    "platform_config",
    "base_mint",
    "quote_mint",
    "base_vault",
    "quote_vault",
    "creator",
)


class LaunchLabCurveManager(CurveManager):
    """Reads LaunchLab pools. Subclasses only name their platform.

    `get_pool_state` returns, besides the decoded pool:

    - `price_per_token`: whole quote units per whole token, from the real
      reserves and the pool's own decimals
    - `fee_rate`: total curve fee in parts per million, and `fee_fraction`
    - `transfer_fee_bps` / `transfer_fee_max`: the coin's Token-2022 transfer
      fee, zero for a mint without one
    """

    def __init__(self, client: SolanaClient, idl_parser: IDLParser) -> None:
        self.client = client
        self._idl_parser = idl_parser
        self._fee_rates: dict[tuple[Pubkey, Pubkey], tuple[int, float]] = {}
        self._transfer_fees: dict[Pubkey, tuple[int, int]] = {}

    async def get_pool_state(
        self, pool_address: Pubkey, commitment: str | None = None
    ) -> dict[str, Any]:
        """Read and decode a pool, with its fee rate and transfer fee.

        Args:
            commitment: Optional override; pass "processed" right after a
                stream event so a pool created in that slot is readable

        Raises:
            ValueError: If the pool account is missing or undecodable
        """
        account = await self.client.get_account_info(
            pool_address, commitment=commitment
        )
        if account is None or not account.data:
            raise ValueError(f"No data in pool state account {pool_address}")  # noqa: TRY003
        pool = self._decode_pool(account.data)
        return await self._with_fees(pool, commitment)

    async def get_pool_state_and_token_program(
        self, pool_address: Pubkey, mint: Pubkey, commitment: str | None = None
    ) -> tuple[dict[str, Any], Pubkey | None]:
        """Read the pool and the coin's mint in one slot-consistent round trip.

        Returns:
            (pool state as from get_pool_state, the mint's token program or None
            if the mint was not readable)

        Raises:
            ValueError: If the pool account is missing or undecodable
        """
        pool_account, mint_account = await self.client.get_multiple_accounts(
            [pool_address, mint], commitment=commitment
        )
        if pool_account is None or not pool_account.data:
            raise ValueError(f"No data in pool state account {pool_address}")  # noqa: TRY003
        if mint_account is not None:
            self._transfer_fees[mint] = mint_transfer_fee(mint_account.data) or (0, 0)
        pool = self._decode_pool(pool_account.data)
        state = await self._with_fees(pool, commitment)
        return state, mint_account.owner if mint_account is not None else None

    def _decode_pool(self, data: bytes) -> dict[str, Any]:
        decoded = self._idl_parser.decode_account_data(
            data, "PoolState", skip_discriminator=True
        )
        if not decoded:
            raise ValueError("Failed to decode PoolState")  # noqa: TRY003
        pool = dict(decoded)
        for field in _PUBKEY_FIELDS:
            pool[field] = Pubkey.from_string(str(pool[field]))
        return pool

    async def _with_fees(
        self, pool: dict[str, Any], commitment: str | None
    ) -> dict[str, Any]:
        """Attach price, curve fee and transfer fee, reading only what is not cached."""
        config_key = (pool["global_config"], pool["platform_config"])
        cached_rate = self._fee_rates.get(config_key)
        rate_fresh = (
            cached_rate is not None
            and monotonic() - cached_rate[1] < _FEE_RATE_TTL_SECONDS
        )
        mint = pool["base_mint"]

        missing = [] if rate_fresh else [pool["global_config"], pool["platform_config"]]
        if mint not in self._transfer_fees:
            missing.append(mint)
        if missing:
            accounts = dict(
                zip(
                    missing,
                    await self.client.get_multiple_accounts(
                        missing, commitment=commitment
                    ),
                    strict=True,
                )
            )
            if not rate_fresh:
                global_config = accounts[pool["global_config"]]
                platform_config = accounts[pool["platform_config"]]
                if global_config is None or platform_config is None:
                    raise ValueError(  # noqa: TRY003
                        f"Fee configs for pool on {pool['platform_config']} unreadable"
                    )
                rate = (
                    struct.unpack_from(
                        "<Q", global_config.data, _GLOBAL_TRADE_FEE_RATE_OFFSET
                    )[0]
                    + struct.unpack_from(
                        "<Q", platform_config.data, _PLATFORM_FEE_RATE_OFFSET
                    )[0]
                    + struct.unpack_from(
                        "<Q", platform_config.data, _PLATFORM_CREATOR_FEE_RATE_OFFSET
                    )[0]
                )
                self._fee_rates[config_key] = (rate, monotonic())
            if mint in accounts and accounts[mint] is not None:
                self._transfer_fees[mint] = mint_transfer_fee(accounts[mint].data) or (
                    0,
                    0,
                )

        fee_rate = self._fee_rates[config_key][0]
        transfer_bps, transfer_max = self._transfer_fees.get(mint, (0, 0))
        price_raw = spot_price_raw(pool)
        return {
            **pool,
            "price_per_token": price_raw
            * 10 ** pool["base_decimals"]
            / 10 ** pool["quote_decimals"],
            "fee_rate": fee_rate,
            "fee_fraction": fee_rate / FEE_RATE_DENOMINATOR,
            "transfer_fee_bps": transfer_bps,
            "transfer_fee_max": transfer_max,
        }

    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Spot price in whole quote units per whole token; 0.0 for an empty curve."""
        return (await self.get_pool_state(pool_address))["price_per_token"]

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Raw tokens delivered for exactly `amount_in` raw quote, after fee and tax."""
        pool = await self.get_pool_state(pool_address)
        return buy_amount_out(
            pool,
            amount_in,
            pool["fee_rate"],
            (pool["transfer_fee_bps"], pool["transfer_fee_max"]),
        )

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Raw quote paid for sending `amount_in` raw tokens, after tax and fee."""
        pool = await self.get_pool_state(pool_address)
        return sell_amount_out(
            pool,
            amount_in,
            pool["fee_rate"],
            (pool["transfer_fee_bps"], pool["transfer_fee_max"]),
        )

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Effective (base, quote) reserves the curve is trading against, raw."""
        pool = await self.get_pool_state(pool_address)
        return (
            pool["virtual_base"] - pool["real_base"],
            pool["virtual_quote"] + pool["real_quote"],
        )

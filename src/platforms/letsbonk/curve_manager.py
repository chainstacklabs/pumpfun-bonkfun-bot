"""
LetsBonk implementation of CurveManager interface.

This module handles LetsBonk (Raydium LaunchLab) specific pool operations
by implementing the CurveManager interface using IDL-based decoding.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import SystemAddresses
from core.quote_engine import QuoteError, calculate_transfer_fee_raw
from interfaces.core import CurveManager, Platform
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class LaunchLabCurveType(IntEnum):
    """Curve variants encoded by LaunchLab GlobalConfig."""

    CONSTANT_PRODUCT = 0
    FIXED_PRICE = 1
    LINEAR_PRICE = 2


class LaunchLabPoolStatus(IntEnum):
    """Execution-relevant LaunchLab pool states."""

    FUNDING = 0
    WAITING_FOR_MIGRATION = 1
    MIGRATED = 2


@dataclass(frozen=True, slots=True)
class LaunchLabFees:
    """Quote-denominated LaunchLab trading fees."""

    protocol_rate: int
    platform_rate: int
    creator_rate: int

    DENOMINATOR = 1_000_000

    def __post_init__(self) -> None:
        for name, rate in (
            ("protocol", self.protocol_rate),
            ("platform", self.platform_rate),
            ("creator", self.creator_rate),
        ):
            if isinstance(rate, bool) or not isinstance(rate, int):
                raise ValueError(f"{name} fee rate must be an integer")
            if not 0 <= rate < self.DENOMINATOR:
                raise ValueError(f"{name} fee rate is out of range: {rate}")
        if self.total_rate >= self.DENOMINATOR:
            raise ValueError(
                f"Combined LaunchLab fee rate is invalid: {self.total_rate}"
            )

    @property
    def total_rate(self) -> int:
        """Return the combined quote fee rate."""
        return self.protocol_rate + self.platform_rate + self.creator_rate

    def deduct_from(self, amount: int) -> int:
        """Deduct fees conservatively, rounding the fee up by one raw unit."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Fee input must be a non-negative raw integer amount")
        fee = (amount * self.total_rate + self.DENOMINATOR - 1) // self.DENOMINATOR
        return amount - fee


class LetsBonkCurveManager(CurveManager):
    """Authoritative LaunchLab pool decoder and constant-curve quote engine."""

    _POOL_STATE_ACCOUNT_LENGTH = 429
    _SUPPORTED_CURVE = LaunchLabCurveType.CONSTANT_PRODUCT
    _MINT_BASE_LENGTH = 82
    _MINT_ACCOUNT_TYPE = 1
    _TRANSFER_FEE_CONFIG_EXTENSION = 1
    _TRANSFER_FEE_CONFIG_LENGTH = 108
    _CLOCK_SYSVAR = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
    _SYSVAR_OWNER = Pubkey.from_string("Sysvar1111111111111111111111111111111111111")

    def __init__(self, client: SolanaClient, idl_parser: IDLParser):
        """Initialize the manager with its RPC client and LaunchLab IDL."""
        self.client = client
        self.address_provider = LetsBonkAddressProvider()
        self._idl_parser = idl_parser
        logger.info("LetsBonk curve manager initialized with injected IDL parser")

    @property
    def platform(self) -> Platform:
        """Get the platform this manager serves."""
        return Platform.LETS_BONK

    async def get_pool_state(
        self, pool_address: Pubkey, commitment: str | None = None
    ) -> dict[str, Any]:
        """Read and validate a LaunchLab pool and its authoritative configs."""
        try:
            account = await self.client.get_account_info(
                pool_address, commitment=commitment
            )
            pool_data_bytes = self._validated_account_data(
                account,
                address=pool_address,
                account_type="PoolState",
                expected_length=self._POOL_STATE_ACCOUNT_LENGTH,
            )
            pool_state = self._decode_pool_state_with_idl(pool_data_bytes)

            global_config = self._require_pubkey(pool_state, "global_config")
            platform_config = self._require_pubkey(pool_state, "platform_config")
            base_mint = self._require_pubkey(pool_state, "base_mint")
            quote_mint = self._require_pubkey(pool_state, "quote_mint")
            token_program_flag = self._require_integer(pool_state, "token_program_flag")
            if token_program_flag & ~0b11:
                raise ValueError(
                    f"Unsupported LaunchLab token_program_flag {token_program_flag}"
                )
            base_token_program = self._token_program_from_flag(
                token_program_flag & 0b01
            )
            quote_token_program = self._token_program_from_flag(
                (token_program_flag >> 1) & 0b01
            )

            account_addresses = [global_config, platform_config]
            token_2022_mints: list[tuple[str, Pubkey, int]] = []
            if base_token_program == SystemAddresses.TOKEN_2022_PROGRAM:
                token_2022_mints.append(
                    (
                        "base",
                        base_mint,
                        self._require_integer(pool_state, "base_decimals"),
                    )
                )
                account_addresses.append(base_mint)
            if quote_token_program == SystemAddresses.TOKEN_2022_PROGRAM:
                token_2022_mints.append(
                    (
                        "quote",
                        quote_mint,
                        self._require_integer(pool_state, "quote_decimals"),
                    )
                )
                account_addresses.append(quote_mint)
            if token_2022_mints:
                account_addresses.append(self._CLOCK_SYSVAR)

            linked_accounts = await self.client.get_multiple_accounts(
                account_addresses, commitment=commitment
            )
            if len(linked_accounts) != len(account_addresses):
                raise ValueError(
                    "LaunchLab linked-account read returned an incomplete account set"
                )

            global_data = self._validated_account_data(
                linked_accounts[0],
                address=global_config,
                account_type="GlobalConfig",
            )
            platform_data = self._validated_account_data(
                linked_accounts[1],
                address=platform_config,
                account_type="PlatformConfig",
            )
            transfer_fees: dict[str, dict[str, int] | None] = {
                "base": None,
                "quote": None,
            }
            if token_2022_mints:
                current_epoch = self._decode_clock_epoch(linked_accounts[-1])
                for offset, (side, mint, decimals) in enumerate(
                    token_2022_mints, start=2
                ):
                    mint_data = self._validated_token_2022_mint_data(
                        linked_accounts[offset], address=mint
                    )
                    transfer_fees[side] = self._decode_transfer_fee_schedule(
                        mint_data,
                        current_epoch=current_epoch,
                        expected_decimals=decimals,
                    )

            decoded_global = self._decode_config(global_data, "GlobalConfig")
            decoded_platform = self._decode_config(platform_data, "PlatformConfig")
            return self._attach_authoritative_metadata(
                pool_state,
                decoded_global=decoded_global,
                decoded_platform=decoded_platform,
                pool_address=pool_address,
                base_transfer_fee=transfer_fees["base"],
                quote_transfer_fee=transfer_fees["quote"],
            )
        except Exception as error:
            logger.exception("Failed to get LaunchLab pool state")
            raise ValueError(f"Invalid LaunchLab pool state: {error}") from error

    async def get_pool_state_and_token_program(
        self,
        pool_address: Pubkey,
        mint: Pubkey,
        commitment: str | None = None,
    ) -> tuple[dict[str, Any], Pubkey]:
        """Return pool state with its authoritative base token program."""
        pool_state = await self.get_pool_state(pool_address, commitment=commitment)
        base_mint = self._require_pubkey(pool_state, "base_mint")
        if base_mint != mint:
            raise ValueError(
                f"Pool base mint {base_mint} does not match requested mint {mint}"
            )
        return pool_state, self._require_pubkey(pool_state, "base_token_program")

    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Calculate quote-token price for a supported, funding pool."""
        pool_state = await self.get_pool_state(pool_address)
        self._require_executable_state(pool_state)
        price = pool_state.get("price_per_token")
        if not isinstance(price, float) or price <= 0:
            raise ValueError("LaunchLab pool has no executable price")
        return price

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate constant-product base output for raw quote input."""
        self._require_positive_raw_amount(amount_in, "amount_in")
        pool_state = await self.get_pool_state(pool_address)
        fees = self._require_executable_state(pool_state)
        quote_in = self._deduct_transfer_fee(
            amount_in, pool_state["quote_transfer_fee"]
        )
        net_quote_in = fees.deduct_from(quote_in)
        if net_quote_in <= 0:
            return 0

        virtual_base = pool_state["virtual_base"]
        virtual_quote = pool_state["virtual_quote"]
        gross_base_out = (net_quote_in * virtual_base) // (virtual_quote + net_quote_in)
        gross_base_out = min(gross_base_out, pool_state["real_base"])
        return self._deduct_transfer_fee(
            gross_base_out, pool_state["base_transfer_fee"]
        )

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate fee-adjusted quote output for raw base input."""
        self._require_positive_raw_amount(amount_in, "amount_in")
        pool_state = await self.get_pool_state(pool_address)
        fees = self._require_executable_state(pool_state)

        effective_base_in = self._deduct_transfer_fee(
            amount_in, pool_state["base_transfer_fee"]
        )
        if effective_base_in <= 0:
            return 0
        virtual_base = pool_state["virtual_base"]
        virtual_quote = pool_state["virtual_quote"]
        gross_quote_out = (effective_base_in * virtual_quote) // (
            virtual_base + effective_base_in
        )
        gross_quote_out = min(gross_quote_out, pool_state["real_quote"])
        quote_after_trading_fees = fees.deduct_from(gross_quote_out)
        return self._deduct_transfer_fee(
            quote_after_trading_fees, pool_state["quote_transfer_fee"]
        )

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Get current virtual reserves in raw base and quote units."""
        pool_state = await self.get_pool_state(pool_address)
        return (pool_state["virtual_base"], pool_state["virtual_quote"])

    def _decode_pool_state_with_idl(self, data: bytes) -> dict[str, Any]:
        """Decode PoolState while preserving every field supplied by the IDL."""
        decoded = self._idl_parser.decode_account_data(
            data, "PoolState", skip_discriminator=True
        )
        if not decoded:
            raise ValueError("Failed to decode PoolState with LaunchLab IDL")

        required_integer_fields = (
            "status",
            "base_decimals",
            "quote_decimals",
            "supply",
            "virtual_base",
            "virtual_quote",
            "real_base",
            "real_quote",
            "token_program_flag",
        )
        for field in required_integer_fields:
            self._require_integer(decoded, field)

        pool_state = dict(decoded)
        for field in (
            "global_config",
            "platform_config",
            "base_mint",
            "quote_mint",
            "base_vault",
            "quote_vault",
            "creator",
        ):
            pool_state[field] = self._require_pubkey(decoded, field)
        return pool_state

    def _decode_config(self, data: bytes, account_type: str) -> dict[str, Any]:
        decoded = self._idl_parser.decode_account_data(
            data, account_type, skip_discriminator=True
        )
        if not decoded:
            raise ValueError(f"Failed to decode {account_type} with LaunchLab IDL")
        return decoded

    def _attach_authoritative_metadata(
        self,
        pool_state: dict[str, Any],
        *,
        decoded_global: dict[str, Any],
        decoded_platform: dict[str, Any],
        pool_address: Pubkey,
        base_transfer_fee: dict[str, int] | None,
        quote_transfer_fee: dict[str, int] | None,
    ) -> dict[str, Any]:
        curve_type_raw = self._require_integer(decoded_global, "curve_type")
        try:
            curve_type = LaunchLabCurveType(curve_type_raw)
        except ValueError as error:
            raise ValueError(
                f"Unknown LaunchLab curve type {curve_type_raw}"
            ) from error

        base_mint = self._require_pubkey(pool_state, "base_mint")
        pool_quote_mint = self._require_pubkey(pool_state, "quote_mint")
        expected_pool = self.address_provider.derive_pool_address(
            base_mint, pool_quote_mint
        )
        if pool_address != expected_pool:
            raise ValueError(
                "Pool address does not match its authoritative base/quote mints"
            )
        if self._require_pubkey(
            pool_state, "base_vault"
        ) != self.address_provider.derive_base_vault(base_mint, pool_quote_mint):
            raise ValueError("Pool base_vault does not match its authoritative mints")
        if self._require_pubkey(
            pool_state, "quote_vault"
        ) != self.address_provider.derive_quote_vault(base_mint, pool_quote_mint):
            raise ValueError("Pool quote_vault does not match its authoritative mints")

        config_quote_mint = self._require_pubkey(decoded_global, "quote_mint")
        if pool_quote_mint != config_quote_mint:
            raise ValueError(
                "Pool quote_mint does not match its authoritative GlobalConfig"
            )

        fees = LaunchLabFees(
            protocol_rate=self._require_integer(decoded_global, "trade_fee_rate"),
            platform_rate=self._require_integer(decoded_platform, "fee_rate"),
            creator_rate=self._require_integer(decoded_platform, "creator_fee_rate"),
        )
        token_program_flag = self._require_integer(pool_state, "token_program_flag")
        if token_program_flag & ~0b11:
            raise ValueError(
                f"Unsupported LaunchLab token_program_flag {token_program_flag}"
            )

        status_raw = self._require_integer(pool_state, "status")
        try:
            status = LaunchLabPoolStatus(status_raw)
        except ValueError as error:
            raise ValueError(f"Unknown LaunchLab pool status {status_raw}") from error

        result = dict(pool_state)
        result.update(
            {
                "pool_address": pool_address,
                "source": result.get("source", "rpc"),
                "curve_type": curve_type,
                "curve_type_name": curve_type.name.lower(),
                "curve_supported": curve_type == self._SUPPORTED_CURVE,
                "status": status,
                "status_name": status.name.lower(),
                "fees": fees,
                "trade_fee_rate": fees.protocol_rate,
                "platform_fee_rate": fees.platform_rate,
                "creator_fee_rate": fees.creator_rate,
                "total_fee_rate": fees.total_rate,
                "fee_rate_denominator": fees.DENOMINATOR,
                "base_token_program": self._token_program_from_flag(
                    token_program_flag & 0b01
                ),
                "quote_token_program": self._token_program_from_flag(
                    (token_program_flag >> 1) & 0b01
                ),
                "base_transfer_fee": base_transfer_fee,
                "quote_transfer_fee": quote_transfer_fee,
            }
        )

        is_tradeable = (
            curve_type == self._SUPPORTED_CURVE
            and status == LaunchLabPoolStatus.FUNDING
        )
        result["is_tradeable"] = is_tradeable
        result["price_per_token"] = None
        if is_tradeable:
            self._validate_reserves(result)
            base_decimals = self._require_integer(result, "base_decimals")
            quote_decimals = self._require_integer(result, "quote_decimals")
            if not 0 <= base_decimals <= 18 or not 0 <= quote_decimals <= 18:
                raise ValueError("LaunchLab pool contains unsupported token decimals")
            result["price_per_token"] = (
                (result["virtual_quote"] / result["virtual_base"])
                * (10**base_decimals)
                / (10**quote_decimals)
            )
        return result

    def _validated_token_2022_mint_data(
        self, account: Any, *, address: Pubkey
    ) -> bytes:
        if account is None:
            raise ValueError(f"Token-2022 mint account {address} does not exist")
        owner = account.get("owner") if isinstance(account, dict) else account.owner
        if owner != SystemAddresses.TOKEN_2022_PROGRAM:
            raise ValueError(
                f"Token-2022 mint account {address} has invalid owner {owner}"
            )
        raw_data = account.get("data") if isinstance(account, dict) else account.data
        if not isinstance(raw_data, bytes):
            raise ValueError(f"Token-2022 mint account {address} has invalid data")
        if len(raw_data) < self._MINT_BASE_LENGTH:
            raise ValueError(
                f"Token-2022 mint account {address} is shorter than a mint"
            )
        return raw_data

    def _decode_clock_epoch(self, account: Any) -> int:
        if account is None:
            raise ValueError("Clock sysvar account does not exist")
        owner = account.get("owner") if isinstance(account, dict) else account.owner
        if owner != self._SYSVAR_OWNER:
            raise ValueError(f"Clock sysvar has invalid owner {owner}")
        raw_data = account.get("data") if isinstance(account, dict) else account.data
        if not isinstance(raw_data, bytes) or len(raw_data) != 40:
            raise ValueError("Clock sysvar has invalid data")
        return struct.unpack_from("<Q", raw_data, 16)[0]

    @classmethod
    def _decode_transfer_fee_schedule(
        cls,
        mint_data: bytes,
        *,
        current_epoch: int,
        expected_decimals: int,
    ) -> dict[str, int] | None:
        """Decode the current Token-2022 TransferFeeConfig without approximation."""
        if (
            isinstance(current_epoch, bool)
            or not isinstance(current_epoch, int)
            or current_epoch < 0
        ):
            raise ValueError("Current epoch must be a non-negative integer")
        if (
            isinstance(expected_decimals, bool)
            or not isinstance(expected_decimals, int)
            or not 0 <= expected_decimals <= 255
        ):
            raise ValueError("Expected mint decimals are invalid")
        if len(mint_data) < cls._MINT_BASE_LENGTH:
            raise ValueError("Token-2022 mint data is truncated")
        if mint_data[44] != expected_decimals:
            raise ValueError("Token-2022 mint decimals do not match the pool state")
        if mint_data[45] != 1:
            raise ValueError("Token-2022 mint is not initialized")
        if len(mint_data) == cls._MINT_BASE_LENGTH:
            return None
        if mint_data[cls._MINT_BASE_LENGTH] != cls._MINT_ACCOUNT_TYPE:
            raise ValueError("Token-2022 account data is not a mint")

        offset = cls._MINT_BASE_LENGTH + 1
        transfer_fee_data: bytes | None = None
        while offset < len(mint_data):
            remaining = mint_data[offset:]
            if not any(remaining):
                break
            if len(remaining) < 4:
                raise ValueError("Token-2022 mint extension header is truncated")
            extension_type, extension_length = struct.unpack_from(
                "<HH", mint_data, offset
            )
            offset += 4
            extension_end = offset + extension_length
            if extension_type == 0 or extension_end > len(mint_data):
                raise ValueError("Token-2022 mint extension data is malformed")
            extension_data = mint_data[offset:extension_end]
            offset = extension_end
            if extension_type != cls._TRANSFER_FEE_CONFIG_EXTENSION:
                continue
            if transfer_fee_data is not None:
                raise ValueError(
                    "Token-2022 mint has duplicate TransferFeeConfig extensions"
                )
            if extension_length != cls._TRANSFER_FEE_CONFIG_LENGTH:
                raise ValueError("Token-2022 TransferFeeConfig has an invalid length")
            transfer_fee_data = extension_data

        if transfer_fee_data is None:
            return None
        older_epoch, older_maximum, older_bps = struct.unpack_from(
            "<QQH", transfer_fee_data, 72
        )
        newer_epoch, newer_maximum, newer_bps = struct.unpack_from(
            "<QQH", transfer_fee_data, 90
        )
        if current_epoch >= newer_epoch:
            epoch, maximum_fee_raw, basis_points = (
                newer_epoch,
                newer_maximum,
                newer_bps,
            )
        else:
            epoch, maximum_fee_raw, basis_points = (
                older_epoch,
                older_maximum,
                older_bps,
            )
        try:
            calculate_transfer_fee_raw(0, basis_points, maximum_fee_raw)
        except QuoteError as error:
            raise ValueError("Token-2022 transfer-fee schedule is invalid") from error
        return {
            "epoch": epoch,
            "basis_points": basis_points,
            "maximum_fee_raw": maximum_fee_raw,
        }

    @staticmethod
    def _deduct_transfer_fee(amount: int, schedule: dict[str, int] | None) -> int:
        if schedule is None or amount == 0:
            return amount
        try:
            fee = calculate_transfer_fee_raw(
                amount,
                schedule["basis_points"],
                schedule["maximum_fee_raw"],
            )
        except (KeyError, QuoteError, TypeError) as error:
            raise ValueError("LaunchLab transfer-fee metadata is invalid") from error
        return amount - fee

    def _validated_account_data(
        self,
        account: Any,
        *,
        address: Pubkey,
        account_type: str,
        expected_length: int | None = None,
    ) -> bytes:
        if account is None:
            raise ValueError(f"{account_type} account {address} does not exist")
        owner = account.get("owner") if isinstance(account, dict) else account.owner
        if owner != self.address_provider.program_id:
            raise ValueError(
                f"{account_type} account {address} has invalid owner {owner}"
            )
        raw_data = account.get("data") if isinstance(account, dict) else account.data
        if not isinstance(raw_data, bytes):
            raise ValueError(f"{account_type} account {address} has invalid data")
        if expected_length is not None and len(raw_data) != expected_length:
            raise ValueError(
                f"{account_type} account {address} has invalid length "
                f"{len(raw_data)} (expected {expected_length})"
            )
        discriminator = self._account_discriminator(account_type)
        if len(raw_data) < len(discriminator) or raw_data[:8] != discriminator:
            raise ValueError(
                f"{account_type} account {address} has invalid discriminator"
            )
        return raw_data

    def _account_discriminator(self, account_type: str) -> bytes:
        for account_definition in self._idl_parser.idl.get("accounts", []):
            if account_definition.get("name") == account_type:
                return bytes(account_definition["discriminator"])
        raise ValueError(
            f"LaunchLab IDL does not define the {account_type} discriminator"
        )

    @staticmethod
    def _token_program_from_flag(flag: int) -> Pubkey:
        if flag == 0:
            return SystemAddresses.TOKEN_PROGRAM
        if flag == 1:
            return SystemAddresses.TOKEN_2022_PROGRAM
        raise ValueError(f"Unsupported LaunchLab token program flag {flag}")

    @staticmethod
    def _require_integer(values: dict[str, Any], field: str) -> int:
        if field not in values:
            raise ValueError(f"LaunchLab metadata is missing {field}")
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"LaunchLab metadata field {field} is not an integer")
        return value

    @staticmethod
    def _require_pubkey(values: dict[str, Any], field: str) -> Pubkey:
        if field not in values or values[field] is None:
            raise ValueError(f"LaunchLab metadata is missing {field}")
        value = values[field]
        if isinstance(value, Pubkey):
            return value
        if isinstance(value, str):
            try:
                return Pubkey.from_string(value)
            except ValueError as error:
                raise ValueError(
                    f"LaunchLab metadata field {field} is not a valid pubkey"
                ) from error
        raise ValueError(f"LaunchLab metadata field {field} is not a pubkey")

    @staticmethod
    def _require_positive_raw_amount(amount: int, field: str) -> None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError(f"{field} must be a positive raw integer amount")
        if amount > 2**64 - 1:
            raise ValueError(f"{field} exceeds the u64 range")

    @staticmethod
    def _validate_reserves(pool_state: dict[str, Any]) -> None:
        for field in ("virtual_base", "virtual_quote", "real_base", "real_quote"):
            value = pool_state.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 2**64 - 1
            ):
                raise ValueError(
                    f"LaunchLab {field} must be a non-negative u64 integer"
                )
        if pool_state["virtual_base"] == 0:
            raise ValueError("LaunchLab virtual_base must be positive")
        if pool_state["virtual_quote"] == 0:
            raise ValueError("LaunchLab virtual_quote must be positive")

    def _require_executable_state(self, pool_state: dict[str, Any]) -> LaunchLabFees:
        curve_type = pool_state.get("curve_type")
        if curve_type != self._SUPPORTED_CURVE:
            label = (
                curve_type.name
                if isinstance(curve_type, LaunchLabCurveType)
                else curve_type
            )
            raise ValueError(
                f"Unsupported LaunchLab curve type {label}; "
                "refusing an approximate executable quote"
            )
        status = pool_state.get("status")
        if status != LaunchLabPoolStatus.FUNDING:
            raise ValueError(f"LaunchLab pool status {status} is not executable")
        self._validate_reserves(pool_state)
        fees = pool_state.get("fees")
        for side in ("base", "quote"):
            token_program = pool_state.get(f"{side}_token_program")
            if token_program not in {
                SystemAddresses.TOKEN_PROGRAM,
                SystemAddresses.TOKEN_2022_PROGRAM,
            }:
                raise ValueError(
                    f"LaunchLab {side} token program is missing or invalid"
                )
            metadata_field = f"{side}_transfer_fee"
            if metadata_field not in pool_state:
                raise ValueError(f"LaunchLab {side} transfer-fee metadata is missing")
            schedule = pool_state[metadata_field]
            if token_program == SystemAddresses.TOKEN_PROGRAM:
                if schedule is not None:
                    raise ValueError(
                        f"Legacy LaunchLab {side} mint has transfer-fee metadata"
                    )
                continue
            if schedule is not None:
                if not isinstance(schedule, dict):
                    raise ValueError(
                        f"LaunchLab {side} transfer-fee metadata is invalid"
                    )
                self._deduct_transfer_fee(1, schedule)
                epoch = schedule.get("epoch")
                if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                    raise ValueError(f"LaunchLab {side} transfer-fee epoch is invalid")
        if not isinstance(fees, LaunchLabFees):
            raise ValueError("LaunchLab fee metadata is missing or invalid")
        return fees

    async def validate_pool_state_structure(self, pool_address: Pubkey) -> bool:
        """Validate that a pool and its linked configs are executable."""
        try:
            pool_state = await self.get_pool_state(pool_address)
            self._require_executable_state(pool_state)
            return True
        except Exception:
            logger.exception("LaunchLab pool state validation failed")
            return False

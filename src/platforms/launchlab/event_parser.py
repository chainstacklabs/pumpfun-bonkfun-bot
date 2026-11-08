"""Raydium LaunchLab launch parsing, optionally narrowed to some platform configs."""

import base64
from collections.abc import Iterable
from time import monotonic
from typing import Any, ClassVar

import base58
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from interfaces.core import EventParser, Platform, PoolReady, TokenInfo
from platforms.launchlab.address_provider import LAUNCHLAB_PROGRAM
from platforms.launchlab.cpmm_market import cpmm_pool_address
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

_INITIALIZE_VARIANTS = ("initialize", "initialize_v2", "initialize_with_token_2022")

# The migration to Raydium CPMM. `migrate_to_amm` (AMM v4) is not reported: the
# bot has no market there.
_MIGRATE_TO_CPSWAP = "migrate_to_cpswap"
_MIGRATE_ACCOUNTS = (
    "base_mint",
    "quote_mint",
    "platform_config",
    "cpswap_pool",
    "cpswap_config",
)
# Anchor logs it at any depth whenever the instruction runs. It is the first
# line the program writes, so log truncation cannot drop it.
MIGRATE_TO_CPSWAP_LOG = "Program log: Instruction: MigrateToCpswap"

# (program index, account indexes, data) of one compiled instruction.
_Instruction = tuple[int, list[int], bytes]


class LaunchLabEventParser(EventParser):
    """Turns a LaunchLab pool initialization into a TokenInfo.

    LaunchLab serves many launchpads from one program, and a launch names its
    launchpad's platform config. This class takes a launch under any config.
    A preset subclass sets `PLATFORM_CONFIGS` to its launchpad's, and
    `platform_configs` narrows either; a launch under another config is ignored.

    Everything comes from the instruction's accounts and arguments: the pool's
    creator is the `creator` account (not the payer, which may differ), the
    quote mint and both token programs are accounts, and the transfer fee is the
    argument the mint is initialized with. None of it is user-supplied in a way
    the pool could disagree with, so the TokenInfo is marked `state_from_event`
    and extreme_fast_mode can buy without reading the pool first.

    Logs alone cannot identify a launch: the program's PoolCreateEvent carries
    neither the base mint, the quote mint nor the platform config.

    Only constant-product curves are taken: the price and trade math assume
    one, and a fixed-price or linear launch is ignored.
    """

    # None takes every platform config.
    PLATFORM_CONFIGS: ClassVar[frozenset[Pubkey] | None] = None

    def __init__(
        self,
        idl_parser: IDLParser,
        platform_configs: Iterable[Pubkey] | None = None,
    ) -> None:
        """Build a parser, narrowed to `platform_configs` when given.

        Raises:
            ValueError: If `platform_configs` is empty, or names a config a
                preset does not cover
        """
        self._idl_parser = idl_parser
        discriminators = idl_parser.get_instruction_discriminators()
        self._discriminators = [discriminators[name] for name in _INITIALIZE_VARIANTS]
        self._migrate_discriminator = discriminators[_MIGRATE_TO_CPSWAP]
        migrate = next(
            ix
            for ix in idl_parser.idl["instructions"]
            if ix["name"] == _MIGRATE_TO_CPSWAP
        )
        positions = [account["name"] for account in migrate["accounts"]]
        self._migrate_positions = tuple(
            positions.index(name) for name in _MIGRATE_ACCOUNTS
        )
        self.platform_configs = self.PLATFORM_CONFIGS
        if platform_configs is not None:
            narrowed = frozenset(platform_configs)
            if not narrowed:
                raise ValueError("platform_configs must name at least one config")  # noqa: TRY003
            outside = narrowed - (self.PLATFORM_CONFIGS or narrowed)
            if outside:
                raise ValueError(  # noqa: TRY003
                    f"{self.platform.value} does not cover platform configs "
                    f"{sorted(map(str, outside))}"
                )
            self.platform_configs = narrowed

    @property
    def platform(self) -> Platform:
        """Every launchpad on LaunchLab."""
        return Platform.LAUNCHLAB

    def get_program_id(self) -> Pubkey:
        """Get the LaunchLab program ID."""
        return LAUNCHLAB_PROGRAM

    def get_stream_filter_accounts(self) -> list[Pubkey]:
        """Subscribe on the platform configs, or the program when taking all.

        Every pool and every trade on it names its platform config, so a
        narrowed parser keeps other launchpads on LaunchLab out of the stream.
        """
        if self.platform_configs is None:
            return [LAUNCHLAB_PROGRAM]
        return sorted(self.platform_configs, key=str)

    def get_creation_filter_accounts(self) -> list[Pubkey]:
        """Same as the stream filter: a launch always names its platform config."""
        return self.get_stream_filter_accounts()

    def get_instruction_discriminators(self) -> list[bytes]:
        """Discriminators of the three pool-initialization instructions."""
        return self._discriminators

    def parse_token_creation_from_logs(
        self,
        logs: list[str],  # noqa: ARG002
        signature: str,  # noqa: ARG002
    ) -> TokenInfo | None:
        """Not supported: LaunchLab's logs do not name the coin or its launchpad."""
        return None

    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        """Decode one initialize instruction into a TokenInfo.

        Returns:
            None for anything but a constant-product pool initialization under
            one of this parser's platform configs
        """
        if not any(instruction_data.startswith(d) for d in self._discriminators):
            return None
        decoded = self._idl_parser.decode_instruction(
            instruction_data, account_keys, accounts
        )
        if not decoded or decoded["instruction_name"] not in _INITIALIZE_VARIANTS:
            return None

        named = decoded["accounts"]
        required = (
            "creator",
            "global_config",
            "platform_config",
            "pool_state",
            "base_mint",
            "quote_mint",
            "base_vault",
            "quote_vault",
            "base_token_program",
            "quote_token_program",
        )
        if any(named.get(name) is None for name in required):
            return None
        keys = {name: Pubkey.from_string(named[name]) for name in required}
        if (
            self.platform_configs is not None
            and keys["platform_config"] not in self.platform_configs
        ):
            return None

        args = decoded["args"]
        # `initialize` names its arguments with a leading underscore.
        curve = args.get("curve_param") or args.get("_curve_param") or {}
        if curve.get("variant") != "Constant":
            logger.debug(
                f"Skipping LaunchLab launch {keys['base_mint']}: "
                f"{curve.get('variant')} curve is not supported"
            )
            return None
        mint_params = args.get("base_mint_param") or {}
        transfer_fee = args.get("transfer_fee_extension_param") or {}
        payer = named.get("payer")
        return TokenInfo(
            name=mint_params.get("name", ""),
            symbol=mint_params.get("symbol", ""),
            uri=mint_params.get("uri", ""),
            mint=keys["base_mint"],
            platform=self.platform,
            pool_state=keys["pool_state"],
            base_vault=keys["base_vault"],
            quote_vault=keys["quote_vault"],
            global_config=keys["global_config"],
            platform_config=keys["platform_config"],
            user=Pubkey.from_string(payer) if payer else keys["creator"],
            creator=keys["creator"],
            token_program_id=keys["base_token_program"],
            quote_mint=keys["quote_mint"],
            quote_token_program_id=keys["quote_token_program"],
            transfer_fee_bps=transfer_fee.get("transfer_fee_basis_points", 0),
            state_from_event=True,
            creation_timestamp=monotonic(),
        )

    def _parse_instructions(
        self, instructions: list[_Instruction], keys: list[bytes]
    ) -> TokenInfo | None:
        """Try each (program index, account indexes, data) against the program."""
        program = bytes(LAUNCHLAB_PROGRAM)
        for program_index, accounts, data in instructions:
            if program_index >= len(keys) or bytes(keys[program_index]) != program:
                continue
            token_info = self.parse_token_creation_from_instruction(
                data, accounts, keys
            )
            if token_info:
                return token_info
        return None

    def parse_token_creation_from_geyser(
        self, transaction_info: Any
    ) -> TokenInfo | None:
        """Parse a geyser transaction update, top-level and inner instructions."""
        try:
            instructions, keys = _geyser_instructions(
                transaction_info.transaction.transaction
            )
            return self._parse_instructions(instructions, keys)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Failed to parse geyser transaction: {e}")
            return None

    def parse_token_creation_from_block(self, block_data: dict) -> TokenInfo | None:
        """Parse blockSubscribe transactions, base64 or JSON encoded.

        Inner instructions come from `meta.innerInstructions` with base58 data;
        lookup-table accounts from `meta.loadedAddresses`.
        """
        for tx in block_data.get("transactions", []):
            try:
                token_info = self._parse_block_transaction(tx)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Failed to parse block transaction: {e}")
                continue
            if token_info:
                return token_info
        return None

    def _parse_block_transaction(self, tx: dict) -> TokenInfo | None:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        found = _block_instructions(tx)
        return None if found is None else self._parse_instructions(*found)

    # Migration to Raydium CPMM. Logs do not name the coin, so every listener
    # decodes the migrate_to_cpswap instruction, and a migration reached by
    # CPI counts on the executed listeners as a launch does.

    def parse_pools_ready_from_block_transaction(
        self, transaction: dict[str, Any]
    ) -> list[PoolReady]:
        """The CPMM pools a successful block transaction's migrations created."""
        meta = transaction.get("meta")
        if not isinstance(meta, dict) or meta.get("err") is not None:
            return []
        logs = meta.get("logMessages") or meta.get("log_messages") or []
        if MIGRATE_TO_CPSWAP_LOG not in logs:
            return []
        try:
            found = _block_instructions(transaction)
            return [] if found is None else self._pools_from_instructions(*found)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Failed to read a migration from a block transaction: {e}")
            return []

    def parse_pools_ready_from_geyser(
        self, transaction_info: object
    ) -> list[PoolReady]:
        """The CPMM pools a successful geyser update's migrations created."""
        tx = transaction_info.transaction.transaction
        if tx.meta.HasField("err") or MIGRATE_TO_CPSWAP_LOG not in tx.meta.log_messages:
            return []
        try:
            return self._pools_from_instructions(*_geyser_instructions(tx))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Failed to read a migration from a geyser update: {e}")
            return []

    def parse_pool_ready_from_instruction(
        self,
        instruction_data: bytes,
        accounts: list[int],
        account_keys: list[bytes],
        signature: str,
    ) -> PoolReady | None:
        """The CPMM pool a top-level migrate_to_cpswap moves the curve into.

        Read before execution: Raydium's migrate wallet retries a migration
        that already landed, and the retry fails.
        """
        return self._pool_from_migrate(
            bytes(instruction_data),
            list(accounts),
            account_keys,
            signature,
            pre_execution=True,
        )

    def get_migration_instruction_discriminators(self) -> list[bytes]:
        """`migrate_to_cpswap`."""
        return [self._migrate_discriminator]

    def get_migration_filter_accounts(self) -> list[Pubkey]:
        """None: a migration names its platform config and the program, which
        the creation subscription already covers."""
        return []

    def _pools_from_instructions(
        self, instructions: list[_Instruction], keys: list[bytes]
    ) -> list[PoolReady]:
        program = bytes(LAUNCHLAB_PROGRAM)
        pools = []
        for program_index, accounts, data in instructions:
            if program_index >= len(keys) or bytes(keys[program_index]) != program:
                continue
            pool = self._pool_from_migrate(data, accounts, keys, "")
            if pool is not None:
                pools.append(pool)
        return pools

    def _pool_from_migrate(
        self,
        data: bytes,
        accounts: list[int],
        keys: list[bytes],
        signature: str,
        *,
        pre_execution: bool = False,
    ) -> PoolReady | None:
        """A PoolReady from one migrate_to_cpswap, if it is this parser's.

        The platform config must be one of this parser's, and the CPMM pool
        the one CPMM derives from the named config and mints, as it is in
        every real migration.
        """
        if not data.startswith(self._migrate_discriminator):
            return None
        named = []
        for position in self._migrate_positions:
            if position >= len(accounts) or accounts[position] >= len(keys):
                return None
            named.append(Pubkey.from_bytes(bytes(keys[accounts[position]])))
        mint, quote_mint, platform_config, pool, cpswap_config = named
        if (
            self.platform_configs is not None
            and platform_config not in self.platform_configs
        ):
            return None
        if pool != cpmm_pool_address(cpswap_config, mint, quote_mint):
            return None
        return PoolReady(
            platform=self.platform,
            mint=mint,
            pool=pool,
            quote_mint=quote_mint,
            signature=signature,
            pre_execution=pre_execution,
            detection_timestamp=monotonic(),
        )


def _geyser_instructions(tx: object) -> tuple[list[_Instruction], list[bytes]]:
    """A geyser transaction's top-level and inner instructions, and its keys.

    Lookup-table accounts are appended after the static keys, writable then
    read-only, which is the order instruction indexes refer to.
    """
    message = tx.transaction.message
    meta = tx.meta
    keys = [
        *message.account_keys,
        *meta.loaded_writable_addresses,
        *meta.loaded_readonly_addresses,
    ]
    instructions = [
        (ix.program_id_index, list(ix.accounts), bytes(ix.data))
        for ix in message.instructions
    ]
    for group in meta.inner_instructions:
        instructions += [
            (ix.program_id_index, list(ix.accounts), bytes(ix.data))
            for ix in group.instructions
        ]
    return instructions, keys


def _block_instructions(
    tx: dict,
) -> tuple[list[_Instruction], list[bytes]] | None:
    """A blockSubscribe transaction's instructions and keys, base64 or JSON.

    Inner instructions come from `meta.innerInstructions` with base58 data;
    lookup-table accounts from `meta.loadedAddresses`. None for an envelope
    in neither encoding.
    """
    meta = tx.get("meta") or {}
    raw = tx.get("transaction")
    if isinstance(raw, list):
        message = VersionedTransaction.from_bytes(base64.b64decode(raw[0])).message
        static_keys = [bytes(key) for key in message.account_keys]
        instructions = [
            (ix.program_id_index, list(ix.accounts), bytes(ix.data))
            for ix in message.instructions
        ]
    elif isinstance(raw, dict) and "message" in raw:
        message = raw["message"]
        static_keys = [bytes(Pubkey.from_string(k)) for k in message["accountKeys"]]
        instructions = [
            (ix["programIdIndex"], ix["accounts"], base58.b58decode(ix["data"]))
            for ix in message["instructions"]
        ]
    else:
        return None

    loaded = meta.get("loadedAddresses") or {}
    keys = static_keys + [
        bytes(Pubkey.from_string(k))
        for k in [*loaded.get("writable", []), *loaded.get("readonly", [])]
    ]
    for group in meta.get("innerInstructions") or []:
        instructions += [
            (ix["programIdIndex"], ix["accounts"], base58.b58decode(ix["data"]))
            for ix in group["instructions"]
        ]
    return instructions, keys

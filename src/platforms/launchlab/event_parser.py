"""Raydium LaunchLab launch parsing, filtered to one launchpad's platform configs."""

import base64
from time import monotonic
from typing import Any, ClassVar

import base58
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from interfaces.core import EventParser, TokenInfo
from platforms.launchlab.address_provider import LAUNCHLAB_PROGRAM
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

_INITIALIZE_VARIANTS = ("initialize", "initialize_v2", "initialize_with_token_2022")


class LaunchLabEventParser(EventParser):
    """Turns a LaunchLab pool initialization into a TokenInfo.

    LaunchLab serves many launchpads from one program; a launch belongs to the
    platform whose config it names. Subclasses set `PLATFORM_CONFIGS`, and any
    launch under another config is ignored.

    Everything comes from the instruction's accounts and arguments: the pool's
    creator is the `creator` account (not the payer, which may differ), the
    quote mint and both token programs are accounts, and the transfer fee is the
    argument the mint is initialized with. None of it is user-supplied in a way
    the pool could disagree with, so the TokenInfo is marked `state_from_event`
    and extreme_fast_mode can buy without reading the pool first.

    Logs alone cannot identify a launch: the program's PoolCreateEvent carries
    neither the base mint, the quote mint nor the platform config.
    """

    PLATFORM_CONFIGS: ClassVar[frozenset[Pubkey]] = frozenset()

    def __init__(self, idl_parser: IDLParser) -> None:
        self._idl_parser = idl_parser
        discriminators = idl_parser.get_instruction_discriminators()
        self._discriminators = [discriminators[name] for name in _INITIALIZE_VARIANTS]

    def get_program_id(self) -> Pubkey:
        """Get the LaunchLab program ID."""
        return LAUNCHLAB_PROGRAM

    def get_stream_filter_accounts(self) -> list[Pubkey]:
        """Subscribe on this launchpad's platform configs, not the whole program.

        Every pool and every trade on it names its platform config, so this
        keeps other launchpads on LaunchLab out of the stream.
        """
        return sorted(self.PLATFORM_CONFIGS, key=str)

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
            None for anything but a pool initialization under one of this
            platform's configs
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
        if keys["platform_config"] not in self.PLATFORM_CONFIGS:
            return None

        args = decoded["args"]
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
        self, instructions: list[tuple[int, list[int], bytes]], keys: list[bytes]
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
        """Parse a geyser transaction update, top-level and inner instructions.

        Lookup-table accounts are appended after the static keys, writable then
        read-only, which is the order instruction indexes refer to.
        """
        try:
            tx = transaction_info.transaction.transaction
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
        return self._parse_instructions(instructions, keys)

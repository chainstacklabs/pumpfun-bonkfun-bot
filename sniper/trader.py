"""
Production-grade Pump.fun trader with Jito bundles for sandwich resistance.

Features:
- Jito bundle submission for MEV protection
- Fallback to direct RPC submission
- Automatic tip account rotation
- IDL-based instruction building
- Telegram notifications
"""

import asyncio
import base64
import json
import random
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

from geyser import TokenInfo


# =============================================================================
# CONSTANTS
# =============================================================================

# Pump.fun program addresses
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_FUN_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FUN_MAYHEM_FEE = Pubkey.from_string("GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS")
PUMP_FUN_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# System addresses
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

# Jito tip accounts (official addresses)
JITO_TIP_ACCOUNTS = [
    Pubkey.from_string("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"),
    Pubkey.from_string("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
    Pubkey.from_string("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"),
    Pubkey.from_string("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"),
    Pubkey.from_string("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"),
    Pubkey.from_string("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
    Pubkey.from_string("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"),
    Pubkey.from_string("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
]

TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class TradeResult:
    """Result of a trade operation."""
    success: bool
    signature: str | None
    tokens_amount: int | None
    sol_amount: int | None
    error: str | None = None
    price_per_token: float | None = None


@dataclass
class Position:
    """Represents an open position."""
    token_info: TokenInfo
    entry_price: float
    tokens_held: int
    sol_spent: int
    entry_time: float


class TelegramNotifier:
    """Send Telegram notifications."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    async def send(self, message: str):
        """Send a message to Telegram."""
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        print(f"[TELEGRAM] Failed to send: {await resp.text()}")
        except Exception as e:
            print(f"[TELEGRAM] Error: {e}")

    async def notify_buy(self, token: TokenInfo, result: TradeResult):
        """Send buy notification."""
        if not result.success:
            return

        sol_spent = (result.sol_amount or 0) / LAMPORTS_PER_SOL
        tokens = (result.tokens_amount or 0) / (10 ** TOKEN_DECIMALS)

        msg = (
            f"<b>BUY</b> {token.symbol}\n"
            f"<code>{token.mint}</code>\n\n"
            f"Spent: {sol_spent:.6f} SOL\n"
            f"Tokens: {tokens:,.2f}\n"
            f"Price: {result.price_per_token:.10f} SOL\n\n"
            f"<a href='https://pump.fun/{token.mint}'>pump.fun</a> | "
            f"<a href='https://solscan.io/tx/{result.signature}'>tx</a>"
        )
        await self.send(msg)

    async def notify_sell(self, token: TokenInfo, result: TradeResult, pnl_percent: float):
        """Send sell notification."""
        if not result.success:
            return

        sol_received = (result.sol_amount or 0) / LAMPORTS_PER_SOL
        tokens = (result.tokens_amount or 0) / (10 ** TOKEN_DECIMALS)
        emoji = "" if pnl_percent >= 0 else ""

        msg = (
            f"<b>SELL</b> {token.symbol} {emoji}\n"
            f"<code>{token.mint}</code>\n\n"
            f"Received: {sol_received:.6f} SOL\n"
            f"Tokens: {tokens:,.2f}\n"
            f"PnL: {pnl_percent:+.2f}%\n\n"
            f"<a href='https://solscan.io/tx/{result.signature}'>tx</a>"
        )
        await self.send(msg)


class PumpFunTrader:
    """Production-grade Pump.fun trader with Jito bundle support."""

    def __init__(
        self,
        rpc_url: str,
        keypair: Keypair,
        jito_url: str | None = None,
        jito_tip_lamports: int = 50000,
        jito_tip_account: Pubkey | None = None,
        buy_slippage: float = 0.3,
        sell_slippage: float = 0.2,
        compute_units_buy: int = 120000,
        compute_units_sell: int = 80000,
        priority_fee: int = 500000,
        account_data_size_limit: int = 512000,
        telegram: TelegramNotifier | None = None,
    ):
        self.rpc_url = rpc_url
        self.keypair = keypair
        self.jito_url = jito_url
        self.jito_tip_lamports = jito_tip_lamports
        self.jito_tip_account = jito_tip_account
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.compute_units_buy = compute_units_buy
        self.compute_units_sell = compute_units_sell
        self.priority_fee = priority_fee
        self.account_data_size_limit = account_data_size_limit
        self.telegram = telegram

        self._client: AsyncClient | None = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_task: asyncio.Task | None = None

        # Load IDL for instruction building
        idl_path = Path(__file__).parent.parent / "idl" / "pump_fun_idl.json"
        with open(idl_path) as f:
            self.idl = json.load(f)

        self._discriminators = {}
        for ix in self.idl.get("instructions", []):
            self._discriminators[ix["name"]] = bytes(ix["discriminator"])

    async def start(self):
        """Initialize the trader."""
        self._client = AsyncClient(self.rpc_url)
        self._blockhash_task = asyncio.create_task(self._update_blockhash_loop())
        print(f"[TRADER] Initialized with wallet: {self.keypair.pubkey()}")

    async def stop(self):
        """Clean up resources."""
        if self._blockhash_task:
            self._blockhash_task.cancel()
            try:
                await self._blockhash_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()

    async def _update_blockhash_loop(self):
        """Background task to keep blockhash fresh."""
        while True:
            try:
                resp = await self._client.get_latest_blockhash(commitment="processed")
                self._cached_blockhash = resp.value.blockhash
            except Exception as e:
                print(f"[TRADER] Blockhash update error: {e}")
            await asyncio.sleep(5)

    async def get_blockhash(self) -> Hash:
        """Get cached or fresh blockhash."""
        if self._cached_blockhash:
            return self._cached_blockhash
        resp = await self._client.get_latest_blockhash(commitment="processed")
        return resp.value.blockhash

    def _get_jito_tip_account(self) -> Pubkey:
        """Get Jito tip account (configured or random)."""
        if self.jito_tip_account:
            return self.jito_tip_account
        return random.choice(JITO_TIP_ACCOUNTS)

    def _derive_addresses(self, token: TokenInfo, user: Pubkey) -> dict[str, Pubkey]:
        """Derive all required addresses for trading."""
        token_program = token.token_program_id

        # User's token account
        user_token_account = get_associated_token_address(user, token.mint, token_program)

        # Global volume accumulator
        global_volume_acc, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"],
            PUMP_FUN_PROGRAM,
        )

        # User volume accumulator
        user_volume_acc, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)],
            PUMP_FUN_PROGRAM,
        )

        # Fee config
        fee_config, _ = Pubkey.find_program_address(
            [b"fee_config", bytes(PUMP_FUN_PROGRAM)],
            PUMP_FUN_FEE_PROGRAM,
        )

        return {
            "user_token_account": user_token_account,
            "global_volume_accumulator": global_volume_acc,
            "user_volume_accumulator": user_volume_acc,
            "fee_config": fee_config,
        }

    async def get_pool_state(self, bonding_curve: Pubkey) -> dict[str, Any] | None:
        """Fetch bonding curve state."""
        try:
            resp = await self._client.get_account_info(bonding_curve, encoding="base64")
            if not resp.value:
                return None

            data = base64.b64decode(resp.value.data[0])

            # Parse bonding curve data (skip 8-byte discriminator)
            offset = 8
            virtual_token_reserves = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            virtual_sol_reserves = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            real_token_reserves = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            real_sol_reserves = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            token_total_supply = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            complete = data[offset] == 1

            price_per_token = virtual_sol_reserves / virtual_token_reserves if virtual_token_reserves > 0 else 0

            return {
                "virtual_token_reserves": virtual_token_reserves,
                "virtual_sol_reserves": virtual_sol_reserves,
                "real_token_reserves": real_token_reserves,
                "real_sol_reserves": real_sol_reserves,
                "token_total_supply": token_total_supply,
                "complete": complete,
                "price_per_token": price_per_token,
            }

        except Exception as e:
            print(f"[TRADER] Error fetching pool state: {e}")
            return None

    def _build_buy_instruction(
        self,
        token: TokenInfo,
        addresses: dict[str, Pubkey],
        token_amount: int,
        max_sol_cost: int,
    ) -> Instruction:
        """Build Pump.fun buy instruction."""
        fee_recipient = PUMP_FUN_MAYHEM_FEE if token.is_mayhem_mode else PUMP_FUN_FEE

        accounts = [
            AccountMeta(PUMP_FUN_GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(fee_recipient, is_signer=False, is_writable=True),
            AccountMeta(token.mint, is_signer=False, is_writable=False),
            AccountMeta(token.bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(token.associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(addresses["user_token_account"], is_signer=False, is_writable=True),
            AccountMeta(self.keypair.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(token.token_program_id, is_signer=False, is_writable=False),
            AccountMeta(token.creator_vault, is_signer=False, is_writable=True),
            AccountMeta(PUMP_FUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(addresses["global_volume_accumulator"], is_signer=False, is_writable=False),
            AccountMeta(addresses["user_volume_accumulator"], is_signer=False, is_writable=True),
            AccountMeta(addresses["fee_config"], is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        # Build instruction data: discriminator + token_amount + max_sol_cost + track_volume
        track_volume = bytes([1, 1])  # Some(true)
        data = (
            self._discriminators["buy"]
            + struct.pack("<Q", token_amount)
            + struct.pack("<Q", max_sol_cost)
            + track_volume
        )

        return Instruction(PUMP_FUN_PROGRAM, data, accounts)

    def _build_sell_instruction(
        self,
        token: TokenInfo,
        addresses: dict[str, Pubkey],
        token_amount: int,
        min_sol_output: int,
    ) -> Instruction:
        """Build Pump.fun sell instruction."""
        fee_recipient = PUMP_FUN_MAYHEM_FEE if token.is_mayhem_mode else PUMP_FUN_FEE

        accounts = [
            AccountMeta(PUMP_FUN_GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(fee_recipient, is_signer=False, is_writable=True),
            AccountMeta(token.mint, is_signer=False, is_writable=False),
            AccountMeta(token.bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(token.associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(addresses["user_token_account"], is_signer=False, is_writable=True),
            AccountMeta(self.keypair.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(token.creator_vault, is_signer=False, is_writable=True),
            AccountMeta(token.token_program_id, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(addresses["fee_config"], is_signer=False, is_writable=False),
            AccountMeta(PUMP_FUN_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        # Build instruction data
        track_volume = bytes([1, 1])
        data = (
            self._discriminators["sell"]
            + struct.pack("<Q", token_amount)
            + struct.pack("<Q", min_sol_output)
            + track_volume
        )

        return Instruction(PUMP_FUN_PROGRAM, data, accounts)

    def _build_compute_budget_instructions(
        self,
        compute_units: int,
        include_priority_fee: bool = True,
    ) -> list[Instruction]:
        """Build compute budget instructions."""
        instructions = []

        # Account data size limit (reduces CU cost)
        data = struct.pack("<BI", 4, self.account_data_size_limit)
        instructions.append(Instruction(COMPUTE_BUDGET_PROGRAM, data, []))

        # Compute unit limit
        instructions.append(set_compute_unit_limit(compute_units))

        # Priority fee (only for non-Jito transactions)
        if include_priority_fee:
            instructions.append(set_compute_unit_price(self.priority_fee))

        return instructions

    async def _send_jito_bundle(self, transactions: list[Transaction]) -> str | None:
        """Send transaction bundle to Jito."""
        if not self.jito_url:
            return None

        try:
            # Serialize transactions to base64
            encoded_txs = [
                base64.b64encode(bytes(tx)).decode("utf-8")
                for tx in transactions
            ]

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [encoded_txs],
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.jito_url}/api/v1/bundles",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()

                    if "error" in result:
                        print(f"[JITO] Bundle error: {result['error']}")
                        return None

                    bundle_id = result.get("result")
                    print(f"[JITO] Bundle submitted: {bundle_id}")
                    return bundle_id

        except Exception as e:
            print(f"[JITO] Error sending bundle: {e}")
            return None

    async def _send_transaction_direct(self, transaction: Transaction) -> str | None:
        """Send transaction directly to RPC."""
        try:
            opts = TxOpts(skip_preflight=True, preflight_commitment=Processed)
            resp = await self._client.send_transaction(transaction, opts)
            return str(resp.value)
        except Exception as e:
            print(f"[TRADER] Direct send error: {e}")
            return None

    async def _confirm_transaction(self, signature: str, timeout: float = 30) -> bool:
        """Wait for transaction confirmation."""
        try:
            await self._client.confirm_transaction(
                signature,
                commitment="confirmed",
                sleep_seconds=0.5,
            )
            return True
        except Exception:
            return False

    async def buy(
        self,
        token: TokenInfo,
        sol_amount: float,
        use_jito: bool = True,
    ) -> TradeResult:
        """
        Buy tokens with sandwich protection.

        Args:
            token: Token information
            sol_amount: Amount of SOL to spend
            use_jito: Whether to use Jito bundles for MEV protection

        Returns:
            TradeResult with transaction details
        """
        print(f"[TRADER] Buying {token.symbol} with {sol_amount} SOL")

        try:
            # Get pool state
            pool_state = await self.get_pool_state(token.bonding_curve)
            if not pool_state:
                return TradeResult(False, None, None, None, "Failed to fetch pool state")

            if pool_state["complete"]:
                return TradeResult(False, None, None, None, "Token already graduated")

            price_per_token = pool_state["price_per_token"]
            print(f"[TRADER] Price per token: {price_per_token:.10f} SOL")

            # Calculate amounts
            sol_lamports = int(sol_amount * LAMPORTS_PER_SOL)
            token_amount_raw = int((sol_amount / price_per_token) * (10 ** TOKEN_DECIMALS))

            # Apply slippage
            min_token_amount = int(token_amount_raw * (1 - self.buy_slippage))
            max_sol_cost = int(sol_lamports * (1 + self.buy_slippage))

            print(f"[TRADER] Expected tokens: {token_amount_raw / (10 ** TOKEN_DECIMALS):,.2f}")
            print(f"[TRADER] Min tokens (with slippage): {min_token_amount / (10 ** TOKEN_DECIMALS):,.2f}")

            # Derive addresses
            addresses = self._derive_addresses(token, self.keypair.pubkey())

            # Build instructions
            instructions = []

            # Compute budget (no priority fee if using Jito)
            instructions.extend(self._build_compute_budget_instructions(
                self.compute_units_buy,
                include_priority_fee=not use_jito,
            ))

            # Create ATA (idempotent)
            ata_ix = create_idempotent_associated_token_account(
                self.keypair.pubkey(),
                self.keypair.pubkey(),
                token.mint,
                token.token_program_id,
            )
            instructions.append(ata_ix)

            # Buy instruction
            buy_ix = self._build_buy_instruction(
                token, addresses, min_token_amount, max_sol_cost
            )
            instructions.append(buy_ix)

            # Build transaction
            blockhash = await self.get_blockhash()
            message = Message(instructions, self.keypair.pubkey())
            tx = Transaction([self.keypair], message, blockhash)

            # Send via Jito or direct
            signature = None
            if use_jito and self.jito_url:
                # Build tip transaction
                tip_ix = transfer(TransferParams(
                    from_pubkey=self.keypair.pubkey(),
                    to_pubkey=self._get_jito_tip_account(),
                    lamports=self.jito_tip_lamports,
                ))
                tip_msg = Message([tip_ix], self.keypair.pubkey())
                tip_tx = Transaction([self.keypair], tip_msg, blockhash)

                # Send bundle (buy tx + tip tx)
                bundle_id = await self._send_jito_bundle([tx, tip_tx])
                if bundle_id:
                    # Jito bundles don't return signature directly, extract from tx
                    signature = str(tx.signatures[0])
                    print(f"[TRADER] Jito bundle sent, signature: {signature}")

            # Fallback to direct send
            if not signature:
                print("[TRADER] Sending directly to RPC...")
                signature = await self._send_transaction_direct(tx)

            if not signature:
                return TradeResult(False, None, None, None, "Failed to send transaction")

            # Wait for confirmation
            print(f"[TRADER] Waiting for confirmation: {signature}")
            confirmed = await self._confirm_transaction(signature)

            if not confirmed:
                return TradeResult(False, signature, None, None, "Transaction not confirmed")

            print(f"[TRADER] Buy confirmed: {signature}")

            result = TradeResult(
                success=True,
                signature=signature,
                tokens_amount=min_token_amount,
                sol_amount=sol_lamports,
                price_per_token=price_per_token,
            )

            # Send Telegram notification
            if self.telegram:
                await self.telegram.notify_buy(token, result)

            return result

        except Exception as e:
            print(f"[TRADER] Buy error: {e}")
            return TradeResult(False, None, None, None, str(e))

    async def sell(
        self,
        token: TokenInfo,
        token_amount: int | None = None,
        use_jito: bool = True,
    ) -> TradeResult:
        """
        Sell tokens with sandwich protection.

        Args:
            token: Token information
            token_amount: Amount to sell (raw units), None for all
            use_jito: Whether to use Jito bundles

        Returns:
            TradeResult with transaction details
        """
        print(f"[TRADER] Selling {token.symbol}")

        try:
            addresses = self._derive_addresses(token, self.keypair.pubkey())

            # Get token balance if not specified
            if token_amount is None:
                resp = await self._client.get_token_account_balance(
                    addresses["user_token_account"]
                )
                if resp.value:
                    token_amount = int(resp.value.amount)
                else:
                    return TradeResult(False, None, None, None, "No tokens to sell")

            if token_amount <= 0:
                return TradeResult(False, None, None, None, "No tokens to sell")

            print(f"[TRADER] Selling {token_amount / (10 ** TOKEN_DECIMALS):,.2f} tokens")

            # Get pool state for price
            pool_state = await self.get_pool_state(token.bonding_curve)
            if not pool_state:
                return TradeResult(False, None, None, None, "Failed to fetch pool state")

            price_per_token = pool_state["price_per_token"]
            expected_sol = int((token_amount / (10 ** TOKEN_DECIMALS)) * price_per_token * LAMPORTS_PER_SOL)
            min_sol_output = int(expected_sol * (1 - self.sell_slippage))

            print(f"[TRADER] Expected SOL: {expected_sol / LAMPORTS_PER_SOL:.6f}")
            print(f"[TRADER] Min SOL (with slippage): {min_sol_output / LAMPORTS_PER_SOL:.6f}")

            # Build instructions
            instructions = []

            # Compute budget
            instructions.extend(self._build_compute_budget_instructions(
                self.compute_units_sell,
                include_priority_fee=not use_jito,
            ))

            # Sell instruction
            sell_ix = self._build_sell_instruction(
                token, addresses, token_amount, min_sol_output
            )
            instructions.append(sell_ix)

            # Build transaction
            blockhash = await self.get_blockhash()
            message = Message(instructions, self.keypair.pubkey())
            tx = Transaction([self.keypair], message, blockhash)

            # Send via Jito or direct
            signature = None
            if use_jito and self.jito_url:
                tip_ix = transfer(TransferParams(
                    from_pubkey=self.keypair.pubkey(),
                    to_pubkey=self._get_jito_tip_account(),
                    lamports=self.jito_tip_lamports,
                ))
                tip_msg = Message([tip_ix], self.keypair.pubkey())
                tip_tx = Transaction([self.keypair], tip_msg, blockhash)

                bundle_id = await self._send_jito_bundle([tx, tip_tx])
                if bundle_id:
                    signature = str(tx.signatures[0])
                    print(f"[TRADER] Jito bundle sent, signature: {signature}")

            if not signature:
                print("[TRADER] Sending directly to RPC...")
                signature = await self._send_transaction_direct(tx)

            if not signature:
                return TradeResult(False, None, None, None, "Failed to send transaction")

            print(f"[TRADER] Waiting for confirmation: {signature}")
            confirmed = await self._confirm_transaction(signature)

            if not confirmed:
                return TradeResult(False, signature, None, None, "Transaction not confirmed")

            print(f"[TRADER] Sell confirmed: {signature}")

            return TradeResult(
                success=True,
                signature=signature,
                tokens_amount=token_amount,
                sol_amount=min_sol_output,
                price_per_token=price_per_token,
            )

        except Exception as e:
            print(f"[TRADER] Sell error: {e}")
            return TradeResult(False, None, None, None, str(e))

    async def get_token_balance(self, token: TokenInfo) -> int:
        """Get token balance for the wallet."""
        try:
            addresses = self._derive_addresses(token, self.keypair.pubkey())
            resp = await self._client.get_token_account_balance(
                addresses["user_token_account"]
            )
            if resp.value:
                return int(resp.value.amount)
            return 0
        except Exception:
            return 0

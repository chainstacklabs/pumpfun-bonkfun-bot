"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import json
import struct
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from utils.logger import get_logger

logger = get_logger(__name__)


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """
    Create SetLoadedAccountsDataSizeLimit instruction to reduce CU consumption.

    By default, Solana transactions can load up to 64MB of account data,
    costing 16k CU (8 CU per 32KB). Setting a lower limit reduces CU
    consumption and improves transaction priority.

    NOTE: CU savings are NOT visible in "consumed CU" metrics, which only
    show execution CU. The 16k CU loaded accounts overhead is counted
    separately for transaction priority/cost calculation.

    Args:
        bytes_limit: Max account data size in bytes (e.g., 512_000 = 512KB)

    Returns:
        Compute Budget instruction with discriminator 4

    Reference:
        https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
    """
    COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
        "ComputeBudget111111111111111111111111111111"
    )

    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(self, rpc_endpoint: str):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_updater_task = asyncio.create_task(
            self.start_blockhash_updater()
        )

    async def start_blockhash_updater(self, interval: float = 5.0):
        """Start background task to update recent blockhash."""
        while True:
            try:
                blockhash = await self.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = blockhash
            except Exception as e:
                logger.warning(f"Blockhash fetch failed: {e!s}")
            finally:
                await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        """Return the most recently cached blockhash."""
        async with self._blockhash_lock:
            if self._cached_blockhash is None:
                raise RuntimeError("No cached blockhash available yet")
            return self._cached_blockhash

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance.

        Returns:
            AsyncClient instance
        """
        if self._client is None:
            self._client = AsyncClient(self.rpc_endpoint)
        return self._client

    async def close(self):
        """Close the client connection and stop the blockhash updater."""
        if self._blockhash_updater_task:
            self._blockhash_updater_task.cancel()
            try:
                await self._blockhash_updater_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

    async def get_health(self) -> str | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        }
        result = await self.post_rpc(body)
        if result and "result" in result:
            return result["result"]
        return None

    async def get_account_info(self, pubkey: Pubkey) -> dict[str, Any]:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account

        Returns:
            Account info response

        Raises:
            ValueError: If account doesn't exist or has no data
        """
        client = await self.get_client()
        response = await client.get_account_info(
            pubkey, encoding="base64"
        )  # base64 encoding for account data by default
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        return response.value

    async def get_token_account_balance(self, token_account: Pubkey) -> int:
        """Get token balance for an account.

        Args:
            token_account: Token account address

        Returns:
            Token balance as integer
        """
        client = await self.get_client()
        response = await client.get_token_account_balance(token_account)
        if response.value:
            return int(response.value.amount)
        return 0

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash.

        Returns:
            Recent blockhash as string
        """
        client = await self.get_client()
        response = await client.get_latest_blockhash(commitment="processed")
        return response.value.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 3,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
    ) -> str:
        """
        Send a transaction with optional priority fee and compute unit limit.

        Args:
            instructions: List of instructions to include in the transaction.
            signer_keypair: Keypair to sign the transaction.
            skip_preflight: Whether to skip preflight checks.
            max_retries: Maximum number of retry attempts.
            priority_fee: Optional priority fee in microlamports.
            compute_unit_limit: Optional compute unit limit. Defaults to 85,000 if not provided.
            account_data_size_limit: Optional account data size limit in bytes (e.g., 512_000).
                                    Reduces CU cost from 16k to ~128 CU. Must be first instruction.

        Returns:
            Transaction signature.
        """
        client = await self.get_client()

        logger.info(
            f"Priority fee in microlamports: {priority_fee if priority_fee else 0}"
        )

        # Add compute budget instructions if applicable
        if (
            priority_fee is not None
            or compute_unit_limit is not None
            or account_data_size_limit is not None
        ):
            fee_instructions = []

            if account_data_size_limit is not None:
                fee_instructions.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
                logger.info(f"Account data size limit: {account_data_size_limit} bytes")

            # Set compute unit limit (use provided value or default to 85,000)
            cu_limit = compute_unit_limit if compute_unit_limit is not None else 85_000
            fee_instructions.append(set_compute_unit_limit(cu_limit))

            # Set priority fee if provided
            if priority_fee is not None:
                fee_instructions.append(set_compute_unit_price(priority_fee))

            instructions = fee_instructions + instructions

        recent_blockhash = await self.get_cached_blockhash()
        message = Message(instructions, signer_keypair.pubkey())
        transaction = Transaction([signer_keypair], message, recent_blockhash)

        for attempt in range(max_retries):
            try:
                tx_opts = TxOpts(
                    skip_preflight=skip_preflight, preflight_commitment=Processed
                )
                response = await client.send_transaction(transaction, tx_opts)
                return response.value

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.exception(
                        f"Failed to send transaction after {max_retries} attempts"
                    )
                    raise

                wait_time = 2**attempt
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed: {e!s}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    async def confirm_transaction(
        self, signature: str, commitment: str = "confirmed"
    ) -> bool:
        """Wait for transaction confirmation.

        Args:
            signature: Transaction signature
            commitment: Confirmation commitment level

        Returns:
            Whether transaction was confirmed
        """
        client = await self.get_client()
        try:
            await client.confirm_transaction(
                signature, commitment=commitment, sleep_seconds=1
            )
            return True
        except Exception:
            logger.exception(f"Failed to confirm transaction {signature}")
            return False

    async def get_transaction_token_balance(
        self, signature: str, user_pubkey: Pubkey, mint: Pubkey
    ) -> int | None:
        """Get the user's token balance after a transaction from postTokenBalances.

        Args:
            signature: Transaction signature
            user_pubkey: User's wallet public key
            mint: Token mint address

        Returns:
            Token balance (raw amount) after transaction, or None if not found
        """
        result = await self._get_transaction_result(signature)
        if not result:
            return None

        meta = result.get("meta", {})
        post_token_balances = meta.get("postTokenBalances", [])

        user_str = str(user_pubkey)
        mint_str = str(mint)

        for balance in post_token_balances:
            if balance.get("owner") == user_str and balance.get("mint") == mint_str:
                ui_amount = balance.get("uiTokenAmount", {})
                amount_str = ui_amount.get("amount")
                if amount_str:
                    return int(amount_str)

        return None

    async def get_buy_transaction_details(
        self, signature: str, mint: Pubkey, sol_destination: Pubkey
    ) -> tuple[int | None, int | None]:
        """Get actual tokens received and SOL spent from a buy transaction.

        Uses preBalances/postBalances to find exact SOL transferred to the
        pool/curve and pre/post token balance diff to find tokens received.

        Args:
            signature: Transaction signature
            mint: Token mint address
            sol_destination: Address where SOL is sent (bonding curve for pump.fun,
                           quote_vault for letsbonk)

        Returns:
            Tuple of (tokens_received_raw, sol_spent_lamports), or (None, None)
        """
        result = await self._get_transaction_result(signature)
        if not result:
            return None, None

        meta = result.get("meta", {})
        mint_str = str(mint)

        # Get tokens received from pre/post token balance diff
        # This works for Token2022 where owner might be different
        tokens_received = None
        pre_token_balances = meta.get("preTokenBalances", [])
        post_token_balances = meta.get("postTokenBalances", [])

        # Build lookup by account index
        pre_by_idx = {b.get("accountIndex"): b for b in pre_token_balances}
        post_by_idx = {b.get("accountIndex"): b for b in post_token_balances}

        # Find positive token diff for our mint (user receiving tokens)
        all_indices = set(pre_by_idx.keys()) | set(post_by_idx.keys())
        for idx in all_indices:
            pre = pre_by_idx.get(idx)
            post = post_by_idx.get(idx)

            # Check if this is our mint
            balance_mint = (post or pre).get("mint", "")
            if balance_mint != mint_str:
                continue

            pre_amount = (
                int(pre.get("uiTokenAmount", {}).get("amount", 0)) if pre else 0
            )
            post_amount = (
                int(post.get("uiTokenAmount", {}).get("amount", 0)) if post else 0
            )
            diff = post_amount - pre_amount

            # Positive diff means tokens received (not the bonding curve's negative)
            if diff > 0:
                tokens_received = diff
                logger.info(f"Tokens received from tx: {tokens_received}")
                break

        # Get SOL spent from preBalances/postBalances at sol_destination
        sol_destination_str = str(sol_destination)
        sol_spent = None
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        account_keys = (
            result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )

        for i, key in enumerate(account_keys):
            key_str = key if isinstance(key, str) else key.get("pubkey", "")
            if key_str == sol_destination_str:
                if i < len(pre_balances) and i < len(post_balances):
                    sol_spent = post_balances[i] - pre_balances[i]
                    if sol_spent > 0:
                        logger.info(f"SOL to pool/curve: {sol_spent} lamports")
                    else:
                        logger.warning(
                            f"SOL destination balance change not positive: {sol_spent}"
                        )
                        sol_spent = None
                break

        return tokens_received, sol_spent

    async def _get_transaction_result(self, signature: str) -> dict | None:
        """Fetch transaction result from RPC.

        Args:
            signature: Transaction signature

        Returns:
            Transaction result dict or None
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }

        response = await self.post_rpc(body)
        if not response or "result" not in response:
            logger.warning(f"Failed to get transaction {signature}")
            return None

        result = response["result"]
        if not result or "meta" not in result:
            return None

        return result

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Send a raw RPC request to the Solana node.

        Args:
            body: JSON-RPC request body.

        Returns:
            Optional[Dict[str, Any]]: Parsed JSON response, or None if the request fails.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    timeout=aiohttp.ClientTimeout(10),  # 10-second timeout
                ) as response:
                    response.raise_for_status()
                    return await response.json()
        except aiohttp.ClientError:
            logger.exception("RPC request failed")
            return None
        except json.JSONDecodeError:
            logger.exception("Failed to decode RPC response")
            return None

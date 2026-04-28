"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import random
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

from core.rpc_rate_limiter import TokenBucketRateLimiter
from utils.logger import get_logger

logger = get_logger(__name__)

HTTP_TOO_MANY_REQUESTS = 429


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

    def __init__(self, rpc_endpoint: str, max_rps: float = 25.0):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
            max_rps: Maximum RPC requests per second (rate limiter)
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_updater_task = asyncio.create_task(
            self.start_blockhash_updater()
        )
        self._rate_limiter = TokenBucketRateLimiter(max_rps=max_rps)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

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

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp session.

        Returns:
            Shared aiohttp.ClientSession instance.
        """
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            return self._session

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

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

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

    async def get_account_info(
        self, pubkey: Pubkey, commitment: str | None = None
    ) -> dict[str, Any]:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account
            commitment: Optional commitment override (e.g., "processed" for
                fresh state right after a geyser event; default "confirmed")

        Returns:
            Account info response

        Raises:
            ValueError: If account doesn't exist or has no data
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await client.get_account_info(pubkey, **kwargs)
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
        await self._rate_limiter.acquire()
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
        await self._rate_limiter.acquire()
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
                await self._rate_limiter.acquire()
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
        """Wait for transaction confirmation and verify execution success.

        Confirms the transaction landed on-chain, then checks meta.err to
        ensure the inner program instructions actually succeeded. A transaction
        can be "confirmed" (included in a block) but still fail execution.

        Args:
            signature: Transaction signature
            commitment: Confirmation commitment level

        Returns:
            Whether transaction was confirmed AND executed successfully
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        try:
            await client.confirm_transaction(
                signature, commitment=commitment, sleep_seconds=1
            )
        except Exception:
            logger.exception(f"Failed to confirm transaction {signature}")
            return False

        # Verify the transaction actually succeeded (no program errors)
        result = await self._get_transaction_result(str(signature))
        if not result:
            logger.warning(
                f"Could not fetch transaction {str(signature)[:16]}... "
                f"to verify execution — treating as unconfirmed"
            )
            return False

        tx_err = result.get("meta", {}).get("err")
        if tx_err:
            logger.error(
                f"Transaction {str(signature)[:16]}... confirmed but failed: {tx_err}"
            )
            return False

        return True

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

        # Check for transaction execution errors (e.g., MaxLoadedAccountsDataSizeExceeded)
        tx_err = meta.get("err")
        if tx_err:
            logger.error(
                f"Transaction {signature[:16]}... failed with error: {tx_err}"
            )
            return None, None

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

    async def post_rpc(
        self, body: dict[str, Any], max_retries: int = 3, max_429_retries: int = 10
    ) -> dict[str, Any] | None:
        """Send a raw RPC request with rate limiting, retry, and 429 handling.

        Args:
            body: JSON-RPC request body.
            max_retries: Maximum number of retry attempts for errors.
            max_429_retries: Maximum number of retry attempts for 429 rate limits.

        Returns:
            Parsed JSON response, or None if all attempts fail.
        """
        method = body.get("method", "unknown")
        error_attempts = 0
        rate_limit_attempts = 0

        while error_attempts < max_retries:
            try:
                await self._rate_limiter.acquire()
                session = await self._get_session()

                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                ) as response:
                    if response.status == HTTP_TOO_MANY_REQUESTS:
                        rate_limit_attempts += 1
                        if rate_limit_attempts >= max_429_retries:
                            logger.error(
                                f"RPC rate limited (429) on {method}, "
                                f"exhausted {max_429_retries} rate-limit retries"
                            )
                            return None
                        retry_after = response.headers.get("Retry-After")
                        try:
                            wait_time = float(retry_after) if retry_after else None
                        except (ValueError, TypeError):
                            wait_time = None
                        if wait_time is None:
                            wait_time = min(2**rate_limit_attempts, 30)
                        jitter = wait_time * random.uniform(0, 0.25)  # noqa: S311
                        total_wait = wait_time + jitter
                        logger.warning(
                            f"RPC rate limited (429) on {method}, "
                            f"429 retry {rate_limit_attempts}/{max_429_retries}, "
                            f"waiting {total_wait:.1f}s"
                        )
                        await asyncio.sleep(total_wait)
                        continue

                    response.raise_for_status()
                    return await response.json()

            except aiohttp.ContentTypeError:
                logger.exception(f"Failed to decode RPC response for {method}")
                return None

            except aiohttp.ClientError:
                error_attempts += 1
                if error_attempts >= max_retries:
                    logger.exception(
                        f"RPC request {method} failed after {max_retries} attempts"
                    )
                    return None

                wait_time = min(2 ** (error_attempts - 1), 16)
                jitter = wait_time * random.uniform(0, 0.25)  # noqa: S311
                logger.warning(
                    f"RPC request {method} failed "
                    f"(attempt {error_attempts}/{max_retries}), "
                    f"retrying in {wait_time + jitter:.1f}s"
                )
                await asyncio.sleep(wait_time + jitter)

        return None

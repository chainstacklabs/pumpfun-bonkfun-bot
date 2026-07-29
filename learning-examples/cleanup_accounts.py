import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from solders.pubkey import Pubkey
from spl.token.instructions import BurnParams, CloseAccountParams, burn, close_account

from core.client import SolanaClient
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from utils.logger import get_logger

load_dotenv()
# get_logger attaches no handler — the bot installs one at startup, but a
# standalone example has to do it itself or every line below goes nowhere. This
# script ran completely silently, success or failure, without it.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
# httpx logs each request at INFO, and the RPC endpoint carries an API key in
# its path — keep it out of the console.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = get_logger(__name__)

RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# Update this address to MINT address of a token you want to close
# Mint of the token account to close: pass as argv[1], or hardcode here.
MINT_ADDRESS = Pubkey.from_string(
    sys.argv[1] if len(sys.argv) > 1 else "9WHpYbqG6LJvfCYfMjvGbyo1wHXgroCrixPb33s2pump"
)

# Token program for the mint - use TOKEN_PROGRAM for legacy SPL tokens, TOKEN_2022_PROGRAM for Token-2022
# This must match the actual token's program to derive the correct ATA address
# Pass "2022" as argv[2] for a Token-2022 mint (every pump.fun coin is).
TOKEN_PROGRAM = (
    SystemAddresses.TOKEN_2022_PROGRAM
    if len(sys.argv) > 2 and sys.argv[2] == "2022"
    else SystemAddresses.TOKEN_PROGRAM
)


async def close_account_if_exists(
    client: SolanaClient, wallet: Wallet, account: Pubkey, mint: Pubkey
):
    """Safely close a token account if it exists and reclaim rent."""
    try:
        try:
            await client.get_account_info(account)
        except ValueError:
            logger.info(f"Account does not exist or already closed: {account}")
            return

        # WARNING: This will permanently burn all tokens in the account before closing it
        # Closing account is impossible if balance is positive
        # Burn + close are combined into a single transaction to avoid race conditions
        instructions = []
        balance = await client.get_token_account_balance(account)
        if balance > 0 and mint == SystemAddresses.WSOL_MINT:
            # Wrapped SOL cannot be burned — the token program rejects it with
            # NativeNotSupported (error 10) and the whole transaction reverts, so
            # the account can never be closed. Closing a WSOL account already
            # returns both the wrapped lamports and the rent to the owner, so
            # there is nothing to burn first. Matches AccountCleanupManager.
            logger.info(
                f"Unwrapping {balance} lamports of wrapped SOL from {account} "
                f"by closing it (burn skipped)"
            )
        elif balance > 0:
            logger.info(f"Burning {balance} tokens from account {account}...")
            burn_ix = burn(
                BurnParams(
                    account=account,
                    mint=mint,
                    owner=wallet.pubkey,
                    amount=balance,
                    program_id=TOKEN_PROGRAM,
                )
            )
            instructions.append(burn_ix)

        # Account exists, attempt to close it
        logger.info(f"Closing account: {account}")
        close_params = CloseAccountParams(
            account=account,
            dest=wallet.pubkey,
            owner=wallet.pubkey,
            program_id=TOKEN_PROGRAM,
        )
        instructions.append(close_account(close_params))

        tx_sig = await client.build_and_send_transaction(
            instructions,
            wallet.keypair,
            skip_preflight=True,
        )
        # confirm_transaction returns False when the transaction landed but
        # reverted — reporting success on that would hide a failed cleanup.
        # The label reflects what was actually built: wrapped SOL is unwrapped by
        # the close, never burned, so it must not claim a burn.
        if balance > 0 and mint == SystemAddresses.WSOL_MINT:
            action = "Unwrapped and closed"
        elif balance > 0:
            action = "Burned and closed"
        else:
            action = "Closed"
        if await client.confirm_transaction(tx_sig):
            logger.info(f"{action} successfully: {account}")
        else:
            logger.error(f"Failed to {action.lower()} account {account}: {tx_sig}")

    except Exception as e:
        logger.error(f"Error while processing account {account}: {e}")


async def main():
    try:
        client = SolanaClient(RPC_ENDPOINT)
        wallet = Wallet(PRIVATE_KEY)

        # Get user's ATA for the token
        ata = wallet.get_associated_token_address(MINT_ADDRESS, TOKEN_PROGRAM)
        await close_account_if_exists(client, wallet, ata, MINT_ADDRESS)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

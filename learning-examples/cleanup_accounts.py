import asyncio
import os

from dotenv import load_dotenv
from solders.pubkey import Pubkey
from spl.token.instructions import BurnParams, CloseAccountParams, burn, close_account

from core.client import SolanaClient
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# Update this address to MINT address of a token you want to close
MINT_ADDRESS = Pubkey.from_string("9WHpYbqG6LJvfCYfMjvGbyo1wHXgroCrixPb33s2pump")

# Token program for the mint - use TOKEN_PROGRAM for legacy SPL tokens, TOKEN_2022_PROGRAM for Token-2022
# This must match the actual token's program to derive the correct ATA address
TOKEN_PROGRAM = SystemAddresses.TOKEN_PROGRAM


async def close_account_if_exists(
    client: SolanaClient, wallet: Wallet, account: Pubkey, mint: Pubkey
):
    """Safely close a token account if it exists and reclaim rent."""
    try:
        solana_client = await client.get_client()
        info = await solana_client.get_account_info(
            account, encoding="base64"
        )  # base64 encoding for account data by deafult

        # WARNING: This will permanently burn all tokens in the account before closing it
        # Closing account is impossible if balance is positive
        balance = await client.get_token_account_balance(account)
        if balance > 0:
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
            await client.build_and_send_transaction([burn_ix], wallet.keypair)
            logger.info(f"Burned tokens from {account}")

        # If account exists, attempt to close it
        if info.value:
            logger.info(f"Closing account: {account}")
            close_params = CloseAccountParams(
                account=account,
                dest=wallet.pubkey,
                owner=wallet.pubkey,
                program_id=TOKEN_PROGRAM,
            )
            ix = close_account(close_params)

            tx_sig = await client.build_and_send_transaction(
                [ix],
                wallet.keypair,
                skip_preflight=True,
            )
            await client.confirm_transaction(tx_sig)
            logger.info(f"Closed successfully: {account}")
        else:
            logger.info(f"Account does not exist or already closed: {account}")

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

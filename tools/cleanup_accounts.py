"""Burn any leftover balance in a token account and close it, reclaiming rent.

WARNING: this submits real transactions and spends real funds.

Usage:
    uv run tools/cleanup_accounts.py <MINT>
"""

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from solders.pubkey import Pubkey
from spl.token.instructions import burn, close_account
from spl.token.models import BurnParams, CloseAccountParams

from core.client import SolanaClient
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from utils.logger import get_logger, install_secret_redaction

load_dotenv()
# get_logger attaches no handler: the bot installs one at startup, but a
# standalone script has to do it itself or every line below goes nowhere.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
# The HTTP clients log each request at INFO and the RPC endpoint carries an API
# key, so raising the root logger to INFO is what leaks it. Naming the clients
# here is not enough — solana-py 0.40 renamed httpx to httpx2 and the guard went
# with it. install_secret_redaction masks the value itself, whichever logger
# emits it.
install_secret_redaction()
logger = get_logger(__name__)

RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# The mint's token program is read from the mint account itself (see
# resolve_token_program). Guessing it derives the wrong ATA address, and the
# script then reports "already closed" for an account that was never looked at.


async def resolve_token_program(client: SolanaClient, mint: Pubkey) -> Pubkey:
    """Return the token program that owns this mint.

    A mint account is owned by whichever token program created it, so the mint
    itself is the authoritative source. Every pump.fun coin is Token-2022 while
    letsbonk coins and USDC are legacy SPL, and the ATA address differs between
    them — deriving with the wrong one silently points at an address that does
    not exist.

    Args:
        client: Solana RPC client

    Returns:
        TOKEN_PROGRAM or TOKEN_2022_PROGRAM

    Raises:
        ValueError: If the mint is missing or owned by something else
    """
    info = await client.get_account_info(mint)
    owner = info.owner
    if owner not in (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM):
        raise ValueError(f"Mint {mint} is not owned by a token program (owner {owner})")
    return owner


async def close_account_if_exists(
    client: SolanaClient,
    wallet: Wallet,
    account: Pubkey,
    mint: Pubkey,
    token_program: Pubkey,
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
            # Wrapped SOL cannot be burned: the token program rejects it with
            # NativeNotSupported (error 10) and the transaction reverts. Closing
            # a WSOL account already returns both the wrapped lamports and the
            # rent, so there is nothing to burn first.
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
                    program_id=token_program,
                )
            )
            instructions.append(burn_ix)

        # Account exists, attempt to close it
        logger.info(f"Closing account: {account}")
        close_params = CloseAccountParams(
            account=account,
            dest=wallet.pubkey,
            owner=wallet.pubkey,
            program_id=token_program,
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


async def cleanup(mint: Pubkey) -> None:
    """Burn any leftover balance in this wallet's token account and close it."""
    client = SolanaClient(RPC_ENDPOINT)
    try:
        wallet = Wallet(PRIVATE_KEY)

        token_program = await resolve_token_program(client, mint)
        logger.info(f"Mint {mint} uses token program {token_program}")

        ata = wallet.get_associated_token_address(mint, token_program)
        await close_account_if_exists(client, wallet, ata, mint, token_program)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        await client.close()


def main() -> None:
    """Parse the command line and close the account."""
    parser = argparse.ArgumentParser(
        description="Burn any leftover balance in a token account and close it"
    )
    parser.add_argument("mint", help="Mint whose token account to close")
    args = parser.parse_args()

    asyncio.run(cleanup(Pubkey.from_string(args.mint)))


if __name__ == "__main__":
    main()

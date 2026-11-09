from solders.pubkey import Pubkey

from cleanup.manager import AccountCleanupManager
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.wallet import Wallet
from utils.logger import get_logger

logger = get_logger(__name__)


def should_cleanup_after_failure(cleanup_mode) -> bool:
    return cleanup_mode == "on_fail"


def should_cleanup_after_sell(cleanup_mode) -> bool:
    return cleanup_mode == "after_sell"


def should_cleanup_post_session(cleanup_mode) -> bool:
    return cleanup_mode == "post_session"


async def handle_cleanup_after_failure(
    client,
    wallet,
    mint,
    token_program_id,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
):
    if should_cleanup_after_failure(cleanup_mode):
        logger.info("[Cleanup] Triggered by failed buy transaction.")
        manager = AccountCleanupManager(
            client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
        )
        await manager.cleanup_ata(mint, token_program_id)


async def handle_cleanup_after_sell(
    client,
    wallet,
    mint,
    token_program_id,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
    after_slot: int | None = None,
):
    """Close a sold coin's account under `after_sell`.

    Args:
        after_slot: The slot the sale landed in; None waits a fixed time instead
    """
    if should_cleanup_after_sell(cleanup_mode):
        logger.info("[Cleanup] Triggered after token sell.")
        manager = AccountCleanupManager(
            client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
        )
        await manager.cleanup_ata(mint, token_program_id, after_slot=after_slot)


async def handle_cleanup_cleared(  # noqa: PLR0913, PLR0917 - like its siblings
    client: SolanaClient,
    wallet: Wallet,
    mint: Pubkey,
    token_program_id: Pubkey,
    priority_fee_manager: PriorityFeeManager,
    cleanup_mode: str,
    cleanup_with_prior_fee: bool,  # noqa: FBT001
) -> None:
    """Close the account of a coin the wallet no longer holds, under `after_sell`.

    For a position cleared because the coin was sold or moved while the bot was
    down. Nothing is burned: only an empty account closes. `post_session`
    closes it at the end of the session, with every other empty account.
    """
    if should_cleanup_after_sell(cleanup_mode):
        logger.info(
            f"[Cleanup] Closing the token account of {mint}: the wallet no longer "
            f"holds the coin."
        )
        manager = AccountCleanupManager(
            client,
            wallet,
            priority_fee_manager,
            cleanup_with_prior_fee,
            force_burn=False,
        )
        await manager.cleanup_ata(mint, token_program_id, settle=False)


async def handle_cleanup_post_session(
    client,
    wallet,
    mints,
    token_program_ids,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
    sweep_except: set[Pubkey] | None = None,
    after_slot: int | None = None,
):
    """Close the session's traded coins' accounts, then every other empty one.

    Args:
        sweep_except: Mints whose accounts the sweep of empty accounts leaves
            open (quote mints, coins kept for the next start); None sweeps none
        after_slot: The slot the session's last sale landed in; None waits a
            fixed time instead
    """
    if should_cleanup_post_session(cleanup_mode):
        logger.info("[Cleanup] Triggered post trading session.")
        manager = AccountCleanupManager(
            client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
        )
        await manager.cleanup_atas(
            mints, token_program_ids, sweep_except=sweep_except, after_slot=after_slot
        )

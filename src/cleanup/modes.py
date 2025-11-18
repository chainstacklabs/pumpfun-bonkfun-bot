from cleanup.manager import AccountCleanupManager
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
):
    if should_cleanup_after_sell(cleanup_mode):
        logger.info("[Cleanup] Triggered after token sell.")
        manager = AccountCleanupManager(
            client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
        )
        await manager.cleanup_ata(mint, token_program_id)


async def handle_cleanup_post_session(
    client,
    wallet,
    mints,
    token_program_ids,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
):
    if should_cleanup_post_session(cleanup_mode):
        logger.info("[Cleanup] Triggered post trading session.")
        manager = AccountCleanupManager(
            client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
        )
        for mint, token_program_id in zip(mints, token_program_ids):
            await manager.cleanup_ata(mint, token_program_id)

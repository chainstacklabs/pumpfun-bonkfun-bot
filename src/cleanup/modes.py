from cleanup.manager import AccountCleanupManager, CleanupResult
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
) -> CleanupResult | None:
    if not should_cleanup_after_failure(cleanup_mode):
        return None
    logger.info("[Cleanup] Triggered by failed buy transaction.")
    manager = AccountCleanupManager(
        client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
    )
    result = await manager.cleanup_ata(mint, token_program_id)
    if not result.success:
        logger.warning(
            f"[Cleanup] Failed-buy cleanup remains {result.status.value} "
            f"for mint {mint}"
        )
    return result


def stage_cleanup_after_sell(
    client,
    wallet,
    mint,
    token_program_id,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
    confirmed_sold_raw: int,
) -> AccountCleanupManager | None:
    """Persist post-sell cleanup before the position journal can be cleared."""
    if cleanup_mode not in {"after_sell", "post_session"}:
        return None
    manager = AccountCleanupManager(
        client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
    )
    manager.stage_confirmed_sell_cleanup(
        mint,
        token_program_id,
        sold_raw=confirmed_sold_raw,
    )
    return manager


async def handle_cleanup_after_sell(
    client,
    wallet,
    mint,
    token_program_id,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
    confirmed_sold_raw: int | None = None,
    staged_manager: AccountCleanupManager | None = None,
) -> CleanupResult | None:
    if confirmed_sold_raw is not None and staged_manager is None:
        AccountCleanupManager.record_confirmed_sell_delta(
            wallet.pubkey,
            mint,
            token_program_id,
            sold_raw=confirmed_sold_raw,
        )
    if not should_cleanup_after_sell(cleanup_mode):
        return None
    logger.info("[Cleanup] Triggered after token sell.")
    manager = staged_manager or AccountCleanupManager(
        client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
    )
    result = await manager.cleanup_ata(mint, token_program_id)
    if not result.success:
        logger.warning(
            f"[Cleanup] Post-sell cleanup remains {result.status.value} for mint {mint}"
        )
    return result


async def handle_cleanup_post_session(
    client,
    wallet,
    mints,
    token_program_ids,
    priority_fee_manager,
    cleanup_mode,
    cleanup_with_prior_fee,
    force_burn,
) -> list[CleanupResult]:
    if not should_cleanup_post_session(cleanup_mode):
        return []
    logger.info("[Cleanup] Triggered post trading session.")
    manager = AccountCleanupManager(
        client, wallet, priority_fee_manager, cleanup_with_prior_fee, force_burn
    )
    results: list[CleanupResult] = []
    for mint, token_program_id in zip(mints, token_program_ids, strict=False):
        result = await manager.cleanup_ata(mint, token_program_id)
        results.append(result)
        if not result.success:
            logger.warning(
                f"[Cleanup] Post-session cleanup remains {result.status.value} "
                f"for mint {mint}"
            )
    return results

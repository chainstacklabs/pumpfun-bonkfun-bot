"""Sales and burns sent but never confirmed, kept in the positions file until resolved.

Such a trade may still land. Until it is known not to, the coin must not move
again: another sale could take coins the wallet holds for another reason, and
a burn could take them too. Both the bot and `pump_bot_positions sell-all`
resolve a recorded one before they touch its position.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from interfaces.core import ConfirmationStatus
from trading.platform_aware import read_deltas
from utils.logger import get_logger

if TYPE_CHECKING:
    from solders.pubkey import Pubkey

    from core.client import SolanaClient
    from interfaces.core import TokenInfo

logger = get_logger(__name__)

SALE, BURN = "sale", "burn"
#: Seconds after which a trade with no known expiry height can no longer land;
#: a blockhash lives a minute or two.
NO_EXPIRY_AFTER = 300.0
#: Looks `sell-all` makes, a second or so apart, before it leaves a trade to
#: the next run.
LOOKS_BEFORE_GIVING_UP = 60


@dataclass(frozen=True)
class Unresolved:
    """A sale or burn whose outcome is unknown.

    Args:
        kind: `SALE` or `BURN`
        last_valid_block_height: The height past which it can no longer land;
            None when the signer did not report it
        marked_at: Unix time it was recorded, for a trade with no expiry height
    """

    kind: str
    signature: str
    last_valid_block_height: int | None
    marked_at: float = field(default_factory=time.time)


class Earlier(Enum):
    """What became of an unresolved trade."""

    UNKNOWN = "still unknown"
    NOT_LANDED = "did not land"
    BURNED = "burned"
    SOLD = "sold"
    UNREADABLE = "landed, but what it sold can't be read"


async def look_up(
    client: SolanaClient, trade: Unresolved, *, looks: int, pause: float
) -> ConfirmationStatus | None:
    """Whether the trade landed.

    The block height is read before the signature, so a trade that landed
    before its expiry is seen by the lookup that follows.

    Returns:
        SUCCESS or REVERTED once a node says so; None once it can no longer
        land and no node knows it; UNCONFIRMED when neither was clear within
        `looks` lookups `pause` seconds apart
    """
    for look in range(looks):
        if look:
            await asyncio.sleep(pause)
        try:
            if trade.last_valid_block_height is None:
                expired = time.time() - trade.marked_at > NO_EXPIRY_AFTER
            else:
                height, _slot = await client.get_block_height_and_slot()
                expired = height > trade.last_valid_block_height
        except Exception:  # noqa: BLE001 - not expired as far as anyone can tell
            expired = False
        try:
            status, _slot = await client.get_signature_outcome(
                trade.signature, search_history=True, quiet=True
            )
        except Exception:
            logger.debug("Looking up an unresolved trade failed", exc_info=True)
            continue
        if status in (ConfirmationStatus.SUCCESS, ConfirmationStatus.REVERTED):
            return status
        if status is None and expired:
            return None
    return ConfirmationStatus.UNCONFIRMED


async def resolve(  # noqa: PLR0913
    client: SolanaClient,
    wallet: Pubkey,
    coin: TokenInfo,
    trade: Unresolved,
    *,
    looks: int,
    pause: float,
) -> tuple[Earlier, int, int | None]:
    """What an unresolved trade did.

    Returns:
        The outcome; for a sale that landed, the raw tokens it sold and the
        slot it landed in (None when unknown); 0 and None otherwise
    """
    status = await look_up(client, trade, looks=looks, pause=pause)
    if status is ConfirmationStatus.UNCONFIRMED:
        return Earlier.UNKNOWN, 0, None
    if status is not ConfirmationStatus.SUCCESS:
        return Earlier.NOT_LANDED, 0, None
    if trade.kind == BURN:
        return Earlier.BURNED, 0, None
    deltas = await read_deltas(client, trade.signature, wallet, coin)
    if deltas is None or deltas.tokens_raw >= 0:
        return Earlier.UNREADABLE, 0, None
    return Earlier.SOLD, -deltas.tokens_raw, deltas.slot

"""Migration entry: a coin whose curve migrated, made ready to buy in its new pool.

A listener reports a migration as a `PoolReady`: the mint, the pool and its
quote mint, nothing else. `MigrationResolver` reads the rest from chain -- the
curve for the coin's creator, flags and transfer fee, the mint for its token
program and metadata -- and waits for the pool to be readable before the buyer
is handed the coin. Mint and curve are re-read until the mint exists and the
curve shows as completed, since a stream reports a migration before every node
serves the slot it landed in: a coin can be created and migrated in the same
second, and on LaunchLab the buy that completes the curve can share that slot.
Both waits are bounded by `POOL_WAIT_SECONDS`. The buyer then finds the curve
graduated and buys in the pool, as it does for any graduated coin.
"""

import asyncio
from time import monotonic, time

from solders.account import Account
from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import cached_quote_token_program, normalize_quote_mint
from core.token_metadata import decode_token_metadata, metadata_address
from interfaces.core import CurveManager, Platform, PoolReady, TokenInfo
from platforms import get_platform_implementations
from platforms.launchlab.curve_math import mint_transfer_fee
from utils.logger import get_logger

logger = get_logger(__name__)

#: Seconds to wait for a reported coin's curve to read as completed, and then
#: for its pool to become readable. A shreds report arrives before the
#: migration runs, and an executed one can still be ahead of the node a read
#: lands on.
POOL_WAIT_SECONDS = 10.0
POOL_POLL_SECONDS = 0.5


class PoolNotTradable(Exception):  # noqa: N818
    """This report of a migration leaves nothing to buy; the message says why.

    `not_yet_visible` is set when the chain did not show the migration within
    the wait (mint missing, curve unreadable or not completed), which a lagging
    node can cause: such a report must not use the mint up.
    """

    def __init__(self, message: str, *, not_yet_visible: bool = False) -> None:
        super().__init__(message)
        self.not_yet_visible = not_yet_visible


def _pubkey(value: object) -> Pubkey | None:
    if not value:
        return None
    return value if isinstance(value, Pubkey) else Pubkey.from_string(str(value))


class MigrationResolver:
    """Turns a `PoolReady` into the `TokenInfo` the buyer and seller trade."""

    def __init__(
        self,
        client: SolanaClient,
        pool_wait_seconds: float = POOL_WAIT_SECONDS,
        platform_configs: frozenset[Pubkey] | None = None,
    ) -> None:
        """Set up the resolver.

        Args:
            platform_configs: The LaunchLab platform configs the bot trades; a
                coin whose curve is under another is refused. None takes any.
        """
        self.client = client
        self.pool_wait_seconds = pool_wait_seconds
        self.platform_configs = platform_configs

    async def resolve(self, pool: PoolReady) -> TokenInfo:
        """Read the coin behind a migration.

        Mint and curve are read at `processed`, the commitment the streams
        report at, and re-read for up to `pool_wait_seconds` until the mint
        exists and the curve shows as completed.

        Raises:
            PoolNotTradable: If the mint stays missing, or the curve unreadable
                or not completed, through the wait (an attempt that cannot land,
                or a node that has not caught up), the curve's quote mint is
                not the reported pool's (an attempt naming a pool the coin will
                not use), or a LaunchLab curve is under a platform config the
                bot does not trade (an attempt naming another config). On
                shreds another attempt may follow.
        """
        implementations = get_platform_implementations(pool.platform, self.client)
        provider = implementations.address_provider
        quote_mint = normalize_quote_mint(pool.quote_mint)
        is_pump_fun = pool.platform == Platform.PUMP_FUN
        curve_address = (
            provider.derive_pool_address(pool.mint)
            if is_pump_fun
            else provider.derive_pool_address(pool.mint, quote_mint)
        )

        state, mint_account, metaplex_account = await self._read_completed(
            implementations.curve_manager, pool.mint, curve_address
        )
        curve_quote = normalize_quote_mint(_pubkey(state.get("quote_mint")))
        if curve_quote != quote_mint:
            raise PoolNotTradable(  # noqa: TRY003
                f"the pool is quoted in {quote_mint}, the curve in {curve_quote}"
            )
        self._check_platform_config(pool.platform, state)

        metadata = decode_token_metadata(
            mint_account.data, metaplex_account.data if metaplex_account else None
        )
        name, symbol, uri = metadata or ("", "", "")
        creator = _pubkey(state.get("creator"))
        token_program = mint_account.owner
        token_info = TokenInfo(
            name=name,
            symbol=symbol or str(pool.mint)[:8],
            uri=uri,
            mint=pool.mint,
            platform=pool.platform,
            creator=creator,
            token_program_id=token_program,
            quote_mint=quote_mint,
            quote_token_program_id=cached_quote_token_program(quote_mint),
            creation_timestamp=pool.detection_timestamp or time(),
            additional_data={
                "entry": "migration",
                "pool": str(pool.pool),
                "migration_signature": pool.signature,
            },
        )
        if is_pump_fun:
            token_info.bonding_curve = curve_address
            token_info.associated_bonding_curve = (
                provider.derive_associated_bonding_curve(
                    pool.mint, curve_address, token_program
                )
            )
            if creator is not None:
                token_info.creator_vault = provider.derive_creator_vault(creator)
            token_info.is_mayhem_mode = state.get("is_mayhem_mode", False)
            token_info.is_cashback_coin = state.get("is_cashback_coin", False)
            token_info.is_holder_reward = state.get("is_holder_reward", False)
            token_info.creator_fee_bps = state.get("creator_fee_bps", 0)
        else:
            token_info.pool_state = curve_address
            token_info.base_vault = state.get("base_vault")
            token_info.quote_vault = state.get("quote_vault")
            token_info.global_config = state.get("global_config")
            token_info.platform_config = state.get("platform_config")
            # From the mint read that saw the mint, not the curve manager's own
            # read: a node behind the stream misses the mint there and the
            # state then reports no fee.
            token_info.transfer_fee_bps = (
                mint_transfer_fee(mint_account.data) or (0, 0)
            )[0]
        return token_info

    async def _read_completed(
        self, curve_manager: CurveManager, mint: Pubkey, curve_address: Pubkey
    ) -> tuple[dict, Account, Account | None]:
        """Curve state, mint and Metaplex account, once the curve shows as completed.

        All three are read at `processed` and re-read until the mint exists and
        the curve is completed, for up to `pool_wait_seconds`.

        Raises:
            PoolNotTradable: With `not_yet_visible`, when the wait ran out first.
        """
        deadline = monotonic() + self.pool_wait_seconds
        while True:
            try:
                (
                    mint_account,
                    metaplex_account,
                ) = await self.client.get_multiple_accounts(
                    [mint, metadata_address(mint)], commitment="processed"
                )
                state = None
                if mint_account is not None:
                    state = await curve_manager.get_pool_state(
                        curve_address, commitment="processed"
                    )
            except Exception as e:  # noqa: BLE001
                not_seen = PoolNotTradable(
                    f"curve or mint unreadable ({e})", not_yet_visible=True
                )
            else:
                if state is None:
                    not_seen = PoolNotTradable(
                        f"its mint was not seen on the last read within "
                        f"{self.pool_wait_seconds:.1f}s",
                        not_yet_visible=True,
                    )
                elif state.get("graduated"):
                    return state, mint_account, metaplex_account
                else:
                    not_seen = PoolNotTradable(
                        "its curve has not completed", not_yet_visible=True
                    )
            if monotonic() >= deadline:
                raise not_seen
            await asyncio.sleep(POOL_POLL_SECONDS)

    def _check_platform_config(self, platform: Platform, state: dict) -> None:
        """Refuse a LaunchLab curve under a platform config the bot does not trade.

        The report was matched on the platform config the migration names; the
        curve's own is the coin's launchpad. pump.fun has no platform configs.
        """
        if platform == Platform.PUMP_FUN or self.platform_configs is None:
            return
        curve_config = _pubkey(state.get("platform_config"))
        if curve_config not in self.platform_configs:
            raise PoolNotTradable(  # noqa: TRY003
                f"its curve is under platform config {curve_config}, "
                f"not one this bot trades"
            )

    async def wait_for_pool(self, token_info: TokenInfo) -> bool:
        """Poll the coin's pool until it is readable, for up to `pool_wait_seconds`.

        Returns:
            True once the venue the buyer will use can price it, False when
            the wait ran out
        """
        market = get_platform_implementations(
            token_info.platform, self.client
        ).graduated_market
        if market is None:
            logger.info(
                f"{token_info.platform.value} has no post-graduation market; "
                f"cannot buy {token_info.symbol}"
            )
            return False
        deadline = monotonic() + self.pool_wait_seconds
        while True:
            try:
                await market.get_market_state(token_info)
            except Exception as e:  # noqa: BLE001
                if monotonic() >= deadline:
                    logger.info(
                        f"Pool for {token_info.symbol} ({token_info.mint}) not "
                        f"readable after {self.pool_wait_seconds:.1f}s: {e}"
                    )
                    return False
                await asyncio.sleep(POOL_POLL_SECONDS)
            else:
                return True

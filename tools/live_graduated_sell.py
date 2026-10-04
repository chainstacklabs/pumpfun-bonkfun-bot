"""Price and sell a coin you already hold whose curve has graduated, via the bot's path.

WARNING: this submits a real transaction and sells real funds.

The bot only holds coins it bought at launch, and a curve rarely graduates while
it holds one, so that hand-off cannot be staged with the bot itself. This runs
the two pieces it would use on the coin you name: the trader's price read
(curve -> CurveGraduatedError -> the platform's GraduatedMarket) and
PlatformAwareSeller, which reads the curve, sees `graduated` and sells into the
AMM pool. Buy the coin first with a cookbook AMM buy script.

Usage:
    uv run tools/live_graduated_sell.py pump_fun <MINT>
    uv run tools/live_graduated_sell.py stonk_fun <MINT> --slippage 0.3
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solana.rpc.core import DataSliceOpts, MemcmpOpts  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import TOKEN_DECIMALS, resolve_quote_token_program  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402
from utils.logger import install_secret_redaction  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

PRIORITY_FEE = 1_000_000
DEFAULT_SLIPPAGE = 0.3
LAUNCHLAB_POOL_SIZE = 429
LAUNCHLAB_BASE_MINT_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8 + 2 * 32


async def token_info_for(
    platform: Platform, mint: Pubkey, client: SolanaClient, impl
) -> TokenInfo:
    """Build the TokenInfo the bot would hold, from the curve's own state."""
    mint_owner = (await client.get_account_info(mint)).owner
    if platform == Platform.PUMP_FUN:
        curve = impl.address_provider.derive_pool_address(mint)
        state = await impl.curve_manager.get_pool_state(curve)
        return TokenInfo(
            name="",
            symbol=str(mint)[:6],
            uri="",
            mint=mint,
            platform=platform,
            bonding_curve=curve,
            creator=Pubkey.from_string(str(state["creator"])),
            quote_mint=state["quote_mint"],
            token_program_id=mint_owner,
        )
    found = await (await client.get_client()).get_program_accounts(
        LAUNCHLAB_PROGRAM,
        encoding="base64",
        data_slice=DataSliceOpts(offset=0, length=0),
        filters=[
            LAUNCHLAB_POOL_SIZE,
            MemcmpOpts(offset=LAUNCHLAB_BASE_MINT_OFFSET, bytes=str(mint)),
        ],
    )
    pool = found.value[0].pubkey
    state = await impl.curve_manager.get_pool_state(pool)
    return TokenInfo(
        name="",
        symbol=str(mint)[:6],
        uri="",
        mint=mint,
        platform=platform,
        pool_state=pool,
        base_vault=state["base_vault"],
        quote_vault=state["quote_vault"],
        global_config=state["global_config"],
        platform_config=state["platform_config"],
        creator=state["creator"],
        quote_mint=state["quote_mint"],
        token_program_id=mint_owner,
    )


async def run(platform: Platform, mint: Pubkey, slippage: float) -> int:
    """Read the price the bot would read, then sell the whole balance."""
    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])
    try:
        impl = get_platform_implementations(platform, client)
        token_info = await token_info_for(platform, mint, client, impl)
        await resolve_quote_token_program(
            token_info.quote_mint, client.get_account_info
        )

        trader = SimpleNamespace(
            platform=platform,
            platform_implementations=impl,
            _graduated_mints=set(),
            _get_pool_address=lambda t: t.bonding_curve or t.pool_state,
        )
        price = await UniversalTrader._read_price(trader, token_info)
        print(f"Price via the trader: {price:.12f} quote per token")

        ata = wallet.get_associated_token_address(mint, token_info.token_program_id)
        balance = await client.get_token_account_balance(ata)
        print(f"Holding: {balance / 10**TOKEN_DECIMALS:,.6f} tokens")

        seller = PlatformAwareSeller(
            client,
            wallet,
            PriorityFeeManager(
                client=client,
                enable_dynamic_fee=False,
                enable_fixed_fee=True,
                fixed_fee=PRIORITY_FEE,
                extra_fee=0.0,
                hard_cap=PRIORITY_FEE,
            ),
            slippage=slippage,
            max_retries=2,
        )
        result = await seller.execute(token_info, balance / 10**TOKEN_DECIMALS, price)
        print(
            f"Sell: success={result.success} tx={result.tx_signature} error={result.error_message}"
        )
        return 0 if result.success else 1
    finally:
        await client.close()


def main() -> None:
    """Parse the command line and run."""
    parser = argparse.ArgumentParser(
        description="Sell a held, graduated coin via the bot's path"
    )
    parser.add_argument("platform", choices=[p.value for p in Platform])
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    args = parser.parse_args()
    install_secret_redaction()
    sys.exit(
        asyncio.run(
            run(Platform(args.platform), Pubkey.from_string(args.mint), args.slippage)
        )
    )


if __name__ == "__main__":
    main()

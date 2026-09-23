import statistics

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee import PriorityFeePlugin
from utils.logger import get_logger

logger = get_logger(__name__)


class DynamicPriorityFee(PriorityFeePlugin):
    """Dynamic priority fee plugin using getRecentPrioritizationFees."""

    def __init__(self, client: SolanaClient):
        """Initialize the dynamic fee plugin.

        Args:
            client: Solana RPC client for network requests.
        """
        self.client = client

    async def get_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """Fetch the recent priority fee using getRecentPrioritizationFees.

        Args:
            accounts: List of accounts to consider for the fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            Optional[int]: Median priority fee in microlamports, or None if the request fails.
        """
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getRecentPrioritizationFees",
                "params": [[str(account) for account in accounts]] if accounts else [],
            }

            response = await self.client.post_rpc(body)
            if not response or "result" not in response:
                logger.error(
                    "Failed to fetch recent prioritization fees: invalid response"
                )
                return None

            fees = [fee["prioritizationFee"] for fee in response["result"]]
            if not fees:
                logger.warning("No prioritization fees found in the response")
                return None

            # Get the 70th percentile of fees for faster processing
            # It means you're paying a fee that's higher than 70% of other transactions
            # Higher percentile = faster transactions but more expensive
            # Lower percentile = cheaper but slower transactions
            prior_fee = int(statistics.quantiles(fees, n=10)[-3])  # 70th percentile

            return prior_fee

        except Exception:
            logger.exception("Failed to fetch recent priority fee")
            return None

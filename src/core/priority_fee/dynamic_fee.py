import math

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee import PriorityFeePlugin
from utils.logger import get_logger

logger = get_logger(__name__)


class DynamicPriorityFee(PriorityFeePlugin):
    """Dynamic priority fee plugin using getRecentPrioritizationFees."""

    MAX_ACCOUNTS = 128
    MAX_U64 = (1 << 64) - 1

    def __init__(self, client: SolanaClient):
        """
        Initialize the dynamic fee plugin.

        Args:
            client: Solana RPC client for network requests.
        """
        if not callable(getattr(client, "post_rpc", None)):
            raise TypeError("client must provide an async post_rpc method")
        self.client = client

    async def get_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """
        Fetch the recent priority fee using getRecentPrioritizationFees.

        Args:
            accounts: List of accounts to consider for the fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            The nearest-rank 70th percentile in microlamports per compute unit,
            or ``None`` when the request or response is invalid.
        """
        self._validate_accounts(accounts)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [[str(account) for account in accounts]] if accounts else [],
        }

        try:
            response = await self.client.post_rpc(body)
        except Exception:
            logger.exception("Failed to fetch recent priority fee")
            return None

        if not isinstance(response, dict) or response.get("error") is not None:
            logger.error("Failed to fetch recent prioritization fees: invalid response")
            return None

        result = response.get("result")
        if not isinstance(result, list):
            logger.error("Failed to fetch recent prioritization fees: invalid result")
            return None
        if not result:
            logger.warning("No prioritization fees found in the response")
            return None

        fees: list[int] = []
        for entry in result:
            if not isinstance(entry, dict):
                logger.error("Rejected malformed prioritization fee entry")
                return None
            fee = entry.get("prioritizationFee")
            if (
                isinstance(fee, bool)
                or not isinstance(fee, int)
                or not 0 <= fee <= self.MAX_U64
            ):
                logger.error("Rejected invalid prioritization fee value")
                return None
            fees.append(fee)

        fees.sort()
        percentile_index = max(0, math.ceil(len(fees) * 0.70) - 1)
        return fees[percentile_index]

    @classmethod
    def _validate_accounts(cls, accounts: list[Pubkey] | None) -> None:
        if accounts is None:
            return
        if not isinstance(accounts, list):
            raise TypeError("accounts must be a list of Pubkey values or None")
        if len(accounts) > cls.MAX_ACCOUNTS:
            raise ValueError(
                f"accounts cannot contain more than {cls.MAX_ACCOUNTS} entries"
            )
        if any(not isinstance(account, Pubkey) for account in accounts):
            raise TypeError("accounts must contain only Pubkey values")

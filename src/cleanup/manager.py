import asyncio
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import burn, close_account
from spl.token.models import BurnParams, CloseAccountParams

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from utils.logger import get_logger

logger = get_logger(__name__)

# Token-2022 account layout: 165-byte base account, one account-type byte, then
# type-length-value extensions. TransferFeeAmount holds the fee withheld here.
_EXTENSIONS_OFFSET = 166
_TRANSFER_FEE_AMOUNT = 2
# TransferFeeExtension, then HarvestWithheldTokensToMint.
_HARVEST_WITHHELD_TO_MINT = bytes([26, 4])


def withheld_transfer_fee(account_data: bytes) -> int:
    """Raw transfer fee withheld in a Token-2022 token account, 0 if none.

    Token-2022 refuses to close an account while this is non-zero.
    """
    offset = _EXTENSIONS_OFFSET
    while offset + 4 <= len(account_data):
        kind, length = struct.unpack_from("<HH", account_data, offset)
        if kind == 0 and length == 0:
            break
        if kind == _TRANSFER_FEE_AMOUNT:
            return struct.unpack_from("<Q", account_data, offset + 4)[0]
        offset += 4 + length
    return 0


def harvest_withheld_to_mint(mint: Pubkey, token_account: Pubkey) -> Instruction:
    """Move the fee withheld in `token_account` into its mint. Needs no signer."""
    return Instruction(
        SystemAddresses.TOKEN_2022_PROGRAM,
        _HARVEST_WITHHELD_TO_MINT,
        [
            AccountMeta(mint, is_signer=False, is_writable=True),
            AccountMeta(token_account, is_signer=False, is_writable=True),
        ],
    )


class AccountCleanupManager:
    """Handles safe cleanup of token accounts (ATA) after trading sessions."""

    #: Seconds to let the RPC node catch up with a sell before reading accounts.
    SETTLE_SECONDS = 15.0
    #: Closes `cleanup_atas` keeps in flight at once.
    CONCURRENT_CLOSES = 4

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        use_priority_fee: bool = False,
        force_burn: bool = False,
    ):
        """Args:
        client: Solana RPC client
        wallet: Wallet for signing transactions
        """
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.use_priority_fee = use_priority_fee
        self.close_with_force_burn = force_burn

    async def cleanup_ata(
        self, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> None:
        """Wait for the RPC node to settle, then burn what is left if asked and close the ATA.

        Skips an account that doesn't exist, or holds tokens without force burn.

        Args:
            token_program_id: TOKEN or TOKEN_2022; None means TOKEN_2022
        """
        await self._settle()
        await self._close_ata(mint, token_program_id)

    async def cleanup_atas(
        self, mints: list[Pubkey], token_program_ids: list[Pubkey | None]
    ) -> None:
        """`cleanup_ata` for many accounts: one settle wait, then the closes side by side.

        The wait covers the last sell before cleanup, so one is enough however
        many accounts follow. At most `CONCURRENT_CLOSES` closes are in flight.
        """
        if not mints:
            return
        await self._settle()
        slots = asyncio.Semaphore(self.CONCURRENT_CLOSES)

        async def close(mint: Pubkey, token_program_id: Pubkey | None) -> None:
            async with slots:
                await self._close_ata(mint, token_program_id)

        await asyncio.gather(
            *(close(m, p) for m, p in zip(mints, token_program_ids, strict=True))
        )

    async def _settle(self) -> None:
        """Let the RPC node catch up with a sell that just landed.

        Read straight after it, the account can still show the sold tokens: the
        account is then skipped, or the burn of tokens already sold fails the
        close with it.
        """
        logger.info(
            f"Waiting for {self.SETTLE_SECONDS:g} seconds for RPC node to synchronize..."
        )
        await asyncio.sleep(self.SETTLE_SECONDS)

    async def _close_ata(self, mint: Pubkey, token_program_id: Pubkey | None) -> None:
        """Burn what is left if asked and close the ATA; never raises."""
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

        ata = self.wallet.get_associated_token_address(mint, token_program_id)

        try:
            priority_fee = (
                await self.priority_fee_manager.calculate_priority_fee([ata])
                if self.use_priority_fee
                else None
            )
            try:
                account = await self.client.get_account_info(ata)
            except ValueError:
                logger.info(f"ATA {ata} does not exist or already closed.")
                return

            balance = await self.client.get_token_account_balance(ata)
            instructions = []

            # Never burn wrapped SOL: closing a WSOL account already returns
            # both the wrapped lamports and the rent to the owner, so burning
            # first would destroy real value.
            if balance > 0 and mint == SystemAddresses.WSOL_MINT:
                logger.info(
                    f"Unwrapping {balance} lamports of wrapped SOL from {ata} "
                    f"by closing it (burn skipped)"
                )

            elif balance > 0 and self.close_with_force_burn:
                logger.info(
                    f"Burning {balance} tokens from ATA {ata} (mint: {mint})..."
                )
                burn_ix = burn(
                    BurnParams(
                        account=ata,
                        mint=mint,
                        owner=self.wallet.pubkey,
                        amount=balance,
                        program_id=token_program_id,
                    )
                )
                instructions.append(burn_ix)

            elif balance > 0:
                logger.info(
                    f"Skipping ATA {ata} with non-zero balance ({balance} tokens) "
                    f"because CLEANUP_FORCE_CLOSE_WITH_BURN is disabled."
                )
                return

            # A coin with a transfer fee withholds it inside the receiving
            # account, and Token-2022 will not close an account holding any.
            withheld = withheld_transfer_fee(bytes(account.data))
            if withheld and token_program_id == SystemAddresses.TOKEN_2022_PROGRAM:
                logger.info(f"Harvesting {withheld} withheld fee units from {ata}")
                instructions.append(harvest_withheld_to_mint(mint, ata))

            # Include close account instruction
            logger.info(f"Closing ATA: {ata}")
            close_ix = close_account(
                CloseAccountParams(
                    account=ata,
                    dest=self.wallet.pubkey,
                    owner=self.wallet.pubkey,
                    program_id=token_program_id,
                )
            )
            instructions.append(close_ix)

            # Send both burn and close instructions in the same transaction
            if instructions:
                tx_sig = await self.client.build_and_send_transaction(
                    instructions,
                    self.wallet.keypair,
                    skip_preflight=True,
                    priority_fee=priority_fee,
                )
                # confirm_transaction returns False when the transaction landed
                # but reverted. Logging success on that would report rent as
                # reclaimed while the account is still open.
                if await self.client.confirm_transaction(tx_sig):
                    logger.info(f"Closed successfully: {ata}")
                else:
                    logger.error(f"Failed to close ATA {ata}: {tx_sig}")

        except Exception as e:
            logger.warning(f"Cleanup failed for ATA {ata}: {e!s}")

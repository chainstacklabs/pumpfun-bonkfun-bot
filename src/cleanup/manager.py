import asyncio
import struct
from collections.abc import Awaitable, Callable
from functools import partial

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.signature import Signature
from spl.token.instructions import burn as burn_instruction
from spl.token.instructions import close_account
from spl.token.models import BurnParams, CloseAccountParams

from core.client import SolanaClient, TransactionNotSent
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from interfaces.core import ConfirmationStatus
from utils.logger import get_logger

logger = get_logger(__name__)

# Token-2022 account layout: 165-byte base account, one account-type byte, then
# type-length-value extensions. TransferFeeAmount holds the fee withheld here.
_EXTENSIONS_OFFSET = 166
_TRANSFER_FEE_AMOUNT = 2
# TransferFeeExtension, then HarvestWithheldTokensToMint.
_HARVEST_WITHHELD_TO_MINT = bytes([26, 4])
# SPL token account layout, shared by Token-2022's base account.
_TOKEN_ACCOUNT_SIZE = 165
_STATE_OFFSET = 108
_STATE_INITIALIZED = 1
_CLOSE_AUTHORITY_OFFSET = 129


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


def _closable_empty(data: bytes, owner: Pubkey) -> tuple[Pubkey, bool]:
    """A token account's mint, and whether `owner` can close it as it is.

    That is: it holds nothing, is not frozen, and has no close authority but
    `owner`.
    """
    mint = Pubkey.from_bytes(data[:32])
    if len(data) < _TOKEN_ACCOUNT_SIZE:
        return mint, False
    amount = struct.unpack_from("<Q", data, 64)[0]
    state = data[_STATE_OFFSET]
    has_close_authority = struct.unpack_from("<I", data, _CLOSE_AUTHORITY_OFFSET)[0]
    close_authority = Pubkey.from_bytes(
        data[_CLOSE_AUTHORITY_OFFSET + 4 : _CLOSE_AUTHORITY_OFFSET + 36]
    )
    return mint, (
        amount == 0
        and state == _STATE_INITIALIZED
        and Pubkey.from_bytes(data[32:64]) == owner
        and (not has_close_authority or close_authority == owner)
    )


class AccountCleanupManager:
    """Handles safe cleanup of token accounts (ATA) after trading sessions."""

    #: Seconds to let the RPC node catch up with a sell before reading accounts.
    SETTLE_SECONDS = 15.0
    #: Closes `cleanup_atas` keeps in flight at once.
    CONCURRENT_CLOSES = 4
    #: Times a close is sent before cleanup gives up on its account. Each waits
    #: until its blockhash has expired, so an earlier one can no longer land.
    CLOSE_ATTEMPTS = 3
    #: Seconds before sending a close again that the node refused to take.
    RESEND_DELAY = 2.0

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
        use_priority_fee: Send closes and burns with the bot's priority fee
        """
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.use_priority_fee = use_priority_fee
        self.close_with_force_burn = force_burn

    async def cleanup_ata(
        self,
        mint: Pubkey,
        token_program_id: Pubkey | None = None,
        *,
        settle: bool = True,
    ) -> None:
        """Wait for the RPC node to settle, then burn what is left if asked and close the ATA.

        Skips an account that doesn't exist, or holds tokens without force burn.

        Args:
            token_program_id: TOKEN or TOKEN_2022; None means TOKEN_2022
            settle: Wait first; only needed right after a sell
        """
        if settle:
            await self._settle()
        await self._close_ata(mint, token_program_id)

    async def cleanup_atas(
        self,
        mints: list[Pubkey],
        token_program_ids: list[Pubkey | None],
        *,
        sweep_except: set[Pubkey] | None = None,
    ) -> None:
        """`cleanup_ata` for many accounts: one settle wait, then the closes side by side.

        The wait covers the last sell before cleanup, so one is enough however
        many accounts follow. At most `CONCURRENT_CLOSES` closes are in flight.

        Args:
            sweep_except: Also close every other empty token account the wallet
                owns, under both token programs, except those of these mints.
                None closes only the accounts of `mints`.
        """
        closes = [
            (mint, partial(self._close_ata, mint, program))
            for mint, program in zip(mints, token_program_ids, strict=True)
        ]
        if mints:
            await self._settle()
        if sweep_except is not None:
            closes += [
                (mint, partial(self._close_swept, mint, account, program))
                for mint, account, program in await self._empty_accounts(
                    sweep_except | set(mints)
                )
            ]
        if not closes:
            return
        slots = asyncio.Semaphore(self.CONCURRENT_CLOSES)

        async def close(run: Callable[[], Awaitable[None]]) -> None:
            async with slots:
                await run()

        # Gathered with the exceptions, so an error in one close can't return
        # early with the others still running into the client closing.
        results = await asyncio.gather(
            *(close(run) for _, run in closes), return_exceptions=True
        )
        for (mint, _), result in zip(closes, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    f"Cleanup failed for the token account of {mint}: {result!s}"
                )

    async def _empty_accounts(
        self, skip: set[Pubkey]
    ) -> list[tuple[Pubkey, Pubkey, Pubkey]]:
        """The wallet's empty token accounts it can close, but those of `skip`.

        Returns:
            (mint, account, token program) of each, under SPL Token and Token-2022
        """
        found = []
        for program in (
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        ):
            try:
                accounts = await self.client.get_token_accounts(
                    self.wallet.pubkey, program
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Could not list the wallet's token accounts under {program}: {e!s}"
                )
                continue
            for account, data in accounts:
                mint, closable = _closable_empty(data, self.wallet.pubkey)
                if closable and mint not in skip:
                    found.append((mint, account, program))
        if found:
            logger.info(
                f"Closing {len(found)} other empty token account(s) in the wallet"
            )
        return found

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
        try:
            ata = self.wallet.get_associated_token_address(mint, token_program_id)
            await self._close(
                mint, ata, token_program_id, burn=self.close_with_force_burn
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Cleanup failed for the token account of {mint}: {e!s}")

    async def _close_swept(
        self, mint: Pubkey, account: Pubkey, token_program_id: Pubkey
    ) -> None:
        """Close an empty account the session did not trade; never burns or raises."""
        try:
            await self._close(mint, account, token_program_id, burn=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Cleanup failed for the token account of {mint}: {e!s}")

    async def _close(
        self, mint: Pubkey, account: Pubkey, token_program_id: Pubkey, *, burn: bool
    ) -> None:
        """Burn what is left if `burn` and close `account`, until it lands.

        A close not confirmed by the time its blockhash expires can no longer
        land, so it is sent again, up to `CLOSE_ATTEMPTS` times; each attempt
        reads the account first, so one already closed is left alone. A close
        that landed is never sent again.
        """
        for attempt in range(1, self.CLOSE_ATTEMPTS + 1):
            instructions = await self._close_instructions(
                mint, account, token_program_id, burn=burn
            )
            if instructions is None:
                return
            outcome, signature = await self._send(account, instructions)
            if outcome is ConfirmationStatus.SUCCESS:
                logger.info(f"Closed successfully: {account}")
                return
            if outcome is ConfirmationStatus.REVERTED:
                logger.error(f"Failed to close ATA {account}: {signature}")
                return
            if attempt < self.CLOSE_ATTEMPTS:
                logger.info(
                    f"Close of {account} was not confirmed; sending it again "
                    f"(attempt {attempt + 1}/{self.CLOSE_ATTEMPTS})"
                )
        logger.warning(
            f"Gave up closing token account {account} of {mint}: none of "
            f"{self.CLOSE_ATTEMPTS} attempts was confirmed. Reclaim its rent with "
            f"tools/cleanup_accounts.py {mint}"
        )

    async def _close_instructions(
        self, mint: Pubkey, account: Pubkey, token_program_id: Pubkey, *, burn: bool
    ) -> list | None:
        """The burn (if any) and close for `account`; None when there is nothing to do."""
        try:
            info = await self.client.get_account_info(account)
        except ValueError:
            logger.info(f"ATA {account} does not exist or already closed.")
            return None

        balance = await self.client.get_token_account_balance(account)
        instructions = []

        # Never burn wrapped SOL: closing a WSOL account already returns
        # both the wrapped lamports and the rent to the owner, so burning
        # first would destroy real value.
        if balance > 0 and mint == SystemAddresses.WSOL_MINT:
            logger.info(
                f"Unwrapping {balance} lamports of wrapped SOL from {account} "
                f"by closing it (burn skipped)"
            )

        elif balance > 0 and burn:
            logger.info(
                f"Burning {balance} tokens from ATA {account} (mint: {mint})..."
            )
            instructions.append(
                burn_instruction(
                    BurnParams(
                        account=account,
                        mint=mint,
                        owner=self.wallet.pubkey,
                        amount=balance,
                        program_id=token_program_id,
                    )
                )
            )

        elif balance > 0:
            logger.info(
                f"Skipping ATA {account} with non-zero balance ({balance} tokens) "
                f"because CLEANUP_FORCE_CLOSE_WITH_BURN is disabled."
            )
            return None

        # A coin with a transfer fee withholds it inside the receiving
        # account, and Token-2022 will not close an account holding any.
        withheld = withheld_transfer_fee(bytes(info.data))
        if withheld and token_program_id == SystemAddresses.TOKEN_2022_PROGRAM:
            logger.info(f"Harvesting {withheld} withheld fee units from {account}")
            instructions.append(harvest_withheld_to_mint(mint, account))

        logger.info(f"Closing ATA: {account}")
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=account,
                    dest=self.wallet.pubkey,
                    owner=self.wallet.pubkey,
                    program_id=token_program_id,
                )
            )
        )
        return instructions

    async def _send(
        self, account: Pubkey, instructions: list
    ) -> tuple[ConfirmationStatus | None, Signature | None]:
        """Send one close and wait until it is confirmed or can no longer land.

        Returns:
            The outcome, None or UNCONFIRMED when it did not land (yet); and
            the signature, None when nothing was sent
        """
        priority_fee = (
            await self.priority_fee_manager.calculate_priority_fee([account])
            if self.use_priority_fee
            else None
        )
        signed: dict = {}

        def on_signed(
            signature: Signature, last_valid_block_height: int | None
        ) -> None:
            signed["signature"] = signature
            signed["last_valid"] = last_valid_block_height

        try:
            signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=1,
                priority_fee=priority_fee,
                on_signed=on_signed,
                quiet=True,
            )
        except TransactionNotSent as e:
            logger.info(f"Close of {account} was not taken by the node: {e!s}")
            await asyncio.sleep(self.RESEND_DELAY)
            return None, None
        except Exception:
            # It may still have reached the network: wait for it like any other.
            if "signature" not in signed:
                raise
            signature = signed["signature"]
        # confirm_transaction is False when the transaction landed but
        # reverted. Logging success on that would report rent as reclaimed
        # while the account is still open.
        if await self.client.confirm_transaction(
            signature, last_valid_block_height=signed.get("last_valid"), quiet=True
        ):
            return ConfirmationStatus.SUCCESS, signature
        return await self._outcome(signature), signature

    async def _outcome(self, signature: Signature) -> ConfirmationStatus | None:
        """What became of a close the confirmation wait did not see land.

        The whole history is searched: a close that landed late, or was seen by
        another node, must not be sent again. One seen but not yet confirmed is
        looked at a few more times.
        """
        for _ in range(self.CLOSE_ATTEMPTS):
            try:
                outcome, slot = await self.client.get_signature_outcome(
                    signature, search_history=True
                )
            except Exception as e:  # noqa: BLE001
                logger.info(
                    f"Could not look up the close {str(signature)[:16]}...: {e}"
                )
                return ConfirmationStatus.UNCONFIRMED
            if outcome is not ConfirmationStatus.UNCONFIRMED or slot is None:
                return outcome
            await asyncio.sleep(self.RESEND_DELAY)
        return outcome

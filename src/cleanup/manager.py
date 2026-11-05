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
    #: Sends of one close, in total, before cleanup gives up on its account. A
    #: close is sent again only once the block height is past its blockhash's
    #: last valid height, so the one before can no longer land.
    CLOSE_ATTEMPTS = 3
    #: Seconds between looks at a close in flight, and before sending again
    #: one the node refused to take.
    RESEND_DELAY = 2.0
    #: Seconds a close is waited on for proof that it landed or expired; past
    #: it, cleanup stops without sending it again.
    MAX_WAIT = 180.0
    #: Expiry by the clock, for a close whose blockhash height is unknown.
    EXPIRY_SECONDS = 120.0
    #: Compute unit limit of a close transaction, so its priority fee is paid
    #: on what it can use, not the client's 85,000. Burn, harvest and close on
    #: a transfer-fee Token-2022 account, the most cleanup sends, uses about
    #: 5,600 with the budget instructions; the rest is headroom.
    COMPUTE_UNITS = 20_000

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
            await self._close(mint, account, token_program_id, burn=False, swept=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Cleanup failed for the token account of {mint}: {e!s}")

    async def _close(
        self,
        mint: Pubkey,
        account: Pubkey,
        token_program_id: Pubkey,
        *,
        burn: bool,
        swept: bool = False,
    ) -> None:
        """Burn what is left if `burn` and close `account`, until it lands.

        A close is sent again only once the block height is past its
        blockhash's last valid height and its signature is not known to have
        landed (no node knows it, or the lookup failed), at most
        `CLOSE_ATTEMPTS` sends in total. Before each resend
        the account is read again at "confirmed", from a node at least at the
        slot the expiry was seen at, so one already closed is left alone.

        Args:
            swept: Found by the post-session sweep, not traded: an account
                holding tokens is skipped as not empty
        """
        slot: int | None = None
        for attempt in range(1, self.CLOSE_ATTEMPTS + 1):
            instructions = await self._close_instructions(
                mint,
                account,
                token_program_id,
                burn=burn,
                swept=swept,
                resend=attempt > 1,
                min_context_slot=slot,
            )
            if instructions is None:
                return
            outcome, signature, slot = await self._send(account, instructions)
            if outcome is ConfirmationStatus.SUCCESS:
                logger.info(f"Closed successfully: {account}")
                return
            if outcome is ConfirmationStatus.REVERTED:
                logger.error(f"Failed to close token account {account}: {signature}")
                return
            if outcome is ConfirmationStatus.UNCONFIRMED:
                logger.warning(
                    f"Could not tell within {self.MAX_WAIT:g}s whether the close of "
                    f"{account} ({signature}) landed, so it is not sent again. If "
                    f"the account is still open, reclaim its rent with "
                    f"tools/cleanup_accounts.py {mint}"
                )
                return
            if attempt < self.CLOSE_ATTEMPTS:
                logger.info(
                    f"Close of {account} did not land; sending it again "
                    f"(attempt {attempt + 1} of {self.CLOSE_ATTEMPTS})"
                )
        logger.warning(
            f"Gave up closing token account {account} of {mint}: none of "
            f"{self.CLOSE_ATTEMPTS} attempts landed. Reclaim its rent with "
            f"tools/cleanup_accounts.py {mint}"
        )

    async def _close_instructions(  # noqa: PLR0913
        self,
        mint: Pubkey,
        account: Pubkey,
        token_program_id: Pubkey,
        *,
        burn: bool,
        swept: bool,
        resend: bool,
        min_context_slot: int | None,
    ) -> list | None:
        """The burn (if any) and close for `account`; None when there is nothing to do.

        Args:
            resend: Read the account at "confirmed", its balance from the same
                read, pinned to `min_context_slot` when given
        """
        label = "token account" if swept else "ATA"
        try:
            if resend:
                info = await self.client.get_account_info(
                    account, commitment="confirmed", min_context_slot=min_context_slot
                )
            else:
                info = await self.client.get_account_info(account)
        except ValueError:
            logger.info(f"{label} {account} does not exist or already closed.")
            return None

        if resend:
            # SPL token account: mint (32), owner (32), amount (u64).
            balance = struct.unpack_from("<Q", bytes(info.data), 64)[0]
        else:
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

        elif balance > 0 and swept:
            logger.info(
                f"Skipping token account {account} of {mint}: it now holds "
                f"{balance} tokens, and only empty accounts are swept"
            )
            return None

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

        logger.info(f"Closing {label}: {account}")
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
    ) -> tuple[ConfirmationStatus | None, Signature | None, int | None]:
        """Send one close and wait until it landed or provably cannot.

        Returns:
            The outcome: SUCCESS or REVERTED; None when it can be sent again
            (refused by the node, or expired and not known to have landed);
            UNCONFIRMED when that could not be told within `MAX_WAIT`. Then the
            signature (None when nothing was sent) and the slot the expiry was
            seen at (None when unknown)
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
                compute_unit_limit=self.COMPUTE_UNITS,
                on_signed=on_signed,
                quiet=True,
            )
        except TransactionNotSent as e:
            logger.info(f"Close of {account} was not taken by the node: {e!s}")
            await asyncio.sleep(self.RESEND_DELAY)
            return None, None, None
        except Exception as e:
            if "signature" not in signed:
                raise
            signature = signed["signature"]
            logger.warning(
                f"Sending the close of {account} failed ({e!s}); it may still "
                f"land, so it is waited on"
            )
        outcome, slot = await self._wait(signature, signed.get("last_valid"))
        return outcome, signature, slot

    async def _wait(
        self, signature: Signature, last_valid: int | None
    ) -> tuple[ConfirmationStatus | None, int | None]:
        """Wait until a close landed, reverted, or provably can no longer land.

        The confirmation wait can end early (a rate limit, a timeout), so an
        unconfirmed close counts as gone only when its signature is not known
        to have landed (no node knows it, or the lookup failed; one seen but
        unconfirmed is waited on) and the block height read after it is past
        `last_valid`. Without a height, it counts as gone `EXPIRY_SECONDS` after
        it was sent. Every wait, the confirmation wait included, ends by
        `MAX_WAIT`.

        Returns:
            SUCCESS, REVERTED, None (gone; with the slot the expiry was seen
            at, if read) or UNCONFIRMED (still unknown at `MAX_WAIT`)
        """
        loop = asyncio.get_running_loop()
        sent_at = loop.time()
        warned = False

        def on_error(error: Exception) -> None:
            # Said once per close; the same error every pass goes to debug.
            nonlocal warned
            line = f"Could not confirm the close {str(signature)[:16]}...: {error!s}"
            if warned:
                logger.debug(line)
            else:
                logger.warning(f"{line}; still waiting on it")
                warned = True

        while True:
            left = max(self.MAX_WAIT - (loop.time() - sent_at), 0.0)
            if await self._confirmed(signature, last_valid, left, on_error):
                return ConfirmationStatus.SUCCESS, None
            outcome, seen_at = await self._lookup(signature)
            if outcome in (ConfirmationStatus.SUCCESS, ConfirmationStatus.REVERTED):
                return outcome, None
            # Seen but not confirmed yet: it may still land, whatever the height.
            if outcome is not ConfirmationStatus.UNCONFIRMED or seen_at is None:
                expired, slot = await self._expired(last_valid, loop.time() - sent_at)
                if expired:
                    return None, slot
            if loop.time() - sent_at >= self.MAX_WAIT:
                return ConfirmationStatus.UNCONFIRMED, None
            await asyncio.sleep(self.RESEND_DELAY)

    async def _confirmed(
        self,
        signature: Signature,
        last_valid: int | None,
        seconds: float,
        on_error: Callable[[Exception], None],
    ) -> bool:
        """The confirmation wait, bounded by `seconds`.

        solana-py's wait has no timeout of its own when given a height: a node
        whose height stops moving would hold it forever. False also when the
        close landed but reverted; logging success on that would report rent
        as reclaimed while the account is still open.
        """
        try:
            return await asyncio.wait_for(
                self.client.confirm_transaction(
                    signature,
                    last_valid_block_height=last_valid,
                    quiet=True,
                    on_error=on_error,
                ),
                seconds,
            )
        except TimeoutError:
            return False

    async def _lookup(
        self, signature: Signature
    ) -> tuple[ConfirmationStatus | None, int | None]:
        """The signature's status and slot, UNCONFIRMED and None when unreadable.

        The whole history is searched: a close that landed late, or was seen
        by another node, must not be sent again.
        """
        try:
            return await self.client.get_signature_outcome(
                signature, search_history=True, quiet=True
            )
        except Exception:  # noqa: BLE001
            return ConfirmationStatus.UNCONFIRMED, None

    async def _expired(
        self, last_valid: int | None, waited: float
    ) -> tuple[bool, int | None]:
        """Whether a close can no longer land, and the slot that was seen at."""
        if last_valid is None:
            return waited >= self.EXPIRY_SECONDS, None
        try:
            height, slot = await self.client.get_block_height_and_slot()
        except Exception:  # noqa: BLE001
            return False, None
        return height > last_valid, slot

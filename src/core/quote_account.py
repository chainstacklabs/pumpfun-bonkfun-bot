"""The token account a trade's quote side settles through."""

import secrets

from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import (
    CreateAccountWithSeedParams,
    TransferParams,
    create_account_with_seed,
    transfer,
)
from spl.token.instructions import (
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_account,
    sync_native,
)
from spl.token.models import (
    CloseAccountParams,
    InitializeAccountParams,
    SyncNativeParams,
)

from core.pubkeys import (
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
    TOKEN_ACCOUNT_SIZE,
    WSOL_MINT,
    SystemAddresses,
)


def quote_settlement_account(
    user: Pubkey, quote_mint: Pubkey, quote_token_program: Pubkey, funding: int = 0
) -> tuple[Pubkey, list[Instruction], list[Instruction]]:
    """The account to pay from or into, with its setup and teardown instructions.

    Wrapped SOL settles through a throwaway account at a fresh seed, funded with
    `funding` lamports and closed in the same transaction, so proceeds arrive as
    SOL and any WSOL the wallet already holds is left alone. Any other quote
    uses the wallet's ATA, created if missing; buying with it needs the balance
    already there.

    Args:
        funding: Lamports to wrap, for a buy; 0 for a sell

    Returns:
        (account, instructions before the trade, instructions after it)
    """
    if quote_mint != WSOL_MINT:
        create_ata = create_idempotent_associated_token_account(
            user, user, quote_mint, quote_token_program
        )
        ata = get_associated_token_address(user, quote_mint, quote_token_program)
        return ata, [create_ata], []

    seed = secrets.token_hex(16)
    wsol_account = Pubkey.create_with_seed(user, seed, SystemAddresses.TOKEN_PROGRAM)
    open_ixs = [
        create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=user,
                to_pubkey=wsol_account,
                base=user,
                seed=seed,
                lamports=funding + TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
                space=TOKEN_ACCOUNT_SIZE,
                owner=SystemAddresses.TOKEN_PROGRAM,
            )
        ),
        initialize_account(
            InitializeAccountParams(
                program_id=SystemAddresses.TOKEN_PROGRAM,
                account=wsol_account,
                mint=WSOL_MINT,
                owner=user,
            )
        ),
    ]
    close_ixs = [
        close_account(
            CloseAccountParams(
                program_id=SystemAddresses.TOKEN_PROGRAM,
                account=wsol_account,
                dest=user,
                owner=user,
            )
        )
    ]
    return wsol_account, open_ixs, close_ixs


def wrapped_sol_ata(
    user: Pubkey, funding: int
) -> tuple[Pubkey, list[Instruction], list[Instruction]]:
    """Pay `funding` lamports as wrapped SOL from the wallet's own WSOL ATA.

    The compact alternative to `quote_settlement_account`'s throwaway account,
    for a trade whose account list leaves no room for it in one transaction:
    the ATA's instructions carry no seed and no owner key. The ATA is closed
    after the trade, so any WSOL the wallet already held in it comes back as SOL
    too.

    Returns:
        (account, instructions before the trade, instructions after it)
    """
    program = SystemAddresses.TOKEN_PROGRAM
    ata = get_associated_token_address(user, WSOL_MINT, program)
    open_ixs = [
        create_idempotent_associated_token_account(user, user, WSOL_MINT, program),
        transfer(TransferParams(from_pubkey=user, to_pubkey=ata, lamports=funding)),
        sync_native(SyncNativeParams(program_id=program, account=ata)),
    ]
    close_ixs = [
        close_account(
            CloseAccountParams(program_id=program, account=ata, dest=user, owner=user)
        )
    ]
    return ata, open_ixs, close_ixs

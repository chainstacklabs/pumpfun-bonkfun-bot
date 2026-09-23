"""Create a pump.fun coin with create_v2 and buy it, in two transactions.

WARNING: this submits real transactions and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_create_and_buy_token_v2.py

Edit the constants below to change the coin's name, ticker, buy amount and
whether it opts into mayhem mode or holder rewards.

It is two transactions rather than one because `buy_v2` takes 27 accounts, which
pushes a combined create+buy message past Solana's 1232-byte packet limit
(measured at 1972 bytes). The legacy 18-account `buy` used to fit; making it
atomic again would need an address lookup table.

`pumpfun_create_token_v2.py` is the create half on its own, and `pumpfun_buy_token_v2.py` the buy half.
"""

import argparse
import asyncio
import os
import struct
import sys
from pathlib import Path
from typing import Final

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions_v2.py sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions_v2 as pump_v2
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import TxOptsModel
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Configuration for the token to be created
DEFAULT_TOKEN_NAME = "Test Token V2"
DEFAULT_TOKEN_SYMBOL = "TEST2"
DEFAULT_TOKEN_URI = "https://example.com/token-v2.json"
DEFAULT_BUY_AMOUNT_SOL = 0.0001  # Amount of SOL to spend on buying
DEFAULT_SLIPPAGE = 0.3  # 30% slippage
PRIORITY_FEE_MICROLAMPORTS = 37_037  # Priority fee in microlamports
COMPUTE_UNIT_LIMIT = 350_000  # Compute unit limit for the transaction
DEFAULT_MAYHEM = True  # Set to True to enable mayhem mode
# Set to True to create a holder-reward coin: the creator fee is set aside for
# holders instead of paid to a creator wallet. Cashback was deprecated
# 2026-09-15 — create_v2 now rejects is_cashback_enabled=[true] with error
# 6082 (CashbackDeprecated) — so this is the only trailing-args path left.
DEFAULT_HOLDER_REWARD = False

load_dotenv()

# Global constants from existing codebase
PUMP_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMP_GLOBAL: Final[Pubkey] = Pubkey.from_string(
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
)
PUMP_EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE: Final[Pubkey] = Pubkey.from_string(
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
)
PUMP_FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
)

PUMP_MINT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
)

# Token2022 and Mayhem constants
TOKEN_2022_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
MAYHEM_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
)
GLOBAL_PARAMS: Final[Pubkey] = Pubkey.from_string(
    "13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ"
)
SOL_VAULT: Final[Pubkey] = Pubkey.from_string(
    "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
)

SYSTEM_PROGRAM: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6

# Discriminators
CREATE_V2_DISCRIMINATOR: Final[bytes] = bytes([214, 144, 76, 236, 95, 139, 49, 180])
EXTEND_ACCOUNT_DISCRIMINATOR: Final[bytes] = bytes(
    [234, 102, 194, 203, 150, 72, 62, 229]
)

# From environment
RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")


def find_bonding_curve_address(mint: Pubkey) -> tuple[Pubkey, int]:
    """Find the bonding curve PDA for a mint."""
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)


def find_associated_bonding_curve(mint: Pubkey, bonding_curve: Pubkey) -> Pubkey:
    """Find the associated bonding curve token account."""
    derived_address, _ = Pubkey.find_program_address(
        [
            bytes(bonding_curve),
            bytes(TOKEN_2022_PROGRAM),
            bytes(mint),
        ],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def find_creator_vault(creator: Pubkey) -> Pubkey:
    """Find the creator vault PDA."""
    derived_address, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)],
        PUMP_PROGRAM,
    )
    return derived_address


def find_mayhem_state(mint: Pubkey) -> Pubkey:
    """Find the mayhem state PDA for a mint.

    Seeds: ["mayhem-state", mint] (note: hyphen, not underscore)
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"mayhem-state", bytes(mint)],
        MAYHEM_PROGRAM_ID,
    )
    return derived_address


def find_mayhem_token_vault(mint: Pubkey) -> Pubkey:
    """Find the mayhem token vault - this is an ATA for sol_vault.

    This is derived as an Associated Token Account with:
    - Owner: SOL_VAULT
    - Mint: mint
    - Token Program: TOKEN_2022_PROGRAM
    """
    return get_associated_token_address(SOL_VAULT, mint, TOKEN_2022_PROGRAM)


def create_pump_create_v2_instruction(
    mint: Pubkey,
    mint_authority: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    global_state: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    is_mayhem_mode: bool = False,
    is_holder_reward: bool = False,
) -> Instruction:
    """Create the pump.fun create_v2 instruction for Token2022.

    Account order matches pump_fun_idl.json create_v2 instruction.

    Args:
        is_holder_reward: Sets the trailing is_holder_reward arg so the
            creator fee is set aside for holders instead of a creator wallet.
            Reaching it on the wire requires also sending is_cashback_enabled
            and creator_fee_bps (see the data-building comment below) — the
            three trailing args are positional, not independently addressable.
            Cashback itself was deprecated 2026-09-15 (create_v2 error 6082),
            so is_cashback_enabled is always sent False here.
    """
    accounts = [
        AccountMeta(pubkey=mint, is_signer=True, is_writable=True),
        AccountMeta(pubkey=mint_authority, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=global_state, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_2022_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
    ]

    # The mayhem accounts are mandatory, not conditional: the IDL marks none of
    # create_v2's 16 accounts optional, and sending only 11 fails with
    # AnchorError 3005 (AccountNotEnoughKeys) on sol_vault whatever the
    # is_mayhem_mode argument says. Verified by simulateTransaction against
    # mainnet, 2026-09-22. They must come before event_authority and program.
    mayhem_state = find_mayhem_state(mint)
    mayhem_token_vault = find_mayhem_token_vault(mint)

    accounts.extend(
        [
            AccountMeta(pubkey=MAYHEM_PROGRAM_ID, is_signer=False, is_writable=True),
            AccountMeta(pubkey=GLOBAL_PARAMS, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SOL_VAULT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mayhem_state, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mayhem_token_vault, is_signer=False, is_writable=True),
        ]
    )

    # Event authority and program come last
    accounts.extend(
        [
            AccountMeta(
                pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
        ]
    )

    # Encode string as length-prefixed
    def encode_string(s: str) -> bytes:
        encoded = s.encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded

    def encode_pubkey(pubkey: Pubkey) -> bytes:
        return bytes(pubkey)

    data = (
        CREATE_V2_DISCRIMINATOR
        + encode_string(name)
        + encode_string(symbol)
        + encode_string(uri)
        + encode_pubkey(creator)
        + struct.pack("<?", is_mayhem_mode)  # is_mayhem_mode (plain bool)
    )

    if is_holder_reward:
        # Trailing args are positional and independently omittable — the
        # program does not require any of them, but sending is_holder_reward
        # means sending is_cashback_enabled and creator_fee_bps first.
        # idl/pump_fun_idl.json's `types` entries for both OptionBool and
        # OptionU64 are single-field structs with no presence tag, so each
        # serializes as its bare inner value: OptionBool as one byte, OptionU64
        # as a little-endian u64 — never a bool-then-value pair. Confirmed by
        # decoding live post-upgrade create_v2 instructions through this
        # repo's IDLParser (2026-09-15): is_cashback_enabled decoded as
        # {'field_0': False}, creator_fee_bps as {'field_0': 0}, matching this
        # packing byte-for-byte.
        # is_cashback_enabled (OptionBool, bare bool): always False here.
        # Cashback is deprecated as of the 2026-09-15 upgrade; create_v2
        # rejects [true] with 6082 CashbackDeprecated.
        is_cashback_enabled = False
        # creator_fee_bps (OptionU64, bare u64): unused for holder-reward
        # coins created here, so 0.
        creator_fee_bps = 0
        data += (
            struct.pack("<?", is_cashback_enabled)
            + struct.pack("<Q", creator_fee_bps)
            + struct.pack("<?", is_holder_reward)
        )

    return Instruction(PUMP_PROGRAM, data, accounts)


def create_extend_account_instruction(
    bonding_curve: Pubkey,
    user: Pubkey,
) -> Instruction:
    """Create the extend_account instruction to expand bonding curve account size."""
    accounts = [
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]

    # No arguments for extend_account instruction
    data = EXTEND_ACCOUNT_DISCRIMINATOR

    return Instruction(PUMP_PROGRAM, data, accounts)


def create_buy_instruction(
    global_state: Pubkey,
    fee_recipient: Pubkey,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    associated_user: Pubkey,
    user: Pubkey,
    creator_vault: Pubkey,
    token_amount: int,
    max_sol_cost: int,
    track_volume: bool = True,
    is_mayhem_mode: bool = False,
) -> Instruction:
    """Create the buy instruction (buy_v2).

    Signature is unchanged for the caller, but this now builds `buy_v2` with its
    27 mandatory accounts. `global_state`, `fee_recipient`,
    `associated_bonding_curve`, `associated_user`, `creator_vault` and
    `track_volume` are accepted for backwards compatibility and derived or
    dropped internally — buy_v2 has no track_volume argument, and pump_v2 picks
    the fee recipient from the documented set.

    These scripts mint the coin themselves with `creator = payer`, so the buyer
    is also the creator.

    Args:
        global_state: Unused; pump_v2 uses the canonical global PDA
        fee_recipient: Unused; pump_v2 selects from the documented set
        mint: Base token mint just created
        bonding_curve: Unused; derived from the mint
        associated_bonding_curve: Unused; derived
        associated_user: Unused; derived
        user: Buyer, and the coin's creator in these scripts
        creator_vault: Unused; derived from the creator
        token_amount: Base tokens to buy, raw units
        max_sol_cost: Spend cap in lamports
        track_volume: Ignored; volume tracking is unconditional under buy_v2
        is_mayhem_mode: Mayhem coins must use a *reserved* fee recipient; passing
            this wrong makes the program reject the buy with NotAuthorized (6000)

    Returns:
        The buy_v2 instruction
    """
    return pump_v2.build_buy_v2_instruction(
        base_mint=mint,
        creator=user,
        user=user,
        token_amount_raw=token_amount,
        max_quote_cost_raw=max_sol_cost,
        quote_mint=pump_v2.WSOL_MINT,
        is_mayhem_mode=is_mayhem_mode,
        base_token_program=TOKEN_2022_PROGRAM,
    )


async def get_fee_recipient_for_mayhem(client: AsyncClient, is_mayhem: bool) -> Pubkey:
    """Get the appropriate fee recipient based on mayhem mode.

    For mayhem tokens, we need to use reserved_fee_recipient from Global account.
    For standard tokens, we use the standard PUMP_FEE.
    """
    if not is_mayhem:
        return PUMP_FEE

    # Fetch Global account to get reserved_fee_recipient for mayhem mode
    response = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
    if not response.value or not response.value.data:
        print("Warning: Could not fetch Global account, using standard fee recipient")
        return PUMP_FEE

    data = response.value.data

    # Parse reserved_fee_recipient from Global account at offset 483
    RESERVED_FEE_RECIPIENT_OFFSET = 483

    if len(data) < RESERVED_FEE_RECIPIENT_OFFSET + 32:
        print("Warning: Global account data too short, using standard fee recipient")
        return PUMP_FEE

    reserved_fee_recipient_bytes = data[
        RESERVED_FEE_RECIPIENT_OFFSET : RESERVED_FEE_RECIPIENT_OFFSET + 32
    ]
    reserved_fee_recipient = Pubkey.from_bytes(reserved_fee_recipient_bytes)

    print(f"Using mayhem mode fee recipient: {reserved_fee_recipient}")
    return reserved_fee_recipient


async def run(  # noqa: PLR0913
    name: str,
    symbol: str,
    uri: str,
    buy_amount: float,
    slippage: float,
    *,
    mayhem: bool,
    holder_reward: bool,
):
    """Create and buy pump.fun token (Token2022) in a single transaction."""
    private_key_bytes = base58.b58decode(PRIVATE_KEY)
    payer = Keypair.from_bytes(private_key_bytes)
    mint_keypair = Keypair()

    print("Creating Token2022 token with:")
    print(f"  Name: {name}")
    print(f"  Symbol: {symbol}")
    print(f"  Mint: {mint_keypair.pubkey()}")
    print(f"  Creator: {payer.pubkey()}")
    print(f"  Mayhem mode: {'Enabled' if mayhem else 'Disabled'}")
    print(f"  Holder reward: {'Enabled' if holder_reward else 'Disabled'}")

    # Derive PDAs
    bonding_curve, _ = find_bonding_curve_address(mint_keypair.pubkey())
    associated_bonding_curve = find_associated_bonding_curve(
        mint_keypair.pubkey(), bonding_curve
    )
    user_ata = get_associated_token_address(
        payer.pubkey(), mint_keypair.pubkey(), TOKEN_2022_PROGRAM
    )
    creator_vault = find_creator_vault(payer.pubkey())

    print("\nDerived addresses:")
    print(f"  Bonding curve: {bonding_curve}")
    print(f"  Associated bonding curve: {associated_bonding_curve}")
    print(f"  User ATA: {user_ata}")
    print(f"  Creator vault: {creator_vault}")

    if mayhem:
        mayhem_state = find_mayhem_state(mint_keypair.pubkey())
        mayhem_token_vault = find_mayhem_token_vault(mint_keypair.pubkey())
        print(f"  Mayhem state: {mayhem_state}")
        print(f"  Mayhem token vault: {mayhem_token_vault}")

    # Calculate buy parameters
    # For pump.fun, we need to calculate expected tokens based on initial curve state
    # Initial virtual reserves (from pump.fun constants)
    initial_virtual_token_reserves = 1_073_000_000 * 10**TOKEN_DECIMALS
    initial_virtual_sol_reserves = 30 * LAMPORTS_PER_SOL

    initial_price = initial_virtual_sol_reserves / initial_virtual_token_reserves

    buy_amount_lamports = int(buy_amount * LAMPORTS_PER_SOL)
    expected_tokens = int(
        (buy_amount_lamports * 0.99) / initial_price
    )  # 1% buffer for fees
    max_sol_cost = int(buy_amount_lamports * (1 + slippage))

    print("\nBuy parameters:")
    print(f"  Buy amount: {buy_amount} SOL")
    print(f"  Expected tokens: {expected_tokens / 10**TOKEN_DECIMALS:.6f}")
    print(f"  Max SOL cost: {max_sol_cost / LAMPORTS_PER_SOL:.6f} SOL")

    # Send transaction
    async with AsyncClient(RPC_ENDPOINT) as client:
        # Get correct fee recipient based on mayhem mode
        fee_recipient = await get_fee_recipient_for_mayhem(client, mayhem)

        instructions = [
            # Priority fee instructions
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            # Create token with pump.fun create_v2 (Token2022)
            create_pump_create_v2_instruction(
                mint=mint_keypair.pubkey(),
                mint_authority=PUMP_MINT_AUTHORITY,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                global_state=PUMP_GLOBAL,
                user=payer.pubkey(),
                creator=payer.pubkey(),
                name=name,
                symbol=symbol,
                uri=uri,
                is_mayhem_mode=mayhem,
                is_holder_reward=holder_reward,
            ),
            # Extend bonding curve account (required for frontend visibility)
            create_extend_account_instruction(
                bonding_curve=bonding_curve,
                user=payer.pubkey(),
            ),
        ]

        # buy_v2's 27 accounts push a combined create+buy message past Solana's
        # 1232-byte packet limit (measured 1972 bytes), so the buy goes in a
        # second transaction. The legacy 18-account buy used to fit; recovering
        # atomicity would need an address lookup table.
        buy_instructions = [
            create_idempotent_associated_token_account(
                payer.pubkey(),
                payer.pubkey(),
                mint_keypair.pubkey(),
                TOKEN_2022_PROGRAM,
            ),
            create_buy_instruction(
                global_state=PUMP_GLOBAL,
                fee_recipient=fee_recipient,
                mint=mint_keypair.pubkey(),
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                associated_user=user_ata,
                user=payer.pubkey(),
                creator_vault=creator_vault,
                token_amount=expected_tokens,
                max_sol_cost=max_sol_cost,
                track_volume=True,
                is_mayhem_mode=mayhem,
            ),
        ]

        recent_blockhash = await client.get_latest_blockhash()
        message = MessageV0.try_compile(
            payer.pubkey(), instructions, [], recent_blockhash.value.blockhash
        )
        transaction = VersionedTransaction(message, [payer, mint_keypair])

        print("\nSending create transaction...")
        opts = TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed)

        try:
            response = await client.send_transaction(transaction, opts)
            tx_hash = response.value
            print(f"Create sent: https://solscan.io/tx/{tx_hash}")
            print("Waiting for confirmation...")
            await client.confirm_transaction(tx_hash, commitment=Confirmed)
            await tx_status.assert_transaction_succeeded(client, tx_hash)
            print("Create confirmed!")

            buy_blockhash = await client.get_latest_blockhash()
            buy_tx = VersionedTransaction(
                MessageV0.try_compile(
                    payer.pubkey(), buy_instructions, [], buy_blockhash.value.blockhash
                ),
                [payer],
            )
            print("\nSending buy transaction (buy_v2)...")
            buy_response = await client.send_transaction(buy_tx, opts)
            buy_hash = buy_response.value
            print(f"Buy sent: https://solscan.io/tx/{buy_hash}")
            await client.confirm_transaction(buy_hash, commitment=Confirmed)
            await tx_status.assert_transaction_succeeded(client, buy_hash)
            print("Buy confirmed!")

            return tx_hash

        except Exception as e:
            print(f"Transaction failed: {e}")
            raise


def main() -> None:
    """Parse the command line and run the create-and-buy."""
    parser = argparse.ArgumentParser(description="Create a pump.fun coin with create_v2 and buy it")
    parser.add_argument("--name", default=DEFAULT_TOKEN_NAME, help="Coin name")
    parser.add_argument("--symbol", default=DEFAULT_TOKEN_SYMBOL, help="Coin ticker")
    parser.add_argument("--uri", default=DEFAULT_TOKEN_URI, help="Metadata URI")
    parser.add_argument(
        "--amount",
        type=float,
        default=DEFAULT_BUY_AMOUNT_SOL,
        help=f"SOL to spend on the buy (default {DEFAULT_BUY_AMOUNT_SOL})",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Slippage tolerance (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--mayhem",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAYHEM,
        help=f"Enable mayhem mode (default {DEFAULT_MAYHEM})",
    )
    parser.add_argument(
        "--holder-reward",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_HOLDER_REWARD,
        help=f"Set the creator fee aside for holders (default {DEFAULT_HOLDER_REWARD})",
    )
    args = parser.parse_args()

    asyncio.run(
        run(
            args.name,
            args.symbol,
            args.uri,
            args.amount,
            args.slippage,
            mayhem=args.mayhem,
            holder_reward=args.holder_reward,
        )
    )


if __name__ == "__main__":
    main()

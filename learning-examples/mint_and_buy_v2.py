import asyncio
import os
import struct
from typing import Final

import base58
import pump_v2
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Configuration for the token to be created
TOKEN_NAME = "Test Token V2"
TOKEN_SYMBOL = "TEST2"
TOKEN_URI = "https://example.com/token-v2.json"
BUY_AMOUNT_SOL = 0.0001  # Amount of SOL to spend on buying
MAX_SLIPPAGE = 0.3  # 30% slippage
PRIORITY_FEE_MICROLAMPORTS = 37_037  # Priority fee in microlamports
COMPUTE_UNIT_LIMIT = 350_000  # Compute unit limit for the transaction
ENABLE_MAYHEM_MODE = True  # Set to True to enable mayhem mode

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

# 8 breaking-upgrade fee recipients (pump.fun program upgrade 2026-04-28).
# One must be appended (mutable) AFTER bonding-curve-v2 on every buy/sell.
# Doc: github.com/pump-fun/pump-public-docs/blob/main/docs/BREAKING_FEE_RECIPIENT.md
BREAKING_FEE_RECIPIENTS: Final[list[Pubkey]] = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]
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
BUY_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 16927863322537952870)
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


def _find_global_volume_accumulator() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"],
        PUMP_PROGRAM,
    )
    return derived_address


def _find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMP_PROGRAM,
    )
    return derived_address


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


def _find_bonding_curve_v2(mint: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"bonding-curve-v2", bytes(mint)],
        PUMP_PROGRAM,
    )
    return derived_address


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
) -> Instruction:
    """Create the pump.fun create_v2 instruction for Token2022.

    Account order matches pump_fun_idl.json create_v2 instruction.
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

    # Add mayhem accounts if enabled (must come before event_authority and program)
    if is_mayhem_mode:
        mayhem_state = find_mayhem_state(mint)
        mayhem_token_vault = find_mayhem_token_vault(mint)

        accounts.extend(
            [
                AccountMeta(
                    pubkey=MAYHEM_PROGRAM_ID, is_signer=False, is_writable=True
                ),
                AccountMeta(pubkey=GLOBAL_PARAMS, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SOL_VAULT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mayhem_state, is_signer=False, is_writable=True),
                AccountMeta(
                    pubkey=mayhem_token_vault, is_signer=False, is_writable=True
                ),
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
        + struct.pack("<?", is_mayhem_mode)  # OptionBool for is_mayhem_mode
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


async def assert_transaction_succeeded(client: AsyncClient, signature) -> None:
    """Raise if a confirmed transaction actually failed on-chain.

    `confirm_transaction` only waits for the transaction to land — a landed
    transaction can still have reverted. Without this check a failed buy prints
    as a success, which is exactly how a wrong fee recipient (NotAuthorized,
    6000) can look like a passing test.

    Args:
        client: Solana RPC client
        signature: Transaction signature to inspect

    Raises:
        RuntimeError: If the transaction reverted
    """
    result = await client.get_transaction(
        signature, commitment="confirmed", max_supported_transaction_version=0
    )
    value = result.value
    if value is None:
        raise RuntimeError(f"Transaction {signature} not found after confirmation")
    err = value.transaction.meta.err if value.transaction.meta else None
    if err:
        raise RuntimeError(f"Transaction {signature} landed but failed on-chain: {err}")


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


async def main():
    """Create and buy pump.fun token (Token2022) in a single transaction."""
    private_key_bytes = base58.b58decode(PRIVATE_KEY)
    payer = Keypair.from_bytes(private_key_bytes)
    mint_keypair = Keypair()

    print("Creating Token2022 token with:")
    print(f"  Name: {TOKEN_NAME}")
    print(f"  Symbol: {TOKEN_SYMBOL}")
    print(f"  Mint: {mint_keypair.pubkey()}")
    print(f"  Creator: {payer.pubkey()}")
    print(f"  Mayhem mode: {'Enabled' if ENABLE_MAYHEM_MODE else 'Disabled'}")

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

    if ENABLE_MAYHEM_MODE:
        mayhem_state = find_mayhem_state(mint_keypair.pubkey())
        mayhem_token_vault = find_mayhem_token_vault(mint_keypair.pubkey())
        print(f"  Mayhem state: {mayhem_state}")
        print(f"  Mayhem token vault: {mayhem_token_vault}")

    # Calculate buy parameters
    # For pump.fun, we need to calculate expected tokens based on initial curve state
    # Initial virtual reserves (from pump.fun constants)
    initial_virtual_token_reserves = 1_073_000_000 * 10**TOKEN_DECIMALS
    initial_virtual_sol_reserves = 30 * LAMPORTS_PER_SOL
    initial_real_token_reserves = 793_100_000 * 10**TOKEN_DECIMALS

    initial_price = initial_virtual_sol_reserves / initial_virtual_token_reserves

    buy_amount_lamports = int(BUY_AMOUNT_SOL * LAMPORTS_PER_SOL)
    expected_tokens = int(
        (buy_amount_lamports * 0.99) / initial_price
    )  # 1% buffer for fees
    max_sol_cost = int(buy_amount_lamports * (1 + MAX_SLIPPAGE))

    print("\nBuy parameters:")
    print(f"  Buy amount: {BUY_AMOUNT_SOL} SOL")
    print(f"  Expected tokens: {expected_tokens / 10**TOKEN_DECIMALS:.6f}")
    print(f"  Max SOL cost: {max_sol_cost / LAMPORTS_PER_SOL:.6f} SOL")

    # Send transaction
    async with AsyncClient(RPC_ENDPOINT) as client:
        # Get correct fee recipient based on mayhem mode
        fee_recipient = await get_fee_recipient_for_mayhem(client, ENABLE_MAYHEM_MODE)

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
                name=TOKEN_NAME,
                symbol=TOKEN_SYMBOL,
                uri=TOKEN_URI,
                is_mayhem_mode=ENABLE_MAYHEM_MODE,
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
                is_mayhem_mode=ENABLE_MAYHEM_MODE,
            ),
        ]

        recent_blockhash = await client.get_latest_blockhash()
        message = Message(instructions, payer.pubkey())
        transaction = Transaction(
            [payer, mint_keypair], message, recent_blockhash.value.blockhash
        )

        print("\nSending create transaction...")
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)

        try:
            response = await client.send_transaction(transaction, opts)
            tx_hash = response.value
            print(f"Create sent: https://solscan.io/tx/{tx_hash}")
            print("Waiting for confirmation...")
            await client.confirm_transaction(tx_hash, commitment="confirmed")
            await assert_transaction_succeeded(client, tx_hash)
            print("Create confirmed!")

            buy_blockhash = await client.get_latest_blockhash()
            buy_tx = Transaction(
                [payer],
                Message(buy_instructions, payer.pubkey()),
                buy_blockhash.value.blockhash,
            )
            print("\nSending buy transaction (buy_v2)...")
            buy_response = await client.send_transaction(buy_tx, opts)
            buy_hash = buy_response.value
            print(f"Buy sent: https://solscan.io/tx/{buy_hash}")
            await client.confirm_transaction(buy_hash, commitment="confirmed")
            await assert_transaction_succeeded(client, buy_hash)
            print("Buy confirmed!")

            return tx_hash

        except Exception as e:
            print(f"Transaction failed: {e}")
            raise


if __name__ == "__main__":
    asyncio.run(main())

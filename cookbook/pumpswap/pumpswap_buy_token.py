"""This standalone script demonstrates how to buy tokens on the PUMP AMM (pAMM) protocol.
It covers the complete flow from finding markets to executing buys with mayhem mode support.

Usage:
    uv run cookbook/pumpswap/pumpswap_buy_token.py <MINT> [SOL_TO_SPEND] [--slippage 0.3]

Key concepts demonstrated:
- Finding AMM pool addresses by token mint
- Parsing binary account data structures
- Dynamic fee recipient calculation (mayhem mode vs standard)
- Program Derived Address (PDA) derivation
- WSOL wrapping (converting SOL to wrapped SOL for SPL token operations)
- Volume tracking incentives integration
- Transaction simulation before sending
- Slippage protection mechanisms
"""

import argparse
import asyncio
import os
import random
import struct
import sys

import base58
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import MemcmpOpts, TxOptsModel
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)
from spl.token.models import SyncNativeParams

sys.path.append(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "solana")
)

import solana_transaction_status as tx_status

load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")

PRIVATE_KEY = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
PAYER = Keypair.from_bytes(PRIVATE_KEY)

# Defaults for the command line below, not fixed settings.
DEFAULT_SOL_AMOUNT = 0.001
DEFAULT_SLIPPAGE = 0.3  # 30% - maximum acceptable price movement during trade

# Token configuration

# Program instruction discriminators (first 8 bytes identify the instruction)
BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")

# ============================================================================
# Solana Program IDs and System Accounts
# ============================================================================

SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string(
    "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
)
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string(
    "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
)
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# 8 breaking-upgrade fee recipients.
# Two new accounts must be appended after pool-v2: the fee recipient (readonly)
# and its quote-mint ATA (mutable).
# Doc: github.com/pump-fun/pump-public-docs/blob/main/docs/BREAKING_FEE_RECIPIENT.md
BREAKING_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]

# ============================================================================
# Constants for Account Structure Parsing
# ============================================================================

# Pool account structure offsets
POOL_DISCRIMINATOR_SIZE = 8
POOL_BASE_MINT_OFFSET = 43  # Where base_mint field starts in pool account data
POOL_MAYHEM_MODE_OFFSET = 243  # Where is_mayhem_mode flag is stored
POOL_IS_CASHBACK_OFFSET = 244
# virtual_quote_reserves is an i128 appended after the flags. Pool fields
# end at 261; live accounts are 301 bytes with trailing padding.
POOL_VIRTUAL_QUOTE_RESERVES_OFFSET = 245
POOL_VIRTUAL_QUOTE_RESERVES_SIZE = 16
POOL_MAYHEM_MODE_MIN_SIZE = 244  # Minimum size for pool data with mayhem flag

# GlobalConfig structure offsets
GLOBALCONFIG_DISCRIMINATOR_SIZE = 8
GLOBALCONFIG_ADMIN_SIZE = 32
GLOBALCONFIG_DEFAULT_FEE_RECIPIENT_SIZE = 32
GLOBALCONFIG_RESERVED_FEE_OFFSET = (
    GLOBALCONFIG_DISCRIMINATOR_SIZE
    + GLOBALCONFIG_ADMIN_SIZE
    + GLOBALCONFIG_DEFAULT_FEE_RECIPIENT_SIZE
)

# Fee recipients
STANDARD_PUMPSWAP_FEE_RECIPIENT = Pubkey.from_string(
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
)

# Solana constants
LAMPORTS_PER_SOL = 1_000_000_000
COMPUTE_UNIT_PRICE = 10_000  # Micro-lamports per compute unit
COMPUTE_UNIT_BUDGET = 200_000  # Max compute units for transaction

# Buy-specific constants
PROTOCOL_FEE_BUFFER = 0.1  # 10% buffer for protocol fees when wrapping SOL
VOLUME_TRACKING_ENABLED = 1  # 1 = true, 0 = false


# ============================================================================
# Market Discovery
# ============================================================================


async def get_market_address_by_base_mint(
    client: AsyncClient, base_mint_address: Pubkey, amm_program_id: Pubkey
) -> Pubkey:
    """Find the AMM pool address for a specific token.

    Uses getProgramAccounts RPC method with a memcmp filter to find the pool
    that matches the given token mint address.

    Args:
        client: Solana RPC client
        base_mint_address: Token mint to find the pool for
        amm_program_id: PUMP AMM program address

    Returns:
        Address of the AMM pool (market) for the token
    """
    # MemcmpOpts takes the bytes base58-encoded, which a Pubkey's str already is.
    filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=str(base_mint_address))]
    response = await client.get_program_accounts(
        amm_program_id, encoding="base64", filters=filters
    )
    return response.value[0].pubkey


async def get_market_data(client: AsyncClient, market_address: Pubkey) -> dict:
    """Parse binary pool account data into a structured dictionary.

    The pool account stores data in a specific binary format. This function
    deserializes that data based on the known structure.

    Args:
        client: Solana RPC client
        market_address: Address of the pool account

    Returns:
        Dictionary with parsed pool data fields
    """
    response = await client.get_account_info(market_address, encoding="base64")
    data = response.value.data
    parsed_data: dict = {}

    offset = POOL_DISCRIMINATOR_SIZE

    # Field definitions: (name, type)
    # Types: u8=1 byte, u16=2 bytes, u64/i64=8 bytes, pubkey=32 bytes
    fields = [
        ("pool_bump", "u8"),
        ("index", "u16"),
        ("creator", "pubkey"),
        ("base_mint", "pubkey"),
        ("quote_mint", "pubkey"),
        ("lp_mint", "pubkey"),
        ("pool_base_token_account", "pubkey"),
        ("pool_quote_token_account", "pubkey"),
        ("lp_supply", "u64"),
        ("coin_creator", "pubkey"),
        # Appended after coin_creator: is_mayhem_mode (243), is_cashback_coin
        # (244), then virtual_quote_reserves as an i128 at 245..261. Live pool
        # accounts are 301 bytes (fields end at 261, rest is padding).
        ("is_mayhem_mode", "u8"),
        ("is_cashback_coin", "u8"),
    ]

    for field_name, field_type in fields:
        if field_type == "pubkey":
            value = data[offset : offset + 32]
            parsed_data[field_name] = base58.b58encode(value).decode("utf-8")
            offset += 32
        elif field_type in {"u64", "i64"}:
            format_char = "<Q" if field_type == "u64" else "<q"
            parsed_data[field_name] = struct.unpack(
                format_char, data[offset : offset + 8]
            )[0]
            offset += 8
        elif field_type == "u16":
            parsed_data[field_name] = struct.unpack("<H", data[offset : offset + 2])[0]
            offset += 2
        elif field_type == "u8":
            parsed_data[field_name] = data[offset]
            offset += 1

    return parsed_data


# Program Derived Address (PDA) derivation: deterministic addresses derived from
# seeds and a program id, letting programs own accounts without a private key.


def find_coin_creator_vault(coin_creator: Pubkey) -> Pubkey:
    """Derive the PDA for the coin creator's fee vault.

    The creator vault collects fees on behalf of the token creator.
    This is a deterministic address that can be recalculated by anyone.

    Args:
        coin_creator: Public key of the token creator

    Returns:
        PDA of the creator's vault authority
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"creator_vault", bytes(coin_creator)],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_global_volume_accumulator() -> Pubkey:
    """Derive the PDA for the global volume accumulator.

    This account tracks total trading volume across all pools.
    Volume tracking is used for incentive programs and analytics.

    Returns:
        PDA of the global volume accumulator
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    """Derive the PDA for a user's volume accumulator.

    Tracks individual user's trading volume, which may qualify them
    for incentives or rewards based on trading activity.

    Args:
        user: Public key of the user

    Returns:
        PDA of the user's volume accumulator
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_fee_config() -> Pubkey:
    """Derive the PDA for the fee configuration account.

    This account stores fee-related configuration for the AMM.
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


DEFAULT_COIN_CREATOR = Pubkey.default()


def find_pool_v2(base_mint: Pubkey) -> Pubkey:
    """Derive the PDA for the pool-v2 account (per-base-mint), required as the
    last "pre-upgrade" account on every pump-swap buy/sell."""
    derived_address, _ = Pubkey.find_program_address(
        [b"pool-v2", bytes(base_mint)],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


# Mayhem mode fee handling: a fee structure that routes to a different recipient.
# The fee recipient changes dynamically based on the pool's mayhem_mode flag.


async def get_reserved_fee_recipient_pumpswap(client: AsyncClient) -> Pubkey:
    """Fetch the mayhem mode fee recipient from GlobalConfig.

    When mayhem mode is active, fees are redirected to a special recipient
    stored in the GlobalConfig account.

    Args:
        client: Solana RPC client

    Returns:
        Public key of the mayhem mode fee recipient
    """
    response = await client.get_account_info(PUMP_SWAP_GLOBAL_CONFIG, encoding="base64")
    if not response.value or not response.value.data:
        msg = "Cannot fetch GlobalConfig account"
        raise ValueError(msg)

    data = response.value.data
    recipient_bytes = data[
        GLOBALCONFIG_RESERVED_FEE_OFFSET : GLOBALCONFIG_RESERVED_FEE_OFFSET + 32
    ]
    return Pubkey.from_bytes(recipient_bytes)


async def get_pumpswap_fee_recipients(
    client: AsyncClient, pool: Pubkey
) -> tuple[Pubkey, Pubkey, bool]:
    """Determine the correct fee recipient and whether the pool is cashback.

    Returns:
        Tuple of (fee_recipient_pubkey, fee_recipient_token_account, is_cashback)
    """
    response = await client.get_account_info(pool, encoding="base64")
    if not response.value or not response.value.data:
        msg = "Cannot fetch pool account"
        raise ValueError(msg)

    pool_data = response.value.data

    is_mayhem_mode = len(pool_data) >= POOL_MAYHEM_MODE_MIN_SIZE and bool(
        pool_data[POOL_MAYHEM_MODE_OFFSET]
    )
    is_cashback = len(pool_data) > POOL_IS_CASHBACK_OFFSET and bool(
        pool_data[POOL_IS_CASHBACK_OFFSET]
    )

    if is_mayhem_mode:
        fee_recipient = await get_reserved_fee_recipient_pumpswap(client)
    else:
        fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT

    fee_recipient_token_account = get_associated_token_address(
        fee_recipient, SOL, SYSTEM_TOKEN_PROGRAM
    )

    return (fee_recipient, fee_recipient_token_account, is_cashback)


# ============================================================================
# Price Calculation
# ============================================================================


async def read_virtual_quote_reserves(client: AsyncClient, pool: Pubkey) -> int:
    """Read Pool::virtual_quote_reserves, the field appended after the flags.

    Args:
        client: Solana RPC client
        pool: Pool (market) address

    Returns:
        Raw virtual quote reserves, or 0 if the account predates the field
    """
    response = await client.get_account_info(pool, encoding="base64")
    if not response.value or not response.value.data:
        return 0
    data = response.value.data
    end = POOL_VIRTUAL_QUOTE_RESERVES_OFFSET + POOL_VIRTUAL_QUOTE_RESERVES_SIZE
    if len(data) < end:
        return 0
    return int.from_bytes(
        data[POOL_VIRTUAL_QUOTE_RESERVES_OFFSET : end], "little", signed=True
    )


async def calculate_token_pool_price(
    client: AsyncClient,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
    virtual_quote_reserves: int = 0,
) -> float:
    """Calculate current token price from AMM pool reserves.

    Price is the ratio of *effective* quote reserves to base reserves:

        effective_quote_reserves =
            pool_quote_token_account.amount + Pool::virtual_quote_reserves

    PumpSwap added `virtual_quote_reserves` to the Pool account. Upstream's
    release note says it is 0 on every pool, but that is out of date: live pools
    carry non-zero values, and quoting off the raw vault balance under-prices
    them. Always add it.

    Args:
        client: Solana RPC client
        pool_base_token_account: Pool's token account (the token being priced)
        virtual_quote_reserves: Pool::virtual_quote_reserves, in raw quote units

    Returns:
        Price in quote asset per token
    """
    base_balance_resp = await client.get_token_account_balance(
        pool_base_token_account, commitment=Confirmed
    )
    quote_balance_resp = await client.get_token_account_balance(
        pool_quote_token_account
    )

    base_amount = float(base_balance_resp.value.ui_amount)
    quote_decimals = int(quote_balance_resp.value.decimals)
    quote_raw = int(quote_balance_resp.value.amount) + int(virtual_quote_reserves)
    quote_amount = quote_raw / 10**quote_decimals

    return quote_amount / base_amount


# ============================================================================
# Token Buying
# ============================================================================


# The `decimals` byte sits at this offset in both SPL Token and Token-2022 mints;
# Token-2022 appends its extensions after the base struct and never moves it.
MINT_DECIMALS_OFFSET = 44


async def get_mint_info(client: AsyncClient, mint_address: Pubkey) -> tuple[Pubkey, int]:
    """Read a mint's token program and decimals from one account fetch.

    Both come off the same `getAccountInfo`, so the decimals cost nothing extra.
    Every coin that graduated from a pump.fun bonding curve has 6, but a pool
    that was never a bonding-curve coin routinely has 9, and a factor of 1000
    scales the quote and the slippage floor together, so the trade reverts
    `ExceededSlippage` (6004) rather than merely mispricing.

    Args:
        client: Connected RPC client
        mint_address: The mint to inspect

    Returns:
        (token program that owns the mint, the mint's decimals)

    Raises:
        ValueError: If the mint is missing or owned by an unknown program
    """
    mint_info = await client.get_account_info(mint_address)

    if not mint_info.value:
        raise ValueError(f"Could not fetch mint info for {mint_address}")

    owner = mint_info.value.owner
    if owner not in (SYSTEM_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError(
            f"Mint account {mint_address} is owned by an unknown program: {owner}"
        )

    data = mint_info.value.data
    if len(data) <= MINT_DECIMALS_OFFSET:
        raise ValueError(f"Mint account {mint_address} is too short to hold decimals")

    return owner, data[MINT_DECIMALS_OFFSET]


async def buy_pump_swap(
    client: AsyncClient,
    market: Pubkey,
    payer: Keypair,
    base_mint: Pubkey,
    user_base_token_account: Pubkey,
    user_quote_token_account: Pubkey,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
    coin_creator_vault_authority: Pubkey,
    coin_creator_vault_ata: Pubkey,
    coin_creator: Pubkey,
    sol_amount_to_spend: float,
    slippage: float = 0.25,
) -> str | None:
    """Execute a token buy on the PUMP AMM with slippage protection.

    Quotes the output from the current price, wraps SOL into WSOL — the AMM only
    moves SPL tokens — then simulates and sends.

    Args:
        client: Solana RPC client
        market: AMM pool address
        payer: Wallet keypair for signing
        base_mint: Token mint address
        user_base_token_account: User's token account (for receiving tokens)
        user_quote_token_account: User's WSOL account
        pool_quote_token_account: Pool's WSOL account
        coin_creator_vault_authority: Creator vault PDA
        coin_creator_vault_ata: Creator's WSOL account
        sol_amount_to_spend: Amount of SOL to spend (in SOL, not lamports)
        slippage: Maximum acceptable slippage (0.25 = 25%)

    Returns:
        Transaction signature if successful, None otherwise
    """
    token_program_id, base_decimals = await get_mint_info(client, base_mint)
    token_price_sol = await calculate_token_pool_price(
        client,
        pool_base_token_account,
        pool_quote_token_account,
        await read_virtual_quote_reserves(client, market),
    )
    print(f"Token price in SOL: {token_price_sol:.10f} SOL")

    # Calculate expected token amount and maximum SOL we're willing to spend
    base_amount_out = int((sol_amount_to_spend / token_price_sol) * 10**base_decimals)
    max_sol_input = int((sol_amount_to_spend * (1 + slippage)) * LAMPORTS_PER_SOL)

    print(f"Buying {base_amount_out / (10**base_decimals):.10f} tokens")
    print(f"Maximum SOL input: {max_sol_input / LAMPORTS_PER_SOL:.10f} SOL")

    # Derive volume accumulator PDAs for incentive tracking
    global_volume_accumulator = find_global_volume_accumulator()
    user_volume_accumulator = find_user_volume_accumulator(payer.pubkey())

    # Get fee recipient based on mayhem mode + detect cashback pool
    (
        fee_recipient,
        fee_recipient_token_account,
        is_cashback,
    ) = await get_pumpswap_fee_recipients(client, market)

    # WSOL ATA of user_volume_accumulator — only required for cashback pools.
    user_volume_accumulator_quote_ata = get_associated_token_address(
        user_volume_accumulator, SOL, SYSTEM_TOKEN_PROGRAM
    )

    # Build account list for buy instruction
    # Order matters! Must match the program's expected account layout
    accounts = [
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_base_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_quote_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=fee_recipient_token_account, is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=token_program_id, is_signer=False, is_writable=False
        ),  # Use dynamic token_program_id
        AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=coin_creator_vault_authority, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=global_volume_accumulator, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),
        AccountMeta(pubkey=find_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]
    # Cashback pools require user_volume_accumulator_quote_ata (writable) BEFORE
    # pool-v2. Confirmed against on-chain post-cutover cashback buy
    # (sig 4JaWdExj6zzU3aGNWqNFhmtCyhbjRU3zLrsA3vASGu9krQrLjAfbBygP9i7yXmruSbuYn4StgMdMFBi22oQCfvjK).
    if is_cashback:
        accounts.append(
            AccountMeta(
                pubkey=user_volume_accumulator_quote_ata,
                is_signer=False,
                is_writable=True,
            )
        )
    # pool-v2 belongs only to a *canonical* pool — one that graduated from a
    # pump.fun bonding curve. `Pool.coin_creator` is the discriminator: it is set
    # for canonical pools and left at `Pubkey::default()` for every other pool
    # (upstream PUMP_SWAP_CREATOR_FEE_README.md). The two buyback accounts below
    # are required either way and are read *positionally* from the end, so on a
    # non-canonical pool sending pool-v2 shifts the pair by one and the program
    # rejects the pool-v2 PDA with `BuybackFeeRecipientNotAuthorized` (6053).
    # BREAKING_FEE_RECIPIENT.md qualifies the "after pool-v2" ordering with "for
    # coins that graduate from bonding curve".
    if coin_creator != DEFAULT_COIN_CREATOR:
        accounts.append(
            AccountMeta(
                pubkey=find_pool_v2(base_mint), is_signer=False, is_writable=False
            )
        )
    # 2 accounts appended AFTER pool-v2: breaking-fee recipient (readonly) + its quote-mint ATA (mutable).
    # Buy counts on a canonical pool: 26 non-cashback / 27 cashback. One fewer
    # on a non-canonical pool, which carries no pool-v2: 25 / 26.
    # Doc: github.com/pump-fun/pump-public-docs/blob/main/docs/BREAKING_FEE_RECIPIENT.md
    breaking_fee_recipient = random.choice(BREAKING_FEE_RECIPIENTS)
    breaking_fee_quote_ata = get_associated_token_address(
        breaking_fee_recipient, SOL, SYSTEM_TOKEN_PROGRAM
    )
    accounts.extend([
        AccountMeta(pubkey=breaking_fee_recipient, is_signer=False, is_writable=False),
        AccountMeta(pubkey=breaking_fee_quote_ata, is_signer=False, is_writable=True),
    ])

    # Instruction data format:
    # discriminator (8 bytes) + amount_out (8 bytes) + max_in (8 bytes) + track_volume (1 byte)
    # All integers are little-endian (<)
    data = (
        BUY_DISCRIMINATOR
        + struct.pack("<Q", base_amount_out)  # Expected token amount
        + struct.pack("<Q", max_sol_input)  # Maximum SOL to spend
        + struct.pack("<B", VOLUME_TRACKING_ENABLED)  # Enable volume tracking
    )

    # Set compute budget to avoid transaction failures
    compute_limit_ix = set_compute_unit_limit(COMPUTE_UNIT_BUDGET)
    compute_price_ix = set_compute_unit_price(COMPUTE_UNIT_PRICE)

    # Create WSOL account if it doesn't exist
    # Note: WSOL always uses the standard Token program, never Token2022
    create_wsol_ata_ix = create_idempotent_associated_token_account(
        payer.pubkey(),
        payer.pubkey(),
        SOL,
        SYSTEM_TOKEN_PROGRAM,  # WSOL always uses standard Token program
    )

    # Calculate amount to wrap (includes buffer for fees)
    wrap_amount = int(
        (sol_amount_to_spend * (1 + PROTOCOL_FEE_BUFFER)) * LAMPORTS_PER_SOL
    )

    # Transfer SOL to WSOL account and sync
    # This converts native SOL to the SPL token version (WSOL)
    transfer_sol_ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=user_quote_token_account,
            lamports=wrap_amount,
        )
    )
    sync_native_ix = sync_native(
        # WSOL always uses the standard Token program
        SyncNativeParams(
            program_id=SYSTEM_TOKEN_PROGRAM, account=user_quote_token_account
        )
    )

    # Create token account for receiving purchased tokens
    create_token_ata_ix = create_idempotent_associated_token_account(
        payer.pubkey(),
        payer.pubkey(),
        base_mint,
        token_program_id,  # Use dynamic token_program_id
    )

    buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

    # Build and sign transaction
    blockhash_resp = await client.get_latest_blockhash()
    msg = MessageV0.try_compile(
        payer.pubkey(),
        [
            compute_limit_ix,
            compute_price_ix,
            create_wsol_ata_ix,
            transfer_sol_ix,
            sync_native_ix,
            create_token_ata_ix,
            buy_ix,
        ],
        [],
        blockhash_resp.value.blockhash,
    )
    tx = VersionedTransaction(message=msg, keypairs=[payer])

    # Simulate first to catch errors before sending
    simulation = await client.simulate_transaction(tx)
    if simulation.value.err:
        print(f"Simulation error: {simulation.value.err}")
        for log in (simulation.value.logs or []):
            print(f"  log: {log}")
        # NOTE: pump-swap may throw AnchorError 6023 (Overflow) at buy.rs:400 on
        # the dynamic creator-fee calc for some pools. The account list matches
        # the IDL; the error comes from the program itself. Under investigation.
        return None

    print(
        f"Simulation successful, compute units used: {simulation.value.units_consumed}"
    )

    try:
        # Skip preflight since we already simulated (faster execution)
        tx_sig = await client.send_transaction(
            tx, opts=TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed)
        )
        tx_hash = tx_sig.value
        print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")

        await tx_status.confirm_and_assert(client, tx_hash)
        print("Transaction confirmed")
        return tx_hash
    except Exception as e:
        print(f"Error: {e!s}")
        return None


# ============================================================================
# Main Execution
# ============================================================================


async def buy(token_mint: Pubkey, sol_amount: float, slippage: float) -> None:
    """Execute the complete buy flow.

    Args:
        token_mint: The coin to buy
        sol_amount: SOL to spend
        slippage: Maximum acceptable price movement
    """
    async with AsyncClient(RPC_ENDPOINT, timeout=120) as client:
        # Step 1: Find the pool address for our token
        market_address = await get_market_address_by_base_mint(
            client, token_mint, PUMP_AMM_PROGRAM_ID
        )

        # Step 2: Parse pool data to get necessary accounts
        market_data = await get_market_data(client, market_address)

        # Determine token program ID for the base mint
        token_program_id, _ = await get_mint_info(client, token_mint)

        # Step 3: Derive PDAs needed for the transaction
        coin_creator_vault_authority = find_coin_creator_vault(
            Pubkey.from_string(market_data["coin_creator"])
        )
        coin_creator_vault_ata = get_associated_token_address(
            coin_creator_vault_authority, SOL, SYSTEM_TOKEN_PROGRAM
        )

        # Step 4: Execute the buy
        await buy_pump_swap(
            client,
            market_address,
            PAYER,
            token_mint,
            get_associated_token_address(PAYER.pubkey(), token_mint, token_program_id),
            get_associated_token_address(PAYER.pubkey(), SOL, SYSTEM_TOKEN_PROGRAM),
            Pubkey.from_string(market_data["pool_base_token_account"]),
            Pubkey.from_string(market_data["pool_quote_token_account"]),
            coin_creator_vault_authority,
            coin_creator_vault_ata,
            Pubkey.from_string(market_data["coin_creator"]),
            sol_amount,
            slippage,
        )


def main() -> None:
    """Parse the command line and run the buy."""
    parser = argparse.ArgumentParser(description="Buy a coin on the PumpSwap AMM")
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument(
        "amount",
        nargs="?",
        type=float,
        default=DEFAULT_SOL_AMOUNT,
        help=f"SOL to spend (default {DEFAULT_SOL_AMOUNT})",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Maximum acceptable price movement (default {DEFAULT_SLIPPAGE})",
    )
    args = parser.parse_args()

    asyncio.run(buy(Pubkey.from_string(args.mint), args.amount, args.slippage))


if __name__ == "__main__":
    main()

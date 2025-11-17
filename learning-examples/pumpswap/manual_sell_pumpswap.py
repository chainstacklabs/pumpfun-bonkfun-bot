"""
This standalone script demonstrates how to sell tokens on the PUMP AMM (pAMM) protocol.
It covers the complete flow from finding markets to executing sells with mayhem mode support.

Key concepts demonstrated:
- Finding AMM pool addresses by token mint
- Parsing binary account data structures
- Dynamic fee recipient calculation (mayhem mode vs standard)
- Program Derived Address (PDA) derivation
- Transaction construction with compute budgets
- Slippage protection mechanisms
"""

import asyncio
import os
import struct

import base58
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
TOKEN_MINT = Pubkey.from_string("...")  # Replace with your token mint address
PRIVATE_KEY = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
PAYER = Keypair.from_bytes(PRIVATE_KEY)
SLIPPAGE = 0.25  # 25% - maximum acceptable price movement during trade

# Token configuration
TOKEN_DECIMALS = 6  # Standard for most pump.fun tokens

# Program instruction discriminators (first 8 bytes identify the instruction)
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")

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

# ============================================================================
# Constants for Account Structure Parsing
# ============================================================================

# Pool account structure offsets
POOL_DISCRIMINATOR_SIZE = 8
POOL_BASE_MINT_OFFSET = 43  # Where base_mint field starts in pool account data
POOL_MAYHEM_MODE_OFFSET = 243  # Where is_mayhem_mode flag is stored
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
COMPUTE_UNIT_BUDGET = 150_000  # Max compute units for transaction


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
    filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(base_mint_address))]
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


# ============================================================================
# Program Derived Address (PDA) Derivation
# ============================================================================
# PDAs are deterministic addresses derived from seeds and a program ID.
# They allow programs to own accounts without needing a private key.


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


def find_fee_config() -> Pubkey:
    """Derive the PDA for the fee configuration account.

    This account stores fee-related configuration for the AMM.
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


# ============================================================================
# Mayhem Mode Fee Handling
# ============================================================================
# Mayhem mode is a special fee structure where fees go to a different recipient.
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
) -> tuple[Pubkey, Pubkey]:
    """Determine the correct fee recipient based on pool's mayhem mode status.

    This function checks if mayhem mode is enabled for the pool and returns
    the appropriate fee recipient and their WSOL token account.

    Args:
        client: Solana RPC client
        pool: Address of the AMM pool

    Returns:
        Tuple of (fee_recipient_pubkey, fee_recipient_token_account)
    """
    response = await client.get_account_info(pool, encoding="base64")
    if not response.value or not response.value.data:
        msg = "Cannot fetch pool account"
        raise ValueError(msg)

    pool_data = response.value.data

    # Check if mayhem mode flag exists and is enabled
    is_mayhem_mode = len(pool_data) >= POOL_MAYHEM_MODE_MIN_SIZE and bool(
        pool_data[POOL_MAYHEM_MODE_OFFSET]
    )

    # Select appropriate fee recipient
    if is_mayhem_mode:
        fee_recipient = await get_reserved_fee_recipient_pumpswap(client)
    else:
        fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT

    # Get the fee recipient's WSOL token account
    fee_recipient_token_account = get_associated_token_address(
        fee_recipient, SOL, SYSTEM_TOKEN_PROGRAM
    )

    return (fee_recipient, fee_recipient_token_account)


# ============================================================================
# Price Calculation
# ============================================================================


async def calculate_token_pool_price(
    client: AsyncClient,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
) -> float:
    """Calculate current token price from AMM pool balances.

    AMM price is determined by the ratio of tokens in the pool:
    price = quote_balance / base_balance

    Args:
        client: Solana RPC client
        pool_base_token_account: Pool's token account (the token being priced)
        pool_quote_token_account: Pool's SOL account (the quote currency)

    Returns:
        Price in SOL per token
    """
    base_balance_resp = await client.get_token_account_balance(pool_base_token_account)
    quote_balance_resp = await client.get_token_account_balance(
        pool_quote_token_account
    )

    base_amount = float(base_balance_resp.value.ui_amount)
    quote_amount = float(quote_balance_resp.value.ui_amount)

    return quote_amount / base_amount


# ============================================================================
# Token Program Determination
# ============================================================================


async def get_token_program_id(client: AsyncClient, mint_address: Pubkey) -> Pubkey:
    """Determines if a mint uses TokenProgram or Token2022Program."""
    mint_info = await client.get_account_info(mint_address)

    if not mint_info.value:
        raise ValueError(f"Could not fetch mint info for {mint_address}")

    owner = mint_info.value.owner

    if owner == SYSTEM_TOKEN_PROGRAM:
        return SYSTEM_TOKEN_PROGRAM
    elif owner == TOKEN_2022_PROGRAM:
        return TOKEN_2022_PROGRAM
    else:
        raise ValueError(
            f"Mint account {mint_address} is owned by an unknown program: {owner}"
        )


# ============================================================================
# Associated Token Account (ATA) Creation
# ============================================================================


def create_ata_idempotent_ix(payer_pubkey: Pubkey) -> Instruction:
    """Create instruction to initialize a WSOL ATA if it doesn't exist.

    Idempotent means this instruction won't fail if the ATA already exists.
    See: https://github.com/solana-program/associated-token-account/blob/main/program/src/instruction.rs

    Args:
        payer_pubkey: Account that will pay for ATA creation

    Returns:
        Instruction to create the ATA
    """
    associated_token_address = get_associated_token_address(payer_pubkey, SOL)

    instruction_accounts = [
        AccountMeta(pubkey=payer_pubkey, is_signer=True, is_writable=True),
        AccountMeta(pubkey=associated_token_address, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer_pubkey, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
    ]

    # Instruction data: single byte with value 1 = CreateIdempotent
    return Instruction(
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM, bytes([1]), instruction_accounts
    )


# ============================================================================
# Token Selling
# ============================================================================


async def sell_pump_swap(
    client: AsyncClient,
    market: Pubkey,
    payer: Keypair,
    base_mint: Pubkey,
    token_program_id: Pubkey,
    user_base_token_account: Pubkey,
    user_quote_token_account: Pubkey,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
    coin_creator_vault_authority: Pubkey,
    coin_creator_vault_ata: Pubkey,
    slippage: float = 0.25,
) -> str | None:
    """Execute a token sell on the PUMP AMM with slippage protection.

    This function:
    1. Fetches current token balance and pool price
    2. Calculates minimum SOL output with slippage tolerance
    3. Constructs and sends the sell transaction

    Args:
        client: Solana RPC client
        market: AMM pool address
        payer: Wallet keypair for signing
        base_mint: Token mint address
        user_base_token_account: User's token account
        user_quote_token_account: User's WSOL account
        pool_base_token_account: Pool's token account
        pool_quote_token_account: Pool's WSOL account
        coin_creator_vault_authority: Creator vault PDA
        coin_creator_vault_ata: Creator's WSOL account
        slippage: Maximum acceptable slippage (0.25 = 25%)

    Returns:
        Transaction signature if successful, None otherwise
    """
    token_balance = int(
        (await client.get_token_account_balance(user_base_token_account)).value.amount
    )
    token_balance_decimal = token_balance / 10**TOKEN_DECIMALS

    print(f"Token balance: {token_balance_decimal}")

    if token_balance == 0:
        print("No tokens to sell.")
        return None

    token_price_sol = await calculate_token_pool_price(
        client, pool_base_token_account, pool_quote_token_account
    )
    print(f"Price per Token: {token_price_sol:.20f} SOL")

    # Calculate minimum SOL we're willing to receive (slippage protection)
    expected_sol_output = token_balance_decimal * token_price_sol
    min_sol_output = int((expected_sol_output * (1 - slippage)) * LAMPORTS_PER_SOL)

    print(f"Selling {token_balance_decimal} tokens")
    print(f"Minimum SOL output: {min_sol_output / LAMPORTS_PER_SOL:.10f} SOL")

    # Get fee recipient based on mayhem mode
    fee_recipient, fee_recipient_token_account = await get_pumpswap_fee_recipients(
        client, market
    )

    # Build account list for sell instruction
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
        AccountMeta(pubkey=find_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]

    # Instruction data format: discriminator (8 bytes) + amount (8 bytes) + min_out (8 bytes)
    # All integers are little-endian (<)
    data = (
        SELL_DISCRIMINATOR
        + struct.pack("<Q", token_balance)  # Amount to sell
        + struct.pack("<Q", min_sol_output)  # Minimum SOL to receive
    )

    # Set compute budget to avoid transaction failures
    compute_limit_ix = set_compute_unit_limit(COMPUTE_UNIT_BUDGET)
    compute_price_ix = set_compute_unit_price(COMPUTE_UNIT_PRICE)

    # Ensure WSOL ATA exists (needed to receive SOL from sell)
    create_ata_ix = create_ata_idempotent_ix(payer.pubkey())

    sell_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

    # Build and sign transaction
    blockhash_resp = await client.get_latest_blockhash()
    msg = Message.new_with_blockhash(
        [compute_limit_ix, compute_price_ix, create_ata_ix, sell_ix],
        payer.pubkey(),
        blockhash_resp.value.blockhash,
    )
    tx = VersionedTransaction(message=msg, keypairs=[payer])

    try:
        # Skip preflight to send transaction faster (useful in competitive scenarios)
        tx_sig = await client.send_transaction(
            tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
        )
        tx_hash = tx_sig.value
        print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")

        await client.confirm_transaction(tx_hash, commitment="confirmed")
        print("Transaction confirmed")
        return tx_hash
    except Exception as e:
        print(f"Error: {e!s}")
        return None


# ============================================================================
# Main Execution
# ============================================================================


async def main() -> None:
    """Execute the complete sell flow."""
    async with AsyncClient(RPC_ENDPOINT) as client:
        # Step 1: Find the pool address for our token
        market_address = await get_market_address_by_base_mint(
            client, TOKEN_MINT, PUMP_AMM_PROGRAM_ID
        )

        # Step 2: Parse pool data to get necessary accounts
        market_data = await get_market_data(client, market_address)

        # Determine token program ID for the base mint
        token_program_id = await get_token_program_id(client, TOKEN_MINT)

        # Step 3: Derive PDAs needed for the transaction
        coin_creator_vault_authority = find_coin_creator_vault(
            Pubkey.from_string(market_data["coin_creator"])
        )
        coin_creator_vault_ata = get_associated_token_address(
            coin_creator_vault_authority, SOL, SYSTEM_TOKEN_PROGRAM
        )

        # Step 4: Execute the sell
        await sell_pump_swap(
            client,
            market_address,
            PAYER,
            TOKEN_MINT,
            token_program_id,
            get_associated_token_address(PAYER.pubkey(), TOKEN_MINT, token_program_id),
            get_associated_token_address(PAYER.pubkey(), SOL, SYSTEM_TOKEN_PROGRAM),
            Pubkey.from_string(market_data["pool_base_token_account"]),
            Pubkey.from_string(market_data["pool_quote_token_account"]),
            coin_creator_vault_authority,
            coin_creator_vault_ata,
            SLIPPAGE,
        )


if __name__ == "__main__":
    asyncio.run(main())

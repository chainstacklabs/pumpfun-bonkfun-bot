"""
Solana PUMP AMM Interface

This module provides functionality to interact with the PUMP AMM program on Solana, enabling:
- Finding market addresses by token mint
- Fetching and parsing market data from PUMP AMM pools
- Calculating token prices in AMM pools
- Creating associated token accounts (ATAs) idempotently
- Buying tokens on the PUMP AMM with slippage protection
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
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    SyncNativeParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

load_dotenv()

# Configuration constants
RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
TOKEN_MINT = Pubkey.from_string("...")
PRIVATE_KEY = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
PAYER = Keypair.from_bytes(PRIVATE_KEY)
SLIPPAGE = 0.3  # Slippage tolerance (30%) - the maximum price movement you'll accept

TOKEN_DECIMALS = 6
BUY_DISCRIMINATOR = bytes.fromhex(
    "66063d1201daebea"
)  # Program instruction identifier for the buy function

# Solana system addresses and program IDs
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string(
    "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
)
PUMP_PROTOCOL_FEE_RECIPIENT = Pubkey.from_string(
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
)
PUMP_PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT = Pubkey.from_string(
    "7GFUN3bWzJMKMRZ34JLsvcqdssDbXnp589SiE33KVwcC"
)
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string(
    "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
)
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
LAMPORTS_PER_SOL = 1_000_000_000
COMPUTE_UNIT_PRICE = 10_000  # Price in micro-lamports per compute unit
COMPUTE_UNIT_BUDGET = 200_000  # Maximum compute units to use


async def get_market_address_by_base_mint(
    client: AsyncClient, base_mint_address: Pubkey, amm_program_id: Pubkey
) -> Pubkey:
    """Find the market address for a given token mint.

    Searches for the AMM pool that contains the specified token mint as its base token
    by querying program accounts with a filter for the base_mint field.

    Args:
        client: Solana RPC client instance
        base_mint_address: Address of the token mint you want to find the market for
        amm_program_id: Address of the AMM program

    Returns:
        The Pubkey of the market (AMM pool) for the token
    """
    base_mint_bytes = bytes(base_mint_address)
    offset = (
        43  # Offset where the base_mint field is stored in the account data structure
    )
    filters = [MemcmpOpts(offset=offset, bytes=base_mint_bytes)]

    response = await client.get_program_accounts(
        amm_program_id, encoding="base64", filters=filters
    )

    market_address = [account.pubkey for account in response.value][0]
    return market_address


async def get_market_data(client: AsyncClient, market_address: Pubkey) -> dict:
    """Fetch and parse market data from the blockchain.

    Retrieves and deserializes the binary data stored in the market account
    into a structured dictionary containing key market information.

    Args:
        client: Solana RPC client instance
        market_address: Address of the market (AMM pool) to fetch data for

    Returns:
        Dictionary containing the parsed market data
    """
    response = await client.get_account_info(market_address, encoding="base64")
    data = response.value.data
    parsed_data: dict = {}

    # Start after the 8-byte discriminator
    offset = 8
    # Define the structure of the market account data
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
            value = (
                struct.unpack("<Q", data[offset : offset + 8])[0]
                if field_type == "u64"
                else struct.unpack("<q", data[offset : offset + 8])[0]
            )
            parsed_data[field_name] = value
            offset += 8
        elif field_type == "u16":
            value = struct.unpack("<H", data[offset : offset + 2])[0]
            parsed_data[field_name] = value
            offset += 2
        elif field_type == "u8":
            value = data[offset]
            parsed_data[field_name] = value
            offset += 1

    return parsed_data


def find_coin_creator_vault(coin_creator: Pubkey) -> Pubkey:
    """Derive the Program Derived Address (PDA) for a coin creator's vault.

    Calculates the deterministic PDA that serves as the vault authority
    for a specific coin creator in the PUMP AMM protocol.

    Args:
        coin_creator: Pubkey of the coin creator account

    Returns:
        Pubkey of the derived coin creator vault authority

    Note:
        This vault is used to collect creator fees from token transactions
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"creator_vault", bytes(coin_creator)],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_global_volume_accumulator() -> Pubkey:
    """Derive the Program Derived Address (PDA) for the global volume accumulator.

    Calculates the deterministic PDA that tracks global trading volume
    across all pools in the PUMP AMM protocol.

    Returns:
        Pubkey of the derived global volume accumulator account
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    """Derive the Program Derived Address (PDA) for a user's volume accumulator.

    Calculates the deterministic PDA that tracks trading volume
    for a specific user in the PUMP AMM protocol.

    Args:
        user: Pubkey of the user account

    Returns:
        Pubkey of the derived user volume accumulator account
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMP_AMM_PROGRAM_ID,
    )
    return derived_address


def find_fee_config() -> Pubkey:
    """Derive the Program Derived Address (PDA) for the fee config.

    Returns:
        Pubkey of the derived fee config account
    """
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


async def calculate_token_pool_price(
    client: AsyncClient,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
) -> float:
    """Calculate the current price of tokens in an AMM pool.

    Fetches the balance of tokens in the pool and calculates the price ratio
    between the base token and quote token (typically SOL).

    Args:
        client: Solana RPC client instance
        pool_base_token_account: Address of the pool's base token account (your token)
        pool_quote_token_account: Address of the pool's quote token account (SOL)

    Returns:
        The price of the base token in terms of the quote token (SOL per token)
    """
    base_balance_resp = await client.get_token_account_balance(pool_base_token_account)
    quote_balance_resp = await client.get_token_account_balance(
        pool_quote_token_account
    )

    # Extract the UI amounts (human-readable with decimals)
    base_amount = float(base_balance_resp.value.ui_amount)
    quote_amount = float(quote_balance_resp.value.ui_amount)

    token_price = quote_amount / base_amount

    return token_price


async def buy_pump_swap(
    client: AsyncClient,
    pump_fun_amm_market: Pubkey,
    payer: Keypair,
    base_mint: Pubkey,
    user_base_token_account: Pubkey,
    user_quote_token_account: Pubkey,
    pool_base_token_account: Pubkey,
    pool_quote_token_account: Pubkey,
    coin_creator_vault_authority: Pubkey,
    coin_creator_vault_ata: Pubkey,
    sol_amount_to_spend: int,
    slippage: float = 0.25,
) -> str | None:
    """Buy tokens on the PUMP AMM with slippage protection.

    Executes a token purchase on the PUMP AMM protocol, calculating the expected
    token amount based on the current pool price and applying slippage protection.

    Args:
        client: Solana RPC client instance
        pump_fun_amm_market: Address of the AMM market
        payer: Keypair of the transaction signer and token buyer
        base_mint: Address of the token mint being purchased
        user_base_token_account: Address of the user's token account for receiving purchased tokens
        user_quote_token_account: Address of the user's SOL token account
        pool_base_token_account: Address of the pool's token account for the token being purchased
        pool_quote_token_account: Address of the pool's SOL token account
        coin_creator_vault_authority: Address of the coin creator's vault authority
        coin_creator_vault_ata: Address of the coin creator's associated token account for fees
        sol_amount_to_spend: Amount of SOL to spend on the purchase (in SOL, not lamports)
        slippage: Maximum acceptable price slippage, as a decimal (0.25 = 25%)

    Returns:
        Transaction signature if successful, None otherwise
    """
    # Calculate token price
    token_price_sol = await calculate_token_pool_price(
        client, pool_base_token_account, pool_quote_token_account
    )
    print(f"Token price in SOL: {token_price_sol:.10f} SOL")

    # Calculate maximum SOL input with slippage protection
    base_amount_out = int((sol_amount_to_spend / token_price_sol) * 10**TOKEN_DECIMALS)
    slippage_factor = 1 + slippage
    max_sol_input = int((sol_amount_to_spend * slippage_factor) * LAMPORTS_PER_SOL)

    print(f"Buying {base_amount_out / (10**TOKEN_DECIMALS):.10f} tokens")
    print(f"Maximum SOL input: {max_sol_input / LAMPORTS_PER_SOL:.10f} SOL")

    # Calculate required PDAs for volume tracking
    global_volume_accumulator = find_global_volume_accumulator()
    user_volume_accumulator = find_user_volume_accumulator(payer.pubkey())

    # Define all accounts needed for the buy instruction
    accounts = [
        AccountMeta(pubkey=pump_fun_amm_market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=base_mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_base_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_quote_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_token_account, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=PUMP_PROTOCOL_FEE_RECIPIENT, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=PUMP_PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT,
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
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
            pubkey=global_volume_accumulator, is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),
        # Index 21: fee_config (readonly)
        AccountMeta(pubkey=find_fee_config(), is_signer=False, is_writable=False),
        # Index 22: fee_program (readonly)
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]

    data = (
        BUY_DISCRIMINATOR
        + struct.pack("<Q", base_amount_out)
        + struct.pack("<Q", max_sol_input)
        + struct.pack("<B", 1)  # track_volume: 1 = true (enable volume tracking)
    )

    compute_limit_ix = set_compute_unit_limit(COMPUTE_UNIT_BUDGET)
    compute_price_ix = set_compute_unit_price(COMPUTE_UNIT_PRICE)

    # Wrapping SOL
    create_wsol_ata_ix = create_idempotent_associated_token_account(
        payer.pubkey(), payer.pubkey(), SOL, SYSTEM_TOKEN_PROGRAM
    )

    # Calculate the amount of SOL to wrap(can also use `max_sol_input`), can get the SOL back by closing the WSOL account while selling
    pump_protocol_fees = sol_amount_to_spend * 0.1  # adding some buffer fees
    wrap_amount = int((sol_amount_to_spend + pump_protocol_fees) * LAMPORTS_PER_SOL)

    transfer_sol_ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=user_quote_token_account,
            lamports=wrap_amount,
        )
    )

    sync_native_ix = sync_native(
        SyncNativeParams(SYSTEM_TOKEN_PROGRAM, user_quote_token_account)
    )

    idempotent_ata_ix = create_idempotent_associated_token_account(
        payer.pubkey(), payer.pubkey(), base_mint, SYSTEM_TOKEN_PROGRAM
    )

    buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

    blockhash_resp = await client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash

    msg = Message.new_with_blockhash(
        [
            compute_limit_ix,
            compute_price_ix,
            create_wsol_ata_ix,
            transfer_sol_ix,
            sync_native_ix,
            idempotent_ata_ix,
            buy_ix,
        ],
        payer.pubkey(),
        recent_blockhash,
    )

    tx_buy = VersionedTransaction(message=msg, keypairs=[payer])

    # Optionally, you can simulate the transaction first and check for errors and get the compute units used
    simulation = await client.simulate_transaction(tx_buy)
    if simulation.value.err:
        print(f"Simulation error: {simulation.value.err}")
        return None

    compute_units_used = simulation.value.units_consumed
    print(f"Simulation successful, compute units used: {compute_units_used}")

    try:
        tx_sig = await client.send_transaction(
            tx_buy,
            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed),
        )

        tx_hash = tx_sig.value
        print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
        await client.confirm_transaction(tx_hash, commitment="confirmed")

        print("Transaction confirmed")
        return tx_hash
    except Exception as e:
        print(f"Error sending transaction: {e!s}")
        return None


async def main():
    """Main function to execute the token buying process."""
    sol_amount_to_spend = 0.000001

    async with AsyncClient(RPC_ENDPOINT) as client:
        market_address = await get_market_address_by_base_mint(
            client, TOKEN_MINT, PUMP_AMM_PROGRAM_ID
        )
        market_data = await get_market_data(client, market_address)
        coin_creator_vault_authority = find_coin_creator_vault(
            Pubkey.from_string(market_data["coin_creator"])
        )
        coin_creator_vault_ata = get_associated_token_address(
            coin_creator_vault_authority, SOL
        )

        await buy_pump_swap(
            client,
            market_address,
            PAYER,
            TOKEN_MINT,
            get_associated_token_address(PAYER.pubkey(), TOKEN_MINT),
            get_associated_token_address(PAYER.pubkey(), SOL),
            Pubkey.from_string(market_data["pool_base_token_account"]),
            Pubkey.from_string(market_data["pool_quote_token_account"]),
            coin_creator_vault_authority,
            coin_creator_vault_ata,
            sol_amount_to_spend,
            SLIPPAGE,
        )


if __name__ == "__main__":
    asyncio.run(main())

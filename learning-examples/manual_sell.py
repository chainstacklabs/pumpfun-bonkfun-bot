import asyncio
import os
import sys

import base58
import pump_v2
import tx_status
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR = pump_v2.BONDING_CURVE_DISCRIMINATOR
TOKEN_DECIMALS = 6
TOKEN_MINT = Pubkey.from_string(
    sys.argv[1] if len(sys.argv) > 1 else "..."
)  # Pass mint as argv[1] or hardcode here

# Global constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SYSTEM_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000
UNIT_PRICE = 10_000_000
UNIT_BUDGET = 100_000

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


BondingCurveState = pump_v2.BondingCurveState


async def get_pump_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return pump_v2.BondingCurveState(data)


def get_bonding_curve_address(mint: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)


def find_associated_bonding_curve(
    mint: Pubkey, bonding_curve: Pubkey, token_program_id: Pubkey
) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [
            bytes(bonding_curve),
            bytes(token_program_id),
            bytes(mint),
        ],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)],
        PUMP_PROGRAM,
    )
    return derived_address


def calculate_pump_curve_price(curve_state: pump_v2.BondingCurveState) -> float:
    """Price of one whole token in whole units of the curve's quote asset.

    Args:
        curve_state: Parsed curve state

    Returns:
        Price in the quote asset

    Raises:
        ValueError: If reserves are empty
    """
    price = curve_state.price_per_token()
    if price <= 0:
        raise ValueError("Invalid reserve state")
    return price


async def get_token_balance(conn: AsyncClient, associated_token_account: Pubkey):
    response = await conn.get_token_account_balance(associated_token_account)
    if response.value:
        return int(response.value.amount)
    return 0


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


async def sell_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    token_program_id: Pubkey,
    slippage: float = 0.25,
    max_retries=5,
):
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        associated_token_account = get_associated_token_address(
            payer.pubkey(), mint, token_program_id
        )

        # Get token balance
        token_balance = await get_token_balance(client, associated_token_account)
        token_balance_decimal = token_balance / 10**TOKEN_DECIMALS
        print(f"Token balance: {token_balance_decimal}")
        if token_balance == 0:
            print("No tokens to sell.")
            return

        # Fetch bonding curve state to calculate price and determine fee recipient
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        print(f"Price per Token: {token_price_sol:.20f} SOL")

        # Minimum payout, in the curve's quote asset raw units.
        quote_mint = pump_v2.normalize_quote_mint(
            getattr(curve_state, "quote_mint", None)
        )
        quote_unit = pump_v2.quote_units(quote_mint)
        amount = token_balance
        expected_output = float(token_balance_decimal) * float(token_price_sol)
        min_quote_output = max(1, int(expected_output * (1 - slippage) * quote_unit))

        print(f"Selling {token_balance_decimal} tokens")
        print(f"Quote asset: {quote_mint}")
        print(
            f"Minimum output: {min_quote_output / quote_unit:.10f} ({min_quote_output} raw)"
        )

        # sell_v2 takes the same 26 mandatory accounts for every coin — no
        # cashback/mayhem branching on the account list any more.
        sell_ix = pump_v2.build_sell_v2_instruction(
            base_mint=mint,
            creator=curve_state.creator,
            user=payer.pubkey(),
            token_amount_raw=amount,
            min_quote_output_raw=min_quote_output,
            quote_mint=quote_mint,
            base_token_program=token_program_id,
            is_mayhem_mode=curve_state.is_mayhem_mode,
        )

        instructions = [set_compute_unit_price(1_000)]
        # Non-SOL proceeds land in the seller's quote ATA, which must exist.
        if not pump_v2.is_sol_paired(quote_mint):
            instructions.append(
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    quote_mint,
                    token_program_id=pump_v2.quote_token_program(quote_mint),
                )
            )
        instructions.append(sell_ix)

        msg = Message(instructions, payer.pubkey())
        recent_blockhash = await client.get_latest_blockhash()
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
        # Continue with the sell transaction
        for attempt in range(max_retries):
            try:
                tx = await client.send_transaction(
                    Transaction(
                        [payer],
                        msg,
                        recent_blockhash.value.blockhash,
                    ),
                    opts=opts,
                )
                tx_hash = tx.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await tx_status.confirm_and_assert(client, tx_hash)
                print("Transaction confirmed")
                return  # Success, exit the function
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e!s}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    print(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")


async def main():
    # Replace these with the actual values for the token you want to sell
    async with AsyncClient(RPC_ENDPOINT) as client:
        token_program_id = await get_token_program_id(client, TOKEN_MINT)

    bonding_curve, _ = get_bonding_curve_address(TOKEN_MINT)
    associated_bonding_curve = find_associated_bonding_curve(
        TOKEN_MINT, bonding_curve, token_program_id
    )

    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)

    creator_vault = find_creator_vault(curve_state.creator)

    slippage = 0.25  # 25% slippage tolerance

    print(f"Bonding curve address: {bonding_curve}")
    print(f"Selling tokens with {slippage * 100:.1f}% slippage tolerance...")
    await sell_token(
        TOKEN_MINT,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        token_program_id,
        slippage,
    )


if __name__ == "__main__":
    asyncio.run(main())

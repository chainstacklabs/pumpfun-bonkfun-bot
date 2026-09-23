"""Sell your whole position in a pump.fun coin, using sell_v2.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_sell_token_v2.py <MINT>

The mirror of `pumpfun_buy_token_v2.py`. It reads how many tokens you hold, reads the curve
for a price, and sells the lot with a slippage floor underneath.

Two things to know before you run it:

- **The floor is yours to set.** `min_sol_output` is computed here from the price
  and the slippage, and the program enforces only that number. Passing a floor
  derived from a stale price is how a sell goes through at a price you did not intend.
- **A dust position may not be sellable at all.** Below a few thousand tokens the
  output rounds to zero and the program rejects the sell with error 6003
  (TooLittleSolReceived), whatever slippage you allow.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions_v2.py sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions_v2 as pump_v2
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Here and later all the discriminators are precalculated. See cookbook/solana/anchor_calculate_discriminator.py
EXPECTED_DISCRIMINATOR = pump_v2.BONDING_CURVE_DISCRIMINATOR
TOKEN_DECIMALS = 6
# Default for the command line below, not a fixed setting.
DEFAULT_SLIPPAGE = 0.25

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

load_dotenv()

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


async def _get_mint_account_info(client: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping AsyncClient's `.value` envelope.

    Adapter for `pump_v2.resolve_quote_token_program`, which expects a
    getter returning the account object (with an `.owner` attribute)
    directly rather than solana-py's RPC response wrapper.

    Args:
        client: Solana RPC client
        address: Account to fetch

    Returns:
        The account object

    Raises:
        ValueError: If the account does not exist
    """
    response = await client.get_account_info(address)
    if response.value is None:
        raise ValueError(f"Could not fetch account info for {address}")
    return response.value


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
        quote_mint = pump_v2.normalize_quote_mint(
            getattr(curve_state, "quote_mint", None)
        )

        # Resolve the quote mint before pricing. The one read gives both the
        # token program -- which can be Token-2022, and is for every tokenized
        # equity pump.fun admits as a quote asset -- and the decimals the price
        # and the slippage floor below are denominated in. Pricing first and
        # resolving after floors the sell against a number that is off by a
        # power of ten.
        quote_token_program_id = await pump_v2.resolve_quote_token_program(
            quote_mint, lambda pk: _get_mint_account_info(client, pk)
        )
        quote_unit = pump_v2.quote_units(quote_mint)

        token_price_sol = calculate_pump_curve_price(curve_state)
        print(f"Price per Token: {token_price_sol:.20f} SOL")

        # Minimum payout, in the curve's quote asset raw units.
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
            quote_token_program_id=quote_token_program_id,
        )

        instructions = [set_compute_unit_price(1_000)]
        # Non-SOL proceeds land in the seller's quote ATA, which must exist.
        if not pump_v2.is_sol_paired(quote_mint):
            instructions.append(
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    quote_mint,
                    token_program_id=quote_token_program_id,
                )
            )
        instructions.append(sell_ix)

        recent_blockhash = await client.get_latest_blockhash()
        # The blockhash is fixed for every attempt, so the transaction is built
        # once: each retry resubmits identical bytes.
        transaction = VersionedTransaction(
            MessageV0.try_compile(
                payer.pubkey(), instructions, [], recent_blockhash.value.blockhash
            ),
            [payer],
        )
        opts = TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed)
        # Continue with the sell transaction
        for attempt in range(max_retries):
            try:
                tx = await client.send_transaction(transaction, opts=opts)
                tx_hash = tx.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await tx_status.confirm_and_assert(client, tx_hash)
                print("Transaction confirmed")
                return  # Success, exit the function
            except tx_status.TransactionRevertedError as e:
                # The signature is already on chain and reverted. The
                # transaction above is fixed, so a retry would resubmit
                # identical bytes and revert identically — stop instead of
                # burning attempts.
                print(f"Transaction reverted on-chain, not retrying: {e}")
                return
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e!s}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    print(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")


async def run(mint: Pubkey, slippage: float) -> None:
    """Sell the whole position in one coin.

    Args:
        mint: The coin to sell
        slippage: Slippage tolerance
    """
    async with AsyncClient(RPC_ENDPOINT) as client:
        token_program_id = await get_token_program_id(client, mint)

    bonding_curve, _ = get_bonding_curve_address(mint)
    associated_bonding_curve = find_associated_bonding_curve(
        mint, bonding_curve, token_program_id
    )

    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)

    creator_vault = find_creator_vault(curve_state.creator)

    print(f"Bonding curve address: {bonding_curve}")
    print(f"Selling tokens with {slippage * 100:.1f}% slippage tolerance...")
    await sell_token(
        mint,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        token_program_id,
        slippage,
    )


def main() -> None:
    """Parse the command line and run the sell."""
    parser = argparse.ArgumentParser(description="Sell a whole pump.fun position")
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"Slippage tolerance (default {DEFAULT_SLIPPAGE})",
    )
    args = parser.parse_args()

    asyncio.run(run(Pubkey.from_string(args.mint), args.slippage))


if __name__ == "__main__":
    main()

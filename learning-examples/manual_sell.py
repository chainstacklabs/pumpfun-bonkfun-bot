import asyncio
import os
import struct

import base58
from construct import Flag, Int64ul, Struct
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6
TOKEN_MINT = Pubkey.from_string("...")  # Replace with actual token mint address

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


class BondingCurveState:
    """Bonding curve state parser with progressive field parsing.

    Parses bonding curve account data progressively based on available bytes,
    making it forward-compatible with future schema versions.
    """

    # Base struct present in all versions
    _BASE_STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data progressively based on available bytes.

        Args:
            data: Raw account data including discriminator

        Raises:
            ValueError: If discriminator is invalid or data is too short
        """
        if len(data) < 8:
            raise ValueError("Data too short to contain discriminator")

        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        # Parse base fields (always present)
        offset = 8
        base_data = data[offset:]
        parsed = self._BASE_STRUCT.parse(base_data)
        self.__dict__.update(parsed)

        # Calculate offset after base struct
        offset += self._BASE_STRUCT.sizeof()

        # Parse creator if bytes remaining (added in V2)
        if len(data) >= offset + 32:
            creator_bytes = data[offset : offset + 32]
            self.creator = Pubkey.from_bytes(creator_bytes)
            offset += 32
        else:
            self.creator = None

        # Parse mayhem mode flag if bytes remaining (added in V3)
        if len(data) >= offset + 1:
            self.is_mayhem_mode = bool(data[offset])
        else:
            self.is_mayhem_mode = False


async def get_pump_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return BondingCurveState(data)


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


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)],
        PUMP_FEE_PROGRAM,
    )
    return derived_address


async def get_fee_recipient(
    client: AsyncClient, curve_state: BondingCurveState
) -> Pubkey:
    """Determine the correct fee recipient based on mayhem mode.

    Mayhem mode tokens use a different fee recipient (reserved_fee_recipient from Global account)
    instead of the standard fee recipient. This function checks the bonding curve state
    and returns the appropriate fee recipient.

    Args:
        client: Solana RPC client to fetch Global account data
        curve_state: Parsed bonding curve state containing is_mayhem_mode flag

    Returns:
        Appropriate fee recipient pubkey (mayhem or standard)
    """
    if not curve_state.is_mayhem_mode:
        return PUMP_FEE

    # Fetch Global account to get reserved_fee_recipient for mayhem mode tokens
    response = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
    if not response.value or not response.value.data:
        # Fallback to standard fee if Global account cannot be fetched
        return PUMP_FEE

    data = response.value.data

    # Parse reserved_fee_recipient from Global account
    # Offset calculation based on pump_fun_idl.json Global struct:
    # discriminator(8) + initialized(1) + authority(32) + fee_recipient(32) +
    # initial_virtual_token_reserves(8) + initial_virtual_sol_reserves(8) +
    # initial_real_token_reserves(8) + token_total_supply(8) + fee_basis_points(8) +
    # withdraw_authority(32) + enable_migrate(1) + pool_migration_fee(8) +
    # creator_fee_basis_points(8) + fee_recipients[7](224) + set_creator_authority(32) +
    # admin_set_creator_authority(32) + create_v2_enabled(1) + whitelist_pda(32) = 483
    RESERVED_FEE_RECIPIENT_OFFSET = 483

    if len(data) < RESERVED_FEE_RECIPIENT_OFFSET + 32:
        # Fallback if account data is too short
        return PUMP_FEE

    reserved_fee_recipient_bytes = data[
        RESERVED_FEE_RECIPIENT_OFFSET : RESERVED_FEE_RECIPIENT_OFFSET + 32
    ]
    return Pubkey.from_bytes(reserved_fee_recipient_bytes)


def calculate_pump_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


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

        # Calculate minimum SOL output
        amount = token_balance
        min_sol_output = float(token_balance_decimal) * float(token_price_sol)
        slippage_factor = 1 - slippage
        min_sol_output = int((min_sol_output * slippage_factor) * LAMPORTS_PER_SOL)

        print(f"Selling {token_balance_decimal} tokens")
        print(f"Minimum SOL output: {min_sol_output / LAMPORTS_PER_SOL:.10f} SOL")

        # Determine fee recipient based on whether token uses mayhem mode
        fee_recipient = await get_fee_recipient(client, curve_state)

        accounts = [
            AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=associated_bonding_curve,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=associated_token_account,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=creator_vault,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=token_program_id, is_signer=False, is_writable=False
            ),  # Use dynamic token_program_id
            AccountMeta(
                pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
            # Index 12: fee_config (readonly)
            AccountMeta(
                pubkey=_find_fee_config(),
                is_signer=False,
                is_writable=False,
            ),
            # Index 13: fee_program (readonly)
            AccountMeta(
                pubkey=PUMP_FEE_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
        ]

        discriminator = struct.pack("<Q", 12502976635542562355)
        # Encode OptionBool for track_volume: [1, 1] = Some(true)
        track_volume_bytes = bytes([1, 1])
        data = (
            discriminator
            + struct.pack("<Q", amount)
            + struct.pack("<Q", min_sol_output)
            + track_volume_bytes
        )
        sell_ix = Instruction(PUMP_PROGRAM, data, accounts)

        msg = Message([set_compute_unit_price(1_000), sell_ix], payer.pubkey())
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
                await client.confirm_transaction(
                    tx_hash, commitment="confirmed", sleep_seconds=1
                )
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

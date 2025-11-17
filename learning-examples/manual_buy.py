import asyncio
import base64
import hashlib
import json
import os
import struct

import base58
import websockets
from construct import Flag, Int64ul, Struct
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6

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
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000

# RPC ENDPOINTS
RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
RPC_WEBSOCKET = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")


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


def calculate_pump_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


def _find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)],
        PUMP_PROGRAM,
    )
    return derived_address


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


async def buy_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    token_program: Pubkey,
    amount: float,
    slippage: float = 0.25,
    max_retries=5,
):
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        associated_token_account = get_associated_token_address(
            payer.pubkey(), mint, token_program_id=token_program
        )
        amount_lamports = int(amount * LAMPORTS_PER_SOL)

        # Fetch bonding curve state to calculate price and determine fee recipient
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        token_amount = amount / token_price_sol

        # Calculate maximum SOL to spend with slippage
        max_amount_lamports = int(amount_lamports * (1 + slippage))

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
                pubkey=token_program, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=_find_global_volume_accumulator(),
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=_find_user_volume_accumulator(payer.pubkey()),
                is_signer=False,
                is_writable=True,
            ),
            # Index 14: fee_config (readonly)
            AccountMeta(
                pubkey=_find_fee_config(),
                is_signer=False,
                is_writable=False,
            ),
            # Index 15: fee_program (readonly)
            AccountMeta(
                pubkey=PUMP_FEE_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
        ]

        discriminator = struct.pack("<Q", 16927863322537952870)
        # Encode OptionBool for track_volume: [1, 1] = Some(true)
        track_volume_bytes = bytes([1, 1])
        data = (
            discriminator
            + struct.pack("<Q", int(token_amount * 10**6))
            + struct.pack("<Q", max_amount_lamports)
            + track_volume_bytes
        )
        buy_ix = Instruction(PUMP_PROGRAM, data, accounts)
        idempotent_ata_ix = create_idempotent_associated_token_account(
            payer.pubkey(), payer.pubkey(), mint, token_program_id=token_program
        )
        msg = Message(
            [set_compute_unit_price(1_000), idempotent_ata_ix, buy_ix], payer.pubkey()
        )
        recent_blockhash = await client.get_latest_blockhash()
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)

        for attempt in range(max_retries):
            try:
                tx_buy = await client.send_transaction(
                    Transaction(
                        [payer],
                        msg,
                        recent_blockhash.value.blockhash,
                    ),
                    opts=opts,
                )
                tx_hash = tx_buy.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await client.confirm_transaction(
                    tx_hash, commitment="confirmed", sleep_seconds=1
                )
                print("Transaction confirmed")
                return  # Success, exit the function
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {str(e)[:50]}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    print(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")


def load_idl(file_path):
    with open(file_path) as f:
        return json.load(f)


def calculate_discriminator(instruction_name):
    sha = hashlib.sha256()
    sha.update(instruction_name.encode("utf-8"))
    return struct.unpack("<Q", sha.digest()[:8])[0]


def decode_create_instruction(ix_data, ix_def, accounts):
    args = {}
    offset = 8  # Skip 8-byte discriminator

    for arg in ix_def["args"]:
        if arg["type"] == "string":
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif arg["type"] == "pubkey":
            value = base58.b58encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")

        args[arg["name"]] = value

    # Add accounts
    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])

    return args


async def listen_for_create_transaction():
    idl_path = os.path.join(os.path.dirname(__file__), "..", "idl", "pump_fun_idl.json")
    idl = load_idl(idl_path)
    create_discriminator = calculate_discriminator("global:create")
    create_v2_discriminator = calculate_discriminator("global:create_v2")

    async with websockets.connect(RPC_WEBSOCKET) as websocket:
        subscription_message = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "blockSubscribe",
                "params": [
                    {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
                    {
                        "commitment": "confirmed",
                        "encoding": "base64",
                        "showRewards": False,
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }
        )
        await websocket.send(subscription_message)
        print(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM}")

        while True:
            response = await websocket.recv()
            data = json.loads(response)

            if "method" in data and data["method"] == "blockNotification":
                if "params" in data and "result" in data["params"]:
                    block_data = data["params"]["result"]
                    if "value" in block_data and "block" in block_data["value"]:
                        block = block_data["value"]["block"]
                        if "transactions" in block:
                            for tx in block["transactions"]:
                                if isinstance(tx, dict) and "transaction" in tx:
                                    tx_data_decoded = base64.b64decode(
                                        tx["transaction"][0]
                                    )
                                    transaction = VersionedTransaction.from_bytes(
                                        tx_data_decoded
                                    )

                                    for ix in transaction.message.instructions:
                                        if str(
                                            transaction.message.account_keys[
                                                ix.program_id_index
                                            ]
                                        ) == str(PUMP_PROGRAM):
                                            ix_data = bytes(ix.data)
                                            discriminator = struct.unpack(
                                                "<Q", ix_data[:8]
                                            )[0]

                                            # Check which create instruction was used
                                            instruction_name = None
                                            token_program = None

                                            if discriminator == create_discriminator:
                                                instruction_name = "create"
                                                token_program = SYSTEM_TOKEN_PROGRAM
                                            elif discriminator == create_v2_discriminator:
                                                instruction_name = "create_v2"
                                                token_program = TOKEN_2022_PROGRAM

                                            if instruction_name:
                                                create_ix = next(
                                                    instr
                                                    for instr in idl["instructions"]
                                                    if instr["name"] == instruction_name
                                                )
                                                account_keys = [
                                                    str(
                                                        transaction.message.account_keys[
                                                            index
                                                        ]
                                                    )
                                                    for index in ix.accounts
                                                ]
                                                decoded_args = (
                                                    decode_create_instruction(
                                                        ix_data, create_ix, account_keys
                                                    )
                                                )
                                                # Add token program info to decoded args
                                                decoded_args["token_program"] = str(token_program)
                                                decoded_args["is_token_2022"] = (token_program == TOKEN_2022_PROGRAM)
                                                return decoded_args


async def main():
    print("Waiting for a new token creation...")
    token_data = await listen_for_create_transaction()
    print("New token created:")
    print(json.dumps(token_data, indent=2))

    sleep_duration_sec = 15
    print(f"Waiting for {sleep_duration_sec} seconds for things to stabilize...")
    await asyncio.sleep(sleep_duration_sec)

    mint = Pubkey.from_string(token_data["mint"])
    bonding_curve = Pubkey.from_string(token_data["bondingCurve"])
    associated_bonding_curve = Pubkey.from_string(token_data["associatedBondingCurve"])
    creator_vault = _find_creator_vault(Pubkey.from_string(token_data["creator"]))
    token_program = Pubkey.from_string(token_data["token_program"])

    # Fetch the token price
    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)

    # Amount of SOL to spend (adjust as needed)
    amount = 0.000_001  # 0.00001 SOL
    slippage = 0.3  # 30% slippage tolerance

    print(f"Bonding curve address: {bonding_curve}")
    print(f"Token Program: {token_program} ({'Token2022' if token_data['is_token_2022'] else 'Standard Token'})")
    print(f"Token price: {token_price_sol:.10f} SOL")
    print(
        f"Buying {amount:.6f} SOL worth of the new token with {slippage * 100:.1f}% slippage tolerance..."
    )
    await buy_token(
        mint, bonding_curve, associated_bonding_curve, creator_vault, token_program, amount, slippage
    )


if __name__ == "__main__":
    asyncio.run(main())

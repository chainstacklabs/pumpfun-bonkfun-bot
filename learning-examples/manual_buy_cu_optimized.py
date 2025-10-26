import asyncio
import base64
import hashlib
import json
import os
import struct

import base58
import websockets
from construct import Bytes, Flag, Int64ul, Struct
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# Discriminators
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
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
RPC_WEBSOCKET = os.environ.get("SOLANA_NODE_WSS_ENDPOINT")


class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),
    )

    def __init__(self, data: bytes) -> None:
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")
        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)
        if hasattr(self, "creator") and isinstance(self.creator, bytes):
            self.creator = Pubkey.from_bytes(self.creator)


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
        [b"creator-vault", bytes(creator)], PUMP_PROGRAM
    )
    return derived_address


def _find_global_volume_accumulator() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"global_volume_accumulator"], PUMP_PROGRAM
    )
    return derived_address


def _find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM
    )
    return derived_address


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM
    )
    return derived_address


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """Create SetLoadedAccountsDataSizeLimit compute budget instruction."""
    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


def build_buy_instruction(
    payer: Keypair,
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    token_amount: int,
    max_amount_lamports: int,
):
    """Build the buy instruction with all accounts."""
    associated_token_account = get_associated_token_address(payer.pubkey(), mint)

    accounts = [
        AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=associated_token_account, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=_find_global_volume_accumulator(), is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=_find_user_volume_accumulator(payer.pubkey()),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=_find_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]

    discriminator = struct.pack("<Q", 16927863322537952870)
    data = (
        discriminator
        + struct.pack("<Q", token_amount)
        + struct.pack("<Q", max_amount_lamports)
    )

    return Instruction(PUMP_PROGRAM, data, accounts)


async def simulate_buy(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    amount: float,
    slippage: float = 0.25,
):
    """Simulate buy transaction and return CU consumption."""
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        amount_lamports = int(amount * LAMPORTS_PER_SOL)
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        token_amount = int((amount / token_price_sol) * 10**6)
        max_amount_lamports = int(amount_lamports * (1 + slippage))

        buy_ix = build_buy_instruction(
            payer,
            mint,
            bonding_curve,
            associated_bonding_curve,
            creator_vault,
            token_amount,
            max_amount_lamports,
        )
        idempotent_ata_ix = create_idempotent_associated_token_account(
            payer.pubkey(), payer.pubkey(), mint
        )

        msg = Message(
            [set_compute_unit_price(1_000), idempotent_ata_ix, buy_ix], payer.pubkey()
        )
        recent_blockhash = await client.get_latest_blockhash()
        tx = Transaction([payer], msg, recent_blockhash.value.blockhash)

        sim_result = await client.simulate_transaction(tx)
        if sim_result.value.err:
            print(f"Simulation error: {sim_result.value.err}")
            return None
        return sim_result.value.units_consumed


async def buy_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    amount: float,
    slippage: float = 0.25,
    use_cu_optimization: bool = False,
):
    """Buy token with or without CU optimization."""
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        amount_lamports = int(amount * LAMPORTS_PER_SOL)
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        token_amount = int((amount / token_price_sol) * 10**6)
        max_amount_lamports = int(amount_lamports * (1 + slippage))

        buy_ix = build_buy_instruction(
            payer,
            mint,
            bonding_curve,
            associated_bonding_curve,
            creator_vault,
            token_amount,
            max_amount_lamports,
        )
        idempotent_ata_ix = create_idempotent_associated_token_account(
            payer.pubkey(), payer.pubkey(), mint
        )

        instructions = [set_compute_unit_price(1_000)]
        if use_cu_optimization:
            instructions.insert(0, set_loaded_accounts_data_size_limit(512_000))
        instructions.extend([idempotent_ata_ix, buy_ix])

        msg = Message(instructions, payer.pubkey())
        recent_blockhash = await client.get_latest_blockhash()
        tx = Transaction([payer], msg, recent_blockhash.value.blockhash)

        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
        tx_result = await client.send_transaction(tx, opts=opts)
        tx_hash = tx_result.value

        print(f"  TX: https://explorer.solana.com/tx/{tx_hash}")
        await client.confirm_transaction(
            tx_hash, commitment="confirmed", sleep_seconds=1
        )

        # Get CU consumption
        tx_details = await client.get_transaction(
            tx_hash, encoding="json", max_supported_transaction_version=0
        )
        if (
            tx_details.value
            and tx_details.value.transaction
            and tx_details.value.transaction.meta
        ):
            cu_consumed = tx_details.value.transaction.meta.compute_units_consumed
            return cu_consumed
        return None


def decode_create_instruction(ix_data, ix_def, accounts):
    """Decode create instruction from transaction data."""
    args = {}
    offset = 8
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

    args["mint"] = str(accounts[0])
    args["bondingCurve"] = str(accounts[2])
    args["associatedBondingCurve"] = str(accounts[3])
    args["user"] = str(accounts[7])
    return args


async def listen_for_create_transaction():
    """Listen for new token creation on pump.fun."""
    idl_path = os.path.join(os.path.dirname(__file__), "..", "idl", "pump_fun_idl.json")
    with open(idl_path) as f:
        idl = json.load(f)

    create_discriminator = struct.unpack(
        "<Q", hashlib.sha256(b"global:create").digest()[:8]
    )[0]

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
                                    from solders.transaction import VersionedTransaction

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

                                            if discriminator == create_discriminator:
                                                create_ix = next(
                                                    instr
                                                    for instr in idl["instructions"]
                                                    if instr["name"] == "create"
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
                                                return decoded_args


async def main():
    print("Waiting for a new token creation...")
    token_data = await listen_for_create_transaction()
    print("New token created:")
    print(json.dumps(token_data, indent=2))

    print("\nWaiting 15 seconds for things to stabilize...")
    await asyncio.sleep(15)

    mint = Pubkey.from_string(token_data["mint"])
    bonding_curve = Pubkey.from_string(token_data["bondingCurve"])
    associated_bonding_curve = Pubkey.from_string(token_data["associatedBondingCurve"])

    # Get creator from bonding curve state
    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)
        creator_vault = _find_creator_vault(curve_state.creator)
        token_price_sol = calculate_pump_curve_price(curve_state)

    amount = 0.001  # 0.001 SOL
    slippage = 0.3

    print(f"\nToken price: {token_price_sol:.10f} SOL")
    print(f"Buying {amount:.6f} SOL worth with {slippage * 100:.1f}% slippage\n")

    # 1. Simulate
    print("=" * 60)
    print("1. SIMULATION")
    print("=" * 60)
    sim_cu = await simulate_buy(
        mint, bonding_curve, associated_bonding_curve, creator_vault, amount, slippage
    )
    if sim_cu:
        print(f"  Simulated CU consumption: {sim_cu:,}")

    # 2. Buy without optimization
    print("\n" + "=" * 60)
    print("2. BUY WITHOUT CU OPTIMIZATION")
    print("=" * 60)
    cu_no_opt = await buy_token(
        mint,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        amount,
        slippage,
        use_cu_optimization=False,
    )
    if cu_no_opt:
        print(f"  Actual CU consumed: {cu_no_opt:,}")

    # 3. Buy with optimization
    print("\n" + "=" * 60)
    print("3. BUY WITH CU OPTIMIZATION (setLoadedAccountsDataSizeLimit)")
    print("   Setting limit to 500 KB (512,000 bytes)")
    print("=" * 60)
    cu_with_opt = await buy_token(
        mint,
        bonding_curve,
        associated_bonding_curve,
        creator_vault,
        amount,
        slippage,
        use_cu_optimization=True,
    )
    if cu_with_opt:
        print(f"  ✓ Actual CU consumed (optimized): {cu_with_opt:,}")
        if cu_no_opt:
            immediate_savings = cu_no_opt - cu_with_opt
            print(f"  ✓ Immediate savings: {immediate_savings:,} CU")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if sim_cu:
        print(f"Simulated:        {sim_cu:,} CU")
    if cu_no_opt:
        print(f"Without optimize: {cu_no_opt:,} CU")
    if cu_with_opt:
        print(f"With optimize:    {cu_with_opt:,} CU")
        if cu_no_opt:
            savings = cu_no_opt - cu_with_opt
            pct = (savings / cu_no_opt) * 100
            print(f"Savings:          {savings:,} CU ({pct:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())

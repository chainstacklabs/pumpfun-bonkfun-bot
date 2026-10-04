"""Buy a graduated StonkFun coin from its Raydium CPMM pool, using swap_base_input.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/stonkfun/trade/stonkfun_buy_token_cpmm.py <MINT> <AMOUNT>
    uv run cookbook/stonkfun/trade/stonkfun_buy_token_cpmm.py <MINT> 0.001 --dry-run

AMOUNT is in the pool's quote asset and is spent in full. For SOL the script
wraps it into a throwaway account; for anything else you must already hold it.

When a LaunchLab curve raises its target, Raydium migrates the coin to a CPMM
pool and the curve stops taking trades. The pool is not searched for: CPMM
derives it from the AMM config the platform names (`PlatformConfig.cpswap_config`)
and the two mints, ordered by their bytes.

The price is the vault balances less the fees sitting in them unclaimed. The
pool takes its trade fee and, when enabled, a creator fee; a *reward* coin then
loses its transfer fee on the way to you. The expected amount accounts for all
three, the creator fee as if charged on the input, which is close enough for a
slippage floor.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import struct
import sys
from pathlib import Path

# solana_transaction_status.py lives in cookbook/solana/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import DataSliceOpts, MemcmpOpts, TxOptsModel
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountWithSeedParams, create_account_with_seed
from solders.transaction import VersionedTransaction
from spl.token.instructions import (
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_account,
)
from spl.token.models import CloseAccountParams, InitializeAccountParams

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

DEFAULT_SLIPPAGE = 0.1
PRIORITY_FEE_MICROLAMPORTS = 1_000
COMPUTE_UNIT_LIMIT = 150_000

LAUNCHLAB_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
CPMM_PROGRAM = Pubkey.from_string("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C")
CPMM_AUTHORITY = Pubkey.find_program_address(
    [b"vault_and_lp_mint_auth_seed"], CPMM_PROGRAM
)[0]
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

SWAP_BASE_INPUT = hashlib.sha256(b"global:swap_base_input").digest()[:8]
TOKEN_ACCOUNT_SIZE = 165
TOKEN_ACCOUNT_RENT = 2_039_280
FEE_RATE_DENOMINATOR = 1_000_000
MIGRATED = 2

# LaunchLab PoolState: discriminator, epoch, auth_bump, status, then after the
# amounts and the vesting schedule, global_config and platform_config.
LAUNCHLAB_POOL_SIZE = 429
LAUNCHLAB_STATUS_OFFSET = 17
LAUNCHLAB_PLATFORM_CONFIG_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8 + 32
LAUNCHLAB_BASE_MINT_OFFSET = LAUNCHLAB_PLATFORM_CONFIG_OFFSET + 32
LAUNCHLAB_QUOTE_MINT_OFFSET = LAUNCHLAB_BASE_MINT_OFFSET + 32
# PlatformConfig.cpswap_config: after the scales, fee_rate, name, web and img.
PLATFORM_CPSWAP_CONFIG_OFFSET = 8 + 8 + 32 + 32 + 3 * 8 + 8 + 64 + 256 + 256
# CPMM PoolState: ten pubkeys, five u8, seven u64, two u8, padding, two u64.
CPMM_KEYS = (
    "amm_config",
    "pool_creator",
    "token_0_vault",
    "token_1_vault",
    "lp_mint",
    "token_0_mint",
    "token_1_mint",
    "token_0_program",
    "token_1_program",
    "observation_key",
)
CPMM_DECIMALS_OFFSET = 8 + 10 * 32 + 3
CPMM_FEES_OFFSET = 8 + 10 * 32 + 5 + 8
CPMM_CREATOR_FEE_FLAG_OFFSET = 8 + 10 * 32 + 5 + 7 * 8 + 1
CPMM_CREATOR_FEES_OFFSET = 8 + 10 * 32 + 5 + 7 * 8 + 2 + 6
# AmmConfig: bump, disable flag, index u16, trade_fee_rate, three more u64, two
# owners, creator_fee_rate.
CONFIG_TRADE_FEE_OFFSET = 8 + 1 + 1 + 2
CONFIG_CREATOR_FEE_OFFSET = CONFIG_TRADE_FEE_OFFSET + 4 * 8 + 2 * 32
TOKEN_2022_EXTENSIONS_OFFSET = 166
TRANSFER_FEE_CONFIG_EXTENSION = 1


def transfer_fee(mint_data: bytes) -> tuple[int, int]:
    """The higher of a mint's scheduled transfer fees, as (basis points, max raw)."""
    offset = TOKEN_2022_EXTENSIONS_OFFSET
    while offset + 4 <= len(mint_data):
        kind, length = struct.unpack_from("<HH", mint_data, offset)
        if kind == 0 and length == 0:
            break
        if kind == TRANSFER_FEE_CONFIG_EXTENSION:
            body = mint_data[offset + 4 : offset + 4 + length]
            _, older_max, older_bps = struct.unpack_from("<QQH", body, 72)
            _, newer_max, newer_bps = struct.unpack_from("<QQH", body, 90)
            return max((older_bps, older_max), (newer_bps, newer_max))
        offset += 4 + length
    return 0, 0


async def find_cpmm_pool(client: AsyncClient, mint: Pubkey) -> tuple[Pubkey, dict]:
    """Find a graduated LaunchLab coin's CPMM pool, oriented around the coin.

    Raises:
        ValueError: If the coin is not a LaunchLab coin or has not migrated
    """
    found = await client.get_program_accounts(
        LAUNCHLAB_PROGRAM,
        encoding="base64",
        data_slice=DataSliceOpts(offset=0, length=0),
        filters=[
            LAUNCHLAB_POOL_SIZE,
            MemcmpOpts(offset=LAUNCHLAB_BASE_MINT_OFFSET, bytes=str(mint)),
        ],
    )
    if not found.value:
        raise ValueError(f"No LaunchLab pool found for {mint}")
    curve = (await client.get_account_info(found.value[0].pubkey)).value.data
    if curve[LAUNCHLAB_STATUS_OFFSET] != MIGRATED:
        raise ValueError(f"{mint} is still on its curve — use stonkfun_buy_token.py")
    platform_config = Pubkey.from_bytes(
        curve[LAUNCHLAB_PLATFORM_CONFIG_OFFSET : LAUNCHLAB_PLATFORM_CONFIG_OFFSET + 32]
    )
    quote_mint = Pubkey.from_bytes(
        curve[LAUNCHLAB_QUOTE_MINT_OFFSET : LAUNCHLAB_QUOTE_MINT_OFFSET + 32]
    )
    platform = (await client.get_account_info(platform_config)).value.data
    amm_config = Pubkey.from_bytes(
        platform[PLATFORM_CPSWAP_CONFIG_OFFSET : PLATFORM_CPSWAP_CONFIG_OFFSET + 32]
    )
    token_0, token_1 = sorted([mint, quote_mint], key=bytes)
    address = Pubkey.find_program_address(
        [b"pool", bytes(amm_config), bytes(token_0), bytes(token_1)], CPMM_PROGRAM
    )[0]
    data = (await client.get_account_info(address)).value.data
    keys = {
        name: Pubkey.from_bytes(data[8 + 32 * i : 8 + 32 * (i + 1)])
        for i, name in enumerate(CPMM_KEYS)
    }
    base, quote = (0, 1) if keys["token_0_mint"] == mint else (1, 0)
    fees = struct.unpack_from("<4Q", data, CPMM_FEES_OFFSET)
    creator_fees = struct.unpack_from("<2Q", data, CPMM_CREATOR_FEES_OFFSET)
    accrued = [fees[i] + fees[2 + i] + creator_fees[i] for i in (0, 1)]
    return address, {
        "amm_config": keys["amm_config"],
        "observation": keys["observation_key"],
        "base_mint": mint,
        "quote_mint": keys[f"token_{quote}_mint"],
        "base_vault": keys[f"token_{base}_vault"],
        "quote_vault": keys[f"token_{quote}_vault"],
        "base_program": keys[f"token_{base}_program"],
        "quote_program": keys[f"token_{quote}_program"],
        "base_decimals": data[CPMM_DECIMALS_OFFSET + base],
        "quote_decimals": data[CPMM_DECIMALS_OFFSET + quote],
        "base_accrued": accrued[base],
        "quote_accrued": accrued[quote],
        "creator_fee_enabled": bool(data[CPMM_CREATOR_FEE_FLAG_OFFSET]),
    }


async def buy(mint: Pubkey, amount: float, slippage: float, *, dry_run: bool) -> None:
    """Spend exactly `amount` of the pool's quote asset on the coin."""
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    user = payer.pubkey()

    async with AsyncClient(RPC_ENDPOINT, commitment=Confirmed) as client:
        address, pool = await find_cpmm_pool(client, mint)
        quote_unit = 10 ** pool["quote_decimals"]
        base_unit = 10 ** pool["base_decimals"]
        amount_in = int(amount * quote_unit)

        config, base_vault, quote_vault, mint_info = (
            await client.get_multiple_accounts(
                [pool["amm_config"], pool["base_vault"], pool["quote_vault"], mint]
            )
        ).value
        fee_rate = struct.unpack_from("<Q", config.data, CONFIG_TRADE_FEE_OFFSET)[0]
        if pool["creator_fee_enabled"]:
            fee_rate += struct.unpack_from(
                "<Q", config.data, CONFIG_CREATOR_FEE_OFFSET
            )[0]
        base_reserve = (
            struct.unpack_from("<Q", base_vault.data, 64)[0] - pool["base_accrued"]
        )
        quote_reserve = (
            struct.unpack_from("<Q", quote_vault.data, 64)[0] - pool["quote_accrued"]
        )
        net_in = amount_in - -(-amount_in * fee_rate // FEE_RATE_DENOMINATOR)
        out = base_reserve * net_in // (quote_reserve + net_in)
        bps, max_fee = transfer_fee(mint_info.data)
        expected = out - (min(-(-out * bps // 10_000), max_fee) if bps else 0)
        minimum_out = int(expected * (1 - slippage))

        print(f"Mint:          {mint}")
        print(f"CPMM pool:     {address}")
        print(f"Quote asset:   {pool['quote_mint']}")
        print(f"Spending:      {amount} ({amount_in} raw)")
        print(f"Pool fees:     {fee_rate / FEE_RATE_DENOMINATOR:.2%}")
        print(f"Transfer tax:  {bps / 100:.2f}%")
        print(f"Expecting:     ~{expected / base_unit:,.6f} tokens")
        print(f"Accepting:     >= {minimum_out / base_unit:,.6f} tokens")

        user_base = get_associated_token_address(user, mint, pool["base_program"])
        instructions = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                user, user, mint, token_program_id=pool["base_program"]
            ),
        ]
        close_after = []
        if pool["quote_mint"] == WSOL_MINT:
            seed = secrets.token_hex(16)
            user_quote = Pubkey.create_with_seed(user, seed, TOKEN_PROGRAM)
            instructions += [
                create_account_with_seed(
                    CreateAccountWithSeedParams(
                        from_pubkey=user,
                        to_pubkey=user_quote,
                        base=user,
                        seed=seed,
                        lamports=amount_in + TOKEN_ACCOUNT_RENT,
                        space=TOKEN_ACCOUNT_SIZE,
                        owner=TOKEN_PROGRAM,
                    )
                ),
                initialize_account(
                    InitializeAccountParams(
                        program_id=TOKEN_PROGRAM,
                        account=user_quote,
                        mint=WSOL_MINT,
                        owner=user,
                    )
                ),
            ]
            close_after.append(
                close_account(
                    CloseAccountParams(
                        program_id=TOKEN_PROGRAM,
                        account=user_quote,
                        dest=user,
                        owner=user,
                    )
                )
            )
        else:
            user_quote = get_associated_token_address(
                user, pool["quote_mint"], pool["quote_program"]
            )
            held = 0
            if (await client.get_account_info(user_quote)).value is not None:
                held = int(
                    (await client.get_token_account_balance(user_quote)).value.amount
                )
            if held < amount_in:
                raise ValueError(
                    f"Need {amount_in} raw units of {pool['quote_mint']}, hold {held}"
                )

        accounts = [
            AccountMeta(user, is_signer=True, is_writable=True),
            AccountMeta(CPMM_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pool["amm_config"], is_signer=False, is_writable=False),
            AccountMeta(address, is_signer=False, is_writable=True),
            AccountMeta(user_quote, is_signer=False, is_writable=True),
            AccountMeta(user_base, is_signer=False, is_writable=True),
            AccountMeta(pool["quote_vault"], is_signer=False, is_writable=True),
            AccountMeta(pool["base_vault"], is_signer=False, is_writable=True),
            AccountMeta(pool["quote_program"], is_signer=False, is_writable=False),
            AccountMeta(pool["base_program"], is_signer=False, is_writable=False),
            AccountMeta(pool["quote_mint"], is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(pool["observation"], is_signer=False, is_writable=True),
        ]
        data = SWAP_BASE_INPUT + struct.pack("<QQ", amount_in, minimum_out)
        instructions += [Instruction(CPMM_PROGRAM, data, accounts), *close_after]

        blockhash = (await client.get_latest_blockhash()).value.blockhash
        transaction = VersionedTransaction(
            MessageV0.try_compile(user, instructions, [], blockhash), [payer]
        )
        if dry_run:
            result = await client.simulate_transaction(transaction)
            print(f"\nSimulated: error={result.value.err}")
            print(f"Compute units: {result.value.units_consumed}")
            if result.value.err:
                for line in result.value.logs or []:
                    print(f"  {line}")
            return

        signature = (
            await client.send_transaction(
                transaction,
                opts=TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed),
            )
        ).value
        print(f"Sent: https://explorer.solana.com/tx/{signature}")
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")


def main() -> None:
    """Parse the command line and run the buy."""
    parser = argparse.ArgumentParser(
        description="Buy a graduated StonkFun coin from its Raydium CPMM pool"
    )
    parser.add_argument("mint", help="The coin's mint address")
    parser.add_argument(
        "amount", type=float, help="Amount to spend, in the quote asset"
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"How far below the expected token count to accept (default {DEFAULT_SLIPPAGE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the buy instead of submitting it — spends nothing",
    )
    args = parser.parse_args()
    asyncio.run(
        buy(
            Pubkey.from_string(args.mint),
            args.amount,
            args.slippage,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()

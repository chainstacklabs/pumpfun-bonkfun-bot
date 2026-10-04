"""Spend an exact amount of a StonkFun coin's quote asset on it, using buy_exact_in.

WARNING: this submits a real transaction and spends real funds.

Usage:
    uv run cookbook/stonkfun/trade/stonkfun_buy_token.py <MINT> <AMOUNT>
    uv run cookbook/stonkfun/trade/stonkfun_buy_token.py <MINT> 0.001 --dry-run

StonkFun coins are Raydium LaunchLab pools, and each one is paired with the
quote asset its launcher picked. AMOUNT is in that asset: 0.001 means 0.001 SOL
on a SOL-paired coin and 0.001 SPCXX on a SpaceX-paired one. For SOL the script
wraps the amount into a temporary account and closes it afterwards. For anything
else you must already hold the asset.

`buy_exact_in` spends exactly AMOUNT. The curve fee comes out of it first, and on
a *reward* coin the Token-2022 transfer fee then comes out of the tokens on their
way to you. The expected amount below accounts for both, so `--slippage` only
has to cover the price moving.
"""

import argparse
import asyncio
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
COMPUTE_UNIT_LIMIT = 200_000

LAUNCHLAB_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
STONKFUN_PLATFORM_CONFIGS = {
    Pubkey.from_string("4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"),
    Pubkey.from_string("6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"),
}
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

BUY_EXACT_IN_DISCRIMINATOR = bytes([250, 234, 13, 123, 213, 156, 19, 236])
# A front end may charge its own share fee on top; this script charges none.
SHARE_FEE_RATE = 0

# Rent-exempt minimum for a 165-byte token account.
TOKEN_ACCOUNT_SIZE = 165
TOKEN_ACCOUNT_RENT = 2_039_280

POOL_STATE_SIZE = 429
POOL_PUBKEYS_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8
POOL_BASE_MINT_OFFSET = POOL_PUBKEYS_OFFSET + 2 * 32
FEE_RATE_DENOMINATOR = 1_000_000
GLOBAL_TRADE_FEE_RATE_OFFSET = 8 + 8 + 1 + 2 + 8
PLATFORM_FEE_RATE_OFFSET = 8 + 8 + 32 + 32 + 3 * 8
PLATFORM_CREATOR_FEE_RATE_OFFSET = PLATFORM_FEE_RATE_OFFSET + 8 + 64 + 256 + 256 + 32
TOKEN_2022_EXTENSIONS_OFFSET = 166
TRANSFER_FEE_CONFIG_EXTENSION = 1


def decode_pool(data: bytes) -> dict:
    """Decode the PoolState fields a trade needs."""
    u64s = struct.unpack_from("<10Q", data, 8 + 8 + 5)
    pubkeys = [
        Pubkey.from_bytes(
            data[POOL_PUBKEYS_OFFSET + 32 * i : POOL_PUBKEYS_OFFSET + 32 * (i + 1)]
        )
        for i in range(7)
    ]
    return {
        "status": data[17],
        "base_decimals": data[18],
        "quote_decimals": data[19],
        "virtual_base": u64s[2],
        "virtual_quote": u64s[3],
        "real_base": u64s[4],
        "real_quote": u64s[5],
        "global_config": pubkeys[0],
        "platform_config": pubkeys[1],
        "base_mint": pubkeys[2],
        "quote_mint": pubkeys[3],
        "base_vault": pubkeys[4],
        "quote_vault": pubkeys[5],
        "creator": pubkeys[6],
    }


def transfer_fee(mint_data: bytes, epoch: int) -> tuple[int, int]:
    """The transfer fee in force for `epoch`, as (basis points, max fee raw)."""
    offset = TOKEN_2022_EXTENSIONS_OFFSET
    while offset + 4 <= len(mint_data):
        kind, length = struct.unpack_from("<HH", mint_data, offset)
        body = mint_data[offset + 4 : offset + 4 + length]
        if kind == TRANSFER_FEE_CONFIG_EXTENSION:
            older = struct.unpack_from("<QQH", body, 72)
            newer = struct.unpack_from("<QQH", body, 90)
            active = newer if epoch >= newer[0] else older
            return active[2], active[1]
        if kind == 0 and length == 0:
            break
        offset += 4 + length
    return 0, 0


def expected_tokens_out(pool: dict, amount_in: int, fee_rate: int, tax: tuple) -> int:
    """Raw tokens that arrive in your account for `amount_in` raw quote.

    The curve fee is taken from the input, rounded up. The rest trades against
    the constant-product curve, then the transfer fee is withheld on delivery.
    """
    fee = -(-amount_in * fee_rate // FEE_RATE_DENOMINATOR)
    net_in = amount_in - fee
    base_reserve = pool["virtual_base"] - pool["real_base"]
    quote_reserve = pool["virtual_quote"] + pool["real_quote"]
    out = base_reserve * net_in // (quote_reserve + net_in)
    bps, max_fee = tax
    withheld = min(-(-out * bps // 10_000), max_fee) if bps else 0
    return out - withheld


def pda(seeds: list[bytes]) -> Pubkey:
    """A LaunchLab program-derived address."""
    return Pubkey.find_program_address(seeds, LAUNCHLAB_PROGRAM)[0]


async def find_pool(client: AsyncClient, mint: Pubkey) -> Pubkey:
    """Find a coin's pool by its base mint, whatever it is quoted in."""
    response = await client.get_program_accounts(
        LAUNCHLAB_PROGRAM,
        encoding="base64",
        data_slice=DataSliceOpts(offset=0, length=0),
        filters=[
            POOL_STATE_SIZE,
            MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=str(mint)),
        ],
    )
    if not response.value:
        raise ValueError(f"No LaunchLab pool found for {mint}")
    return response.value[0].pubkey


def build_buy_exact_in(
    pool_address: Pubkey,
    pool: dict,
    payer: Pubkey,
    user_base_account: Pubkey,
    user_quote_account: Pubkey,
    quote_token_program: Pubkey,
    amount_in: int,
    minimum_amount_out: int,
) -> Instruction:
    """The 15 accounts the IDL names, then three the program reads positionally.

    The fee vaults are keyed by quote mint, so a coin paired with anything but
    SOL needs them derived with its own quote, not with wrapped SOL.
    """
    quote_mint = pool["quote_mint"]
    accounts = [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(pda([b"vault_auth_seed"]), is_signer=False, is_writable=False),
        AccountMeta(pool["global_config"], is_signer=False, is_writable=False),
        AccountMeta(pool["platform_config"], is_signer=False, is_writable=False),
        AccountMeta(pool_address, is_signer=False, is_writable=True),
        AccountMeta(user_base_account, is_signer=False, is_writable=True),
        AccountMeta(user_quote_account, is_signer=False, is_writable=True),
        AccountMeta(pool["base_vault"], is_signer=False, is_writable=True),
        AccountMeta(pool["quote_vault"], is_signer=False, is_writable=True),
        AccountMeta(pool["base_mint"], is_signer=False, is_writable=False),
        AccountMeta(quote_mint, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(quote_token_program, is_signer=False, is_writable=False),
        AccountMeta(pda([b"__event_authority"]), is_signer=False, is_writable=False),
        AccountMeta(LAUNCHLAB_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pda([bytes(pool["platform_config"]), bytes(quote_mint)]),
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(
            pda([bytes(pool["creator"]), bytes(quote_mint)]),
            is_signer=False,
            is_writable=True,
        ),
    ]
    data = BUY_EXACT_IN_DISCRIMINATOR + struct.pack(
        "<QQQ", amount_in, minimum_amount_out, SHARE_FEE_RATE
    )
    return Instruction(LAUNCHLAB_PROGRAM, data, accounts)


async def buy(mint: Pubkey, amount: float, slippage: float, *, dry_run: bool) -> None:
    """Spend exactly `amount` of the coin's quote asset on it."""
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    user = payer.pubkey()

    async with AsyncClient(RPC_ENDPOINT, commitment=Confirmed) as client:
        pool_address = await find_pool(client, mint)
        pool = decode_pool((await client.get_account_info(pool_address)).value.data)
        if pool["platform_config"] not in STONKFUN_PLATFORM_CONFIGS:
            raise ValueError(f"{mint} is a LaunchLab coin, but not a StonkFun one")
        if pool["status"] != 0:
            raise ValueError(f"{mint} has left the curve (status {pool['status']})")

        quote_mint = pool["quote_mint"]
        global_info, platform_info, mint_info, quote_info = (
            await client.get_multiple_accounts(
                [pool["global_config"], pool["platform_config"], mint, quote_mint]
            )
        ).value
        fee_rate = (
            struct.unpack_from("<Q", global_info.data, GLOBAL_TRADE_FEE_RATE_OFFSET)[0]
            + struct.unpack_from("<Q", platform_info.data, PLATFORM_FEE_RATE_OFFSET)[0]
            + struct.unpack_from(
                "<Q", platform_info.data, PLATFORM_CREATOR_FEE_RATE_OFFSET
            )[0]
        )
        epoch = (await client.get_epoch_info()).value.epoch
        tax = transfer_fee(mint_info.data, epoch)
        # xStocks and some other quotes are Token-2022 mints; the program needs
        # the quote's own token program, so read it rather than assume.
        quote_token_program = quote_info.owner

        quote_unit = 10 ** pool["quote_decimals"]
        base_unit = 10 ** pool["base_decimals"]
        amount_in = int(amount * quote_unit)
        expected = expected_tokens_out(pool, amount_in, fee_rate, tax)
        minimum_out = int(expected * (1 - slippage))

        print(f"Mint:          {mint}")
        print(f"Pool:          {pool_address}")
        print(f"Quote asset:   {quote_mint}")
        print(f"Spending:      {amount} ({amount_in} raw)")
        print(f"Curve fee:     {fee_rate / FEE_RATE_DENOMINATOR:.2%}")
        print(f"Transfer tax:  {tax[0] / 100:.2f}%")
        print(f"Expecting:     {expected / base_unit:,.6f} tokens after fee and tax")
        print(f"Accepting:     >= {minimum_out / base_unit:,.6f} tokens")

        user_base_account = get_associated_token_address(user, mint, TOKEN_2022_PROGRAM)
        instructions = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            create_idempotent_associated_token_account(
                user, user, mint, token_program_id=TOKEN_2022_PROGRAM
            ),
        ]

        is_sol = quote_mint == WSOL_MINT
        if is_sol:
            # A throwaway account at a fresh seed, funded with the spend and
            # closed in the same transaction. It leaves any WSOL you already
            # hold alone.
            seed = secrets.token_hex(16)
            user_quote_account = Pubkey.create_with_seed(user, seed, TOKEN_PROGRAM)
            instructions += [
                create_account_with_seed(
                    CreateAccountWithSeedParams(
                        from_pubkey=user,
                        to_pubkey=user_quote_account,
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
                        account=user_quote_account,
                        mint=WSOL_MINT,
                        owner=user,
                    )
                ),
            ]
        else:
            user_quote_account = get_associated_token_address(
                user, quote_mint, quote_token_program
            )
            held = 0
            if (await client.get_account_info(user_quote_account)).value is not None:
                balance = await client.get_token_account_balance(user_quote_account)
                held = int(balance.value.amount)
            if held < amount_in:
                raise ValueError(
                    f"Need {amount_in} raw units of {quote_mint}, hold {held}"
                )

        instructions.append(
            build_buy_exact_in(
                pool_address,
                pool,
                user,
                user_base_account,
                user_quote_account,
                quote_token_program,
                amount_in,
                minimum_out,
            )
        )
        if is_sol:
            instructions.append(
                close_account(
                    CloseAccountParams(
                        program_id=TOKEN_PROGRAM,
                        account=user_quote_account,
                        dest=user,
                        owner=user,
                    )
                )
            )

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
        description="Spend an exact amount of a StonkFun coin's quote asset on it"
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

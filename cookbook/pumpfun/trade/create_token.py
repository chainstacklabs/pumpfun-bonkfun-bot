"""Create a pump.fun coin with create_v2, without buying any of it.

WARNING: this submits a real transaction and spends real funds (transaction
fees and account rent — no coins are bought).

Usage:
    uv run cookbook/pumpfun/trade/create_token.py
    uv run cookbook/pumpfun/trade/create_token.py --name "My Coin" --symbol MINE
    uv run cookbook/pumpfun/trade/create_token.py --mayhem --holder-reward

`mint_and_buy_v2.py` does this and then buys the coin in a second transaction.
This script is the create half on its own, which is the part worth reading:
everything about a coin — Token-2022, mayhem mode, holder rewards, the quote
asset — is fixed here and cannot be changed afterwards.

Two things that surprise people:

- The mint is a **keypair you generate**, not something pump.fun hands back.
  It signs the create transaction alongside your wallet.
- `create_v2` mints under **Token-2022**, so the associated bonding curve is a
  Token-2022 ATA. Deriving it with SPL Token gives a valid-looking address
  that does not exist on chain.

The `extend_account` instruction that follows the create is what makes the coin
visible on pump.fun's own frontend. The coin trades without it.
"""

import argparse
import asyncio
import os
import struct
import sys
from pathlib import Path
from typing import Final

# tx_status.py is a shared helper at the cookbook root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import base58
import tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

PUMP_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMP_GLOBAL: Final[Pubkey] = Pubkey.from_string(
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
)
PUMP_EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_MINT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
)
SYSTEM_PROGRAM: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_2022_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
ASSOCIATED_TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Mayhem mode is a separate program with its own accounts, appended to the
# create_v2 account list only when the coin opts into it.
MAYHEM_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
)
MAYHEM_GLOBAL_PARAMS: Final[Pubkey] = Pubkey.from_string(
    "13ec7XdrjF3h3YcqBTFDSReRcUFwbCnJaAQspM4j6DDJ"
)
MAYHEM_SOL_VAULT: Final[Pubkey] = Pubkey.from_string(
    "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
)

# First 8 bytes of sha256("global:<instruction name>").
CREATE_V2_DISCRIMINATOR: Final[bytes] = bytes([214, 144, 76, 236, 95, 139, 49, 180])
EXTEND_ACCOUNT_DISCRIMINATOR: Final[bytes] = bytes(
    [234, 102, 194, 203, 150, 72, 62, 229]
)

COMPUTE_UNIT_LIMIT: Final[int] = 350_000
PRIORITY_FEE_MICROLAMPORTS: Final[int] = 37_037


def encode_string(value: str) -> bytes:
    """Encode a string the way Borsh does: a u32 length, then the bytes.

    Args:
        value: The string to encode

    Returns:
        Length-prefixed UTF-8 bytes
    """
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def build_create_v2_instruction(  # noqa: PLR0913
    mint: Pubkey,
    user: Pubkey,
    name: str,
    symbol: str,
    uri: str,
    *,
    is_mayhem_mode: bool,
    is_holder_reward: bool,
) -> Instruction:
    """Build the create_v2 instruction.

    Args:
        mint: The new coin's mint (a keypair you generate)
        user: Wallet paying for and creating the coin
        name: Coin name
        symbol: Coin ticker
        uri: Metadata URI
        is_mayhem_mode: Whether the coin opts into mayhem mode
        is_holder_reward: Whether the creator fee is set aside for holders

    Returns:
        The create_v2 instruction
    """
    bonding_curve = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)], PUMP_PROGRAM
    )[0]
    # An ordinary ATA, but under Token-2022 because create_v2 mints there.
    associated_bonding_curve = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(TOKEN_2022_PROGRAM), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]

    accounts = [
        AccountMeta(pubkey=mint, is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_MINT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_2022_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False
        ),
    ]

    # The five mayhem accounts are mandatory even on a coin that is not a mayhem
    # coin — the IDL marks none of them optional, and omitting them fails with
    # AnchorError 3005 (AccountNotEnoughKeys) on sol_vault. The is_mayhem_mode
    # argument below, not the account list, is what makes a coin a mayhem coin.
    # Verified by simulateTransaction against mainnet, 2026-09-22: 11 accounts
    # fails 3005, 16 accounts succeeds with the argument either way.
    mayhem_state = Pubkey.find_program_address(
        [b"mayhem-state", bytes(mint)], MAYHEM_PROGRAM
    )[0]
    accounts += [
        AccountMeta(pubkey=MAYHEM_PROGRAM, is_signer=False, is_writable=True),
        AccountMeta(pubkey=MAYHEM_GLOBAL_PARAMS, is_signer=False, is_writable=False),
        AccountMeta(pubkey=MAYHEM_SOL_VAULT, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mayhem_state, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=get_associated_token_address(
                MAYHEM_SOL_VAULT, mint, TOKEN_2022_PROGRAM
            ),
            is_signer=False,
            is_writable=True,
        ),
    ]

    accounts += [
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]

    data = (
        CREATE_V2_DISCRIMINATOR
        + encode_string(name)
        + encode_string(symbol)
        + encode_string(uri)
        + bytes(user)  # creator
        + struct.pack("<?", is_mayhem_mode)
    )

    if is_holder_reward:
        # The three trailing args are positional, not independently optional:
        # reaching is_holder_reward means sending is_cashback_enabled and
        # creator_fee_bps first, even though both are unused here. Neither is
        # a discriminated Option — each serializes as its bare inner value,
        # one byte and a little-endian u64.
        #
        # is_cashback_enabled is always False: create_v2 has rejected [true]
        # with error 6082 (CashbackDeprecated) since 2026-09-15.
        is_cashback_enabled = False
        creator_fee_bps = 0
        data += struct.pack("<?", is_cashback_enabled)
        data += struct.pack("<Q", creator_fee_bps)
        data += struct.pack("<?", is_holder_reward)

    return Instruction(PUMP_PROGRAM, data, accounts)


def build_extend_account_instruction(
    bonding_curve: Pubkey, user: Pubkey
) -> Instruction:
    """Build the extend_account instruction, which takes no arguments.

    Args:
        bonding_curve: The coin's bonding curve
        user: Wallet paying for the extra account space

    Returns:
        The extend_account instruction
    """
    accounts = [
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
    ]
    return Instruction(PUMP_PROGRAM, EXTEND_ACCOUNT_DISCRIMINATOR, accounts)


async def create(
    name: str, symbol: str, uri: str, *, mayhem: bool, holder_reward: bool
) -> None:
    """Create one coin and print its mint.

    Args:
        name: Coin name
        symbol: Coin ticker
        uri: Metadata URI
        mayhem: Whether to opt into mayhem mode
        holder_reward: Whether the creator fee goes to holders
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    mint_keypair = Keypair()
    mint = mint_keypair.pubkey()
    bonding_curve = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)], PUMP_PROGRAM
    )[0]

    print(f"Mint:    {mint}")
    print(f"Curve:   {bonding_curve}")
    print(f"Creator: {payer.pubkey()}")
    print(f"Name:    {name} ({symbol})")
    print(f"Mayhem:  {mayhem}   Holder reward: {holder_reward}")

    instructions = [
        set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
        set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
        build_create_v2_instruction(
            mint,
            payer.pubkey(),
            name,
            symbol,
            uri,
            is_mayhem_mode=mayhem,
            is_holder_reward=holder_reward,
        ),
        build_extend_account_instruction(bonding_curve, payer.pubkey()),
    ]

    async with AsyncClient(RPC_ENDPOINT) as client:
        blockhash = (await client.get_latest_blockhash()).value.blockhash
        # The mint signs too — it is a brand-new account being created.
        transaction = Transaction(
            [payer, mint_keypair], Message(instructions, payer.pubkey()), blockhash
        )
        signature = (
            await client.send_transaction(
                transaction,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed),
            )
        ).value

        print(f"\nSent: https://explorer.solana.com/tx/{signature}")
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")
        print(f"\nBuy it with:  uv run cookbook/pumpfun/trade/buy_token.py {mint}")


def main() -> None:
    """Parse the command line and create the coin."""
    parser = argparse.ArgumentParser(description="Create one pump.fun coin")
    parser.add_argument("--name", default="Test Token", help="Coin name")
    parser.add_argument("--symbol", default="TEST", help="Coin ticker")
    parser.add_argument(
        "--uri", default="https://example.com/token.json", help="Metadata URI"
    )
    parser.add_argument("--mayhem", action="store_true", help="Enable mayhem mode")
    parser.add_argument(
        "--holder-reward",
        action="store_true",
        help="Set the creator fee aside for holders instead of a creator wallet",
    )
    args = parser.parse_args()

    asyncio.run(
        create(
            args.name,
            args.symbol,
            args.uri,
            mayhem=args.mayhem,
            holder_reward=args.holder_reward,
        )
    )


if __name__ == "__main__":
    main()

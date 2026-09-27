"""Create a pump.fun coin with create_v2 in a v1 transaction, without buying it.

WARNING: this submits a real transaction and spends real funds (transaction fees
and account rent — no coins are bought).

Usage:
    uv run cookbook/pumpfun/trade/pumpfun_create_token_txv1.py
    uv run cookbook/pumpfun/trade/pumpfun_create_token_txv1.py --name "My Coin" --symbol MINE
    uv run cookbook/pumpfun/trade/pumpfun_create_token_txv1.py --mayhem --holder-reward
    uv run cookbook/pumpfun/trade/pumpfun_create_token_txv1.py --creator-fee-bps 300

`pumpfun_create_token_txv0.py` is the same create sent as a v0 transaction —
pick whichever the endpoint takes. A v1 message carries the compute budget in
its own header, so this one sends no `ComputeBudget` instructions and states the
priority fee as a total in lamports rather than micro-lamports per compute unit. `pumpfun_create_and_buy_token_txv1.py` does
this and then buys the coin in the same transaction; its `_txv0` variant does it
in two. This is the
create half on its own: everything about a coin — Token-2022, mayhem mode, the
creator fee, holder rewards, the quote asset — is fixed here and cannot be
changed afterwards.

- The mint is a **keypair you generate**, not something pump.fun hands back. It
  signs the create transaction alongside your wallet.
- `create_v2` mints under **Token-2022**, so the associated bonding curve is a
  Token-2022 ATA. Deriving it with SPL Token gives a valid-looking address that
  does not exist on chain.
- `--creator` need not be the wallet that pays. On a `--holder-reward` coin
  neither is what the curve ends up carrying — the program substitutes its own
  address so the fee accrues to holders.
- **`--creator-fee-bps` only does anything on a coin priced in something other
  than SOL.** pump.fun accepts the argument on a SOL-paired coin and stores
  zero, with no error. Pass `--quote-mint` alongside it.
- `--quote-mint` is checked against **`QuoteControl`**, not
  `Global.whitelisted_quote_mints`: the former admits the mints coins are
  actually priced in, the latter lists one. `pumpfun_read_quote_mints.py`
  prints the registry.

The `extend_account` instruction that follows the create is what makes the coin
visible on pump.fun's own frontend. The coin trades without it.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Final

# solana_transaction_status.py lives in cookbook/solana/; pumpfun_instructions.py sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solana"))

import base58
import pumpfun_instructions as pump
import solana_transaction_status as tx_status
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.keypair import Keypair
from solders.message import MessageV1, TransactionConfig
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY")

COMPUTE_UNIT_LIMIT: Final[int] = 350_000
# A v1 message states one absolute figure where v0 stated micro-lamports per
# compute unit; 37,037 microlamports/CU across this CU limit is the same spend.
PRIORITY_FEE_LAMPORTS: Final[int] = 12_963
# Required on a v1 message: left unset it is zero, not the network default, and
# the transaction is rejected for exceeding it. The figure counts the executable
# data of every program touched, which is megabytes, not the kilobytes the
# account list suggests. This is the default a v0 transaction gets.
LOADED_ACCOUNTS_DATA_LIMIT: Final[int] = 64 * 1024 * 1024

# Defaults for the command line below, not fixed settings. Mayhem is off because
# the plain coin is the one a reader gets by typing nothing.
DEFAULT_MAYHEM = False


async def get_parsed_mint(client: AsyncClient, address: Pubkey) -> dict[str, Any]:
    """Fetch a mint decoded by the RPC, so its Token-2022 extensions are readable.

    Raises:
        ValueError: If the account does not exist or is not a parsed mint
    """
    # solana-py exposes jsonParsed as its own method; passing
    # encoding="jsonParsed" to get_account_info returns a payload its typed
    # response cannot deserialize, and fails with a bare SerdeJSONError.
    response = await client.get_account_info_json_parsed(address)
    if response.value is None:
        msg = f"Quote mint not found: {address}"
        raise ValueError(msg)
    try:
        return response.value.data.parsed["info"]
    except (AttributeError, KeyError, TypeError) as error:
        msg = f"{address} is not a token mint"
        raise ValueError(msg) from error


async def get_account(client: AsyncClient, address: Pubkey) -> Account:
    """Fetch an account, unwrapping solana-py's `.value` envelope.

    Raises:
        ValueError: If the account does not exist
    """
    response = await client.get_account_info(address, encoding="base64")
    if response.value is None:
        msg = f"Account not found: {address}"
        raise ValueError(msg)
    return response.value


def check_creator_fee(global_state: dict, creator_fee_bps: int | None) -> None:
    """Refuse a creator fee the program will not accept.

    Bounds come from Global rather than a literal, because both the switch and
    the ceiling move under `update_creator_fee_config`.

    Args:
        global_state: Decoded Global account
        creator_fee_bps: Requested fee, or None if the arg is being omitted

    Raises:
        ValueError: If a fee is requested while fees are not configurable, or
            above the current ceiling
    """
    if not creator_fee_bps:
        return
    if not global_state.get("creator_fee_configurable"):
        msg = (
            "Global.creator_fee_configurable is false; the program is not "
            "accepting a configurable creator fee right now"
        )
        raise ValueError(msg)
    ceiling = global_state.get("max_configurable_creator_fee_bps", 0)
    if creator_fee_bps > ceiling:
        msg = (
            f"--creator-fee-bps {creator_fee_bps} is above Global's current "
            f"max_configurable_creator_fee_bps of {ceiling}"
        )
        raise ValueError(msg)


async def create(  # noqa: PLR0913
    *,
    name: str,
    symbol: str,
    uri: str,
    creator: Pubkey | None,
    mayhem: bool,
    creator_fee_bps: int | None,
    holder_reward: bool | None,
    quote_mint: Pubkey,
) -> None:
    """Create one coin and print its mint.

    Args:
        name: Coin name
        symbol: Coin ticker
        uri: Metadata URI
        creator: Creator written into the curve; the payer if None
        mayhem: Whether to opt into mayhem mode
        creator_fee_bps: Creator fee; None omits the argument. Ignored by the
            program unless quote_mint is not SOL
        holder_reward: Whether the creator fee goes to holders; None omits it
        quote_mint: Asset the coin is priced in
    """
    payer = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    mint_keypair = Keypair()
    mint = mint_keypair.pubkey()
    creator = creator or payer.pubkey()
    bonding_curve = pump.find_bonding_curve(mint)

    async with AsyncClient(RPC_ENDPOINT) as client:
        global_state = await pump.fetch_global(lambda pk: get_account(client, pk))
        check_creator_fee(global_state, creator_fee_bps)
        pump.check_mayhem_quote_pairing(quote_mint, is_mayhem_mode=mayhem)
        # The token program is a property of the chosen mint, so it is read, not
        # assumed: create_v2 takes an SPL Token or a Token-2022 quote mint and
        # the associated_quote_bonding_curve ATA derives under whichever it is.
        quote_program = await pump.resolve_quote_token_program(
            quote_mint, lambda pk: get_account(client, pk)
        )
        registry: dict = {}
        if quote_mint != pump.WSOL_MINT or creator_fee_bps:
            registry = await pump.fetch_quote_control(
                lambda pk: get_account(client, pk)
            )
        # Refuses a fee against a mint that would silently store zero.
        pump.check_creator_fee_quote(registry, quote_mint, creator_fee_bps)
        if quote_mint != pump.WSOL_MINT:
            # Raises for a mint QuoteControl does not admit.
            opening = pump.opening_quote_reserves(registry, quote_mint, global_state)
            multiplier = pump.check_quote_mint_tradable(
                quote_mint, await get_parsed_mint(client, quote_mint)
            )
            print(f"Quote:   {quote_mint}")
            print(f"  opening virtual quote reserves: {opening:,} raw units")
            if multiplier is not None and multiplier != 1.0:
                print(
                    f"  note: scaled-UI mint, multiplier {multiplier}. Trade "
                    f"amounts here are raw units, not displayed units."
                )

        # What the curve will actually carry, which is not `creator` on a
        # holder-reward coin. Printed because it is what a buy must derive
        # creator_vault from.
        on_curve = pump.curve_creator(
            mint, creator, is_holder_reward=bool(holder_reward)
        )

        print(f"Mint:    {mint}")
        print(f"Curve:   {bonding_curve}")
        print(f"Payer:   {payer.pubkey()}")
        print(f"Creator: {creator}" + ("  (arg)" if on_curve != creator else ""))
        if on_curve != creator:
            print(f"  -> curve will carry {on_curve} (holder rewards PDA)")
        print(f"Name:    {name} ({symbol})")
        print(
            f"Mayhem:  {mayhem}   Creator fee: "
            f"{'omitted' if creator_fee_bps is None else f'{creator_fee_bps} bps'}"
            f"   Holder reward: {'omitted' if holder_reward is None else holder_reward}"
        )

        instructions = [
            pump.build_create_v2_instruction(
                mint=mint,
                user=payer.pubkey(),
                creator=creator,
                name=name,
                symbol=symbol,
                uri=uri,
                is_mayhem_mode=mayhem,
                creator_fee_bps=creator_fee_bps,
                is_holder_reward=holder_reward,
                quote_mint=quote_mint,
                quote_token_program=quote_program,
            ),
            pump.build_extend_account_instruction(bonding_curve, payer.pubkey()),
        ]

        blockhash = (await client.get_latest_blockhash()).value.blockhash
        # The mint signs too — it is a brand-new account being created.
        transaction = VersionedTransaction(
            MessageV1.try_compile(
                payer.pubkey(),
                instructions,
                blockhash,
                TransactionConfig(
                    compute_unit_limit=COMPUTE_UNIT_LIMIT,
                    priority_fee=PRIORITY_FEE_LAMPORTS,
                    loaded_accounts_data_size_limit=LOADED_ACCOUNTS_DATA_LIMIT,
                ),
            ),
            [payer, mint_keypair],
        )
        signature = (
            await client.send_transaction(
                transaction,
                opts=TxOptsModel(skip_preflight=True, preflight_commitment=Confirmed),
            )
        ).value

        print(f"\nSent: https://explorer.solana.com/tx/{signature}")
        await tx_status.confirm_and_assert(client, signature)
        print("Confirmed")
        print(
            f"\nBuy it with:  uv run cookbook/pumpfun/trade/pumpfun_buy_token.py {mint}"
        )


def main() -> None:
    """Parse the command line and create the coin."""
    parser = argparse.ArgumentParser(description="Create one pump.fun coin")
    parser.add_argument("--name", default="Test Token", help="Coin name")
    parser.add_argument("--symbol", default="TEST", help="Coin ticker")
    parser.add_argument(
        "--uri", default="https://example.com/token.json", help="Metadata URI"
    )
    parser.add_argument(
        "--creator",
        type=Pubkey.from_string,
        default=None,
        help="Creator written into the curve (default: the paying wallet)",
    )
    parser.add_argument(
        "--quote-mint",
        type=Pubkey.from_string,
        default=pump.WSOL_MINT,
        help=(
            "Asset the coin is priced in (default wrapped SOL). Must be a mint "
            "QuoteControl admits; required for --creator-fee-bps to take effect"
        ),
    )
    parser.add_argument(
        "--mayhem",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAYHEM,
        help=f"Enable mayhem mode (default {DEFAULT_MAYHEM})",
    )
    parser.add_argument(
        "--creator-fee-bps",
        type=int,
        default=None,
        help=(
            "Creator fee in basis points, validated against Global. Omitted "
            "entirely by default; pass 0 to send an explicit zero"
        ),
    )
    parser.add_argument(
        "--holder-reward",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Set the creator fee aside for holders. Omitted entirely by "
            "default; --no-holder-reward sends an explicit false"
        ),
    )
    args = parser.parse_args()

    asyncio.run(
        create(
            name=args.name,
            symbol=args.symbol,
            uri=args.uri,
            creator=args.creator,
            mayhem=args.mayhem,
            creator_fee_bps=args.creator_fee_bps,
            holder_reward=args.holder_reward,
            quote_mint=args.quote_mint,
        )
    )


if __name__ == "__main__":
    main()

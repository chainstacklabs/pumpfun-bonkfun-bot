"""Launch a coin on Raydium LaunchLab under any platform config, on a local validator.

For testing the bot against a launch it can see: start surfpool forking
mainnet, run a bot with `platform: launchlab` (or a preset) against it, then
launch here. Pool initialization is permissionless, so any platform config on
mainnet works once surfpool has forked it.

Refuses any RPC that is not on this machine: the wallet in `.env` pays the
launch, and a launch on mainnet would spend real funds.

The coin is a constant-product curve with letsbonk's and StonkFun's parameters
unless overridden. `initialize_v2` makes an SPL Token coin with Metaplex
metadata; `--token-2022` uses `initialize_with_token_2022`, with an optional
transfer fee. Platforms that restrict their global configs or curve parameters
need the allow-config and curve-rule PDAs after the IDL's accounts, so they are
always passed.

Prints the mint on stdout, the signature on stderr.

Usage:
    uv run tools/launchlab_create_coin.py --platform-config <ADDRESS>
    uv run tools/launchlab_create_coin.py --platform-config <ADDRESS> \\
        --token-2022 --transfer-fee-bps 100 --symbol TEST
"""

import argparse
import asyncio
import os
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solana.rpc.async_api import AsyncClient  # noqa: E402
from solders.compute_budget import (  # noqa: E402
    set_compute_unit_limit,
    set_compute_unit_price,
)
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.pubkeys import SystemAddresses  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

WSOL = SystemAddresses.WSOL_MINT
METAPLEX = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
CONSTANT_CURVE = 0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _pda(*seeds: bytes) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), LAUNCHLAB_PROGRAM)[0]


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def build_launch(
    payer: Pubkey,
    mint: Pubkey,
    quote_token_program: Pubkey,
    args: argparse.Namespace,
) -> Instruction:
    """The pool initialization, with the two restriction PDAs appended."""
    platform_config, quote_mint = args.platform_config, args.quote_mint
    idl = get_idl_manager().get_parser(Platform.LAUNCHLAB)
    name = "initialize_with_token_2022" if args.token_2022 else "initialize_v2"
    data = idl.get_instruction_discriminators()[name]
    data += struct.pack("<B", args.decimals)
    data += _string(args.name) + _string(args.symbol) + _string(args.uri)
    # CurveParams::Constant { supply, total_base_sell, total_quote_fund_raising,
    # migrate_type }, then VestingParams (none) and AmmCreatorFeeOn::QuoteToken.
    data += struct.pack(
        "<BQQQB",
        CONSTANT_CURVE,
        args.supply,
        args.total_base_sell,
        args.fund_raising,
        args.migrate_type,
    )
    data += struct.pack("<QQQ", 0, 0, 0)
    data += struct.pack("<B", 0)
    if args.token_2022:
        if args.transfer_fee_bps:
            data += struct.pack("<BHQ", 1, args.transfer_fee_bps, args.supply)
        else:
            data += struct.pack("<B", 0)

    global_config = _pda(
        b"global_config",
        bytes(quote_mint),
        bytes([CONSTANT_CURVE]),
        struct.pack("<H", 0),
    )
    pool = _pda(b"pool", bytes(mint), bytes(quote_mint))

    def meta(
        key: Pubkey, *, signer: bool = False, writable: bool = False
    ) -> AccountMeta:
        return AccountMeta(key, signer, writable)

    accounts = [
        meta(payer, signer=True, writable=True),
        meta(payer),  # creator
        meta(global_config),
        meta(platform_config),
        meta(_pda(b"vault_auth_seed")),
        meta(pool, writable=True),
        meta(mint, signer=True, writable=True),
        meta(quote_mint),
        meta(_pda(b"pool_vault", bytes(pool), bytes(mint)), writable=True),
        meta(_pda(b"pool_vault", bytes(pool), bytes(quote_mint)), writable=True),
    ]
    event_authority = _pda(b"__event_authority")
    if args.token_2022:
        accounts += [
            meta(SystemAddresses.TOKEN_2022_PROGRAM),
            meta(quote_token_program),
            meta(SystemAddresses.SYSTEM_PROGRAM),
            meta(event_authority),
            meta(LAUNCHLAB_PROGRAM),
        ]
    else:
        metadata = Pubkey.find_program_address(
            [b"metadata", bytes(METAPLEX), bytes(mint)], METAPLEX
        )[0]
        accounts += [
            meta(metadata, writable=True),
            meta(SystemAddresses.TOKEN_PROGRAM),
            meta(quote_token_program),
            meta(METAPLEX),
            meta(SystemAddresses.SYSTEM_PROGRAM),
            meta(SystemAddresses.RENT),
            meta(event_authority),
            meta(LAUNCHLAB_PROGRAM),
        ]
    seeds = (bytes(platform_config), bytes(global_config))
    accounts += [
        meta(_pda(b"platform_allow_config", *seeds)),
        meta(_pda(b"platform_curve_rule", *seeds)),
    ]
    return Instruction(LAUNCHLAB_PROGRAM, bytes(data), accounts)


async def _confirm(
    client: AsyncClient, signature: object, timeout: float = 30.0
) -> None:
    """Wait for the launch to land, and raise if it reverted."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = (await client.get_signature_statuses([signature])).value[0]
        if status is not None and status.confirmation_status is not None:
            if status.err is not None:
                raise RuntimeError(f"launch {signature} reverted: {status.err}")  # noqa: TRY003
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"launch {signature} not confirmed in {timeout}s")  # noqa: TRY003


async def main() -> int:
    """Parse the arguments, launch, print the mint."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform-config", required=True, type=Pubkey.from_string)
    parser.add_argument("--rpc", default="http://127.0.0.1:8899")
    parser.add_argument("--quote-mint", default=str(WSOL), type=Pubkey.from_string)
    parser.add_argument("--token-2022", action="store_true")
    parser.add_argument(
        "--transfer-fee-bps", type=int, default=0, help="Token-2022 only"
    )
    parser.add_argument("--name", default="LaunchLab Test")
    parser.add_argument("--symbol", default="LLTEST")
    parser.add_argument("--uri", default="https://example.com/coin.json")
    parser.add_argument("--decimals", type=int, default=6)
    parser.add_argument("--supply", type=int, default=10**15, help="raw base units")
    parser.add_argument("--total-base-sell", type=int, default=793_100_000_000_000)
    parser.add_argument(
        "--fund-raising",
        type=int,
        default=85 * 10**9,
        help="raw quote units the curve raises before it graduates",
    )
    parser.add_argument("--migrate-type", type=int, default=1, help="1 CPMM, 0 AMM v4")
    args = parser.parse_args()

    if urlparse(args.rpc).hostname not in LOCAL_HOSTS:
        print(
            f"refusing a non-local RPC: {urlparse(args.rpc).hostname}", file=sys.stderr
        )
        return 2
    if args.transfer_fee_bps and not args.token_2022:
        print("--transfer-fee-bps needs --token-2022", file=sys.stderr)
        return 2

    payer = Keypair.from_base58_string(os.environ["SOLANA_PRIVATE_KEY"])
    mint = Keypair()
    async with AsyncClient(args.rpc) as client:
        quote_account = (await client.get_account_info(args.quote_mint)).value
        if quote_account is None:
            print(f"quote mint {args.quote_mint} not found", file=sys.stderr)
            return 1
        launch = build_launch(payer.pubkey(), mint.pubkey(), quote_account.owner, args)
        blockhash = (await client.get_latest_blockhash()).value.blockhash
        message = MessageV0.try_compile(
            payer.pubkey(),
            [set_compute_unit_limit(400_000), set_compute_unit_price(10_000), launch],
            [],
            blockhash,
        )
        transaction = VersionedTransaction(message, [payer, mint])
        signature = (await client.send_raw_transaction(bytes(transaction))).value
        await _confirm(client, signature)
    print(f"Signature: {signature}", file=sys.stderr)
    print(mint.pubkey())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

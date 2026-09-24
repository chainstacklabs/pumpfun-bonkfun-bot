"""Watch for pump.fun coins approaching graduation, over Geyser gRPC.

Usage:
    uv run cookbook/pumpfun/graduation/pumpfun_watch_graduating_geyser.py
    uv run cookbook/pumpfun/graduation/pumpfun_watch_graduating_geyser.py --min-progress 95

Needs GEYSER_ENDPOINT, GEYSER_API_TOKEN and GEYSER_AUTH_TYPE in .env, plus
SOLANA_NODE_RPC_ENDPOINT for the two things the stream cannot answer: the Global
baseline and each coin's mint. Geyser is a paid add-on;
`pumpfun_watch_graduating_programsubscribe.py` is the portable version of this
report and runs on any endpoint. This variant gives the slot and transaction
signature behind every update, which the WebSocket feed does not.

Why a subscription and not `getProgramAccounts`: the pump.fun program owns over
10 million accounts and every provider refuses to scan it — the rejection is on
program size, before filters apply. A curve can only approach graduation by being
traded, and every write pushes the full account, so each update carries
everything needed to compute progress.

Selecting a graduation threshold
--------------------------------
Progress is measured against `Global.initial_real_token_reserves` read from
chain, not a hardcoded constant: a mayhem coin can launch with different virtual
params and would otherwise show the wrong percentage.

The server-side pre-filter matches exact bytes, so it cannot express "anything
above 90%" — only a few fixed cutoffs. `--min-progress` picks the closest cutoff
that is still wide enough, then makes the exact comparison here:

    filter               cutoff                 ~ graduated past
    2 zero bytes @ 30    281.5M tokens left     64.5%
    3 zero bytes @ 29    1.1M tokens left       99.86%
    4 zero bytes @ 28    4,295 tokens left      99.9995%

So the pre-filter saves bandwidth, it does not decide the answer — whatever
percentage you ask for is honoured either way.

This script does **not** filter on `datasize`. Geyser ORs across named account
groups and ANDs within one, so enumerating lengths means one group per length —
and `extend_account` can grow a curve to any length the program allows (125, 151
and 256 all occur live), so no finite set of groups closes the gap. The
discriminator memcmp alone already restricts delivery to `BondingCurve`
accounts, so naming no length loses nothing; a client-side `MIN_CURVE_LEN` floor
keeps a stray short or legacy account out of the decoder.
"""

import argparse
import asyncio
import os
import struct
import sys
from pathlib import Path
from typing import Any, Final

import grpc
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.core import TokenAccountOpts
from solders.pubkey import Pubkey
from solders.signature import Signature

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.geyser.generated import (
    geyser_pb2,
    geyser_pb2_grpc,
)

load_dotenv()

# Constants
RPC_ENDPOINT: Final[str] = os.environ.get("SOLANA_NODE_RPC_ENDPOINT", "")
GEYSER_ENDPOINT: Final[str] = os.environ.get("GEYSER_ENDPOINT", "")
GEYSER_API_TOKEN: Final[str] = os.environ.get("GEYSER_API_TOKEN", "")
GEYSER_AUTH_TYPE: Final[str] = os.environ.get("GEYSER_AUTH_TYPE", "x-token").lower()

PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMP_GLOBAL: Final[Pubkey] = Pubkey.from_string(
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
)

# Coins created by `create_v2` are Token-2022, so that is tried first. Querying under
# the wrong token program returns nothing at all.
TOKEN_2022_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
TOKEN_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)

# See cookbook/solana/anchor_calculate_discriminator.py
BONDING_CURVE_DISCRIMINATOR: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")

# create_v2 allocates the 125-byte struct; extend_account can grow it to any
# length the program allows (151 and 256 both occur live), so there is no fixed
# set to enumerate. This is a floor, not an allowlist: anything shorter than the
# smallest real struct is dropped, everything at or above it is decoded.
MIN_CURVE_LEN: Final[int] = 125

TOKEN_DECIMALS: Final[int] = 6
_RESERVES_OFFSET: Final[int] = 24  # real_token_reserves, u64 LE
_COMPLETE_OFFSET: Final[int] = 48
_QUOTE_MINT_OFFSET: Final[int] = 83
_GLOBAL_INITIAL_REAL_TOKEN_RESERVES_OFFSET: Final[int] = 89

_BAD_DISCRIMINATOR_MSG: Final[str] = "Invalid discriminator for bonding curve"
_BAD_AUTH_TYPE_MSG: Final[str] = "GEYSER_AUTH_TYPE must be 'x-token' or 'basic'"

# Quote assets. `quote_mint` is all zeros on SOL-paired coins, and the quote-side
# reserves are in that mint's raw units — 1e9 for SOL, 1e6 for USDC.
DEFAULT_QUOTE_MINT: Final[Pubkey] = Pubkey.from_bytes(bytes(32))
WSOL_MINT: Final[Pubkey] = Pubkey.from_string(
    "So11111111111111111111111111111111111111112"
)
USDC_MINT: Final[Pubkey] = Pubkey.from_string(
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)
QUOTE_DECIMALS: Final[dict[Pubkey, int]] = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS: Final[dict[Pubkey, str]] = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}

# Same offset in SPL Token and Token-2022: extensions are appended after it.
_MINT_DECIMALS_OFFSET: Final[int] = 44

# Only used if the Global account cannot be read: 1B supply less 206.9M reserved.
FALLBACK_INITIAL_REAL_TOKEN_RESERVES: Final[float] = 793_100_000.0

# A qualifying curve is traded several times a second. Reprint it only once it has
# moved this far, so the output stays readable.
REPRINT_STEP_PCT: Final[float] = 0.25

RECONNECT_DELAY: Final[int] = 5


def zero_prefix_gate(bound_raw: int) -> tuple[int, bytes] | None:
    """Pick the tightest server-side cutoff that still lets every match through.

    See the module docstring for the cutoffs on offer. Returns None when none of
    them is wide enough to be safe, in which case there is no pre-filtering and
    every curve is checked here instead.

    Args:
        bound_raw: Highest reserves value, in raw units, that should still qualify

    Returns:
        An (offset, zero bytes) pair for a memcmp filter, or None for no filter
    """
    # Only 2, 3 and 4 zero bytes are offered. One zero byte would be a cutoff so
    # high that no coin could ever fail it, which filters nothing while looking
    # like it does.
    for zero_bytes in (4, 3, 2):
        if 2 ** (8 * (8 - zero_bytes)) > bound_raw:
            return 32 - zero_bytes, bytes(zero_bytes)
    return None


def build_subscribe_request(bound_raw: int) -> geyser_pb2.SubscribeRequest:
    """Build the Geyser account subscription for near-graduation curves.

    One named group, with no `datasize` filter: `extend_account` can grow a
    curve past 125 bytes to any length the program allows, so there is no
    fixed set of lengths to enumerate across multiple named groups. (An
    earlier version of this script did use two named groups, one per
    enumerated length, because `SubscribeRequest.accounts` ANDs the filters
    *inside* a group and there was no way to ask one group for "datasize 125
    or 151" — see the module docstring for why that stopped being enough and
    what it costs in bandwidth to drop entirely.) The discriminator memcmp
    alone already restricts delivery to `BondingCurve` accounts, so a single
    group loses no precision by not naming a length.

    Args:
        bound_raw: Highest qualifying `real_token_reserves`, in raw units

    Returns:
        The subscription request
    """
    request = geyser_pb2.SubscribeRequest()
    gate = zero_prefix_gate(bound_raw)

    accounts = request.accounts["graduating_curves"]
    accounts.owner.append(str(PUMP_PROGRAM_ID))

    discriminator = accounts.filters.add().memcmp
    discriminator.offset = 0
    discriminator.bytes = BONDING_CURVE_DISCRIMINATOR

    not_complete = accounts.filters.add().memcmp
    not_complete.offset = _COMPLETE_OFFSET
    not_complete.bytes = b"\x00"  # Not graduated yet

    if gate:
        reserves = accounts.filters.add().memcmp
        reserves.offset, reserves.bytes = gate

    request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return request


def create_geyser_connection() -> tuple[Any, grpc.aio.Channel]:
    """Open an authenticated gRPC channel to the Geyser endpoint.

    Returns:
        The Geyser stub and the channel backing it

    Raises:
        ValueError: If GEYSER_AUTH_TYPE is not a supported scheme
    """
    if GEYSER_AUTH_TYPE == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback((("x-token", GEYSER_API_TOKEN),), None)
        )
    elif GEYSER_AUTH_TYPE == "basic":
        auth = grpc.metadata_call_credentials(
            lambda _, callback: callback(
                (("authorization", f"Basic {GEYSER_API_TOKEN}"),), None
            )
        )
    else:
        raise ValueError(_BAD_AUTH_TYPE_MSG)

    creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
    endpoint = (
        GEYSER_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    )
    channel = grpc.aio.secure_channel(endpoint, creds)
    return geyser_pb2_grpc.GeyserStub(channel), channel


def parse_curve(data: bytes) -> dict[str, Any]:
    """Decode the bonding curve fields needed for a progress report.

    Works at any length the account might arrive at — 125 bytes as created,
    151 once extended, 256 confirmed live, or any other length the program
    allows in the future — since every field read here sits in the first 115
    bytes, well before where any resizing appends. Callers are expected to
    apply `MIN_CURVE_LEN` first; this function does not re-check it.

    Args:
        data: Raw bonding curve account data

    Returns:
        Token reserves in whole tokens, quote reserves raw, plus the quote mint

    Raises:
        ValueError: If the discriminator does not match a bonding curve
    """
    if data[:8] != BONDING_CURVE_DISCRIMINATOR:
        raise ValueError(_BAD_DISCRIMINATOR_MSG)

    real_token_reserves = struct.unpack_from("<Q", data, _RESERVES_OFFSET)[0]
    real_quote_reserves = struct.unpack_from("<Q", data, _RESERVES_OFFSET + 8)[0]

    quote_mint = Pubkey.from_bytes(data[_QUOTE_MINT_OFFSET : _QUOTE_MINT_OFFSET + 32])
    if quote_mint == DEFAULT_QUOTE_MINT:
        quote_mint = WSOL_MINT

    # Quote-side reserves stay raw: only the quote mint's decimals scale them.
    return {
        "real_token_reserves": real_token_reserves / 10**TOKEN_DECIMALS,
        "real_quote_reserves_raw": real_quote_reserves,
        "quote_mint": quote_mint,
        "quote_symbol": QUOTE_SYMBOLS.get(quote_mint, str(quote_mint)),
    }


async def read_quote_decimals(conn: AsyncClient, quote_mint: Pubkey) -> int:
    """Read a quote mint's decimals from chain.

    `QuoteControl` admits mints from 4 to 12 decimals, so a default of 9 is
    wrong for most of them and misscales every quote-side figure silently.

    Raises:
        ValueError: If the mint is missing or too short to be a mint
    """
    if quote_mint in QUOTE_DECIMALS:
        return QUOTE_DECIMALS[quote_mint]

    response = await conn.get_account_info(quote_mint, encoding="base64")
    if response.value is None:
        raise ValueError(f"Quote mint {quote_mint} does not exist on chain")
    data = bytes(response.value.data)
    if len(data) <= _MINT_DECIMALS_OFFSET:
        raise ValueError(
            f"Account {quote_mint} is only {len(data)} bytes, too short to be a mint"
        )
    return data[_MINT_DECIMALS_OFFSET]


async def fetch_initial_real_token_reserves(client: AsyncClient) -> float:
    """Read the launch-time real token reserves from the Global account.

    Global layout up to this field: discriminator(8) + initialized(1) +
    authority(32) + fee_recipient(32) + initial_virtual_token_reserves(8) +
    initial_virtual_sol_reserves(8), so initial_real_token_reserves sits at 89.

    Args:
        client: Connected RPC client

    Returns:
        Initial real token reserves in whole tokens, or the fallback constant
    """
    try:
        resp = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
        raw = struct.unpack_from(
            "<Q", resp.value.data, _GLOBAL_INITIAL_REAL_TOKEN_RESERVES_OFFSET
        )[0]
        if raw:
            return raw / 10**TOKEN_DECIMALS
    except Exception as e:  # noqa: BLE001 - fall back rather than abort the watcher
        print(f"⚠️ Could not read Global, using the fallback baseline: {e}")
    return FALLBACK_INITIAL_REAL_TOKEN_RESERVES


async def resolve_mint(client: AsyncClient, curve: Pubkey) -> Pubkey | None:
    """Recover a coin's mint from its bonding curve address.

    The curve account carries no mint field and `["bonding-curve", mint]` is not
    reversible, so this goes through the associated bonding curve — an ordinary ATA
    owned by the curve. That ATA belongs to Token-2022 for `create_v2` coins, which is
    every coin now being launched, so Token-2022 is tried first. The answer is checked
    by re-deriving the curve PDA from the mint.

    Args:
        client: Connected RPC client
        curve: The bonding curve address

    Returns:
        The mint, or None if no owned token account resolves back to this curve
    """
    for program_id in (TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID):
        try:
            resp = await client.get_token_accounts_by_owner(
                curve, TokenAccountOpts(program_id=program_id)
            )
        except Exception as e:  # noqa: BLE001 - a miss here is not fatal
            print(f"⚠️ Mint lookup failed for {curve}: {e}")
            continue

        if not resp.value:
            continue

        mint = Pubkey(resp.value[0].account.data[:32])
        derived, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)], PUMP_PROGRAM_ID
        )
        if derived == curve:
            return mint

    return None


def progress_to_bound(baseline: float, min_progress: float) -> int:
    """Convert a progress threshold into a raw `real_token_reserves` ceiling.

    Args:
        baseline: Launch-time real token reserves, in whole tokens
        min_progress: Graduation progress threshold, as a percentage

    Returns:
        The highest raw reserves value that still qualifies
    """
    return int(baseline * (1 - min_progress / 100) * 10**TOKEN_DECIMALS)


def print_banner(baseline: float, min_progress: float) -> None:
    """Describe the baseline and the filter that will be installed.

    Args:
        baseline: Launch-time real token reserves, in whole tokens
        min_progress: Graduation progress threshold, as a percentage
    """
    print(f"Graduation baseline: {baseline:,.0f} tokens (from Global)")
    print(f"Reporting curves at or above {min_progress:.2f}% graduated")

    gate = zero_prefix_gate(progress_to_bound(baseline, min_progress))
    if gate:
        cutoff_tokens = 2 ** (8 * (8 - len(gate[1]))) / 10**TOKEN_DECIMALS
        cutoff_pct = max(100 - cutoff_tokens * 100 / baseline, 0.0)
        print(
            f"Pre-filter: the server sends only curves past ~{cutoff_pct:.2f}% "
            f"({cutoff_tokens:,.0f} tokens left); the rest is checked here"
        )
    else:
        print("Pre-filter: none, so every curve arrives and is checked here")
    print("Waiting for trades on qualifying curves...\n")


class GraduationReporter:
    """Turns raw curve updates into one printed line per meaningful change.

    Holds the mint cache and the last-printed progress per curve, so the transport
    loop only has to hand over decoded account bytes.
    """

    def __init__(
        self, client: AsyncClient, baseline: float, min_progress: float
    ) -> None:
        """Initialize the reporter.

        Args:
            client: Connected RPC client, used to resolve mints
            baseline: Launch-time real token reserves, in whole tokens
            min_progress: Graduation progress threshold, as a percentage
        """
        self.client = client
        self.baseline = baseline
        self.min_progress = min_progress
        self.mints: dict[Pubkey, Pubkey | None] = {}
        self.quote_decimals: dict[Pubkey, int] = {}
        self.last_printed: dict[Pubkey, float] = {}

    async def handle(self, curve: Pubkey, data: bytes, suffix: str = "") -> None:
        """Report one curve update, if it qualifies and has moved far enough.

        Args:
            curve: The bonding curve address
            data: Raw bonding curve account data
            suffix: Extra provenance to append to the line
        """
        try:
            state = parse_curve(data)
        except (ValueError, struct.error) as e:
            print(f"⚠️ Could not decode {curve}: {e}")
            return

        # The server-side gate is coarser than the requested threshold, so the exact
        # comparison happens here.
        progress = max(100 - state["real_token_reserves"] * 100 / self.baseline, 0.0)
        if progress < self.min_progress:
            return

        previous = self.last_printed.get(curve)
        if previous is not None and abs(progress - previous) < REPRINT_STEP_PCT:
            return
        self.last_printed[curve] = progress

        if curve not in self.mints:
            self.mints[curve] = await resolve_mint(self.client, curve)
        mint = self.mints[curve]

        quote_mint = state["quote_mint"]
        if quote_mint not in self.quote_decimals:
            self.quote_decimals[quote_mint] = await read_quote_decimals(
                self.client, quote_mint
            )
        quote_unit = 10 ** self.quote_decimals[quote_mint]

        print(
            f"🎓 {progress:6.2f}%  "
            f"mint={mint if mint else '<unresolved>'}  "
            f"curve={curve}  "
            f"{state['real_token_reserves']:,.0f} tokens left  "
            f"{state['real_quote_reserves_raw'] / quote_unit:,.4f} "
            f"{state['quote_symbol']}"
            f"{suffix}"
        )


async def stream_once(reporter: GraduationReporter, bound_raw: int) -> None:
    """Subscribe over Geyser and consume account updates until the stream ends.

    Args:
        reporter: Sink for decoded curve updates
        bound_raw: Highest qualifying `real_token_reserves`, in raw units
    """
    stub, channel = create_geyser_connection()
    try:
        request = build_subscribe_request(bound_raw)
        async for update in stub.Subscribe(iter([request])):
            if not update.HasField("account"):
                continue

            account = update.account.account
            data = bytes(account.data)
            if len(data) < MIN_CURVE_LEN:
                # Shorter than the smallest real BondingCurve struct — not a
                # shape the decoder should be trusted with. See MIN_CURVE_LEN.
                continue

            signature = (
                str(Signature(bytes(account.txn_signature)))
                if account.txn_signature
                else "<none>"
            )
            await reporter.handle(
                Pubkey.from_bytes(bytes(account.pubkey)),
                data,
                suffix=f"  slot={update.account.slot}  sig={signature}",
            )
    finally:
        await channel.close()


async def watch(min_progress: float) -> None:
    """Stream curve updates over Geyser and report coins at or above `min_progress`.

    Args:
        min_progress: Graduation progress threshold, as a percentage
    """
    if not GEYSER_ENDPOINT or not GEYSER_API_TOKEN:
        print("❌ Set GEYSER_ENDPOINT and GEYSER_API_TOKEN in .env")
        return
    if not RPC_ENDPOINT:
        print("❌ Set SOLANA_NODE_RPC_ENDPOINT in .env (needed for Global and mints)")
        return

    async with AsyncClient(RPC_ENDPOINT) as client:
        baseline = await fetch_initial_real_token_reserves(client)
        print_banner(baseline, min_progress)

        bound_raw = progress_to_bound(baseline, min_progress)
        reporter = GraduationReporter(client, baseline, min_progress)

        while True:
            try:
                await stream_once(reporter, bound_raw)
            except ValueError as e:
                print(f"❌ {e}")
                return
            except grpc.aio.AioRpcError as e:
                print(
                    f"⚠️ gRPC error ({e.code()}): {e.details()}; "
                    f"reconnecting in {RECONNECT_DELAY}s"
                )
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as e:  # noqa: BLE001 - keep watching across hiccups
                print(f"⚠️ {type(e).__name__}: {e}; reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)


def main() -> None:
    """Parse arguments and start watching."""
    # A watcher is usually piped into a file or grep, where block buffering would
    # hold every line back — and lose them entirely if the process is killed.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Report pump.fun coins approaching graduation, over Geyser gRPC"
    )
    parser.add_argument(
        "--min-progress",
        type=float,
        default=90.0,
        help="Only report curves at or above this graduation percentage "
        "(default: 90.0)",
    )
    args = parser.parse_args()
    asyncio.run(watch(args.min_progress))


if __name__ == "__main__":
    main()

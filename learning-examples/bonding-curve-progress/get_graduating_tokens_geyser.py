"""Watch for pump.fun coins approaching graduation, over Geyser gRPC.

Usage:
    uv run learning-examples/bonding-curve-progress/get_graduating_tokens_geyser.py
    uv run learning-examples/bonding-curve-progress/get_graduating_tokens_geyser.py --min-progress 95

Needs GEYSER_ENDPOINT, GEYSER_API_TOKEN and GEYSER_AUTH_TYPE in .env, plus
SOLANA_NODE_RPC_ENDPOINT for the two things the stream cannot answer: the Global
baseline and each coin's mint. Geyser is a paid add-on, so `get_graduating_tokens.py`
is the portable version of this report — it runs on any endpoint, including the public
one. This variant exists because Geyser is common among traders and gives you the slot
and transaction signature behind every update, which the WebSocket feed does not.

Why a subscription and not `getProgramAccounts`: the pump.fun program now owns over
10 million accounts, and every provider refuses to scan it — the rejection is on
program size, before filters apply. A curve can only approach graduation by being
traded, and every write pushes the full 151-byte account, so each update carries
everything needed to compute progress: no accumulated state, no cold start beyond the
next trade.

Selecting a graduation threshold
--------------------------------
Progress is measured against `Global.initial_real_token_reserves` (~793.1M tokens)
read from chain, not a hardcoded constant, because a mayhem coin can launch with
different virtual params and would otherwise show the wrong percentage.

Geyser's account filters are the same shape as the RPC's — `datasize` and exact-byte
`memcmp` — so the only inequality expressible on a little-endian u64 is "the top N
bytes are zero", i.e. `value < 2**(8*(8-N))`. `real_token_reserves` spans offsets
24..31, so the top N bytes sit at offset 32-N, giving these gates against a 793.1M
baseline:

    offset  zero bytes  admits reserves below      ≈ progress above
    31      1           2**56  (72,057,594M tok)   0%      (no real filtering)
    30      2           2**48  (281.47M tok)       64.51%
    29      3           2**40  (1.0995M tok)       99.86%
    28      4           2**32  (4,294.97 tok)      99.9995%

The steps are coarse — 64.51% jumps straight to 99.86% — so `--min-progress` picks the
tightest gate that still admits every qualifying curve and the exact threshold is
applied client-side. To hand-tune, change the gate: the last moments before migration
want offset 29, a wider funnel wants offset 30.

`datasize = 151` restricts this to the current curve layout. The original 49-byte
layout still has accounts with `complete = false`, but none of them are written to any
more — verified over a 45s window in which all 205 updates across 24 curves were
151-byte accounts.
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
from solana.rpc.types import TokenAccountOpts
from solders.pubkey import Pubkey
from solders.signature import Signature

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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

# See learning-examples/calculate_discriminator.py
BONDING_CURVE_DISCRIMINATOR: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")
CURVE_ACCOUNT_LEN: Final[int] = 151

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

# Only used if the Global account cannot be read: 1B supply less 206.9M reserved.
FALLBACK_INITIAL_REAL_TOKEN_RESERVES: Final[float] = 793_100_000.0

# A qualifying curve is traded several times a second. Reprint it only once it has
# moved this far, so the output stays readable.
REPRINT_STEP_PCT: Final[float] = 0.25

RECONNECT_DELAY: Final[int] = 5


def zero_prefix_gate(bound_raw: int) -> tuple[int, bytes] | None:
    """Pick the tightest `memcmp` gate that still admits `real_token_reserves`.

    See the module docstring for the derivation.

    Args:
        bound_raw: Highest `real_token_reserves` value, in raw units, that should
            still qualify

    Returns:
        An (offset, zero bytes) pair for a memcmp filter, or None if not even the
        widest gate would constrain anything
    """
    for zero_bytes in (4, 3, 2, 1):
        if 2 ** (8 * (8 - zero_bytes)) > bound_raw:
            return 32 - zero_bytes, bytes(zero_bytes)
    return None


def build_subscribe_request(bound_raw: int) -> geyser_pb2.SubscribeRequest:
    """Build the Geyser account subscription for near-graduation curves.

    Args:
        bound_raw: Highest qualifying `real_token_reserves`, in raw units

    Returns:
        The subscription request
    """
    request = geyser_pb2.SubscribeRequest()
    accounts = request.accounts["graduating_curves"]
    accounts.owner.append(str(PUMP_PROGRAM_ID))

    accounts.filters.add().datasize = CURVE_ACCOUNT_LEN

    discriminator = accounts.filters.add().memcmp
    discriminator.offset = 0
    discriminator.bytes = BONDING_CURVE_DISCRIMINATOR

    not_complete = accounts.filters.add().memcmp
    not_complete.offset = _COMPLETE_OFFSET
    not_complete.bytes = b"\x00"  # Not graduated yet

    gate = zero_prefix_gate(bound_raw)
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
    """Decode the 151-byte bonding curve fields needed for a progress report.

    Args:
        data: Raw bonding curve account data

    Returns:
        Reserves in whole tokens, plus the quote asset's symbol

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
    quote_unit = 10 ** QUOTE_DECIMALS.get(quote_mint, 9)

    return {
        "real_token_reserves": real_token_reserves / 10**TOKEN_DECIMALS,
        "real_quote_reserves": real_quote_reserves / quote_unit,
        "quote_symbol": QUOTE_SYMBOLS.get(quote_mint, str(quote_mint)),
    }


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
        offset, zeros = gate
        admits = 2 ** (8 * (8 - len(zeros))) / 10**TOKEN_DECIMALS
        print(
            f"Server-side gate: memcmp {len(zeros)} zero byte(s) at offset {offset}, "
            f"admitting reserves below {admits:,.0f} tokens"
        )
    else:
        print("Server-side gate: none (threshold too low to constrain)")
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

        print(
            f"🎓 {progress:6.2f}%  "
            f"mint={mint if mint else '<unresolved>'}  "
            f"curve={curve}  "
            f"{state['real_token_reserves']:,.0f} tokens left  "
            f"{state['real_quote_reserves']:,.4f} {state['quote_symbol']}"
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
            signature = (
                str(Signature(bytes(account.txn_signature)))
                if account.txn_signature
                else "<none>"
            )
            await reporter.handle(
                Pubkey.from_bytes(bytes(account.pubkey)),
                bytes(account.data),
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

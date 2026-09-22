"""Watch for pump.fun coins approaching graduation, over plain WebSocket RPC.

Usage:
    uv run cookbook/pumpfun/graduation/get_graduating_tokens.py
    uv run cookbook/pumpfun/graduation/get_graduating_tokens.py --min-progress 95

Why a subscription and not `getProgramAccounts`: the pump.fun program now owns
over 10 million accounts, and every provider refuses to scan it. Helius, Alchemy
and dRPC reject with `Too many accounts requested (10000001 pubkeys)`; QuickNode
and Chainstack time out. No filter set fixes that — the rejection is on program
size, before filters apply. `getProgramAccountsV2` is a provider extension (Helius,
Solana Tracker), not core Agave, and its `limit` is a *scan* budget rather than a
result count, so answering this question with it means ~1000 sequential pages.

`programSubscribe` sidesteps the scan entirely. A curve can only approach
graduation by being traded, and every write to it pushes the full account — 125
bytes as `create_v2` allocates it, or 151 once `extend_account` has run on it —
so each notification carries everything needed to compute progress — there is no
state to accumulate and no cold start beyond the next trade. Verified accepted on
both a paid endpoint and the public `api.mainnet-beta.solana.com`.

See `get_graduating_tokens_geyser.py` for the same report over Geyser gRPC, which
also gives you the transaction signature behind each update.

Selecting a graduation threshold
--------------------------------
Progress is measured against `Global.initial_real_token_reserves` (~793.1M tokens)
read from chain, not a hardcoded constant, because a mayhem coin can launch with
different virtual params and would otherwise show the wrong percentage.

The pre-filter the server applies can only match exact bytes, so it cannot do
"anything above 90%". It can only do a few fixed cutoffs. `--min-progress` uses the
closest cutoff that is still wide enough, then makes the exact comparison here:

    filter               cutoff                 ≈ graduated past
    2 zero bytes @ 30    281.5M tokens left     64.5%
    3 zero bytes @ 29    1.1M tokens left       99.86%
    4 zero bytes @ 28    4,295 tokens left      99.9995%

    --min-progress       pre-filtered by the server?
    below 64.5%          no, every curve arrives and is filtered here
    64.5% to 99.86%      yes, at the 64.5% cutoff
    99.86% and up        yes, at the 99.86% cutoff

So the pre-filter saves traffic, it does not decide the answer — whatever percentage
you ask for is honoured either way. Low thresholds just cost more bandwidth. If you
want to hand-tune, pick a different cutoff from the table: the last moments before
migration want the 3-byte one, a wider funnel the 2-byte one.

Checked against mainnet by running the filtered and unfiltered subscriptions side by
side for a minute: same curves, nothing dropped, nothing extra.

This script does **not** filter on `dataSize`. `create_v2` allocates the curve
at exactly 125 bytes, an account grows to 151 once `extend_account` has run on
it, and a rarer third length (256 bytes, confirmed live below) also exists.
`extend_account` can grow a curve to any length — nothing enumerates every
size it might produce — so a fixed `dataSize` allowlist is whack-a-mole: the
next length silently drops curves again, and the failure mode is invisible,
since the script just prints fewer results rather than an error. The
discriminator `memcmp` alone already restricts delivery to `BondingCurve`
accounts, so dropping `dataSize` costs no precision — only bandwidth (see the
measurement below). A `MIN_CURVE_LEN` floor still discards anything shorter
than the smallest real struct, so a stray short/legacy account can't reach the
decoder.

Confirmed live on 2026-09-15 two ways. First, directly: `getAccountInfo` on
`EJpNsfxnTB6mtVdzrTcgQ9xfywobHSSsUtu1Gh1GFvEg` (a 256-byte curve reported
elsewhere) returned 256 bytes, discriminator matching, owned by the pump
program, and it decoded cleanly through this repo's own IDL-driven decoder
(`PumpFunCurveManager._decode_curve_state_with_idl`) with sane reserves —
everything past the documented fields is zero padding. Second, over the
wire: a 120s `programSubscribe` window with this script's own filters
(discriminator + `complete = false`, no `dataSize`) took in 1,066 updates —
207 at 125 bytes, 853 at 151, and 6 at that same 256 length, among ordinary
traffic. None of the 1,066 were the legacy 49-byte layout (no `creator`
field). UNVERIFIED: whether that layout still has any `complete = false`
accounts left on chain, and whether anything still writes to them — not
re-measured here.

**Bandwidth trade-off, measured 2026-09-15.** A first attempt ran the
`dataSize`-filtered shape and the unfiltered shape back to back, 90s each,
and looked like unfiltered cost *less* (0.68x) — that was noise: pump.fun
trading volume swings a lot minute to minute, and two sequential windows just
land on different volume. Rerun with all three filter groups (`dataSize
125`, `dataSize 151`, and no `dataSize`) subscribed **simultaneously on one
connection**, so all three watch the identical trade stream over the same
120s: the two enumerated lengths together took in 1,060 updates / 589,279
bytes; the unfiltered subscription took in 1,066 updates / 593,509 bytes —
6 extra updates, 4,230 extra bytes, all of it the 256-byte curve neither
enumerated length can match. That is a **1.006x update ratio / 1.007x byte
ratio** — under 1% either way, not the double subscription's worth intuition
might suggest, because in this trade window virtually every update already
lands on one of the two dataSize-filtered lengths (125 or 151), and 256 is
rare. Dropping the filter is effectively free here; if a resize-happy period
ever shifts that mix, the cost scales with however much traffic sits outside
the two filtered lengths (125/151, i.e. the rarer 256-byte curves and
beyond), not with total volume.

UNVERIFIED: a curve was once observed going from 125 to 151 bytes, with
several 125-byte trades logged in between, suggesting `extend_account` ran as
its own later transaction rather than bundled into `create_v2` — not
re-measured here. `extend_account` **can** land in the same transaction as
`create_v2` — `cookbook/pumpfun/trade/mint_and_buy_v2.py` does exactly that,
appending `create_extend_account_instruction` right after
`create_pump_create_v2_instruction` in the same instruction list — so both
orderings occur; how common a further-resized (256-byte) curve is remains
unmeasured.
"""

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
from typing import Any, Final

import websockets
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts
from solders.pubkey import Pubkey

load_dotenv()

# Constants
RPC_ENDPOINT: Final[str] = os.environ.get("SOLANA_NODE_RPC_ENDPOINT", "")
WSS_ENDPOINT: Final[str] = os.environ.get("SOLANA_NODE_WSS_ENDPOINT", "")
PUMP_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMP_GLOBAL: Final[Pubkey] = Pubkey.from_string(
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
)

# Coins created by `create_v2` are Token-2022, so that is tried first. Querying
# under the wrong token program returns nothing at all.
TOKEN_2022_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
TOKEN_PROGRAM_ID: Final[Pubkey] = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)

# See cookbook/pumpfun/decode/calculate_discriminator.py
BONDING_CURVE_DISCRIMINATOR: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")

# create_v2 allocates the 125-byte struct; extend_account can grow it past
# that to any length the program allows (151 and 256 both confirmed live,
# 2026-09-15) — there is no fixed set of lengths to enumerate. This is a
# floor, not an allowlist: anything shorter than the smallest real struct is
# dropped client-side, everything at or above it is decoded and let through.
MIN_CURVE_LEN: Final[int] = 125

TOKEN_DECIMALS: Final[int] = 6
_RESERVES_OFFSET: Final[int] = 24  # real_token_reserves, u64 LE
_COMPLETE_OFFSET: Final[int] = 48
_QUOTE_MINT_OFFSET: Final[int] = 83
_GLOBAL_INITIAL_REAL_TOKEN_RESERVES_OFFSET: Final[int] = 89

_BAD_DISCRIMINATOR_MSG: Final[str] = "Invalid discriminator for bonding curve"

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


def build_filters(bound_raw: int) -> list[dict[str, Any]]:
    """Assemble the server-side `programSubscribe` filters.

    No `dataSize` filter: `extend_account` can grow a curve past 125 bytes to
    any length the program allows, so there is no fixed set of lengths to
    match. The discriminator memcmp alone already restricts delivery to
    `BondingCurve` accounts — see the module docstring for the bandwidth this
    trades away and the client-side `MIN_CURVE_LEN` floor that replaces it.

    Args:
        bound_raw: Highest qualifying `real_token_reserves`, in raw units

    Returns:
        Filter dicts in the shape the RPC expects
    """

    def memcmp(offset: int, raw: bytes) -> dict[str, Any]:
        return {
            "memcmp": {
                "offset": offset,
                "bytes": base64.b64encode(raw).decode(),
                "encoding": "base64",
            }
        }

    filters: list[dict[str, Any]] = [
        memcmp(0, BONDING_CURVE_DISCRIMINATOR),
        memcmp(_COMPLETE_OFFSET, b"\x00"),  # Not graduated yet
    ]

    gate = zero_prefix_gate(bound_raw)
    if gate:
        filters.append(memcmp(*gate))
    return filters


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
    owned by the curve. That ATA belongs to Token-2022 for `create_v2` coins, which
    is every coin now being launched, so Token-2022 is tried first. The answer is
    checked by re-deriving the curve PDA from the mint.

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


def progress_to_bound(baseline: float, min_progress: float) -> int:
    """Convert a progress threshold into a raw `real_token_reserves` ceiling.

    Args:
        baseline: Launch-time real token reserves, in whole tokens
        min_progress: Graduation progress threshold, as a percentage

    Returns:
        The highest raw reserves value that still qualifies
    """
    return int(baseline * (1 - min_progress / 100) * 10**TOKEN_DECIMALS)


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
            suffix: Extra provenance to append to the line, if the transport has any
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


async def stream_once(
    reporter: GraduationReporter, filters: list[dict[str, Any]]
) -> None:
    """Subscribe and consume notifications until the connection drops.

    One subscription is enough now that there is no `dataSize` filter to
    fan out over — a curve at any length is delivered on this single
    filter set. Only the subscription id handed back by our own ack is
    treated as ours, so a notification from an unrelated subscription on this
    connection (there should not be one) is ignored rather than mishandled.

    Args:
        reporter: Sink for decoded curve updates
        filters: The server-side filter list to install

    Raises:
        ConnectionRefusedError: If the endpoint rejects the subscription outright
    """
    async with websockets.connect(WSS_ENDPOINT, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "programSubscribe",
                    "params": [
                        str(PUMP_PROGRAM_ID),
                        {
                            "encoding": "base64",
                            "commitment": "processed",
                            "filters": filters,
                        },
                    ],
                }
            )
        )
        ack = json.loads(await ws.recv())
        if "error" in ack:
            raise ConnectionRefusedError(str(ack["error"]))
        subscription_id = ack["result"]

        while True:
            # ConnectionClosed deliberately propagates to the reconnect handler in
            # watch(). Swallowing it here would make every further recv() raise
            # instantly, forever. A JSONDecodeError is per-message rather than
            # per-connection, so that one is safe to skip.
            try:
                message = json.loads(await ws.recv())
            except json.JSONDecodeError:
                continue

            if message.get("method") != "programNotification":
                continue
            if message["params"]["subscription"] != subscription_id:
                continue

            value = message["params"]["result"]["value"]
            data = base64.b64decode(value["account"]["data"][0])
            if len(data) < MIN_CURVE_LEN:
                # Shorter than the smallest real BondingCurve struct — not a
                # shape the decoder should be trusted with. See MIN_CURVE_LEN.
                continue
            await reporter.handle(Pubkey.from_string(value["pubkey"]), data)


async def watch(min_progress: float) -> None:
    """Stream curve updates and report coins at or above `min_progress`.

    Args:
        min_progress: Graduation progress threshold, as a percentage
    """
    if not WSS_ENDPOINT or not RPC_ENDPOINT:
        print("❌ Set SOLANA_NODE_RPC_ENDPOINT and SOLANA_NODE_WSS_ENDPOINT in .env")
        return

    async with AsyncClient(RPC_ENDPOINT) as client:
        baseline = await fetch_initial_real_token_reserves(client)
        print_banner(baseline, min_progress)

        bound_raw = progress_to_bound(baseline, min_progress)
        filters = build_filters(bound_raw)
        reporter = GraduationReporter(client, baseline, min_progress)

        while True:
            try:
                await stream_once(reporter, filters)
            except ConnectionRefusedError as e:
                print(f"❌ Subscription rejected: {e}")
                return
            except Exception as e:  # noqa: BLE001 - keep watching across hiccups
                print(f"⚠️ {type(e).__name__}: {e}; reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)


def main() -> None:
    """Parse arguments and start watching."""
    # A watcher is usually piped into a file or grep, where block buffering would
    # hold every line back — and lose them entirely if the process is killed.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Report pump.fun coins approaching graduation, over WebSocket RPC"
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

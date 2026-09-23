"""Measure what Geyser's pre-execution deshred stream buys you, and what it costs.

Read-only. No funds are moved and nothing is submitted.

`SubscribeDeshred` emits a transaction when entries are formed from shreds,
before execution. This races it against the ordinary executed `Subscribe`
stream on the same endpoint and reports two things:

  1. Lead time, over every pump.fun transaction and over token creations
     separately. Creates behave differently from general traffic and are the
     number that matters for sniping.
  2. How many creates the deshred stream cannot detect. A coin created through a
     router reaches the program as a CPI, and inner instructions are produced
     *by* execution, so they do not exist on a pre-execution stream at all and
     no decoding recovers them. Every create is classified as top-level or inner
     to size that blind spot.

Usage:
    uv run tools/compare_deshred_latency.py
    uv run tools/compare_deshred_latency.py --duration 600
"""

import argparse
import collections
import os
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import grpc  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from geyser.generated import geyser_pb2, geyser_pb2_grpc  # noqa: E402

load_dotenv()

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# First 8 bytes of sha256("global:create") and sha256("global:createV2").
# Run cookbook/solana/anchor_calculate_discriminator.py to recompute them.
CREATE = struct.pack("<Q", 8576854823835016728)
CREATE_V2 = bytes([214, 144, 76, 236, 95, 139, 49, 180])

DEFAULT_DURATION = 300


def build_stub() -> geyser_pb2_grpc.GeyserStub:
    """Open an authenticated Geyser channel.

    Returns:
        A stub bound to the configured endpoint
    """
    token = os.environ["GEYSER_API_TOKEN"]
    endpoint = os.environ["GEYSER_ENDPOINT"]
    auth = grpc.metadata_call_credentials(
        lambda _, callback: callback((("x-token", token),), None)
    )
    creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
    return geyser_pb2_grpc.GeyserStub(grpc.secure_channel(endpoint, creds))


def is_create(data: bytes) -> bool:
    """Report whether instruction data is a pump.fun create.

    Args:
        data: Raw instruction data

    Returns:
        True for `create` or `create_v2`
    """
    return data.startswith(CREATE) or data.startswith(CREATE_V2)


def summarise(leads: list[float]) -> dict:
    """Reduce a list of lead times to the distribution worth printing.

    Args:
        leads: Lead times in milliseconds, positive when deshred was first

    Returns:
        Counts and percentiles
    """
    ordered = sorted(leads)
    count = len(ordered)

    def at(fraction: float) -> float:
        return ordered[min(count - 1, int(fraction * count))]

    return {
        "n": count,
        "ahead": sum(1 for value in ordered if value > 0),
        "p10": at(0.10),
        "p25": at(0.25),
        "median": statistics.median(ordered),
        "p75": at(0.75),
        "p90": at(0.90),
        "p99": at(0.99),
        "mean": statistics.mean(ordered),
        "max": ordered[-1],
    }


def report(label: str, leads: list[float]) -> None:
    """Print one lead-time distribution.

    Args:
        label: What this sample covers
        leads: Lead times in milliseconds
    """
    if not leads:
        print(f"\n{label}: no overlapping signatures")
        return
    stats = summarise(leads)
    share = 100 * stats["ahead"] / stats["n"]
    print(f"\n{label}  (n={stats['n']})")
    print(f"  deshred first : {stats['ahead']}/{stats['n']}  ({share:.1f}%)")
    print(
        f"  lead ms       : p10 {stats['p10']:+.1f} | p25 {stats['p25']:+.1f} | "
        f"median {stats['median']:+.1f} | p75 {stats['p75']:+.1f} | "
        f"p90 {stats['p90']:+.1f} | p99 {stats['p99']:+.1f} | max {stats['max']:+.1f}"
    )
    print(f"  mean          : {stats['mean']:+.1f} ms")


class Collector:
    """Shared state both streams write into while the race runs."""

    def __init__(self) -> None:
        self.executed_at: dict[str, float] = {}
        self.deshred_at: dict[str, float] = {}
        # sig -> "top-level" or "inner (CPI)", from the executed stream, which is
        # the only one that can tell the difference.
        self.create_shape: dict[str, str] = {}
        self.deshred_creates: set[str] = set()
        self.deshred_all: set[str] = set()
        self.errors: dict[str, str] = {}


def _keepalive(request, deadline: float):  # noqa: ANN001, ANN202 - grpc generator
    """Hold the subscription open until the deadline passes."""
    yield request
    while time.time() < deadline:
        time.sleep(0.5)


def collect_executed(collector: Collector, deadline: float) -> None:
    """Record arrival times and create shapes from the executed stream.

    Args:
        collector: Shared state to write into
        deadline: Wall-clock time to stop at
    """
    try:
        request = geyser_pb2.SubscribeRequest()
        request.transactions["pump"].account_include.append(PUMP_PROGRAM_ID)
        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        for update in build_stub().Subscribe(_keepalive(request, deadline)):
            seen = time.monotonic()
            if time.time() > deadline:
                break
            if not update.HasField("transaction"):
                continue
            info = update.transaction.transaction
            signature = base58.b58encode(bytes(info.signature)).decode()
            collector.executed_at.setdefault(signature, seen)

            if not any(
                "Instruction: CreateV2" in line or line.endswith("Instruction: Create")
                for line in info.meta.log_messages
            ):
                continue
            top_level = any(
                is_create(ix.data) for ix in info.transaction.message.instructions
            )
            collector.create_shape[signature] = (
                "top-level" if top_level else "inner (CPI)"
            )
    except grpc.RpcError as error:
        collector.errors["executed"] = f"{error.code()}: {error.details()}"


def collect_deshred(collector: Collector, deadline: float) -> None:
    """Record arrival times and detectable creates from the deshred stream.

    Args:
        collector: Shared state to write into
        deadline: Wall-clock time to stop at
    """
    try:
        request = geyser_pb2.SubscribeDeshredRequest()
        filters = request.deshred_transactions["pump"]
        filters.account_include.append(PUMP_PROGRAM_ID)
        filters.vote = False
        for update in build_stub().SubscribeDeshred(_keepalive(request, deadline)):
            seen = time.monotonic()
            if time.time() > deadline:
                break
            if not update.HasField("deshred_transaction"):
                continue
            info = update.deshred_transaction.transaction
            signature = base58.b58encode(bytes(info.signature)).decode()
            collector.deshred_at.setdefault(signature, seen)
            collector.deshred_all.add(signature)
            # No logs and no inner instructions here, by construction.
            if any(is_create(ix.data) for ix in info.transaction.message.instructions):
                collector.deshred_creates.add(signature)
    except grpc.RpcError as error:
        collector.errors["deshred"] = f"{error.code()}: {error.details()}"


def print_results(collector: Collector, duration: int) -> None:
    """Print the counts, the lead distributions and the blind-spot breakdown.

    Args:
        collector: State both streams wrote into
        duration: Sampling window in seconds, for the header
    """
    print(f"errors: {collector.errors or 'none'}")
    print(f"window: {duration}s")
    print(
        f"executed stream : {len(collector.executed_at)} txs, "
        f"{len(collector.create_shape)} creates"
    )
    print(
        f"deshred stream  : {len(collector.deshred_at)} txs, "
        f"{len(collector.deshred_creates)} creates"
    )

    both = set(collector.executed_at) & set(collector.deshred_at)
    print(
        f"signatures in both: {len(both)}  "
        f"(deshred only {len(set(collector.deshred_at) - set(collector.executed_at))}, "
        f"executed only {len(set(collector.executed_at) - set(collector.deshred_at))})"
    )

    leads = {
        sig: (collector.executed_at[sig] - collector.deshred_at[sig]) * 1000
        for sig in both
    }
    report("ALL pump.fun transactions", list(leads.values()))
    report(
        "CREATES only", [leads[sig] for sig in collector.create_shape if sig in leads]
    )

    shapes = collections.Counter(collector.create_shape.values())
    print(f"\ncreate shapes seen: {dict(shapes)}")

    missed = {
        sig: shape
        for sig, shape in collector.create_shape.items()
        if sig not in collector.deshred_creates
    }
    print(
        f"creates deshred could not detect: {len(missed)}/{len(collector.create_shape)}"
    )
    breakdown = collections.Counter(
        f"{shape}, "
        f"{'present on deshred' if sig in collector.deshred_all else 'ABSENT from deshred'}"
        for sig, shape in missed.items()
    )
    for reason, count in breakdown.most_common():
        print(f"  {count:3d}  {reason}")
    print(
        "\nA CPI create that is present but undetected is the structural limit: "
        "inner instructions are produced by execution, so a pre-execution stream "
        "never carries them."
    )


def main() -> int:
    """Race the two streams and print the comparison.

    Returns:
        Process exit code
    """
    parser = argparse.ArgumentParser(description="Compare deshred and executed streams")
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help=f"seconds to sample (default: {DEFAULT_DURATION})",
    )
    args = parser.parse_args()

    collector = Collector()
    deadline = time.time() + args.duration
    threads = [
        threading.Thread(target=collect_executed, args=(collector, deadline)),
        threading.Thread(target=collect_deshred, args=(collector, deadline)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(args.duration + 20)

    print_results(collector, args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())

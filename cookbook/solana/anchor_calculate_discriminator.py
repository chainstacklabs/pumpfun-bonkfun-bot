"""Compute the 8-byte Anchor discriminator for an instruction or account.

Usage:
    uv run cookbook/solana/anchor_calculate_discriminator.py
    uv run cookbook/solana/anchor_calculate_discriminator.py global:buy_v2
    uv run cookbook/solana/anchor_calculate_discriminator.py account:BondingCurve

Anchor prefixes every instruction's data and every account's data with the first
8 bytes of `sha256("<namespace>:<name>")` — the namespace is `global` for
instructions and `account` for account types. That prefix is how you identify
what you are looking at, and the only reliable way: several pump.fun
instructions take the same number of accounts, so counting them mislabels one as
another.

Docs: https://www.anchor-lang.com/docs/basics/idl
"""

import argparse
import hashlib
import struct

DEFAULT_NAME = "account:BondingCurve"


def calculate_discriminator(instruction_name):
    # Create a SHA256 hash object
    sha = hashlib.sha256()

    # Update the hash with the instruction name
    sha.update(instruction_name.encode("utf-8"))

    # Get the first 8 bytes of the hash
    discriminator_bytes = sha.digest()[:8]

    # Convert the bytes to a 64-bit unsigned integer (little-endian)
    discriminator = struct.unpack("<Q", discriminator_bytes)[0]

    return discriminator


def main() -> None:
    """Parse the command line and print the discriminator."""
    parser = argparse.ArgumentParser(
        description="Compute an Anchor 8-byte discriminator"
    )
    parser.add_argument(
        "name",
        nargs="?",
        default=DEFAULT_NAME,
        help=f"Namespaced name, e.g. global:buy_v2 (default {DEFAULT_NAME})",
    )
    args = parser.parse_args()

    discriminator = calculate_discriminator(args.name)
    little_endian = discriminator.to_bytes(8, "little")
    print(f"Discriminator for '{args.name}':")
    print(f"  u64:   {discriminator}")
    print(f"  bytes: {list(little_endian)}")
    print(f"  hex:   {little_endian.hex()}")


if __name__ == "__main__":
    main()

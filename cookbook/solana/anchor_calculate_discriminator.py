"""Compute the 8-byte Anchor discriminator for an instruction or account.

Usage:
    uv run cookbook/solana/anchor_calculate_discriminator.py

Edit `instruction_name` below. Anchor prefixes every instruction's data and every
account's data with the first 8 bytes of `sha256("<namespace>:<name>")` — the
namespace is `global` for instructions and `account` for account types. That
prefix is how you identify what you are looking at, and the only reliable way:
several pump.fun instructions take the same number of accounts, so counting them
mislabels one as another.

Docs: https://book.anchor-lang.com/anchor_bts/discriminator.html
"""

import hashlib
import struct

# https://book.anchor-lang.com/anchor_bts/discriminator.html
# Set the instruction name here
instruction_name = "account:BondingCurve"


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


# Calculate the discriminator for the specified instruction
discriminator = calculate_discriminator(instruction_name)

print(f"Discriminator for '{instruction_name}' instruction: {discriminator}")

# global:buy discriminator - 16927863322537952870
# global:sell discriminator - 12502976635542562355
# global:create discriminator - 8576854823835016728
# account:BondingCurve discriminator - 6966180631402821399

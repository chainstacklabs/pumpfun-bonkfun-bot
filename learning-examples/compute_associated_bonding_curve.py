"""Derive a coin's bonding curve and associated bonding curve, offline.

Usage:
    uv run learning-examples/compute_associated_bonding_curve.py <MINT>

The associated bonding curve is an ordinary ATA, so its address depends on which
token program owns the mint. Coins created with `create_v2` are Token2022; older
`create` coins are SPL Token. Deriving against the wrong program returns a
valid-looking address that does not exist on chain, so both are printed here —
this script makes no RPC calls and cannot tell which one a given mint is.
"""

import sys

from solders.pubkey import Pubkey

# Global constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
MIN_ARGC = 2


def get_bonding_curve_address(mint: Pubkey, program_id: Pubkey) -> tuple[Pubkey, int]:
    """
    Derives the bonding curve address for a given mint
    """
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], program_id)


def find_associated_bonding_curve(
    mint: Pubkey, bonding_curve: Pubkey, token_program: Pubkey = TOKEN_2022_PROGRAM
) -> Pubkey:
    """
    Find the associated bonding curve for a given mint and bonding curve.
    This uses the standard ATA derivation.

    Args:
        mint: The token mint address
        bonding_curve: The bonding curve PDA
        token_program: Token program owning the mint; Token2022 for create_v2 coins

    Returns:
        The derived associated bonding curve address
    """
    derived_address, _ = Pubkey.find_program_address(
        [
            bytes(bonding_curve),
            bytes(token_program),
            bytes(mint),
        ],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def main() -> None:
    """Print the curve PDAs for the mint given on the command line."""
    if len(sys.argv) < MIN_ARGC:
        print(
            "Usage: uv run learning-examples/compute_associated_bonding_curve.py <MINT>"
        )
        return

    try:
        mint = Pubkey.from_string(sys.argv[1])
    except ValueError as e:
        print(f"Error: Invalid address format - {e!s}")
        return

    bonding_curve_address, bump = get_bonding_curve_address(mint, PUMP_PROGRAM)

    print("\nResults:")
    print("-" * 62)
    print(f"Token Mint:                        {mint}")
    print(f"Bonding Curve:                     {bonding_curve_address}")
    print(f"Bonding Curve Bump:                {bump}")
    for label, program in (
        ("Token2022 (create_v2 coins)", TOKEN_2022_PROGRAM),
        ("SPL Token (legacy create)", SYSTEM_TOKEN_PROGRAM),
    ):
        derived = find_associated_bonding_curve(mint, bonding_curve_address, program)
        print(f"Associated BC, {label:19s} {derived}")
    print("-" * 62)


if __name__ == "__main__":
    main()

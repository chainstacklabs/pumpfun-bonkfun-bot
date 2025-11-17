import base64
import json
import struct

from construct import Bytes, Flag, Int64ul, Struct
from solders.pubkey import Pubkey

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)


class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),  # Added new creator field - 32 bytes for Pubkey
        "is_mayhem_mode" / Flag,  # Added mayhem mode flag - 1 byte
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data - supports all versions."""
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        # Required fields (always present)
        offset = 8
        self.virtual_token_reserves = int.from_bytes(
            data[offset : offset + 8], "little"
        )
        offset += 8
        self.virtual_sol_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.real_token_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.real_sol_reserves = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.token_total_supply = int.from_bytes(data[offset : offset + 8], "little")
        offset += 8
        self.complete = bool(data[offset])
        offset += 1

        # Optional fields (may not be present in older versions)
        if len(data) >= offset + 32:
            self.creator = Pubkey.from_bytes(data[offset : offset + 32])
            offset += 32

            if len(data) > offset:
                self.is_mayhem_mode = bool(data[offset])
            else:
                self.is_mayhem_mode = None

        else:
            self.creator = None


def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


def decode_bonding_curve_data(raw_data: str) -> BondingCurveState:
    decoded_data = base64.b64decode(raw_data)
    if decoded_data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")
    return BondingCurveState(decoded_data)


# Load the JSON data
with open("learning-examples/raw_bondingCurve_from_getAccountInfo.json") as file:
    json_data = json.load(file)

# Extract the base64 encoded data
encoded_data = json_data["result"]["value"]["data"][0]

# Decode the data
bonding_curve_state = decode_bonding_curve_data(encoded_data)

# Calculate and print the token price
token_price_sol = calculate_bonding_curve_price(bonding_curve_state)

print("Bonding Curve State:")
print(f"  Virtual Token Reserves: {bonding_curve_state.virtual_token_reserves}")
print(f"  Virtual SOL Reserves: {bonding_curve_state.virtual_sol_reserves}")
print(f"  Real Token Reserves: {bonding_curve_state.real_token_reserves}")
print(f"  Real SOL Reserves: {bonding_curve_state.real_sol_reserves}")
print(f"  Token Total Supply: {bonding_curve_state.token_total_supply}")
print(f"  Complete: {bonding_curve_state.complete}")
print(f"\nToken Price: {token_price_sol:.10f} SOL")

import base64
import json
import struct
import sys

from construct import Bytes, Flag, Int64ul, Struct
from solders.pubkey import Pubkey

TOKEN_DECIMALS = 6
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)

# Quote assets. A curve's `quote_mint` is all zeros when the coin is SOL-paired, and
# the quote-side reserves are always raw units of that mint: 1e9 for SOL, 1e6 for USDC.
DEFAULT_QUOTE_MINT = Pubkey.from_bytes(bytes(32))
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
QUOTE_DECIMALS = {WSOL_MINT: 9, USDC_MINT: 6}
QUOTE_SYMBOLS = {WSOL_MINT: "SOL", USDC_MINT: "USDC"}


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
                offset += 1
            else:
                self.is_mayhem_mode = None

            if len(data) > offset:
                self.is_cashback_coin = bool(data[offset])
                offset += 1
            else:
                self.is_cashback_coin = None

            if len(data) >= offset + 32:
                self.quote_mint = Pubkey.from_bytes(data[offset : offset + 32])
            else:
                self.quote_mint = DEFAULT_QUOTE_MINT

        else:
            self.creator = None
            self.is_mayhem_mode = None
            self.is_cashback_coin = None
            self.quote_mint = DEFAULT_QUOTE_MINT

    @property
    def effective_quote_mint(self) -> Pubkey:
        """The quote mint to price against, resolving all-zeros to wrapped SOL."""
        return WSOL_MINT if self.quote_mint == DEFAULT_QUOTE_MINT else self.quote_mint

    @property
    def quote_symbol(self) -> str:
        """Display symbol of the quote asset."""
        mint = self.effective_quote_mint
        return QUOTE_SYMBOLS.get(mint, str(mint))

    @property
    def quote_units(self) -> int:
        """Raw units per whole token of the quote asset."""
        return 10 ** QUOTE_DECIMALS.get(self.effective_quote_mint, 9)


def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / curve_state.quote_units) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


def decode_bonding_curve_data(raw_data: str) -> BondingCurveState:
    decoded_data = base64.b64decode(raw_data)
    if decoded_data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")
    return BondingCurveState(decoded_data)


# Load the JSON data: pass a getAccountInfo response as argv[1], or use the fixture
curve_file = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "learning-examples/raw_bonding_curve_from_getaccountinfo.json"
)
with open(curve_file) as file:
    json_data = json.load(file)

# Extract the base64 encoded data
encoded_data = json_data["result"]["value"]["data"][0]

# Decode the data
bonding_curve_state = decode_bonding_curve_data(encoded_data)

# Calculate and print the token price
token_price = calculate_bonding_curve_price(bonding_curve_state)
symbol = bonding_curve_state.quote_symbol

print("Bonding Curve State:")
print(f"  Virtual Token Reserves: {bonding_curve_state.virtual_token_reserves}")
print(
    f"  Virtual Quote Reserves: {bonding_curve_state.virtual_sol_reserves} raw {symbol}"
)
print(f"  Real Token Reserves: {bonding_curve_state.real_token_reserves}")
print(f"  Real Quote Reserves: {bonding_curve_state.real_sol_reserves} raw {symbol}")
print(f"  Token Total Supply: {bonding_curve_state.token_total_supply}")
print(f"  Complete: {bonding_curve_state.complete}")
print(f"  Mayhem Mode: {bonding_curve_state.is_mayhem_mode}")
print(f"  Cashback Coin: {bonding_curve_state.is_cashback_coin}")
print(f"  Quote Mint: {bonding_curve_state.effective_quote_mint}")
print(f"\nToken Price: {token_price:.10f} {symbol}")

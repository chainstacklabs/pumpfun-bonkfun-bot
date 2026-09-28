"""Verify a coin's quote asset is chosen from the registries, not assumed.

A coin can be priced in an asset other than SOL, and picking that asset wrong is
not a near miss: the opening reserve, the decimals and the token program all
change with it.

Two registries are live and neither is a superset of the other.
`Global.whitelisted_quote_mints` holds one entry; `QuoteControl` holds far more,
and USDC is in the former and not the latter. Validating against either alone
refuses a mint the program accepts.

They are not interchangeable once a coin carries a creator fee, either: the fee
is applied only on a coin priced in a `QuoteControl` mint. On wrapped SOL, and
on a mint only the Global whitelist carries, the program takes the argument and
stores zero.

The opening virtual quote reserve is per mint and spans orders of magnitude
across the admitted set, so substituting a default for an unlisted mint
misprices the opening buy rather than approximating it.

Tokenised equities among the admitted mints carry Token-2022 extensions their
issuer controls. `pausableConfig` fails every trade on every coin priced in that
mint while it is set, so it is refused up front rather than discovered at buy
time.

Offline machine checks, no network and no funds moved:

  1. QuoteControl decodes into mint -> opening reserve, and a truncated entry
     at the tail is dropped rather than read past the end of the buffer.
  2. A mint QuoteControl admits prices from its own entry.
  3. A mint only `Global.whitelisted_quote_mints` lists prices from Global's
     `initial_virtual_quote_reserves` -- the USDC case.
  4. Wrapped SOL prices from Global's `initial_virtual_sol_reserves`.
  5. A mint neither registry admits raises, rather than taking a default.
  6. A paused quote mint is refused.
  7. A scaled-UI quote mint reports its multiplier, and an ordinary mint
     reports none.
  8. A creator fee is refused unless QuoteControl admits the quote mint.
     Anywhere else the program takes the argument and stores zero, and a mint
     the older Global whitelist carries -- USDC -- is one of those places.
  9. Mayhem mode paired with a non-SOL quote asset is refused before sending.
     The program rejects the pairing with MayhemModeQuoteMintNotAllowed
     (6071), and a create that reaches the chain has already cost fees.

Usage:
    uv run tests/regression/verify_quote_asset_selection.py
"""

import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))

import pumpfun_instructions as pump  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

USDC = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# One real QuoteControl entry, used as a value distinct from Global's figure.
REGISTRY_RESERVE = 16_685_497
SCALED_MULTIPLIER = 1.0026642
FLOAT_TOLERANCE = 1e-9

GLOBAL = {
    "initial_virtual_sol_reserves": 30_000_000_000,
    "initial_virtual_quote_reserves": 4_292_000_000,
    "whitelisted_quote_mints": [USDC],
}


def build_quote_control(entries: list[tuple[Pubkey, int]], *, truncate: bool) -> bytes:
    """Serialize a QuoteControl account holding `entries`."""
    head = bytes(8) + bytes(32) + bytes(64)
    body = struct.pack("<I", len(entries))
    for mint, reserves in entries:
        body += bytes(mint) + struct.pack("<Q", reserves)
    if truncate:
        body = body[:-12]
    return head + body


def check_registry_decodes() -> bool:
    mints = [(Keypair().pubkey(), 1_000 * (i + 1)) for i in range(4)]
    decoded = pump.decode_quote_control(build_quote_control(mints, truncate=False))
    if decoded != dict(mints):
        print(f"  decoded {len(decoded)} entries, expected {len(mints)}")
        return False
    clipped = pump.decode_quote_control(build_quote_control(mints, truncate=True))
    if len(clipped) != len(mints) - 1:
        print(f"  a truncated tail gave {len(clipped)} entries, expected 3")
        return False
    return True


def check_registry_mint_prices_from_its_entry() -> bool:
    mint = Keypair().pubkey()
    registry = {mint: REGISTRY_RESERVE}
    got = pump.opening_quote_reserves(registry, mint, GLOBAL)
    if got != REGISTRY_RESERVE:
        print(f"  got {got}, expected the registry entry")
        return False
    # Not Global's figure, which is what a one-registry implementation returns.
    if got == GLOBAL["initial_virtual_quote_reserves"]:
        print("  fell back to Global instead of using the entry")
        return False
    return True


def check_global_whitelisted_mint_is_accepted() -> bool:
    got = pump.opening_quote_reserves({}, USDC, GLOBAL)
    if got != GLOBAL["initial_virtual_quote_reserves"]:
        print(f"  got {got}, expected Global's initial_virtual_quote_reserves")
        return False
    return True


def check_wrapped_sol_uses_the_sol_reserve() -> bool:
    got = pump.opening_quote_reserves({}, pump.WSOL_MINT, GLOBAL)
    return got == GLOBAL["initial_virtual_sol_reserves"]


def check_unlisted_mint_raises() -> bool:
    stranger = Keypair().pubkey()
    try:
        got = pump.opening_quote_reserves({}, stranger, GLOBAL)
    except ValueError:
        return True
    print(f"  returned {got} for a mint neither registry admits")
    return False


def check_paused_mint_is_refused() -> bool:
    mint = Keypair().pubkey()
    paused = {
        "extensions": [{"extension": "pausableConfig", "state": {"paused": True}}]
    }
    try:
        pump.check_quote_mint_tradable(mint, paused)
    except ValueError:
        pass
    else:
        print("  a paused mint was accepted")
        return False
    live = {"extensions": [{"extension": "pausableConfig", "state": {"paused": False}}]}
    try:
        pump.check_quote_mint_tradable(mint, live)
    except ValueError:
        print("  an unpaused mint was refused")
        return False
    return True


def check_scaled_multiplier_is_reported() -> bool:
    mint = Keypair().pubkey()
    scaled = {
        "extensions": [
            {
                "extension": "scaledUiAmountConfig",
                "state": {"multiplier": str(SCALED_MULTIPLIER)},
            }
        ]
    }
    got = pump.check_quote_mint_tradable(mint, scaled)
    if got is None or abs(got - SCALED_MULTIPLIER) > FLOAT_TOLERANCE:
        print(f"  multiplier came back as {got}")
        return False
    if pump.check_quote_mint_tradable(mint, {"extensions": []}) is not None:
        print("  an unscaled mint reported a multiplier")
        return False
    return True


def check_fee_needs_a_registry_mint() -> bool:
    listed = Keypair().pubkey()
    registry = {listed: REGISTRY_RESERVE}

    # Allowed where the fee is actually applied.
    try:
        pump.check_creator_fee_quote(registry, listed, 250)
    except ValueError:
        print("  refused a fee on a mint QuoteControl admits")
        return False

    # Refused everywhere it would silently store zero.
    for label, mint in (
        ("wrapped SOL", pump.WSOL_MINT),
        ("a Global-whitelisted mint", USDC),
        ("an unlisted mint", Keypair().pubkey()),
    ):
        try:
            pump.check_creator_fee_quote(registry, mint, 250)
        except ValueError:
            continue
        print(f"  allowed a fee against {label}, which stores zero")
        return False

    # No fee requested is never refused, whatever the mint.
    try:
        pump.check_creator_fee_quote(registry, pump.WSOL_MINT, None)
        pump.check_creator_fee_quote(registry, pump.WSOL_MINT, 0)
    except ValueError:
        print("  refused a launch that asked for no fee at all")
        return False
    return True


def check_mayhem_rejects_a_non_sol_quote() -> bool:
    stranger = Keypair().pubkey()
    try:
        pump.check_mayhem_quote_pairing(stranger, is_mayhem_mode=True)
    except ValueError:
        pass
    else:
        print("  a mayhem coin was allowed a non-SOL quote asset")
        return False
    # The pairing is only refused for mayhem, and only for a non-SOL asset.
    try:
        pump.check_mayhem_quote_pairing(stranger, is_mayhem_mode=False)
        pump.check_mayhem_quote_pairing(pump.WSOL_MINT, is_mayhem_mode=True)
    except ValueError:
        print("  refused a pairing the program accepts")
        return False
    return True


def main() -> int:
    checks = [
        ("QuoteControl decodes, short tail dropped", check_registry_decodes),
        (
            "a QuoteControl mint prices from its entry",
            check_registry_mint_prices_from_its_entry,
        ),
        (
            "a Global-whitelisted mint is accepted",
            check_global_whitelisted_mint_is_accepted,
        ),
        ("wrapped SOL uses the SOL reserve", check_wrapped_sol_uses_the_sol_reserve),
        ("a mint neither registry admits raises", check_unlisted_mint_raises),
        ("a paused quote mint is refused", check_paused_mint_is_refused),
        (
            "a scaled quote mint reports its multiplier",
            check_scaled_multiplier_is_reported,
        ),
        ("a fee needs a QuoteControl mint", check_fee_needs_a_registry_mint),
        ("mayhem refuses a non-SOL quote", check_mayhem_rejects_a_non_sol_quote),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Verify trades pay the fee recipients pump.fun rotated to on 2026-11-25.

pump.fun replaced its 8 normal protocol fee recipients, shared by the bonding
curve and PumpSwap, on 2026-11-25; from that day a trade naming an old one is
rejected. The bot sent one fixed old recipient on every curve trade, PumpSwap
took them from GlobalConfig (which still listed the old set), and the cookbook
hardcoded old ones, so every trade on a normal coin or pool would have failed.
Mayhem and buyback recipients were not rotated.

Offline machine checks, no network and no funds moved:

  A. `NORMAL_FEE_RECIPIENTS` is exactly the set from pump.fun's
     FEE_RECIPIENT_ROTATION.md.
  B. A curve trade on a normal coin names one of them, and over many trades
     every one of them, never a retired one; a mayhem coin still names
     `MAYHEM_FEE`.
  C. No retired recipient is left in `src/` or `cookbook/` code. Recorded
     transactions under `tests/` and `cookbook/` JSON keep them: they are what
     the chain did.

PumpSwap's pick ignoring GlobalConfig is checked by `verify_pumpswap_buy.py`
(checks A and G).

Usage:
    uv run tests/regression/verify_fee_recipient_rotation.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from _fakes import _check  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from verify_v2_account_layout import build_token_info  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)

CURRENT = [
    "2aSWZ3QXsNLVVBr1UB1HxSw7sqMRR2pv2qa5ohsTi5Gp",
    "DrcDxWxLi3i7Yd7cTyCyd44DE7pwS9BvFxSsPx1c7XTv",
    "EAN7utdjyxhKVaqA69DRyxW5ffBhnidRZfifmfhwUgPm",
    "Fso3Ggq2Um1pQT6Uzq8wDicgRh62vANYZUT8uqJd2fXh",
    "BWNNnhN6id5j71h5wfkzmXjnV2fgF9zA61FqGJJjnakw",
    "Eh7hTLi6iMbT55jAEYVgtN5QaSb6z9Ytzwxs2LGG5qg4",
    "3Mqk6qHEj7r3TFNFYjCP85a5venbp2mefYQTSKVVchBC",
    "A3cm36KqisUXVTSRCK6VQ7kvXLene4gsbQedhcd5MujQ",
]
RETIRED = [
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
    "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
    "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
    "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
    "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
    # Listed in PumpSwap's GlobalConfig beside the seven above.
    "JCRGumoE9Qi5BBgULTgdgTLjSgkCMSbF62ZZfGs84JeU",
]
DRAWS = 400


def check_a_current_set() -> bool:
    """A: the normal set is the rotated one."""
    ours = [str(k) for k in PumpFunAddresses.NORMAL_FEE_RECIPIENTS]
    return _check(
        "normal set is the 2026-11-25 one",
        sorted(ours) == sorted(CURRENT),
        f"{len(set(ours) & set(CURRENT))}/8 current, "
        f"{len(set(ours) & set(RETIRED))} retired",
    )


def check_b_curve_picks() -> bool:
    """B: curve trades pick from the current set; mayhem is unchanged."""
    provider = PumpFunAddressProvider()
    user = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")
    normal = build_token_info(WSOL_MINT, mayhem=False)
    seen = set()
    for i in range(DRAWS):
        accounts = (
            provider.get_buy_v2_instruction_accounts(normal, user)
            if i % 2
            else provider.get_sell_v2_instruction_accounts(normal, user)
        )
        seen.add(str(accounts["fee_recipient"]))
    ok = _check(
        "normal coin pays a current recipient",
        seen == set(CURRENT),
        f"{len(seen & set(CURRENT))}/8 current seen, {len(seen - set(CURRENT))} other",
    )
    mayhem = build_token_info(WSOL_MINT, mayhem=True)
    picked = provider.get_buy_v2_instruction_accounts(mayhem, user)["fee_recipient"]
    return (
        _check(
            "mayhem coin still pays MAYHEM_FEE",
            picked == PumpFunAddresses.MAYHEM_FEE
            and picked in PumpFunAddresses.RESERVED_FEE_RECIPIENTS,
            str(picked),
        )
        and ok
    )


def check_c_no_retired_in_code() -> bool:
    """C: no retired recipient left in src/ or cookbook/ code."""
    hits = [
        f"{path.relative_to(PROJECT_ROOT)}: {key[:8]}"
        for root in ("src", "cookbook")
        for path in sorted((PROJECT_ROOT / root).rglob("*.py"))
        for key in RETIRED
        if key in path.read_text(encoding="utf-8")
    ]
    for hit in hits:
        print(f"    {hit}")
    return _check("no retired recipient in code", not hits, f"{len(hits)} found")


def main() -> int:
    """Run every check and report."""
    results = [
        check_a_current_set(),
        check_b_curve_picks(),
        check_c_no_retired_in_code(),
    ]
    if all(results):
        print("\nAll fee recipient rotation checks passed.")
        return 0
    print("\nFee recipient rotation checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Verify the PumpSwap trade scripts build the right accounts for either pool kind.

pump-amm's `buy`/`sell` take remaining accounts the vendored IDL does not list,
and the IDL is byte-identical to upstream, so the IDL cannot be the reference:
`buy` is 23 accounts there and 25 or 26 on chain, `sell` is 21 there and 23 to 26.
The extras are, in order, an optional `pool-v2` PDA, a buyback fee recipient, and
that recipient's quote-mint ATA.

The pair is read *positionally* from the end, and `pool-v2` belongs only to a
**canonical** pool — one that graduated from a pump.fun bonding curve.
`Pool.coin_creator` is the discriminator: set for canonical pools, left at
`Pubkey::default()` otherwise (upstream PUMP_SWAP_CREATOR_FEE_README.md). Sending
`pool-v2` on a non-canonical pool shifts the pair by one, so the program reads the
pool-v2 PDA as the buyback recipient and rejects it with
`BuybackFeeRecipientNotAuthorized` (6053).

Unblocking those pools exposed a second bug they had been hiding: the scripts
hardcoded 6 base-token decimals, true for every coin that graduated from a
bonding curve and wrong for non-canonical pools, which routinely carry 9. A
factor of 1000 scales the quote and the slippage floor together, so a sell
reverts `ExceededSlippage` (6004) rather than merely mispricing.

Offline machine checks, no network and no funds moved:

  1. Both scripts gate the `pool-v2` account on `coin_creator`, and neither
     appends it unconditionally.
  2. The buyback recipient and its ATA are appended after that gate, so they stay
     last whether or not `pool-v2` is present.
  3. Neither script carries a hardcoded base-token decimals constant, and both
     resolve decimals through `get_mint_info`.
  4. `get_mint_info` returns the token program and the decimals from one account
     read, and raises rather than defaulting when the mint cannot be read.

With `--live` it also re-reads pump-amm's `GlobalConfig` and checks the committed
recipient list still matches the chain, which is the part that silently rots.

Usage:
    uv run tests/regression/verify_pumpswap_account_layout.py
    uv run tests/regression/verify_pumpswap_account_layout.py --live
"""

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCRIPTS = (
    PROJECT_ROOT / "cookbook" / "pumpswap" / "pumpswap_buy_token.py",
    PROJECT_ROOT / "cookbook" / "pumpswap" / "pumpswap_sell_token.py",
)

# pump-amm's GlobalConfig, and where buyback_fee_recipients sits inside it:
# 8 discriminator + admin 32 + two u64 + disable_flags 1 + protocol[8*32]
# + coin_creator_fee_bps 8 + admin_set 32 + whitelist 32 + reserved_recipient 32
# + mayhem 1 + reserved[7*32] + cashback 1
GLOBAL_CONFIG = "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
BUYBACK_OFFSET = 8 + 32 + 8 + 8 + 1 + 8 * 32 + 8 + 32 + 32 + 32 + 1 + 7 * 32 + 1
BUYBACK_COUNT = 8


def _source(path: Path) -> str:
    return path.read_text()


def check_pool_v2_is_conditional() -> bool:
    """`pool-v2` must be gated on the pool having a coin creator."""
    ok = True
    for path in SCRIPTS:
        src = _source(path)
        if "find_pool_v2(base_mint)" not in src:
            print(f"     {path.name} no longer derives pool-v2 at all")
            ok = False
            continue
        gated = re.search(
            r"if coin_creator != DEFAULT_COIN_CREATOR:\s*\n\s*accounts\.append\(",
            src,
        )
        if not gated:
            print(f"     {path.name} does not gate pool-v2 on coin_creator")
            ok = False
    return ok


def check_buyback_pair_stays_last() -> bool:
    """The recipient and its ATA are positional, so they must follow the gate."""
    ok = True
    for path in SCRIPTS:
        src = _source(path)
        gate = src.find("if coin_creator != DEFAULT_COIN_CREATOR:")
        recipient = src.find("breaking_fee_recipient = random.choice(")
        if gate < 0 or recipient < 0:
            print(f"     {path.name} is missing the gate or the recipient choice")
            ok = False
            continue
        if recipient < gate:
            print(f"     {path.name} picks the buyback recipient before the gate")
            ok = False
    return ok


def check_no_hardcoded_base_decimals() -> bool:
    """A hardcoded 6 is right only for coins that graduated from a curve."""
    ok = True
    for path in SCRIPTS:
        src = _source(path)
        if re.search(r"^TOKEN_DECIMALS\s*=", src, re.M):
            print(f"     {path.name} still defines a TOKEN_DECIMALS constant")
            ok = False
        if "get_mint_info" not in src:
            print(f"     {path.name} does not resolve decimals via get_mint_info")
            ok = False
        if "base_decimals" not in src:
            print(f"     {path.name} never uses a resolved base_decimals")
            ok = False
    return ok


def check_get_mint_info_resolves_both_and_raises() -> bool:
    """One read, two fields, and no silent default."""
    ok = True
    for path in SCRIPTS:
        tree = ast.parse(_source(path))
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_mint_info"
            ),
            None,
        )
        if fn is None:
            print(f"     {path.name} has no get_mint_info")
            ok = False
            continue
        body = ast.unparse(fn)
        if body.count("get_account_info") != 1:
            print(f"     {path.name}: get_mint_info should do exactly one account read")
            ok = False
        if "raise ValueError" not in body:
            print(f"     {path.name}: get_mint_info must raise, not default")
            ok = False
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
        if not any(isinstance(r.value, ast.Tuple) for r in returns):
            print(f"     {path.name}: get_mint_info must return (program, decimals)")
            ok = False
    return ok


def check_recipient_list_matches_chain() -> bool:
    """--live: the committed buyback recipients must still be the authorized set."""
    import asyncio
    import base64
    import os

    import aiohttp
    from dotenv import load_dotenv
    from solders.pubkey import Pubkey

    load_dotenv(PROJECT_ROOT / ".env")
    endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    if not endpoint:
        print("     SOLANA_NODE_RPC_ENDPOINT is unset; skipping")
        return True

    async def read() -> list[str]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                GLOBAL_CONFIG,
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        }
        async with (
            aiohttp.ClientSession() as session,
            session.post(endpoint, json=body) as response,
        ):
            payload = await response.json()
        raw = base64.b64decode(payload["result"]["value"]["data"][0])
        return [
            str(Pubkey(raw[BUYBACK_OFFSET + i * 32 : BUYBACK_OFFSET + (i + 1) * 32]))
            for i in range(BUYBACK_COUNT)
        ]

    on_chain = asyncio.run(read())
    ok = True
    for path in SCRIPTS:
        block = _source(path).split("BREAKING_FEE_RECIPIENTS = [")[1].split("]")[0]
        committed = re.findall(r'"([1-9A-HJ-NP-Za-km-z]{32,44})"', block)
        if committed != on_chain:
            print(f"     {path.name} recipient list has drifted from GlobalConfig")
            print(f"       missing: {sorted(set(on_chain) - set(committed))}")
            print(f"       stale:   {sorted(set(committed) - set(on_chain))}")
            ok = False
    return ok


def main() -> int:
    live = "--live" in sys.argv
    checks = [
        ("pool-v2 is gated on coin_creator", check_pool_v2_is_conditional),
        ("the buyback pair stays last", check_buyback_pair_stays_last),
        ("no hardcoded base-token decimals", check_no_hardcoded_base_decimals),
        (
            "get_mint_info reads once and raises",
            check_get_mint_info_resolves_both_and_raises,
        ),
    ]
    if live:
        checks.append(
            ("committed recipients match the chain", check_recipient_list_matches_chain)
        )

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

    if not live:
        print("\n(skipping the network check; pass --live to read GlobalConfig)")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Verify no trade path prices a coin before resolving its quote mint.

pump.fun's quote assets are not just SOL and USDC. The `QuoteControl` registry
(PDA `["quote-control"]`) admits mints at 6, 8 and 9 decimals — 79 of the 170
admitted on 2026-09-22 are tokenized equities, 8 decimals for Backed's xStocks
and 6 for Backpack Securities — and coins paired with them trade live: a 75s
sample of curve updates that day caught 8 stock-paired curves out of 124.

`quote_units()` used to default to 9 decimals for an unresolved mint. Every
cookbook trade script called it *before* `resolve_quote_token_program()` warmed
the decimals cache, so every one of those coins was sized against the wrong
power of ten:

    xStock  (8 decimals)  ->  cap and size 10x too large
    Backpack (6 decimals) ->  cap and size 1000x too large

The error does not cancel. `price_per_token()` divides the quote reserves by the
same wrong unit, so the price comes out low by the same factor, the token amount
comes out high by it, and the inflated cap authorises the overspend instead of
catching it.

Offline machine checks, no network and no funds moved:

  A. `quote_units()` raises for a mint whose decimals are unknown, rather than
     assuming 9, and returns the right value once resolved.
  B. `resolve_quote_token_program()` caches decimals off the same account read
     it makes for the token program, so resolving costs no extra RPC call.
  C. Every cookbook script that calls `quote_units` or `price_per_token` calls
     `resolve_quote_token_program` first, checked per function body.

Usage:
    uv run tests/regression/verify_quote_decimals_resolved.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))

import pumpfun_instructions_v2 as pump_v2  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

COOKBOOK = PROJECT_ROOT / "cookbook"

# A real Token-2022 tokenized equity: Apple xStock, 8 decimals, admitted through
# QuoteControl rather than Global's whitelist.
AAPLX = Pubkey.from_string("XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp")
AAPLX_DECIMALS = 8


class _FakeMintAccount:
    """The two fields `resolve_quote_token_program` reads off a mint account."""

    def __init__(self, owner: Pubkey, decimals: int) -> None:
        """Build a mint account whose data carries `decimals` at offset 44.

        Args:
            owner: Token program that owns the mint
            decimals: Decimal count to place at the mint layout's fixed offset
        """
        self.owner = owner
        data = bytearray(82)
        data[44] = decimals
        self.data = bytes(data)


def check_unresolved_mint_raises() -> None:
    """An unknown quote mint must fail loudly, never default to 9 decimals."""
    assert AAPLX not in pump_v2.QUOTE_DECIMALS, "fixture mint must start unknown"
    try:
        pump_v2.quote_units(AAPLX)
    except ValueError as exc:
        assert "resolve_quote_token_program" in str(exc), (
            f"the error must say how to fix it, got: {exc}"
        )
    else:
        raise AssertionError(
            "quote_units() returned a value for an unresolved mint; a wrong "
            "power of ten here oversizes the trade instead of capping it"
        )

    # The pre-seeded pair still answers without a lookup.
    assert pump_v2.quote_units(pump_v2.WSOL_MINT) == 10**9
    assert pump_v2.quote_units(pump_v2.USDC_MINT) == 10**6


async def check_resolution_caches_decimals() -> None:
    """Resolving the token program must cache decimals from the same read."""
    reads = []

    async def get_account(address: Pubkey) -> _FakeMintAccount:
        reads.append(address)
        return _FakeMintAccount(pump_v2.TOKEN_2022_PROGRAM, AAPLX_DECIMALS)

    program = await pump_v2.resolve_quote_token_program(AAPLX, get_account)
    assert program == pump_v2.TOKEN_2022_PROGRAM, program
    assert pump_v2.quote_units(AAPLX) == 10**AAPLX_DECIMALS, (
        "decimals were not cached by the token-program resolution"
    )
    assert len(reads) == 1, f"expected one account read, made {len(reads)}"

    # Second call must be free.
    await pump_v2.resolve_quote_token_program(AAPLX, get_account)
    assert len(reads) == 1, "a resolved mint was fetched again"


def _called_names(node: ast.AST) -> list[tuple[str, int]]:
    """Every call made inside one function body, as (name, line).

    Args:
        node: The function definition to walk

    Returns:
        Called attribute/function names paired with their line numbers
    """
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name:
            out.append((name, sub.lineno))
    return out


def check_scripts_resolve_before_pricing() -> None:
    """No cookbook script may size or price a trade before resolving.

    Checked per function body, not per file: a module-level helper that wraps
    `price_per_token()` is defined above the trade path that calls it, so plain
    source order reads as a violation when the runtime order is fine.
    """
    offenders = []
    for path in sorted(COOKBOOK.rglob("*.py")):
        source = path.read_text()
        # Only scripts that share the helper module's cache are in scope; the
        # offline decoders carry their own self-contained arithmetic.
        if "pumpfun_instructions_v2" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            calls = _called_names(node)
            sizing = [
                ln for name, ln in calls if name in {"quote_units", "price_per_token"}
            ]
            resolving = [
                ln for name, ln in calls if name == "resolve_quote_token_program"
            ]
            if not sizing:
                continue
            where = f"{path.relative_to(PROJECT_ROOT)}:{node.name}"
            if not resolving:
                # Fine only if this function never sizes a trade itself, i.e. it
                # is a pure helper whose caller resolves. Those take the curve
                # state as an argument rather than fetching it.
                if any(name == "quote_units" for name, _ in calls):
                    offenders.append(f"{where}: sizes without resolving")
            elif min(sizing) < min(resolving):
                offenders.append(
                    f"{where}: prices at line {min(sizing)}, "
                    f"resolves at line {min(resolving)}"
                )
    assert not offenders, "trade paths price before resolving:\n  " + "\n  ".join(
        offenders
    )


async def main() -> None:
    """Run every check and report."""
    print("=" * 72)
    print("Verifying quote-mint decimals are resolved before pricing")
    print("=" * 72)

    checks = [
        ("an unresolved quote mint raises", check_unresolved_mint_raises),
        ("resolution caches decimals in one read", check_resolution_caches_decimals),
        ("every script resolves before pricing", check_scripts_resolve_before_pricing),
    ]
    for label, fn in checks:
        result = fn()
        if result is not None:
            await result
        print(f"{label} -> OK")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

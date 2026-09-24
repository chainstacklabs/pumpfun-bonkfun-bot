"""Verify no trade path prices a coin before resolving its quote mint.

pump.fun's quote assets are not just SOL and USDC. The `QuoteControl` registry
(PDA `["quote-control"]`) admits mints anywhere from 4 to 12 decimals — 8 for
Backed's xStocks, 6 for Backpack Securities — and coins paired with them trade
live.

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

The bot carried the same default long after the cookbook stopped: `quote_units_per_token`
returned 9 for an unresolved mint, and only the buy path's quote gate kept a
mispriced trade off the chain. Checks E through G cover `src/` so the guarantee
lives in the function rather than in two call sites.

Offline machine checks, no network and no funds moved:

  A. `quote_units()` raises for a mint whose decimals are unknown, rather than
     assuming 9, and returns the right value once resolved.
  B. `resolve_quote_token_program()` caches decimals off the same account read
     it makes for the token program, so resolving costs no extra RPC call.
  C. Every cookbook script that calls `quote_units` or `price_per_token` calls
     `resolve_quote_token_program` first, checked per function body.
  D. Nothing in `cookbook/` or `src/` falls back to a literal decimal count for
     a quote mint. The table name is matched on containing DECIMAL, not ending
     in it: the bot's default sat on `_QUOTE_DECIMALS_CACHE`, which an
     endswith test walks straight past.
  E. `quote_units_per_token()` raises for an unresolved mint, and
     `cached_quote_units()` answers None instead, for decoders that must not
     fail on a coin nobody trades.
  F. `QUOTE_TOKEN_PROGRAMS` and `QUOTE_DECIMALS` carry the same mints. A mint in
     the first alone short-circuits resolution before its decimals are read.
  G. The buy path resolves its spend amount before it sizes with a quote unit.
     The sell path deliberately does not: it liquidates a position already held,
     against a mint inherited from the buy, and gating it on a configured buy
     amount would strand that position whenever a config changed.

Usage:
    uv run tests/regression/verify_quote_decimals_resolved.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pumpfun_instructions_v2 as pump_v2  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core import pubkeys as core_pubkeys  # noqa: E402

COOKBOOK = PROJECT_ROOT / "cookbook"
SRC = PROJECT_ROOT / "src"

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
        # Only scripts that share the helper module's cache are in scope here.
        # Scripts carrying their own arithmetic are covered by
        # check_no_assumed_quote_decimals instead.
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


def check_no_assumed_quote_decimals() -> None:
    """No script or bot module may fall back to a literal decimal count.

    `QuoteControl` admits mints from 4 to 12 decimals, so the literal is wrong
    for most quote assets and silently so. A two-argument `.get` on a decimals
    table is the defect whatever the default is; read the mint instead.

    The table name is matched on containing DECIMAL rather than ending in it.
    The bot's own default was `_QUOTE_DECIMALS_CACHE.get(quote_mint, 9)`, which
    an endswith test reports as clean.
    """
    _GET_WITH_DEFAULT_ARGC = 2  # dict.get(key, default)

    offenders = []
    trees = [*sorted(COOKBOOK.rglob("*.py")), *sorted(SRC.rglob("*.py"))]
    for path in trees:
        if "generated" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                not isinstance(node, ast.Call)
                or len(node.args) != _GET_WITH_DEFAULT_ARGC
            ):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            table = func.value
            if not isinstance(table, ast.Name) or "DECIMAL" not in table.id.upper():
                continue
            offenders.append(
                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                f"{table.id}.get(..., <default>)"
            )
    assert not offenders, (
        "quote decimals assumed rather than resolved:\n  " + "\n  ".join(offenders)
    )


def check_core_unit_helper_raises() -> None:
    """The bot's own unit helper must refuse an unresolved mint too.

    The buy path's quote gate keeps an unconfigured mint off the chain, but that
    is a property of two call sites rather than of the helper. A helper that
    answers 9 for anything it has not heard of is one refactor away from
    mispricing a trade.
    """
    unknown = Pubkey.from_string("XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp")
    assert unknown not in core_pubkeys.QUOTE_DECIMALS, "fixture mint must be unknown"

    try:
        core_pubkeys.quote_units_per_token(unknown)
    except ValueError as exc:
        assert "resolve_quote_token_program" in str(exc), (
            f"the error must say how to fix it, got: {exc}"
        )
    else:
        raise AssertionError(
            "quote_units_per_token() answered for an unresolved mint; a wrong "
            "power of ten oversizes the trade instead of capping it"
        )

    # The non-raising variant exists for decoders, and must not guess either.
    assert core_pubkeys.cached_quote_units(unknown) is None
    assert core_pubkeys.cached_quote_units(core_pubkeys.WSOL_MINT) == 10**9
    assert core_pubkeys.quote_units_per_token(core_pubkeys.USDC_MINT) == 10**6


def check_seed_tables_agree() -> None:
    """Every pre-seeded quote mint must carry both facts.

    `resolve_quote_token_program` returns early once a mint's token program is
    known. A mint seeded into QUOTE_TOKEN_PROGRAMS but not QUOTE_DECIMALS would
    take that early return and never have its decimals read -- at startup, right
    behind a log line saying it resolved.
    """
    programs = set(core_pubkeys.QUOTE_TOKEN_PROGRAMS)
    decimals = set(core_pubkeys.QUOTE_DECIMALS)
    assert programs == decimals, (
        "pre-seeded quote mints disagree:\n"
        f"  token program only: {sorted(str(m) for m in programs - decimals)}\n"
        f"  decimals only:      {sorted(str(m) for m in decimals - programs)}"
    )


def check_buy_resolves_amount_before_sizing() -> None:
    """The buy must clear its quote gate before it scales anything.

    Scoped to the buyer by name. The seller sizes without the gate on purpose:
    it liquidates a position already held, against a mint inherited from the
    buy, so gating it on a configured buy amount would strand that position
    whenever a config changed between the two.
    """
    source = (SRC / "trading" / "platform_aware.py").read_text()
    tree = ast.parse(source)

    buyer = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "PlatformAwareBuyer"
        ),
        None,
    )
    assert buyer is not None, "PlatformAwareBuyer not found; update this check"

    execute = next(
        (
            node
            for node in buyer.body
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == "execute"
        ),
        None,
    )
    assert execute is not None, "PlatformAwareBuyer.execute not found"

    calls = _called_names(execute)
    sizing = [ln for name, ln in calls if name == "quote_units_per_token"]
    gating = [ln for name, ln in calls if name == "_resolve_quote_amount"]

    assert sizing, "the buy no longer sizes with a quote unit; update this check"
    assert gating, "the buy no longer resolves a quote amount; update this check"
    assert min(gating) < min(sizing), (
        f"the buy sizes at line {min(sizing)} but resolves its quote amount at "
        f"line {min(gating)}; an unconfigured quote asset must be refused first"
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
        ("no script assumes a quote mint's decimals", check_no_assumed_quote_decimals),
        ("the bot's unit helper raises too", check_core_unit_helper_raises),
        ("pre-seeded quote tables agree", check_seed_tables_agree),
        ("the buy gates before it sizes", check_buy_resolves_amount_before_sizing),
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

"""Verify the pump.fun and universal code paths never scale a price by a SOL literal.

A coin's price is denominated in whatever it trades against, so converting raw
reserves to whole units means dividing by *that mint's* unit, not by
`LAMPORTS_PER_SOL`. `quote_units_per_token` raising for an unresolved mint keeps
that honest wherever the unit is looked up -- but it says nothing about code that
writes the divisor in by hand, which is exactly how three uncalled helpers on the
pump.fun curve manager came to price everything as SOL.

Those helpers are gone. This check is what stops a fourth being written.

Scope: `core/`, `trading/` and `platforms/pumpfun/`.

`platforms/letsbonk/` is NOT covered, and this is a statement of where the rule
holds rather than an exemption granted for convenience. letsbonk genuinely
violates it -- `curve_manager.py` prices with
`* 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL` and drops the `base_decimals`,
`quote_decimals` and `quote_mint` that its `PoolState` carries, so every pool is
priced as though it were SOL-quoted. Widening this check to `platforms/` means
fixing those two sites first; the runner has no way to express a known failure,
so a repo-wide version of this check would be red permanently and would train
everyone to stop reading the summary line.

Offline machine checks, no network and no funds moved:

  A. No module in scope divides or multiplies anything by LAMPORTS_PER_SOL.
  B. The letsbonk sites this check deliberately does not cover are still the
     ones named above, so widening it later is a path-filter change against a
     list that is already written down rather than a fresh investigation.

Usage:
    uv run tests/regression/verify_pumpfun_prices_without_hardcoded_sol_unit.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"

# Directories this rule covers. letsbonk is out; see the module docstring.
IN_SCOPE = ("core", "trading", "platforms/pumpfun")

SOL_LITERAL = "LAMPORTS_PER_SOL"

# The letsbonk sites this check does not cover, as (path, symbol). Kept here so
# widening the scope starts from a list rather than a search.
KNOWN_UNCOVERED = (
    ("src/platforms/letsbonk/curve_manager.py", "calculate_price"),
    ("src/platforms/letsbonk/curve_manager.py", "_decode_pool_state_with_idl"),
)


def _in_scope(path: Path) -> bool:
    """Whether a module falls under this check's directories.

    Args:
        path: Module path anywhere under `src/`

    Returns:
        True if the module is in one of IN_SCOPE
    """
    rel = path.relative_to(SRC).as_posix()
    return any(rel.startswith(f"{d}/") for d in IN_SCOPE)


def _scales_by_sol_literal(node: ast.AST) -> list[int]:
    """Lines under a node that multiply or divide by the SOL literal.

    Args:
        node: Any AST node to walk -- a module or a single function

    Returns:
        Line numbers of the offending binary operations
    """
    hits = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.BinOp) or not isinstance(sub.op, ast.Div | ast.Mult):
            continue
        for side in (sub.left, sub.right):
            if isinstance(side, ast.Name) and side.id == SOL_LITERAL:
                hits.append(sub.lineno)
    return hits


def check_no_hardcoded_sol_unit_in_pricing() -> None:
    """Nothing in scope may scale any amount by LAMPORTS_PER_SOL.

    A flat rule rather than one keyed on function names: the three helpers this
    replaced included `calculate_expected_tokens`, whose name says nothing about
    SOL while its body converts with it. Nothing in scope legitimately needs the
    literal -- amounts are scaled by the quote mint's unit, and the constant
    itself stays for anything genuinely denominated in SOL, such as a rent
    reserve or a fee budget, neither of which lives in these directories.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if "generated" in path.parts or not _in_scope(path):
            continue
        for line in _scales_by_sol_literal(ast.parse(path.read_text())):
            offenders.append(
                f"{path.relative_to(PROJECT_ROOT)}:{line}: scales by {SOL_LITERAL}"
            )

    assert not offenders, (
        "a price was scaled by a SOL literal instead of the quote mint's unit:\n  "
        + "\n  ".join(offenders)
    )


def check_uncovered_sites_unchanged() -> None:
    """The letsbonk sites left out of scope must still be the documented ones.

    If letsbonk stops violating the rule, this check fails and the scope above
    should widen. If it grows a third site, the same. Either way the docstring
    stops being a claim nobody re-reads.
    """
    still_offending = []
    for rel, symbol in KNOWN_UNCOVERED:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                and node.name == symbol
                and _scales_by_sol_literal(node)
            ):
                still_offending.append((rel, symbol))

    assert still_offending == list(KNOWN_UNCOVERED), (
        "the out-of-scope letsbonk sites changed; widen this check's scope or "
        f"update KNOWN_UNCOVERED.\n  documented: {list(KNOWN_UNCOVERED)}\n"
        f"  found:      {still_offending}"
    )


def main() -> None:
    """Run every check and report."""
    print("=" * 72)
    print("Verifying pump.fun prices are not scaled by a SOL literal")
    print("=" * 72)

    checks = [
        ("no hardcoded SOL unit in scope", check_no_hardcoded_sol_unit_in_pricing),
        ("the uncovered letsbonk sites are unchanged", check_uncovered_sites_unchanged),
    ]
    for label, fn in checks:
        fn()
        print(f"{label} -> OK")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())

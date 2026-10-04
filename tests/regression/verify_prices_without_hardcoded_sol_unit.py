"""Verify no trading or platform code scales a price by a SOL literal.

A coin's price is denominated in whatever it trades against, so converting raw
reserves to whole units means dividing by *that mint's* unit, not by
`LAMPORTS_PER_SOL`. `quote_units_per_token` raising for an unresolved mint keeps
that honest wherever the unit is looked up -- but it says nothing about code that
writes the divisor in by hand, which is how three helpers on the pump.fun curve
manager, and the LaunchLab curve manager after them, came to price everything
as SOL.

Scope: `core/`, `trading/` and every platform under `platforms/`.

Offline machine check, no network and no funds moved:

  A. No module in scope divides or multiplies anything by LAMPORTS_PER_SOL.

Usage:
    uv run tests/regression/verify_prices_without_hardcoded_sol_unit.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"

IN_SCOPE = ("core", "trading", "platforms")

SOL_LITERAL = "LAMPORTS_PER_SOL"


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

    A flat rule rather than one keyed on function names: one of the helpers
    this replaced was `calculate_expected_tokens`, whose name says nothing about
    SOL while its body converts with it. Amounts are scaled by the quote mint's
    unit; the constant stays for anything genuinely denominated in SOL, such as
    a rent reserve or a fee budget, neither of which lives in these directories.
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


def main() -> None:
    """Run the check and report."""
    print("=" * 72)
    print("Verifying prices are not scaled by a SOL literal")
    print("=" * 72)

    check_no_hardcoded_sol_unit_in_pricing()
    print("no hardcoded SOL unit in scope -> OK")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())

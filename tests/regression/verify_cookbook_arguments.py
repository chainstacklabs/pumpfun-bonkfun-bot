"""Verify every cookbook script takes its input on the command line.

The cookbook is a reference collection: someone reads a script, then runs it
against their own coin. That only works if the coin is an argument. Scripts used
to carry it as a module-level constant instead:

    TOKEN_MINT = Pubkey.from_string(sys.argv[1] if len(sys.argv) > 1 else "...")

which reads as "optional", but `Pubkey.from_string("...")` raises at import, so
running the script the way its own usage line describes it — with no argument —
died on `ValueError: Invalid Base58 string` before printing anything. Nine
scripts did this. Others took their amount and slippage from environment
variables, which no usage line mentioned at all.

The rule is that a value the caller would vary per run is an argument. A default
is fine, and encouraged; a default that is not a real value is not a default.

Offline machine checks, no network and no funds moved:

  A. No runnable script reads `sys.argv` at module level.
  B. No script contains a placeholder standing in for a real address.
  C. Every script that takes input builds an `ArgumentParser`, so `--help` works
     and a bad address is rejected with a usage message rather than a traceback.
  D. Every `ArgumentParser` default is a usable value, not a placeholder.

Usage:
    uv run tests/regression/verify_cookbook_arguments.py
"""

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COOKBOOK = PROJECT_ROOT / "cookbook"

# A value that looks like configuration but cannot be used as one.
PLACEHOLDER = re.compile(r'"(\.\.\.|YOUR_[A-Z_]+|<[A-Z_]+>)"')

# Scripts that genuinely take no input: they subscribe to a program and print
# what arrives. Nothing to parameterise, so nothing to get wrong.
NO_INPUT = {
    "pumpfun_listen_tokens_logsubscribe.py",
    "pumpfun_listen_tokens_blocksubscribe.py",
    "pumpfun_listen_tokens_geyser.py",
    "pumpfun_listen_tokens_deshred.py",
    "pumpfun_listen_tokens_pumpportal.py",
    "pumpfun_listen_migrations_logsubscribe.py",
    "pumpfun_listen_migrations_programsubscribe.py",
    "pumpfun_capture_transactions_blocksubscribe.py",
}


def runnable_scripts() -> list[Path]:
    """Every cookbook script with a `__main__` guard.

    Returns:
        The runnable scripts, sorted. Import-only helpers are excluded.
    """
    return sorted(
        p for p in COOKBOOK.rglob("*.py") if '__name__ == "__main__"' in p.read_text()
    )


def check_no_module_level_argv() -> None:
    """Input must be read inside a function, not while the module imports."""
    offenders = []
    for path in runnable_scripts():
        tree = ast.parse(path.read_text())
        for node in tree.body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "argv":
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{sub.lineno}")
    assert not offenders, (
        "sys.argv read at module level, so the script fails before main() runs:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def check_no_placeholders() -> None:
    """A placeholder is not a default; it is a crash with extra steps."""
    offenders = []
    for path in runnable_scripts():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if PLACEHOLDER.search(line) and not line.lstrip().startswith("#"):
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()[:70]}"
                )
    assert not offenders, "placeholder values standing in for real input:\n  " + (
        "\n  ".join(offenders)
    )


def check_scripts_use_argparse() -> None:
    """Anything taking input parses it, so --help and errors work."""
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in runnable_scripts()
        if path.name not in NO_INPUT and "ArgumentParser" not in path.read_text()
    ]
    assert not offenders, "scripts taking input without argparse:\n  " + "\n  ".join(
        offenders
    )


def check_defaults_are_usable() -> None:
    """Every argparse default must be a value the script can actually run with."""
    offenders = []
    for path in runnable_scripts():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg != "default":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value, str
                ):
                    if PLACEHOLDER.search(f'"{keyword.value.value}"'):
                        offenders.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                            f"default={keyword.value.value!r}"
                        )
    assert not offenders, "argparse defaults that are not usable values:\n  " + (
        "\n  ".join(offenders)
    )


def main() -> None:
    """Run every check and report."""
    print("=" * 72)
    print("Verifying cookbook scripts take their input as arguments")
    print("=" * 72)

    scripts = runnable_scripts()
    print(f"{len(scripts)} runnable scripts, {len(NO_INPUT)} of them taking no input\n")

    for label, check in [
        ("no module-level sys.argv", check_no_module_level_argv),
        ("no placeholder values", check_no_placeholders),
        ("input is parsed with argparse", check_scripts_use_argparse),
        ("every default is usable", check_defaults_are_usable),
    ]:
        check()
        print(f"{label} -> OK")

    missing = sorted(NO_INPUT - {p.name for p in scripts})
    if missing:
        print(f"\nNote: exempt scripts no longer present: {', '.join(missing)}")
        sys.exit(1)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()

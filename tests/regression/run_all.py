"""Run every offline regression verifier and report which ones failed.

Usage:
    uv run tests/regression/run_all.py
    uv run tests/regression/run_all.py verify_v2_account_layout verify_rpc_deadline

Each `verify_*.py` beside this file guards one fixed bug: its docstring names the
bug and the checks it runs. They are offline and move no funds, so running the
whole set is cheap and is the thing to do after a pump.fun program upgrade or
any change to `src/`. A verifier exits non-zero when its check fails.

Pass script names to run a subset. The `--live` modes some verifiers offer are
not exercised here; run those individually.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]


def discover(names: list[str]) -> list[Path]:
    """Return the verifier scripts to run.

    Args:
        names: Script names to run, without the `.py`. Empty means all of them.

    Returns:
        The matching script paths, sorted.
    """
    if not names:
        return sorted(HERE.glob("verify_*.py"))
    return [HERE / f"{name.removesuffix('.py')}.py" for name in names]


def run(script: Path) -> bool:
    """Run one verifier and echo its output.

    Args:
        script: Path to the verifier.

    Returns:
        True if the verifier exited zero.
    """
    print(f"\n{'=' * 70}\n{script.name}\n{'=' * 70}")
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)], cwd=PROJECT_ROOT, check=False
    )
    return result.returncode == 0


def main() -> None:
    """Run the selected verifiers and exit non-zero if any failed."""
    scripts = discover(sys.argv[1:])
    missing = [s for s in scripts if not s.exists()]
    if missing:
        print("No such verifier: " + ", ".join(s.name for s in missing))
        sys.exit(2)

    failed = [s.name for s in scripts if not run(s)]

    print(f"\n{'=' * 70}")
    print(f"{len(scripts) - len(failed)}/{len(scripts)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()

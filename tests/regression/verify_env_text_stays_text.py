"""Verify a `${VAR}` setting becomes a bool only when it is an on/off setting.

Every `${VAR}` whose value read `true` or `false` became a bool, text settings
included: `match_string: ${MATCH}` with `MATCH=true` filtered on the bool True
instead of the word "true".

Offline machine checks, no network and no funds moved:

  A. A text setting set from the environment to `true`, `False` or `TRUE`
     keeps the text.
  B. Each on/off setting in `config_loader.SWITCHES`, set to `TRUE` or
     `false`, becomes the bool it spells; any other value stays text.
  C. Every setting a committed `bots/*.yaml` gives a bool value is listed in
     `SWITCHES`, so a new switch can't be left out of the list.

Usage:
    uv run tests/regression/verify_env_text_stays_text.py
"""

import os
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import _report  # noqa: E402

from config_loader import SWITCHES, resolve_env_vars  # noqa: E402

VAR = "VERIFY_ENV_TEXT"


def _resolve(path: str, value: str) -> object:
    """Resolve `${VAR}` at dotted `path` with VAR set to `value`."""
    config: dict = {}
    node = config
    *parents, leaf = path.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = f"${{{VAR}}}"
    os.environ[VAR] = value
    try:
        resolve_env_vars(config)
    finally:
        os.environ.pop(VAR, None)
    node = config
    for key in parents:
        node = node[key]
    return node[leaf]


def check_a_text_stays_text() -> bool:
    print("A: a text setting keeps `true` / `false` as text.")
    problems = []
    for path in ("filters.match_string", "filters.bro_address", "name"):
        for value in ("true", "False", "TRUE"):
            got = _resolve(path, value)
            if got != value:
                problems.append(f"{path}={value!r}: resolved to {got!r}")
    return _report(problems)


def check_b_switches_become_bools() -> bool:
    print("B: an on/off setting becomes the bool it spells.")
    problems = []
    for path in sorted(SWITCHES):
        for value, want in (("TRUE", True), ("false", False), (" True ", True)):
            got = _resolve(path, value)
            if got is not want:
                problems.append(f"{path}={value!r}: resolved to {got!r}")
        if _resolve(path, "yes") != "yes":
            problems.append(f"{path}='yes': not left as text")
    return _report(problems)


def _bool_paths(node: dict, prefix: str = "") -> set[str]:
    found = set()
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found |= _bool_paths(value, f"{path}.")
        elif isinstance(value, bool):
            found.add(path)
    return found


def check_c_committed_switches_listed() -> bool:
    print("C: every bool setting in bots/*.yaml is listed in SWITCHES.")
    problems = []
    configs = sorted((PROJECT_ROOT / "bots").glob("*.yaml"))
    if not configs:
        problems.append("no committed bots/*.yaml found")
    for config in configs:
        raw = yaml.safe_load(config.read_text())
        problems += [
            f"{config.name}: {path} is a bool but not in SWITCHES"
            for path in sorted(_bool_paths(raw) - SWITCHES)
        ]
    return _report(problems)


def main() -> int:
    checks = (
        check_a_text_stays_text,
        check_b_switches_become_bools,
        check_c_committed_switches_listed,
    )
    passed = 0
    for check in checks:
        ok = check()
        print("  PASS" if ok else "  FAIL")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

---
paths:
  - "tests/**"
---

[docs/regression-tests.md](../../docs/regression-tests.md) lists what each
verifier guards, so you can pick the ones your change reaches.

Every fix ships with an offline verifier here. None of them move funds.
`uv run tests/regression/run_all.py` runs the set.

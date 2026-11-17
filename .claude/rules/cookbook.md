---
paths:
  - "cookbook/**"
---

Read [docs/cookbook-conventions.md](../../docs/cookbook-conventions.md) before
adding or changing a script here.

One script, one action; it runs standalone with `uv run` and imports nothing
from `src/` but the generated geyser stubs; every input is a command-line
argument, never an edited constant; and a script that submits a transaction
carries the `WARNING:` line right after its docstring summary.

---
paths:
  - "cookbook/**"
---

Read [docs/cookbook-conventions.md](../../docs/cookbook-conventions.md) before
adding or changing a script here.

One script, one action; it runs standalone with `uv run` and imports nothing from
`src/`; every input is a command-line argument, never an edited constant; and a
script that spends real funds says so on the first line of its docstring.

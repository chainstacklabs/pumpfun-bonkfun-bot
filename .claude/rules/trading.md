---
paths:
  - "src/trading/**"
  - "src/cleanup/**"
---

Read [docs/trading-lifecycle.md](../../docs/trading-lifecycle.md) before changing
positions, the positions file, shutdown, cleanup or PnL.

Shutdown waits for every position task to end **before** cleanup runs, and a
buy that may have landed is never cleaned up as a failed buy.

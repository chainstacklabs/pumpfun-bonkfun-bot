---
paths:
  - "src/platforms/pumpfun/**"
  - "cookbook/pumpfun/**"
  - "cookbook/pumpswap/**"
  - "cookbook/legacy/**"
  - "idl/pump_*.json"
---

Read [docs/pumpfun-protocol.md](../../docs/pumpfun-protocol.md) before changing
account layouts, instruction arguments, fee recipients or quote-asset handling.

The IDL is not the reference for account counts: it under-reports PumpSwap's
`buy` and `sell`, `create_v2` (accounts 17-20) and `migrate_v2`; the doc lists
them, and the cookbook builder documents `buy_exact_sol_in`'s.

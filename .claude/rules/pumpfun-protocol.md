---
paths:
  - "src/platforms/pumpfun/**"
  - "src/platforms/letsbonk/**"
  - "cookbook/pumpfun/**"
  - "cookbook/pumpswap/**"
  - "cookbook/legacy/**"
  - "idl/**"
---

Read [docs/pumpfun-protocol.md](../../docs/pumpfun-protocol.md) before changing
account layouts, instruction arguments, fee recipients or quote-asset handling.

The IDL under-reports every pump-amm instruction and the pre-v2
`buy_exact_sol_in`, so it cannot be the reference for account counts — the doc
lists pump-amm's, and the cookbook builder documents `buy_exact_sol_in`'s.

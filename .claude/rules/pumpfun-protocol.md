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

The IDL under-reports the legacy `buy`/`sell` and every pump-amm instruction, so
it cannot be the reference for account counts — the doc lists the real ones.

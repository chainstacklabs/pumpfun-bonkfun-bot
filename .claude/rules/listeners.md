---
paths:
  - "src/monitoring/**"
  - "src/geyser/**"
  - "cookbook/pumpfun/listen/**"
  - "cookbook/pumpfun/graduation/**"
  - "cookbook/pumpfun/decode/**"
  - "tools/compare_*.py"
---

Read [docs/listeners-and-geyser.md](../../docs/listeners-and-geyser.md) before
changing a listener, the vendored geyser stubs or a decoder.

The two rules that break a listener silently: route on `meta.logMessages` rather
than the envelope decode, and break out of a `recv()` loop on
`websockets.ConnectionClosed` instead of swallowing it in a broad `except`.

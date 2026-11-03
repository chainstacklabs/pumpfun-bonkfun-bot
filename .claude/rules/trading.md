---
paths:
  - "src/trading/**"
  - "src/cleanup/**"
---

Each position's exit runs as its own task (`PositionManager.open`), up to
`trade.max_open_positions` (default 1); the trader takes the next coin only once
a slot is free. Shutdown stops every position task and waits for each to end
**before** cleanup: post-session cleanup burns and closes token accounts, so it
must never race a sell still in flight. `verify_concurrent_positions.py` guards
the order.

Each monitored position is in `positions/<bot name>.json` (`PositionStore`) from
its buy until its **sale**, and only a sale removes it: a position stopped at
shutdown, or whose exit gave up or could not be resolved, still holds its coin.
On start `PositionManager.resume` reads the wallet's balance before holding a
position again; a coin no longer held is cleared, never sold. A resumed coin is
marked processed in the intake, so it is never bought a second time.
`verify_positions_survive_restart.py` guards all three.

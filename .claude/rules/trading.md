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
the order. A position already sold and closing its account (`after_sell`) is not
stopped or counted as open: shutdown waits for the close, bounded by
`_STOP_BUDGET`, then cancels it with a warning. Post-session cleanup skips
every coin still in the positions file: those are kept for the next start
(`verify_cleanup_closes_every_account.py`).
Under `manual` or `marry_mode` it burns nothing and only closes empty accounts.

Each monitored position is in `positions/<bot name>.json` (`PositionStore`) from
the moment its buy **may have reached the chain** until its **sale**. The buyer's
`on_sent` writes it as `pending`, with the signature and the expected amount,
once the send returns, or when a cancellation lands mid-send (the signature
comes from `build_and_send_transaction`'s `on_signed`); a buy cancelled before
it was signed leaves nothing. A landed buy rewrites it; a `REVERTED` or
`SUBMIT_FAILED` buy removes it; any other failure keeps it pending and skips the
failed-buy cleanup, which could burn a coin the buy delivered. A pending record
is resumed at the balance the wallet holds and rewritten as landed, or cleared
by the same balance rule as below. Only a sale of all of it removes a position
(a sale the curve could pay for only in part leaves the rest in the file, still
monitored, and no cleanup runs): a position stopped at shutdown, or whose exit
gave up or could not be resolved, still holds its coin.
On start `PositionManager.resume` reads the wallet's balance before holding a
position again; a coin no longer held is cleared, never sold. A position
another wallet bought (each record names its wallet) is kept and skipped.
Clearing needs every one of its spaced balance reads to find nothing; a failed
read keeps the position, since a lagging RPC node may not show a fresh buy yet.
Every coin still in the file, resumed or not, is marked processed in the intake,
so it is never bought a second time.
`verify_positions_survive_restart.py` guards all three;
`verify_shutdown_keeps_trades_in_flight.py` guards the pending record and the
close of a sold position at shutdown.

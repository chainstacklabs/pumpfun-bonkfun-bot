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
every coin still in the positions file, pending ones included: those are kept
for the next start (`verify_cleanup_closes_every_account.py`). It then closes
every other empty token account the wallet owns (SPL Token and Token-2022),
never a quote mint's (`UniversalTrader._quote_mints`), and burns nothing in them.
Under `manual` or `marry_mode` it burns nothing and only closes empty accounts.
A position cleared because the wallet no longer holds its coin closes the empty
account under `after_sell` (`PositionManager._close_cleared`; `stop` waits for it
like a sold one) and is left to the sweep under `post_session`. Every close goes
out with the priority fee unless `cleanup.with_priority_fee` is explicitly false,
waits until its blockhash expires, and is sent again (at most
`AccountCleanupManager.CLOSE_ATTEMPTS`) only when the signature lookup shows it
did not land; each attempt re-reads the account first
(`verify_cleanup_closes_what_it_leaves.py`).

Each monitored position is in `positions/<bot name>.json` (`PositionStore`) from
the moment its buy **may have reached the chain** until its **sale**. The buyer's
`on_sent` writes it with a `pending` object (`PendingBuy`: the most it can
deliver, where its spend lands, an exact-in spend, the blockhash's last valid
block height) once the send returns, or once it was signed when a cancellation
or an ambiguous send error lands (`on_signed`); a buy cancelled before it was
signed leaves nothing. `TransactionNotSent` is the only send error that is
`SUBMIT_FAILED`. A confirmed buy rewrites the record; a `REVERTED` or
`SUBMIT_FAILED` buy removes it; any other failure keeps it and starts
`open_pending`, and no `UNCONFIRMED` buy is cleaned up as a failed buy, monitored
or not. `open_pending` (also what `resume` runs for a pending record) resolves by
signature (`getSignatureStatuses`; only the first lookup searches the whole
history; SUCCESS or REVERTED only at `confirmed` or above): landed → the
transaction's fill price and amounts, the amount capped by the balance held
(none: cleared, unreadable: kept pending), logged to trades.log; reverted →
cleared; unknown → looked at again until the blockhash expired, then the balance
decides, capped at `max_amount_raw`, price None. Those balance reads pass
`min_context_slot` (the slot the buy landed in, or the one its expiry was seen
at): a node behind it errors, which keeps the record, instead of answering "not
found". A position with price None reads a price above zero before any exit
runs, and keeps it in the store (`set_price`). Balance reads on resume are at
`confirmed`; the client's default is `finalized`, which can lag. Only a sale of all of it removes a position
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

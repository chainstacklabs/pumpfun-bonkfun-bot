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
the order. A sold position's `after_sell` close runs in its own task
(`_closes`, the tail of the sale's report task), so it holds no slot and is
not stopped or counted as open: `wait_all` waits for it, and shutdown waits
for it bounded by `_STOP_BUDGET`, then cancels it with a warning. Post-session
cleanup skips every coin still in the positions file, pending ones included:
those are kept for the next start (`verify_cleanup_closes_every_account.py`). It then closes
every other empty token account the wallet owns (SPL Token and Token-2022),
never a quote mint's (`UniversalTrader._quote_mints`), and burns nothing in them.
Under `manual` or `marry_mode` it burns nothing and only closes empty accounts.
A position cleared because the wallet no longer holds its coin closes the empty
account under `after_sell` (`PositionManager._clear`, also in `_closes`)
and is left to the sweep under `post_session`. Under `after_sell` a sold or
cleared position's record stays in the file marked `sold` until its close ends;
the next start closes the account of one still there, never selling it,
whatever its exit strategy. Every close goes out with the
priority fee unless `cleanup.with_priority_fee` is false (validated as a bool)
and a tight compute unit limit. A confirmation wait can end early (a rate limit,
a timeout), so a close is sent again only when `get_block_height_and_slot`
shows the height past its blockhash's last valid height **and** its signature
is not known to have landed: no node knows it, or the lookup failed. One seen
but unconfirmed, or not provably expired, is waited on; every wait, the
confirmation wait included, ends by `MAX_WAIT`, then the close is given up
without a resend, and a confirmation error is warned once per close. At most
`AccountCleanupManager.CLOSE_ATTEMPTS` sends in total; before each resend the
account is read at `confirmed` with `min_context_slot` set to the slot the
expiry was seen at, so a close that landed is never sent again
(`verify_cleanup_closes_what_it_leaves.py`). The first read is at `confirmed`
too: after a sale whose slot its read-back gave (`TradeDeltas.slot`), pinned to
that slot, asked again while the node is behind, instead of waiting
`SETTLE_SECONDS`; post-session pins to the session's latest sale
(`PositionManager.last_sale_slot`) and waits when any sale's slot is unknown.

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
monitored, and no cleanup runs, counting the part in `parts_sold` and its PnL in
`realised`, so a restart totals the whole position): a position stopped at shutdown, or whose exit
gave up or could not be resolved, still holds its coin. An exit that gives up
(`PositionManager._give_up`) marks the record `given_up` and the session lists
it; resuming clears the mark. `bot_runner` holds `<positions file>.lock`
(`trading/run_lock.py`) while a bot runs. `cleanup.burn_given_up_below` burns,
at the start of shutdown cleanup, only coins given up this session, SOL-quoted,
priced above zero and worth less than it (`verify_burn_given_up.py`).
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

Every landed buy and sale, a late-confirmed one included, is read back with
`SolanaClient.get_trade_deltas` (`core/trade_deltas.py`, one reader for both
sides; only a transaction the wallet paid for): the wallet's own coin, quote,
network fee and token-account rent. A failed read never fails a landed trade;
it makes the figures estimates, and the log says so. A buy's
`TradeResult.price` stays the venue's fill (protocol fees out), which take
profit and stop loss count from: the wallet's cost can carry one-off charges,
such as a fresh creator vault's rent, that would fire a stop loss at once. The
seller does not read its sale back: the manager settles a landed sale in the
store first, then reads it, writes its trades.log row and PnL line and, once
all of it is sold, runs the `after_sell` close, all in one task in `_closes`
(`_after_sale`), so the slot is free during the read, `stop` and `wait_all`
wait for it, and a failing report never undoes the settlement. PnL counts from
the cost basis (`trading/pnl.py`), per sale, the parts of a position in order,
with the whole position on the last line; rent is never a loss. The positions
file keeps the buy's deltas so a restart counts from the same cost
(`verify_pnl_from_chain.py`).

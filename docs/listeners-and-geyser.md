# Listeners, geyser and decoding

Read this before changing anything under `src/monitoring/`, `src/geyser/`
or `cookbook/pumpfun/listen/`.

## Geyser protos

Pinned to yellowstone **`v16.0.0-rc10+solana.4.3.0`** (commit `301e9cf`,
`yellowstone-grpc-proto` 13.0.0-rc4) — the build the endpoint reports from
`GetVersion`. Ask it before assuming, and pin to what it answers. Not `master`:
master has diverged and is *missing* `block_footer`, `bank_id` and `VATDebit`.
The committed stubs are protobuf gencode **7.35.1** against runtime 7.36.2.
Regenerating is not a plain `protoc` run — the committed files import each other
absolutely (`from geyser.generated.solana_storage_pb2 import *`), which the
compiler does not emit, so the output needs rewriting.

- **`Message.config` (field 7) is the only way to spot a transaction v1 over
  geyser.** There is no version number on the wire and `versioned` is true for v0
  and v1 alike. The field is set only for v1 and carries the inline budget
  (priority fee in **total lamports**, CU limit, loaded-accounts data size, heap)
  that v1 moved off the ComputeBudget instructions, so a v1 coin has no
  ComputeBudget instructions to read a budget from at all.
- **Total lamports is the whole model, not just the unit.** SIMD-0385 puts the
  fee at config mask bits `[0, 1]` as an 8-byte LE u64 and defines it as one
  absolute figure; v0's micro-lamports-per-compute-unit is gone. Converting
  between them needs the CU limit, so a fee read off a v1 frame is not
  comparable to a v0 one without it.
- **A v1 message must state its loaded-accounts data size limit.** Left unset
  the limit is zero rather than the network default, and the transaction is
  rejected for exceeding it. With `skip_preflight` that rejection never reaches
  the sender: the signature is returned and simply never lands, which reads as a
  dropped transaction rather than a rejected one.
- **solders' builder docstring contradicts this and is wrong.**
  `TransactionConfig.priority_fee` (solders 0.29.0, the current release) is
  documented as micro-lamports. It is not converted — the value is written
  verbatim into the u64 the spec defines as lamports, so believing the docstring
  overpays by the CU limit. Trust the SIMD and this note, not the docstring.
- Older protos **skip `config` as an unknown field rather than failing**, so a
  stale stub degrades in silence: frames still decode, only the budget goes
  missing. `verify_transaction_v1.py` pins the geyser route on a committed frame
  so a rollback fails loudly.
- `TransactionStatusMeta.cost_units` and `Reward.commission_bps` are new too.
- **`CommitmentLevel` lost members 3-6**; they moved to a separate `SlotStatus`
  enum. Nothing here sends anything but `PROCESSED`.
- A yellowstone build **older than 15.1.1 silently downgrades v1 to v0 on the
  wire**, which no local change recovers. Check `GetVersion` before debugging a
  v1 gap.

## SubscribeDeshred

A separate RPC delivering transactions **before execution**, as entries form from
shreds. It backs the **`shreds` listener**
(`monitoring/universal_shreds_listener.py`, pump.fun and LaunchLab,
`bots/bot-sniper-5-shreds.yaml`), off by default. Deshred arrives ahead of
`Subscribe` on most creates; `tools/compare_deshred_latency.py` measures the lead
per signature, and `tools/compare_listeners.py` runs it as a lane against the
other four to show which coins it never reports.

- There is no `TransactionStatusMeta`, so no `meta.log_messages` and no
  CreateEvent. The listener decodes the create instruction — the fallback route
  everywhere else, the only route here. It sets `state_from_event` itself so
  `extreme_fast_mode` submits with zero RPC: the curve account does not exist
  yet, so a refresh can only time out and skip the coin.
- Outcomes are unknown at detection: a create that goes on to revert is delivered
  exactly like one that lands.
- The same create can come again in another transaction. Every listener logs a
  coin once, in one INFO line naming the mint, written before the name and
  creator filters so a refused coin is still named. It hands a coin on once per
  mint (`BaseTokenListener._deliver`); that hand-off is counted after the
  filters, so an attempt they refused does not hide a later one. The parsers'
  own detail is at DEBUG.
- **A coin created through a router is invisible on it.** The create arrives as a
  CPI, and inner instructions are produced *by* execution, so a pre-execution
  stream never carries them — present on the stream and undetectable, not
  dropped. This inverts the CPI note under *Listener and decoder pitfalls*:
  **trades** are overwhelmingly inner instructions, **creates** overwhelmingly
  top-level.

That miss rate is the trade: a few percent of coins never seen, against a head
start on the rest. `geyser` stays the default; pick `shreds` deliberately.
`cookbook/pumpfun/listen/pumpfun_listen_tokens_deshred.py` demonstrates the raw
stream; `tests/regression/verify_shreds_listener.py` pins the listener on
pump.fun and `verify_launchlab_shreds.py` on LaunchLab.

## Create slot

Every listener sets `TokenInfo.creation_slot` from the frame the create came in:
`context.slot` (logs), `value.slot` (blocks), `transaction.slot` (geyser),
`deshred_transaction.slot` (shreds). `filters.age_from: chain` reads it, so
a listener that drops it makes every coin count from detection instead.

## Migrations

`listen_for_tokens` takes an optional `pool_callback`. With it set, every
listener also reports a curve's migration as a `PoolReady` (mint, pool, quote
mint, signature): pump.fun to PumpSwap, and LaunchLab to Raydium CPMM on
`blocks`, `geyser` and `shreds`. Without it nothing is parsed and the shreds
subscription is unchanged. The name and creator filters apply only to new
coins. `logs`, `blocks` and `geyser` report a migration that executed, once per
mint. `shreds` reports each migrate transaction before execution with
`pre_execution` set, so the pool may not exist yet and the call may be a no-op,
fail, or name a pool the coin does not use. The same mint can come more than
once, one report per attempt; read the pool before trading it. *Detecting a migration* in
`docs/pumpfun-protocol.md` and `docs/launchlab-protocol.md` has the sources.

`trade.entry: migration` is the consumer (`trading/intake.py`,
`trading/migration.py`). It reads the coin's curve, mint and metadata to apply
the filters, then waits up to `POOL_WAIT_SECONDS` for the pool to be readable.
The curve is read at `processed` and re-read for up to `POOL_WAIT_SECONDS`
until it shows as completed: a stream reports a migration before every node
serves its slot, and a LaunchLab migration can land in the slot of the buy that
completed the curve. A pre-execution report that leaves nothing to buy (curve
not complete, a quote the curve does not use, no pool in time) does not use the
mint up, so a later attempt is still bought; an executed one does, unless its
curve never showed as completed.

## Listener and decoder pitfalls

Each was a live bug in the cookbook scripts, invisible offline and only visible
against mainnet.

- **A `while True: recv()` loop must break out on `websockets.ConnectionClosed`.**
  Catching it in a broad `except Exception` that only logs makes the next
  `recv()` raise immediately, forever, and leaves the outer reconnect handler and
  its `sleep` unreachable. A narrow `except TimeoutError` or
  `except json.JSONDecodeError` is fine to swallow — those are per-message.
- **Never gate a listener's dispatch on decoding the transaction envelope.** The
  envelope is the one part of a transaction whose format changes under you, and
  the installed solders is only ever one version behind. Route on
  `meta.logMessages`, which the RPC has already decoded and which is
  version-agnostic. A parser may read the envelope once the logs have routed a
  transaction to it: pump.fun's when the logs carry no CreateEvent, LaunchLab's
  for every launch, since its logs never name the coin.
- **Match a create marker as a whole log line, not a substring.** Anchor writes
  `Program log: Instruction: <Name>` for every program, so
  `"Program log: Instruction: Create" in log` also accepts CreateTokenAccount,
  CreateFeeSharingConfig, CreatePool and CreateConsume from whichever programs
  share the transaction. pump.fun writes exactly
  `Program log: Instruction: Create` or `... CreateV2`. (The associated
  token account program writes `Program log: Create`, with no `Instruction: `
  prefix, so it never matched either way.)
- **Resolve v0 lookup-table accounts before indexing them.** An instruction's
  account indices can point past `message.account_keys` into the address lookup
  table, which geyser reports in `meta.loaded_writable_addresses` then
  `loaded_readonly_addresses` (that order). Ignoring them raises `IndexError`.
- **Identify an instruction by its 8-byte discriminator, never by account count.**
  Several pump.fun instructions share a count, so counting mislabels them and
  prints every account under the wrong name. `buy_exact_sol_in` is 18 accounts on
  chain, same as legacy `buy`.
- **Walk `meta.innerInstructions`, not just `message.instructions`.** Most trades
  reach the program as a CPI from a router or aggregator, so top-level pump
  instructions are heavily outnumbered by inner ones. Anchor's event-CPI prefix
  (`e445a52e51cb9a1d`) accounts for a good share of them; the event's own
  discriminator follows it.
- **`getProgramAccounts` over the whole pump program is rejected** by current
  providers: *"Too many accounts requested (10000001 pubkeys) … use
  getProgramAccountsV2 with pagination"*. It still works against pump-amm, which
  is small enough. Don't take that message as a fix: `getProgramAccountsV2` is a
  provider extension (Helius, Solana Tracker), **not core Agave**, and its
  `limit` is a *scan* budget rather than a result count — a page can legally
  return zero accounts and a non-null `paginationKey`, so one filtered answer
  over the pump program costs ~1000 sequential pages. Reach for a filtered
  subscription instead; see the two
  `cookbook/pumpfun/graduation/pumpfun_watch_graduating_*.py` examples.
- **Filtered `programSubscribe` on the pump program is the portable way to find
  curves by state.** `dataSize` + `memcmp` are applied server-side and accepted
  even by the public `api.mainnet-beta.solana.com`. `memcmp` matches exact bytes
  only, so it cannot express "reserves below X" — only a handful of fixed
  cutoffs. Treat it as a bandwidth saver and make the real comparison
  client-side. Geyser's account filters have the same shape and add the slot and
  signature.
- **Resolve a curve's mint under Token-2022, not SPL Token.** The curve account
  has no mint field and `["bonding-curve", mint]` is not reversible, so the mint
  comes from the associated bonding curve ATA — Token-2022 for every `create_v2`
  coin. `get_token_accounts_by_owner` with the SPL Token program returns an empty
  list for all of them, silently.
- **`SetLoadedAccountsDataSizeLimit` must stay generous: 16 MB, not 512 KB.** On
  a Token-2022 mint with extensions, 512 KB and 4 MB both fail
  `MaxLoadedAccountsDataSizeExceeded` with `unitsConsumed=0` (never executed),
  while 16 MB reaches the buy instruction and is still 4x under the 64 MB
  default. solders has no builder for it; encode `struct.pack("<BI", 4, n)`
  against the compute-budget program.

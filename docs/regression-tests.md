# Regression verifiers

## The verifiers

Every fix ships with an offline verifier under `tests/regression/`. Each script's
docstring carries the bug it guards and the checks it runs — read that before
touching the code it covers, and run the ones your change reaches. None move
funds. `uv run tests/regression/run_all.py` runs the whole set, or name
individual scripts for a subset.

| Script | Checks |
|---|---|
| `verify_v2_account_layout.py` | buy_v2/sell_v2 account layouts, PDA/ATA derivations, encoding — against `idl/pump_fun_idl.json` |
| `verify_curve_account_sizes.py` | 125/151/256-byte curves all decode, and nothing filters on account length |
| `verify_create_v2_optional_args.py` | omitted trailing option-typed `create_v2` args decode as unset; mandatory args still fail |
| `verify_create_v2_launch_args.py` | the launch path derives a holder-reward coin's curve creator, reaches all four trailing-arg wire forms, and sizes the opening buy from `Global` |
| `verify_atomic_launch_v1.py` | the launch is one v1 transaction holding create and buy, fits 4096 bytes where v0 could not hold it, sends no ComputeBudget instructions, and states the priority fee as a lamport total |
| `verify_quote_asset_selection.py` | both quote registries are consulted, each admitted mint prices from its own opening reserve, an unlisted mint is refused, and a paused mint is rejected up front |
| `verify_transaction_v1.py` | every reader asks `maxSupportedTransactionVersion: 1`; a v1 `create_v2` is detected from logs alone with the envelope unreadable, and from the envelope alone with the logs stripped; the same two routes over geyser, plus the inline v1 budget |
| `verify_shreds_listener.py` | pre-execution creates decode from the instruction alone: `user` at `create_v2` account 5, a holder-reward creator derived as `PDA(["holder-rewards", mint])`, truncated trailing args decoding as not-holder-reward, lookup-table accounts resolved, and nothing reading a `meta` the stream has no field for |
| `verify_block_null_guard.py` | a `blockSubscribe` frame with `value.block: null` is skipped, not logged as an error |
| `verify_logs_listener_skips_failed_tx.py` | a `logsNotification` carrying an `err` yields no coin, so a create whose transaction reverted is not bought; a successful one is still detected, and geyser still excludes failed transactions server-side |
| `verify_creator_migration_from_logs.py` | a fee-sharing coin's `creator` comes from `MigrateBondingCurveCreatorEvent` in the same logs, not from `CreateEvent.creator`, so `creator_vault` matches the curve and the buy does not revert `ConstraintSeeds`; ordinary creates unchanged, `state_from_event` kept, and neither event is honoured unless pump.fun's own invocation emitted it |
| `verify_ping_loop_close_is_quiet.py` | a WebSocket closing normally ends the ping loop quietly in every listener, while an unexpected failure is still logged |
| `verify_listener_cancellation.py` | a cancelled WebSocket listener stops, even when `websockets` reports cancellation as `AssertionError` |
| `verify_create_log_gate.py` | the create marker is matched as a whole log line, so a foreign `Create*` instruction is not a coin and a real create sharing its transaction with one is still detected |
| `verify_extreme_fast_zero_rpc.py` | zero RPC calls between detection and submission for CreateEvent-sourced tokens |
| `verify_curve_refresh_buy_path.py` | a TokenInfo without `state_from_event` refreshes first: an unreadable curve skips the buy, and curve + mint are read in one slot-consistent batch that corrects the token program |
| `verify_cleanup_survives_shutdown.py` | shutdown cleanup runs to completion through the cancellation that triggered it, bounded, and a cleanup that hangs or raises still lets the process exit |
| `verify_token_queue_shutdown.py` | cancelling the token queue processor ends it without raising, and `task_done()` stays balanced on the paths that took an item |
| `verify_pumpfun_buy_token_amount.py` | a pump.fun `buy_v2` asks for the whole sized token count with slippage on the cost ceiling, in extreme_fast_mode and on the regular path |
| `verify_exact_token_amounts.py` | a position sells the raw token count its buy read, not a float rebuilt from it, including when the buy falls back to the wallet balance; an exact-in buy spends the configured amount to the raw unit |
| `verify_buy_result_not_lost.py` | a landed buy is never reported failed, and a reverted one never reported landed |
| `verify_tx_status_checks.py` | every path reads `meta.err`; `--live` replays known reverted signatures |
| `verify_tp_sl_exit_price.py` | the tp/sl exit prices off the trigger price, and a reverted sell is retried, bounded |
| `verify_time_based_exit_retry.py` | the default `time_based` exit retries a reverted sell instead of stranding the position |
| `verify_time_exit_without_price.py` | `max_hold_time` still fires when every price read fails |
| `verify_exit_sell_confirmation.py` | an exit sell is retried only when retrying is provably safe |
| `verify_rpc_deadline.py` | `post_rpc` bounds wall time, not just attempts (virtual clock) |
| `verify_quote_decimals_resolved.py` | no trade path prices a coin before resolving its quote mint's decimals; nothing in `cookbook/` or `src/` falls back to a literal decimal count; both unit helpers raise for an unresolved mint; the two pre-seeded quote tables carry the same mints; the buy clears its quote gate before it sizes |
| `verify_pumpfun_migration_detection.py` | each listener's real loop reports a real pump.fun migration with the mint, pool and quote mint of pump.fun's own `CompletePumpAmmMigrationEvent` (`migrate_v2` with SOL, USDC and other quotes, legacy `migrate`, v0 and v1); `logs`, `blocks` and `geyser` report it once per mint and nothing for a no-op migrate, a failed or reverted transaction, a `CreatePoolEvent` another program emitted or one naming a non-canonical pool; `shreds` reports each migrate transaction pre-execution, ignores one naming a non-canonical pool, lets no earlier failing or wrong-quote attempt hide the real one, and subscribes on the withdraw authority only when asked; without a pool callback nothing is parsed; a raising callback does not stop the listener |
| `verify_migration_event_discriminator.py` | the migration decoder rejects any payload that is not the wrapper program's `CreatePoolEvent`, including a foreign one long enough for the schema |
| `verify_cookbook_arguments.py` | every cookbook script takes its input as a command-line argument |
| `verify_documentation_links.py` | no known-dead URL is back; `--live` fetches every one and fails on 4xx/5xx |
| `verify_no_rpc_credentials_logged.py` | credentials masked in every log record, including a URL passed as a non-`str` argument, and every site that installs a root handler installs the redaction first |
| `verify_prices_without_hardcoded_sol_unit.py` | nothing in `core/`, `trading/` or `platforms/` scales an amount by `LAMPORTS_PER_SOL` |
| `verify_sell_after_graduation.py` | a graduated pump.fun curve and a migrated LaunchLab pool report `graduated` and refuse a price; the PumpSwap and CPMM pools derive from the mint; each market's sell matches a real sell on chain; prices come from the pools; the seller and the trader's price read switch to the market, and a pool not created yet sends nothing |
| `verify_pumpswap_buy.py` | a PumpSwap buy names the accounts of real buys into SOL, USDC and mayhem pools, is `buy_exact_quote_in` with `track_volume`, adds the right cashback accounts, wraps exactly the spend, and fits one transaction on a cashback pool too; the buyer buys a graduated coin in its pool sized off the pool, still buys a live curve on the curve, sends nothing for a pool not created yet, and `extreme_fast_mode` skips a graduated coin |
| `verify_cpmm_buy.py` | a CPMM buy names the accounts of real buys into migrated LaunchLab pools (SOL with SPL Token and Token-2022 coins, USDC, an xStock-paired transfer-fee coin), is `swap_base_input` with the sides of the sell swapped, wraps exactly the spend, creates the coin's account under its own program, and fits one transaction; the buyer buys a graduated coin in its pool with a floor net of the pool fee and the coin's transfer fee, still buys a live curve on the curve, sends nothing for a pool not created yet or a coin on AMM v4, and `extreme_fast_mode` skips a graduated coin |
| `verify_pumpswap_account_layout.py` | pump-amm's `pool-v2` account is gated on `coin_creator`, the buyback pair stays last, and base-token decimals are resolved rather than assumed; `--live` re-reads the authorized recipients from `GlobalConfig` |
| `verify_launchlab_trades.py` | against real StonkFun trades, the LaunchLab curve math reproduces each buy and sell to the unit — fee rate read from the configs, transfer fee from the mint; price comes from virtual plus real reserves; the swap names the on-chain 18 accounts with fee vaults keyed by the pool's quote mint; an exact-in buy gets the configured amount unpadded; buy and sell floors are net of curve fee and transfer fee; single-token mode waits past a coin its filters refuse instead of exiting on it |
| `verify_launchlab_launch_parsing.py` | legacy, v0 and v1 StonkFun launches parse from every account slot over blockSubscribe and geyser alike, lookup-table accounts included; the creator slot wins over the payer; a router-issued launch is found; letsbonk and StonkFun each keep to their own platform configs; LaunchLab listeners subscribe on platform configs, pump.fun on its program |
| `verify_generic_launchlab.py` | `platform: launchlab` decodes real launches from other launchpads over blocks and geyser and records their platform config; presets ignore them; `launchlab.platform_configs` narrows the parser and the block and geyser subscriptions (program when unset), is refused outside LaunchLab or outside a preset's configs, and gets its own registry entry; a non-constant-product launch is ignored; the trade log names the config |
| `verify_launchlab_shreds.py` | the shreds listener decodes real letsbonk, StonkFun and other-launchpad launches pre-execution to the TokenInfo the geyser listener builds, trusted for `extreme_fast_mode`; a listener for every platform routes a launch to the platform geyser picks; `launchlab.platform_configs` narrows the shreds subscription and decode; a router-created launch is invisible on shreds; the LaunchLab platforms validate with `shreds`, not `logs` |
| `verify_cleanup_harvests_withheld_fees.py` | cleanup harvests withheld Token-2022 transfer fees into the mint before closing a token account, and sends a bare close when nothing is withheld |
| `verify_behaviour_snapshots.py` | every buy and sell the bot builds, the parser output for each `raw_*.json` fixture, and `Position.should_exit` over a recorded price series match the JSON snapshots in `tests/regression/snapshots/`; `--update` rewrites them, only for a change meant to alter that behaviour |
| `verify_trader_settings_wiring.py` | each bot config key lands in its own `TraderSettings` field with the runner's default when left out, `UniversalTrader` hands each setting to the part that acts on it, `max_exit_sell_attempts` below 1 is clamped, and the settings repr hides endpoints and keys |

Two mainnet simulations, also no funds moved:

```bash
uv run tools/simulate_v2_trades.py <MINT>   # buy_v2/sell_v2 for one coin, reports CU
uv run tools/simulate_bot_buy_path.py       # the bot's whole buy path, fresh coin
```

After any pump.fun program upgrade run `verify_v2_account_layout`,
`verify_curve_account_sizes` and both simulations, then retune
`get_buy_compute_unit_limit` / `get_sell_compute_unit_limit` in
`platforms/pumpfun/instruction_builder.py` from the reported `unitsConsumed`.

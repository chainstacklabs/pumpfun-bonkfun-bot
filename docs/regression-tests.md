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
| `verify_transaction_v1.py` | every reader asks `maxSupportedTransactionVersion: 1`; a v1 `create_v2` is detected from logs alone with the envelope unreadable, and from the envelope alone with the logs stripped; the same two routes over geyser, plus the inline v1 budget |
| `verify_shreds_listener.py` | pre-execution creates decode from the instruction alone: `user` at `create_v2` account 5, a holder-reward creator derived as `PDA(["holder-rewards", mint])`, truncated trailing args decoding as not-holder-reward, lookup-table accounts resolved, and nothing reading a `meta` the stream has no field for |
| `verify_block_null_guard.py` | a `blockSubscribe` frame with `value.block: null` is skipped, not logged as an error |
| `verify_listener_cancellation.py` | a cancelled WebSocket listener stops, even when `websockets` reports cancellation as `AssertionError` |
| `verify_pumpportal_buy_path.py` | curve derived from the mint, unreadable curve skips the buy, curve+mint read in one slot-consistent batch |
| `verify_pumpportal_bonk_fields.py` | bonk payloads (no name/symbol/uri) still produce a `TokenInfo`; `--live` re-checks the real feed |
| `verify_extreme_fast_zero_rpc.py` | zero RPC calls between detection and submission for CreateEvent-sourced tokens |
| `verify_buy_result_not_lost.py` | a landed buy is never reported failed, and a reverted one never reported landed |
| `verify_tx_status_checks.py` | every path reads `meta.err`; `--live` replays known reverted signatures |
| `verify_tp_sl_exit_price.py` | the tp/sl exit prices off the trigger price, and a reverted sell is retried, bounded |
| `verify_time_based_exit_retry.py` | the default `time_based` exit retries a reverted sell instead of stranding the position |
| `verify_time_exit_without_price.py` | `max_hold_time` still fires when every price read fails |
| `verify_exit_sell_confirmation.py` | an exit sell is retried only when retrying is provably safe |
| `verify_rpc_deadline.py` | `post_rpc` bounds wall time, not just attempts (virtual clock) |
| `verify_quote_decimals_resolved.py` | no trade path prices a coin before resolving its quote mint's decimals, and no script anywhere falls back to a literal decimal count |
| `verify_cookbook_arguments.py` | every cookbook script takes its input as a command-line argument |
| `verify_documentation_links.py` | no known-dead URL is back; `--live` fetches every one and fails on 4xx/5xx |
| `verify_no_rpc_credentials_logged.py` | credentials masked in every log record, including a URL passed as a non-`str` argument, and every site that installs a root handler installs the redaction first |
| `verify_pumpswap_account_layout.py` | pump-amm's `pool-v2` account is gated on `coin_creator`, the buyback pair stays last, and base-token decimals are resolved rather than assumed; `--live` re-reads the authorized recipients from `GlobalConfig` |

Two mainnet simulations, also no funds moved:

```bash
uv run tools/simulate_v2_trades.py <MINT>   # buy_v2/sell_v2 for one coin, reports CU
uv run tools/simulate_bot_buy_path.py       # the bot's whole buy path, fresh coin
```

After any pump.fun program upgrade run `verify_v2_account_layout`,
`verify_curve_account_sizes` and both simulations, then retune
`get_buy_compute_unit_limit` / `get_sell_compute_unit_limit` in
`platforms/pumpfun/instruction_builder.py` from the reported `unitsConsumed`.

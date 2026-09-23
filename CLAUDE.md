# Agent guide

Solana trading bot for pump.fun and letsbonk.fun. Snipes newly created tokens and exits on a configured strategy. See [README.md](README.md) for setup and configuration; this file covers what an agent needs that the code doesn't make obvious.

`AGENTS.md` is a symlink to this file, so Claude Code, Codex, Cursor, and Windsurf all read the same guide.

## Ground rules

- **Never run a bot with real funds** to test a change. Use `cookbook/`, or the simulation scripts below, which move no funds.
- **Never** touch `.env` or print its contents. `SOLANA_PRIVATE_KEY` is a live key.
- Don't commit anything from `logs/`.
- Test with a cookbook script before touching `src/`.

## Layout

```
src/                 bot source — this dir is the import root (see below)
cookbook/            standalone scripts; each runs on its own, no bot config
  solana/            chain-level basics + solana_transaction_status.py
  pumpfun/{listen,read,trade,graduation,decode}/
    trade/pumpfun_instructions_v2.py   buy_v2/sell_v2 layouts, by their callers
  pumpswap/  letsbonk/  legacy/
tests/regression/    one offline verifier per fixed bug; imports src/
tools/               dev harness — simulations, live round trips, benchmarks
bots/                one YAML per bot instance
idl/                 vendored Anchor IDLs
logs/                {bot_name}_{timestamp}.log
```

**Imports are rooted at `src/`, not at the repo.** `uv pip install -e .` puts
`src/` itself on `sys.path`, so it is `from core.client import SolanaClient` and
`from utils.logger import get_logger` — **not** `from src.core...`. Cookbook
scripts are deliberately self-contained and don't import from `src` at all.
Don't "fix" one by rewiring it to import the bot — that is what `tools/` is for.

Two helpers are exempt, because both are things that must never drift between
copies. `cookbook/solana/solana_transaction_status.py` (the `meta.err` check) is
used by every platform, so it sits with the other chain-level scripts.
`cookbook/pumpfun/trade/pumpfun_instructions_v2.py` (the buy_v2/sell_v2 account
layouts) sits with its only callers, who import it as a plain sibling; `legacy/`
reaches into that directory for it. Both are imported under a short alias
(`as pump_v2`, `as tx_status`) so call sites stay readable. A script that needs
one adds that directory to `sys.path` first; the geyser scripts add the repo root
too, for `src.geyser.generated`.

Dependency layers, low to high — don't introduce an upward import:

`interfaces` → `utils` → `core` → `platforms` → `monitoring` → `trading` → `bot_runner`

`interfaces` is the leaf — it imports nothing internal, and `utils/idl_manager.py`
imports `interfaces.core`. `geyser` holds only generated stubs and likewise
imports nothing internal; `cleanup` sits on `core`/`utils` and is pulled in by
`trading`.

Platform differences are resolved through `interfaces/core.py` abstractions
(`AddressProvider`, curve manager, event parser, instruction builder) and a
registry in `platforms/__init__.py`. Listeners and the trader are
platform-agnostic (`Universal*`); anything platform-shaped belongs under
`platforms/<name>/`.

### What belongs in `cookbook/`

- **One script, one action.** A newcomer should be able to open a single file and
  see the whole thing. Duplication across scripts is the accepted cost of that —
  don't factor shared helpers out of them. `pumpfun_instructions_v2.py` and
  `solana_transaction_status.py` are the two deliberate exceptions and the list is
  closed. Buy and sell never share a file; `pumpfun_create_and_buy_token_v2.py` is
  the sole two-action script, because that pair is what people ask for.
- **Every script runs on its own**: `uv run cookbook/<path>`, reading `.env`. No
  bot config, no import from `src/`. Anything that needs the bot goes in `tools/`;
  anything that asserts a past bug stays fixed goes in `tests/regression/`.
- **Directories are single lowercase words** grouped by what you are doing —
  `listen`, `read`, `trade`, `graduation`, `decode` — under a platform directory.
- **Files are `<protocol>_<verb>_<noun>[_<variant>].py`**, all snake_case:
  `pumpfun_buy_token_v2.py`, `pumpfun_listen_tokens_geyser.py`,
  `letsbonk_sell_token_exact_out.py`, `solana_read_balances.py`. The protocol
  repeats what the directory already says, on purpose — a basename is what shows
  up in an editor tab, a grep hit or a docs link.
  - protocol: `pumpfun`, `pumpswap`, `letsbonk`, `solana`, `anchor`
  - verb: `buy`, `sell`, `create`, `snipe`, `listen`, `watch`, `read`, `derive`,
    `decode`, `check`, `capture`, `find`
  - noun: `token`, `price`, `curve`, `pool`, `balances`, `transaction`, `migrations`
  - variant: instruction version (`v1`, `v2`, `exact_in`, `exact_out`) or transport
  - the two non-runnable helpers take no verb, because they do nothing:
    `pumpfun_instructions_v2.py`, `solana_transaction_status.py`
- **RPC and service names are lowercased into one token**, never camelCase:
  `blocksubscribe`, `logsubscribe`, `programsubscribe`, `getaccountinfo`,
  `gettransaction`, `pumpportal`. So `pumpfun_decode_transaction_gettransaction.py`,
  not `..._getTransaction.py`.
- **Anything not specific to a launchpad belongs under `solana/`**, not `pumpfun/`.
- **Input is a command-line argument, never a constant you edit.** Every script
  builds an `ArgumentParser` in `main()`: required values are positionals, tunables
  are `--options`, and anything the caller varies per run — mint, wallet, amount,
  slippage, fixture path — is one of them. Constants named `DEFAULT_*` supply the
  defaults and are the only place a literal belongs.
  - A placeholder is not a default. `TOKEN_MINT = "..."` reads as optional but
    `Pubkey.from_string("...")` raises at import, so the script dies before
    printing its own usage. Nine scripts did this.
  - Don't read config from environment variables either — `.env` is for
    endpoints and keys, not for trade parameters no usage line mentions.
  - `sys.argv` never appears at module level. The seven listeners take no input
    at all and are exempt; they are listed in the verifier.
- Fixtures keep their own form, `raw_<what>_from_<method>.json`, next to the script
  that reads them.
- **Cite a URL only after checking it resolves.** Three rotted unnoticed by
  2026-09-22 — Anchor restructured its docs and two Chainstack pages moved.
  `uv run tests/regression/verify_documentation_links.py --live` fetches every URL
  in the repo; run it when adding one.
- **A script that spends says so on the first line of its docstring**, and the
  cookbook README marks it. The name is not a safety signal: every `*_buy_*`,
  `*_sell_*`, `*_create_*` and `*_snipe_*` script submits real transactions. Read
  the docstring before running anything.

## Commands

```bash
uv sync                      # install runtime deps + the dev group (ruff)
uv pip install -e .          # editable install (required for the imports above)
pump_bot                     # run all enabled bots
uv run src/bot_runner.py     # same, without the console script
```

Lint and format **the files you touched**, not the whole tree:

```bash
uv run ruff check --fix <paths> && uv run ruff format <paths>
```

A bare `uv run ruff check` reports ~1660 pre-existing errors across the repo.
That is the known baseline, not something your change caused — don't try to fix
it wholesale, and don't read it as a failing build. Just don't add new ones in
the files you edit.

Ruff config lives in `pyproject.toml`: line length 88, double quotes, target
py311, `E501` ignored. Selected rule families include `ANN` (type annotations),
`S` (security), `BLE`/`TRY` (exceptions), `C90`/`PL` (complexity), `ERA` (no
commented-out code). Type-hint public functions, Google-style docstrings, and
`get_logger(__name__)` for logging.

Python 3.11+ (`requires-python = ">=3.11"`, matching ruff's target). Runtime
deps are declared in `[project.dependencies]`; `ruff` and `grpcio-tools` live in
`[dependency-groups] dev`, which `uv sync` installs by default. `grpcio-tools`
is protoc — needed only to regenerate the `geyser_pb2` stubs in
`src/geyser/generated/` from `src/geyser/proto/`, never at runtime. That is the
**only** copy: the geyser examples reach it by putting the repo root on
`sys.path` and importing `src.geyser.generated`. Don't add a second copy under
`cookbook/` — the last one drifted out of sync with the protos.

Where the Solana libraries live, since solana-py 0.40 moved most of them
(upgraded 2026-09-23 from solders 0.26 / solana 0.36.6):

- `TxOpts` is **`TxOptsModel`**, and it, `MemcmpOpts` and `TokenAccountOpts` are
  in `solana.rpc.core`, not `solana.rpc.types`.
- spl-token's `*Params` (`BurnParams`, `CloseAccountParams`, `SyncNativeParams`,
  …) are in `spl.token.models`, not `spl.token.instructions`, which still holds
  the instruction builders.
- All of those are **pydantic models now**, so they are keyword-only and they
  validate: `MemcmpOpts(bytes=...)` wants the base58 **str** a Pubkey's `str()`
  already gives, and raw `bytes` raises `ValidationError`. Nothing in this class
  of break shows up offline — an import sweep passes and the call fails against
  mainnet.
- A bare `commitment="processed"` string still works; `Commitment` is a str enum.

### Verifying a change

Every fix here ships with an offline verifier under `tests/regression/`. Each
script's docstring carries the bug it guards and the checks it runs — read that
before touching the code it covers, and run the ones your change reaches. None of
them move funds. `uv run tests/regression/run_all.py` runs the whole set, or
name individual scripts to run a subset.

| Script | Checks |
|---|---|
| `verify_v2_account_layout.py` | buy_v2/sell_v2 account layouts, PDA/ATA derivations, encoding — against `idl/pump_fun_idl.json` |
| `verify_curve_account_sizes.py` | 125/151/256-byte curves all decode, and nothing filters on account length |
| `verify_create_v2_optional_args.py` | omitted trailing option-typed `create_v2` args decode as unset; mandatory args still fail |
| `verify_transaction_v1.py` | every reader asks `maxSupportedTransactionVersion: 1`, a v1 `create_v2` is detected from its logs alone with the envelope made unreadable, the envelope decode still works as the fallback, and no cookbook/tools listener detects by opening the envelope |
| `verify_block_null_guard.py` | a `blockSubscribe` frame with `value.block: null` is skipped, not logged as an error |
| `verify_listener_cancellation.py` | a cancelled WebSocket listener stops, even when `websockets` reports cancellation as `AssertionError` |
| `verify_pumpportal_buy_path.py` | curve derived from the mint, unreadable curve skips the buy, curve+mint read in one slot-consistent batch |
| `verify_pumpportal_bonk_fields.py` | bonk payloads (no name/symbol/uri) still produce a `TokenInfo`; `--live` re-checks the real feed |
| `verify_extreme_fast_zero_rpc.py` | zero RPC calls between detection and submission for CreateEvent-sourced tokens |
| `verify_buy_result_not_lost.py` | a landed buy is never reported failed, and a reverted one never reported landed |
| `verify_tx_status_checks.py` | every path reads `meta.err`; `--live` replays the reverted signatures from #175 |
| `verify_tp_sl_exit_price.py` | the tp/sl exit prices off the trigger price, and a reverted sell is retried, bounded |
| `verify_time_based_exit_retry.py` | the default `time_based` exit retries a reverted sell instead of stranding the position |
| `verify_time_exit_without_price.py` | `max_hold_time` still fires when every price read fails |
| `verify_exit_sell_confirmation.py` | an exit sell is retried only when retrying is provably safe |
| `verify_rpc_deadline.py` | `post_rpc` bounds wall time, not just attempts (virtual clock) |
| `verify_quote_decimals_resolved.py` | no trade path prices a coin before resolving its quote mint's decimals |
| `verify_cookbook_arguments.py` | every cookbook script takes its input as a command-line argument |
| `verify_documentation_links.py` | no known-dead URL is back; `--live` fetches every one and fails on 4xx/5xx |

Two mainnet simulations, also no funds moved:

```bash
uv run tools/simulate_v2_trades.py <MINT>   # buy_v2/sell_v2 for one coin, reports CU
uv run tools/simulate_bot_buy_path.py       # the bot's whole buy path, fresh coin
```

After any pump.fun program upgrade run `verify_v2_account_layout`,
`verify_curve_account_sizes` and both simulations, then retune
`get_buy_compute_unit_limit` / `get_sell_compute_unit_limit` in
`platforms/pumpfun/instruction_builder.py` from the reported `unitsConsumed`.

### Invariants

Rules that constrain code you might write next, rather than a bug already fixed.
The reasoning behind each lives in the verifier named beside it.

**Listeners and RPC**

- `maxSupportedTransactionVersion` is a **whole-frame** setting. Asking
  `blockSubscribe` for `0` does not skip the v1 transactions in a block, it nulls
  `value.block` for the entire notification — indistinguishable from a skipped
  slot, and a near-total outage of the blocks listener. Send `1` everywhere.
- **A listener routes on `meta.logMessages`, never on the envelope decode.**
  The RPC has already decoded the envelope by the time it emits the logs, and
  they read the same for every transaction version; the byte decode is the
  fallback. The repo now runs solders 0.29, which *does* read a v1 envelope
  (0.26, 0.27.1 and 0.28 all raise `ValueError: io error: unexpected end of
  file` on the committed v1 fixture, measured 2026-09-22) — that does not make
  the log route redundant, because the next version byte will be unreadable in
  its turn. `verify_transaction_v1.py` pins both routes separately.
- The bot **sends** v0 transactions (`MessageV0.try_compile` +
  `VersionedTransaction`, no lookup tables), as of the 2026-09-23 dependency
  upgrade. Nothing else builds a legacy `Message`.
- `post_rpc` must catch `asyncio.TimeoutError` alongside `aiohttp.ClientError` —
  aiohttp raises the former on a request timeout, it is not a `ClientError`, and
  `str()` on it is empty, so the caller logs a blank reason.
- `post_rpc` bounds attempts; `deadline_seconds` (default `None`, the historical
  behaviour) bounds wall time. Don't wrap a lookup in `asyncio.timeout` instead —
  cutting off an in-flight `getTransaction` and returning None is the "can't see
  it, so call it failed" conflation #206 removed.
- `build_and_send_transaction` returns a solders `Signature`, not a `str`.
  Normalize at the boundary: a `Signature` is neither JSON serializable nor
  sliceable, and solana-py's `confirm_transaction` rejects a `str`.

**Buying**

- **Resolve a coin's quote mint before pricing or sizing anything.**
  `resolve_quote_token_program` returns the token program and caches the mint's
  decimals off the same read; `quote_units` raises rather than guessing, because
  a wrong power of ten inflates the price *and* the slippage cap in the same
  direction, so they compound into an overspend instead of cancelling. pump.fun's
  `QuoteControl` registry (PDA `["quote-control"]`) admits mints at 6, 8 and 9
  decimals — 79 of the 170 admitted on 2026-09-22 are tokenized equities, and
  coins paired with them trade live (8 of 124 curves in a 75s sample that day).
- `trade.curve_refresh_budget` (seconds, default 2.0) bounds the pre-buy curve
  read in `extreme_fast_mode`; when it expires the token is **skipped**, because a
  buy built from listener-guessed defaults reverts with `NotAuthorized` (6000),
  `ConstraintSeeds` (2006) or, on letsbonk, `AccountNotInitialized` (3012). The
  sell path keeps the opposite fallback — proceed with cached values — since
  skipping a sell strands the position.
- The refresh is skipped entirely when `TokenInfo.state_from_event` is set, i.e.
  the listener parsed the **CreateEvent**. Instruction-parsed `TokenInfo`
  deliberately does not set it: `args.creator` is user-supplied and post-2026-04-28
  may differ from the canonical `BC.creator`. `trade.trust_create_event: false`
  forces the refresh back on; PumpPortal payloads always refresh.

**Selling**

- `PlatformAwareSeller.execute` does not read a price — the `token_price` it is
  handed **is** the slippage floor (`min_quote_output = amount * price *
  (1 - slippage)`). The caller owns that floor, so an exit prices off the value
  that triggered it, never the entry price.
- The seller's `max_retries` covers **transaction submission only**. An on-chain
  revert comes back as `success=False` and is retried in the monitor loop, where
  the price is re-read first, bounded by `trade.max_exit_sell_attempts` (default
  3, validated 1..100).
- An exit sell is **not idempotent**, so "it failed" is not enough to act on.
  `confirm_transaction_detailed` / `verify_transaction_status` return
  `ConfirmationStatus` (`SUCCESS` / `REVERTED` / `UNCONFIRMED`); a `REVERTED`
  retries, an `UNCONFIRMED` signature is re-checked first. A missing
  `failure_reason` means "unknown", never "reverted".
- `SUBMIT_FAILED` means "no transaction ever reached the chain", and nothing else
  — it is the one reason besides `REVERTED` that retries without asking the chain.
  A post-submission throw reports `UNCONFIRMED` with its signature.
- `confirm_transaction` and `verify_transaction_succeeded` deliberately stay
  bools. Returning the enum would be silent: every member is truthy, so every
  `if await client.confirm_transaction(sig):` would start passing unconditionally.
- `calculate_price` returns `0.0` for a curve with no virtual token reserves — it
  does not raise, and the seller rejects it before its own error handling. Never
  store a non-positive read as the last known price or floor a sell against one;
  `0.0` also satisfies the stop-loss comparison.

### Listener and decoder pitfalls

Each of these was a live bug in the cookbook scripts, all of them invisible
offline and only visible after a couple of minutes against mainnet.

- **A `while True: recv()` loop must break out on `websockets.ConnectionClosed`.**
  Catching it in a broad `except Exception` that only logs makes the next `recv()`
  raise immediately, forever: `tools/compare_listeners.py` produced **13,090,862 error
  lines / 888 MB in 150 s** and never reached its own 30-second report. The outer
  reconnect handler with its `sleep` is unreachable in that shape. A narrow
  `except TimeoutError` or `except json.JSONDecodeError` is fine to swallow —
  those are per-message, not per-connection.
- **Never gate a listener's dispatch on decoding the transaction envelope.**
  The envelope is the one part of a transaction whose format changes under you,
  and the installed solders is only ever one version behind that.
  `meta.logMessages` is already decoded by
  the RPC and is version-agnostic, so route on it and keep the byte decode as a
  fallback. See the Invariants above.
- **Resolve v0 lookup-table accounts before indexing them.** An instruction's
  account indices can point past `message.account_keys` into the address lookup
  table, which geyser reports in `meta.loaded_writable_addresses` then
  `loaded_readonly_addresses` (that order). Ignoring them crashed the geyser
  example with `IndexError` after ~11 coins in 150 s; resolving them removed the
  crash and brought its detection count level with the WebSocket listeners
  (35 coins each over the same window).
- **Identify an instruction by its 8-byte discriminator, never by account count.**
  Several pump.fun instructions share a count, so counting mislabels them and
  then prints every account under the wrong name — a real 19-account `create_v2`
  was reported as `claim_cashback`. Note `buy_exact_sol_in` is also 18 accounts
  on chain, same as legacy `buy`.
- **Walk `meta.innerInstructions`, not just `message.instructions`.** Most trades
  reach the program as a CPI from a router or aggregator: in 40 consecutive
  pump.fun transactions there was **1 top-level** pump instruction against
  **8 inner** ones. Anchor's event-CPI prefix (`e445a52e51cb9a1d`) accounts for a
  good share of the inner instructions; the event's own discriminator follows it.
- **`getProgramAccounts` over the whole pump program is rejected** by current
  providers: *"Too many accounts requested (10000001 pubkeys) … use
  getProgramAccountsV2 with pagination"*. It still works against pump-amm, which
  is small enough. Don't take that error message as a fix: `getProgramAccountsV2`
  is a provider extension (Helius, Solana Tracker), **not core Agave**, and its
  `limit` is a *scan* budget rather than a result count — a page can legally
  return zero accounts and a non-null `paginationKey`, so one filtered answer over
  the pump program costs ~1000 sequential pages. Reach for a filtered
  subscription instead; see the two
  `cookbook/pumpfun/graduation/pumpfun_watch_graduating_*.py` examples.
- **Filtered `programSubscribe` on the pump program is the portable way to find
  curves by state.** `dataSize` + `memcmp` are applied server-side, and it is
  accepted even by the public `api.mainnet-beta.solana.com`. `memcmp` only matches
  exact bytes, so it cannot express "reserves below X" — only a handful of fixed
  cutoffs. Treat it as a bandwidth saver and do the real comparison client-side;
  don't assume a threshold is being enforced upstream. Geyser's account filters
  have the same shape and add the slot and signature.
- **Resolve a curve's mint under Token-2022, not SPL Token.** The curve account
  has no mint field and `["bonding-curve", mint]` is not reversible, so the mint
  comes from the associated bonding curve ATA — which is Token-2022 for every
  `create_v2` coin. `get_token_accounts_by_owner` with the SPL Token program
  returns an empty list for all of them, silently. Verified four for four on live
  curves, each confirmed by re-deriving the curve PDA from the recovered mint.
- **`SetLoadedAccountsDataSizeLimit` must stay generous: 16 MB, not 512 KB.**
  Verified by simulation on a Token-2022 mint with extensions — 512 KB and 4 MB
  both fail `MaxLoadedAccountsDataSizeExceeded` with `unitsConsumed=0` (never
  executed), while 16 MB reaches the buy instruction and is still 4x under the
  64 MB default. solders has no builder for it; encode `struct.pack("<BI", 4, n)`
  against the compute-budget program.

## Pump.fun protocol notes (gotchas)

The IDLs under `idl/` are vendored verbatim from `github.com/pump-fun/pump-public-docs`
(`idl/pump.json` → `pump_fun_idl.json`, `pump_amm.json` → `pump_swap_idl.json`,
`pump_fees.json`). Refresh them from upstream rather than hand-editing.

### Quote assets and the v2 trade instructions (current path)

- pump.fun supports quote assets other than SOL, and it's no longer just
  SOL and USDC. `BondingCurve.quote_mint` is `Pubkey::default()` (all zeros)
  for SOL-paired coins; USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
  is one whitelisted entry in `Global`, but coins paired with Token-2022
  quote mints are live on chain too. **Legacy `buy`/`sell` cannot trade
  non-SOL-paired coins at all.**
- **`Global.whitelisted_quote_mints` is not the authoritative quote-mint
  registry.** The 2026-09-15 upgrade added a `QuoteControl` account with its
  own mint list, which is how a coin can pair with a mint `Global` never
  lists. Error `6064` was relaxed to match — it used to require legacy SPL
  Token and now accepts SPL Token or Token-2022.
- **The quote mint's token program is resolved from chain, not assumed.**
  `resolve_quote_token_program` (`src/core/pubkeys.py`) reads a mint's owner
  once — pre-seeded with WSOL/USDC so those stay free — and caches it for the
  process; `cached_quote_token_program` is the hot-path read used by event
  parsing and address resolution. The bot warms this cache once at startup
  for every configured quote mint, so `extreme_fast_mode`'s zero-RPC contract
  between detection and submission still holds.
- **The quote mint's decimals are resolved from chain too, in the same read.**
  Amounts like `max_sol_cost` and `min_sol_output` are in the quote mint's raw
  units, so assuming 9 decimals for a 6-decimal mint overstates the cap by
  1000x and effectively disables slippage protection. `getAccountInfo` returns
  the owner and the mint data together, so decimals cost no extra call; they
  are cached alongside the token program and pre-seeded for WSOL (9) and
  USDC (6). A quote mint whose decimals cannot be resolved fails at startup
  rather than silently mis-scaling a trade. The `decimals` byte sits at
  offset 44 in both SPL Token and Token-2022 mints — extensions are appended
  after the base struct and never move it (verified 2026-09-16 against
  `jsonParsed` on six mints: plain SPL at 82 bytes and Token-2022 with
  extensions at 405, 690 and 866 bytes, all matching).
- The bot uses **`buy_v2` (27 accounts)** and **`sell_v2` (26 accounts)**. Every
  account is mandatory and the order is identical for every coin — whatever
  quote mint it's paired with, mayhem or not, cashback or not. `sell_v2` is
  `buy_v2` minus `global_volume_accumulator`. Layouts live in `_BUY_V2_ACCOUNTS` /
  `_SELL_V2_ACCOUNTS` in `platforms/pumpfun/instruction_builder.py` and are
  machine-checked against the IDL by `tests/regression/verify_v2_account_layout.py`.
- v2 args carry **no `track_volume` OptionBool** (24-byte data: discriminator +
  two u64). Volume tracking is unconditional now that `user_volume_accumulator`
  is mandatory. `max_sol_cost`/`min_sol_output` are in the **quote mint's** raw
  units — lamports for SOL, 1e-6 for USDC.
- Even for SOL-paired coins you must pass **wrapped SOL** as `quote_mint`, not
  `Pubkey::default()`. Transfers still happen in native SOL, and the
  `associated_quote_*` accounts are only seed-constrained — do **not** create the
  user's WSOL ATA, it would burn ~0.002 SOL of rent for nothing.
- Fee recipients: 24 total, in three sets of 8 (`NORMAL_FEE_RECIPIENTS`,
  `RESERVED_FEE_RECIPIENTS` for mayhem coins, `BUYBACK_FEE_RECIPIENTS`). Every
  v2 buy/sell needs a `fee_recipient` **and** a `buyback_fee_recipient`. The set
  is randomized per tx, per pump.fun's guidance on spreading program throughput.
- `sharing_config` (PDA `["sharing-config", base_mint]`) lives under the **pump
  fees program**, not the pump program. Easy to derive against the wrong program.

### BondingCurve account layout

- **`create_v2` now allocates a 125-byte account, not 151.** The 36 reserved
  padding bytes are gone, and three fields were appended after `quote_mint`:
  `creator_fee_bps` (u64), `can_edit_creator_fee` (bool, reserved — always
  false) and `is_holder_reward` (bool). Every field the bot reads sits before
  where the padding used to start, and none of it moved.
- `extend_account` can grow a curve past 125 bytes, and a length allowlist is
  whack-a-mole against that — don't filter on account length; decode any
  length at or above 125 the same way:

  | Length | What it is |
  |---|---|
  | 125 bytes | What `create_v2` allocates now (2026-09-15 upgrade) |
  | 151 bytes | The old allocation size; still reachable via `extend_account` |
  | 256 bytes | A rarer `extend_account` target, confirmed live 2026-09-15 |

  `tests/regression/verify_curve_account_sizes.py` checks that all three
  decode correctly and that the graduating-token examples don't filter on
  account length.
- The SOL-named fields were **renamed**: `virtual_sol_reserves` →
  `virtual_quote_reserves`, `real_sol_reserves` → `real_quote_reserves`. The
  curve manager still exposes the old names as aliases, so pre-existing callers
  keep working for SOL-paired coins — but anything doing arithmetic must scale by
  the quote mint's decimals (`quote_units_per_token`), not a hardcoded 1e9.
- PumpSwap `Pool` gained a trailing **`virtual_quote_reserves: i128`** (16 bytes,
  offset 245). Pool fields end at 261; live accounts are **301 bytes** with
  trailing padding. Quote against **effective** reserves:
  `pool_quote_token_account.amount + virtual_quote_reserves`.
  **Upstream's release note claims it is 0 on all pools — that is out of date.**
  Verified on mainnet: pool `6Bv1JM1deBPe…` carries 17.584505433 SOL of virtual
  reserves against a 148.455 SOL vault, so quoting off the raw vault balance
  under-prices by ~10.6%. It is `i128`, not `u64` — reading 8 bytes happens to
  work only while the high half is zero.
- pump-amm has **no** `buy_v2`/`sell_v2`. The AMM instruction names are
  unchanged; only the pool layout and quoting moved.

### Coin creation

- The IDL instruction is `create_v2` (snake_case) and takes **eight args**:
  `name (str), symbol (str), uri (str), creator (pubkey), is_mayhem_mode
  (bool), is_cashback_enabled (OptionBool), creator_fee_bps (OptionU64),
  is_holder_reward (OptionBool)`. The last two were added by the 2026-09-15
  upgrade. `OptionBool` and `OptionU64` are single-field Anchor structs with
  no presence tag — each serializes as its bare inner value, 1 byte and 8
  bytes respectively, never a discriminated Option.
- **`is_cashback_enabled = [true]` is rejected as of 2026-09-15**, with error
  `6082 CashbackDeprecated` — `create_v2` can no longer mint a new cashback
  coin. Existing cashback coins are unaffected: they keep trading, keep
  accruing, and remain claimable, so every cashback code path in this repo
  (the legacy sell path's cashback branch, `is_cashback_coin` on `TokenInfo`,
  etc.) stays live and must not be treated as dead.
- **The trailing args are positional, not independently optional.** Reaching
  `is_holder_reward` (arg 8) means sending `is_cashback_enabled` (6) and
  `creator_fee_bps` (7) first, even when both are false/zero. The three
  committed fixtures show three different wire lengths: the blocksubscribe
  fixture omits all trailing args (0 bytes after `is_mayhem_mode`), the
  "omitted fee bps" getTransaction fixture sends `is_cashback_enabled` only
  (1 byte), and the "with fee bps" getTransaction fixture sends
  `is_cashback_enabled` + `creator_fee_bps` (9 bytes) — counted directly from
  each fixture's instruction data, 2026-09-15. `is_holder_reward` (the
  three-trailing-arg form) is legal per the IDL but has not been observed on
  the wire — UNVERIFIED whether it is ever sent. A decoder that reads a
  fixed number of trailing bytes raises `IndexError` on the shorter forms.
  Decode trailing args defensively and report a missing one as unset —
  `utils/idl_parser.py` does this for trailing option-typed args since #184
  (`uv run tests/regression/verify_create_v2_optional_args.py` checks it).
- `create_v2` accounts 1-16 are in the IDL; accounts **17-19 are optional
  remaining accounts** (`quote_mint`, `associated_quote_bonding_curve`,
  `quote_token_program`). All three or none. This is the only way to read a new
  coin's quote asset from the instruction rather than the event. In practice they
  are appended for **SOL-paired coins too**, carrying wrapped SOL — a live
  SOL-paired `create_v2` was observed with 19 accounts and account 17 = WSOL — so
  do not treat a 19-account `create_v2` as proof of a non-SOL quote asset. Read
  `quote_mint` off the curve instead.
- The **associated bonding curve is an ordinary ATA**, so its address depends on
  which token program owns the mint: Token2022 for `create_v2` coins, SPL Token
  for legacy `create`. Deriving with the wrong program returns a valid-looking
  address that does not exist on chain. Verified: curve `3jJ83ND…` derives to
  `Cd4iC3Jn…` under Token2022 (matches chain) and `AhNzZsBp…` under SPL Token.
- `extreme_fast_mode` skips the curve-state price fetch. Whether it also reads
  the curve for mayhem/cashback/creator/**quote_mint** depends on provenance
  (see the Invariants above): CreateEvent-sourced tokens
  (`state_from_event`) trade on the event data with zero RPC calls, while
  pumpportal/incomplete-event tokens refresh from chain — the wrong quote mint
  means spending the wrong balance entirely. Event parsers populate
  `quote_mint` from `CreateEvent` (which gained `quote_mint` and
  `virtual_quote_reserves` as trailing fields).

### Holder reward coins

`create_v2` can set `is_holder_reward` so the creator fee is set aside for
holders instead of paid to a creator wallet. On such a coin
`BondingCurve.creator` holds a pump.fun address rather than the actual
creator's — that's expected, and the `creator_vault` derivation is unchanged
and still correct either way, since it derives from whatever `creator` the
curve carries. **No trade instruction changed**: `buy`, `sell`, `buy_v2`,
`sell_v2` and the PumpSwap instructions take identical accounts and arguments
whether or not a coin is a holder-reward coin. `Global.is_holder_reward_enabled`
can switch creation off globally.

`TokenInfo.is_holder_reward` and `TokenInfo.creator_fee_bps` surface this to
the bot — see the field comments on `TokenInfo` in `src/interfaces/core.py`
(dated 2026-09-15) for what's been verified live: SOL- and USDC-paired coins
keep `creator_fee_bps` at 0 (checked on 21 SOL-paired and 10 USDC-paired
coins), while every custom-pair coin checked in the same pass carried a
nonzero value.

### Legacy instructions (fallback only)

Retained behind `PumpFunInstructionBuilder(..., use_legacy_instructions=True)`.
The IDL under-reports these: `buy` is **18 accounts** on-chain (IDL lists 16) and
`sell` is **16 non-cashback / 17 cashback** (IDL lists 14). The extras are
`bonding-curve-v2` (PDA `["bonding-curve-v2", mint]`) followed by a buyback fee
recipient (mutable); the cashback sell path also inserts
`user_volume_accumulator` before `bonding-curve-v2`. On the PumpSwap side the
legacy path needs `pool-v2` (PDA `["pool-v2", base_mint]` under pump-amm) —
without it pump-amm throws `AnchorError 6023 (Overflow)` after the transfers
complete, a misleading code for a missing account. Prefer v2 — it is the
interface pump.fun maintains.

## Config notes

- Bot YAML supports `${VAR}` interpolation from the file named by `env_file`.
  Actual variable names are `SOLANA_NODE_RPC_ENDPOINT`,
  `SOLANA_NODE_WSS_ENDPOINT`, `SOLANA_PRIVATE_KEY`, `GEYSER_*`.
- **PumpPortal sends a thinner payload for `bonk` pools than for `pump` ones.**
  A bonk `create` carries no `name`, `symbol` or `uri` — verified against the
  live feed 2026-09-16, which ran 24 pump creates and 7 bonk creates in 90s.
  Requiring those fields rejected every bonk token that ever arrived (issue
  #200), so only `mint` and `traderPublicKey` are required; everything else the
  trade path needs is derived from the mint. Consequence worth knowing:
  `filters.match_string` matches on name/symbol, so it can never match a bonk
  token from this feed. `tests/regression/verify_pumpportal_bonk_fields.py`
  checks this against committed fixtures, and `--live` re-checks it against the
  real feed. The bonk **trade** path past detection is still unverified — see
  issue #201.
- **PumpPortal does not report coins created in a transaction v1.** Measured
  2026-09-16 across two windows: 0 of 6 v1 creates pushed, against near-complete
  coverage of v0 creates (104 of 105 in one 240s window). It is a third-party
  feed, so no local change recovers them — the coin is never sent. This compounds
  the thin-bonk-payload gap below, and it means `pumpportal` is a sampling feed
  now, not a complete one. `bots/bot-sniper-4-pp.yaml` carries the same warning.
- `src/config_loader.py` (repo root of the package, not under `core/`) validates the platform/listener pairing before startup:
  pump.fun supports `logs`, `blocks`, `geyser`, `pumpportal`; letsbonk.fun
  supports `blocks`, `geyser`, `pumpportal` — **not `logs`**. Adding a listener
  means updating `PLATFORM_LISTENER_COMPATIBILITY` there too.
- Bots with `separate_process: true` run in their own process. One log file per
  bot instance.

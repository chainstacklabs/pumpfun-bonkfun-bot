# Agent guide

Solana trading bot for pump.fun and letsbonk.fun. Snipes newly created tokens and exits on a configured strategy. See [README.md](README.md) for setup and configuration; this file covers what an agent needs that the code doesn't make obvious.

`AGENTS.md` is a symlink to this file, so Claude Code, Codex, Cursor, and Windsurf all read the same guide.

## Ground rules

- **Never run a bot with real funds** to test a change. Use `learning-examples/`, or the simulation scripts below, which move no funds.
- **Never** touch `.env` or print its contents. `SOLANA_PRIVATE_KEY` is a live key.
- Don't commit anything from `logs/`.
- Test with a learning example before touching `src/`.

## Layout

```
src/            bot source — this dir is the import root (see below)
learning-examples/   standalone scripts; each runs on its own, no bot config
bots/           one YAML per bot instance
idl/            vendored Anchor IDLs
logs/           {bot_name}_{timestamp}.log
```

**Imports are rooted at `src/`, not at the repo.** `uv pip install -e .` puts
`src/` itself on `sys.path`, so it is `from core.client import SolanaClient` and
`from utils.logger import get_logger` — **not** `from src.core...`. Learning
examples are deliberately self-contained: they import siblings like `pump_v2`
and `tx_status` as top-level modules and mostly don't import from `src` at all.
Don't "fix" an example by rewiring it to import the bot.

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

### Naming inside `learning-examples/`

- **Directories are kebab-case** (`bonding-curve-progress`, `listen-new-tokens`,
  `copy-trading`). A single-token product name stays one word (`pumpswap`).
- **Files are snake_case, verb first** — `fetch_price.py`, `decode_from_*.py`,
  `extract_blocksubscribe_transactions.py`, `verify_*.py`, `simulate_*.py`.
  Exceptions are the shared helper modules `pump_v2.py` and `tx_status.py`, which
  are libraries rather than runnable scripts.
- **RPC and service names are lowercased into one token**, never camelCase:
  `blocksubscribe`, `logsubscribe`, `programsubscribe`, `getaccountinfo`,
  `gettransaction`, `pumpportal`. So `decode_from_gettransaction.py`, not
  `decode_from_getTransaction.py`.
- Fixtures are `raw_<what>_from_<method>.json` next to the script that reads
  them, under the same rules.
- `simulate_*` and `verify_*` never move funds — that half of the naming is
  load-bearing and machine-checked. The inverse is **not** true: `live_*` is not
  the only prefix that spends. `manual_*` (including the `pumpswap/` and
  `letsbonk-buy-sell/` ones), `mint_and_buy*` and `cleanup_accounts.py` all
  submit real transactions. Read the module docstring before running anything
  that is not `simulate_*` or `verify_*`.

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

A bare `uv run ruff check` reports ~1700 pre-existing errors across the repo.
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
`learning-examples/` — the last one drifted out of sync with the protos.

### Verifying pump.fun v2 trade instructions

```bash
# Offline: cross-check buy_v2/sell_v2 account layouts, PDA/ATA derivations,
# instruction encoding and quote-asset config against idl/pump_fun_idl.json
uv run learning-examples/verify_v2_account_layout.py

# Offline: 125/151/256-byte bonding curves all decode, and the graduating-
# token examples don't filter on account length
uv run learning-examples/verify_curve_account_sizes.py

# Mainnet, no funds moved: simulate buy_v2/sell_v2 for one coin, report CU
uv run learning-examples/simulate_v2_trades.py <MINT>

# Mainnet, no funds moved: run the bot's whole buy path against a fresh coin
uv run learning-examples/simulate_bot_buy_path.py
uv run learning-examples/simulate_bot_buy_path.py --no-extreme-fast
```

Run all four after any pump.fun program upgrade. The simulations report
`unitsConsumed`; use it to retune `get_buy_compute_unit_limit` /
`get_sell_compute_unit_limit` in `platforms/pumpfun/instruction_builder.py`.

### Verifying the listener-to-buy path (issue #170)

```bash
# Offline: bonding curve derived from the mint (payload bondingCurveKey not
# trusted), unreadable curve skips the buy instead of submitting with guessed
# accounts, curve+mint read in one slot-consistent batch
uv run learning-examples/verify_pumpportal_buy_path.py

# Offline: extreme_fast_mode stays at ZERO RPC calls between detection and
# submission for CreateEvent-sourced tokens; pumpportal still refreshes
uv run learning-examples/verify_extreme_fast_zero_rpc.py
```

Fast listeners (pumpportal especially, but geyser too) can announce a token
seconds before every node behind a load-balanced RPC endpoint can read its
accounts — two back-to-back reads on the same endpoint may be served from
nodes at different slots. `trade.curve_refresh_budget` (seconds, default 2.0)
bounds the pre-buy curve read in `extreme_fast_mode`; when it expires the token
is skipped, because a buy built from listener-guessed defaults reverts on-chain
with `NotAuthorized` (6000), `ConstraintSeeds` (2006) or, on letsbonk,
`AccountNotInitialized` (3012). The sell path deliberately keeps the opposite
fallback — proceed with cached values — since skipping a sell strands the
position.

The refresh is skipped entirely — extreme_fast_mode's zero-RPC contract —
when `TokenInfo.state_from_event` is set, i.e. the listener parsed the
**CreateEvent** (geyser/logs/blocks), which carries the canonical creator,
mayhem/cashback flags and quote_mint. Instruction `args.creator` is
user-supplied and post-2026-04-28 may differ from the canonical `BC.creator`
(PFEE PDA delegation), so instruction-parsed TokenInfo deliberately does
**not** set the flag; the geyser parser prefers `meta.log_messages` over
instruction decoding for exactly this reason. `trade.trust_create_event:
false` is the escape hatch back to always-refresh. PumpPortal payloads carry
none of these fields and always refresh. Related pitfall (fixed in #184): the
IDL instruction decoder used to reject `create_v2` transactions that omit the
trailing `is_cashback_enabled` OptionBool (a legal wire form), silently
dropping those coins from the instruction path. It now reports omitted
trailing option-typed args as unset — `verify_create_v2_optional_args.py`
machine-checks that, and that mandatory args still fail the decode. The
log/event path stays preferred for the canonical-creator reason above.

### Verifying transaction-status handling

```bash
# Offline: stub checks plus a scan that every example verifies meta.err
uv run learning-examples/verify_tx_status_checks.py

# Adds a mainnet replay of the reverted signatures from issue #175
uv run learning-examples/verify_tx_status_checks.py --live
```

`confirm_transaction` answers "did this land in a block?", never "did it
succeed". A landed transaction can have reverted, and RPC reports that only in
`meta.err`. Reporting success without reading it is issue #175: buys reverting
with `BuybackFeeRecipientMissing` (6062) printed as confirmed buys.

- Examples use `learning-examples/tx_status.py` — `confirm_and_assert` in place
  of a bare `confirm_transaction`, or `assert_transaction_succeeded` after one.
  The verifier above fails the build if a new example skips it.
- The bot uses `SolanaClient.confirm_transaction`, which folds `meta.err` into
  its return value. **Read the boolean** — discarding it is the same bug.
- `_get_transaction_result` must send `maxSupportedTransactionVersion: 0` or the
  RPC rejects every versioned (v0) transaction with `-32015`, and a good trade
  reads back as unconfirmed.
- `build_and_send_transaction` returns a solders `Signature`, not a `str`. A
  `Signature` is not JSON serializable and does not support slicing; a `str` is
  rejected by solana-py's `confirm_transaction`. Normalize at the boundary.
- `post_rpc` must catch `asyncio.TimeoutError` alongside `aiohttp.ClientError`.
  aiohttp raises the former when the request timeout fires and it is **not** a
  `ClientError`, so leaving it out lets every RPC timeout escape unretried —
  and `str()` on it is empty, so the caller logs a blank reason. A slow
  `getAccountInfo` is enough to take down a whole listener run this way.

### Verifying the tp/sl exit path (issue #189)

```bash
# Offline: the exit sell prices off the price that triggered it, a reverted
# exit sell is retried, and the retry is bounded
uv run learning-examples/verify_tp_sl_exit_price.py
```

`PlatformAwareSeller.execute` does not read a price — the `token_price` it is
handed **is** the slippage floor (`min_quote_output = amount * price *
(1 - slippage)`). So the caller owns the floor's correctness. A tp/sl exit fires
precisely because price left `entry_price`, so pricing the sell off the entry
sets a floor the pool cannot pay on a stop-loss and the sell reverts with 6003
`TooLittleSolReceived` — during the drop the stop-loss exists to escape. On a
take-profit the same mistake runs the other way and the floor protects nothing.
`_monitor_position_until_exit` already fetches `current_price` at the top of
each iteration, so passing it costs no extra RPC call; `_handle_time_based_exit`
genuinely has nothing fresher and keeps passing the buy price.

The seller's `max_retries` covers **transaction submission only**. An on-chain
revert comes back as `success=False` and is not retried there, so the retry has
to happen in the monitor loop, where the price is re-read first.
`trade.max_exit_sell_attempts` (default 3, validated to 1..100) bounds it so a
token that keeps reverting cannot pin the bot on one position, and the counter
resets if the price recovers out of the exit band. After the last attempt the
position is left open and unmonitored — logged loudly, since the tokens are
still held. Watch the `break`: before #189 it sat outside both branches of
`if sell_result.success:`, so a failed sell abandoned the position after a
single try while leaving `is_active=True`.

### Verifying exit-sell safety and RPC deadlines (issues #207, #208, #209)

```bash
# Offline: an exit sell is retried only when retrying is provably safe, and
# confirm_transaction still returns a bool rather than a truthy enum
uv run learning-examples/verify_exit_sell_confirmation.py

# Offline: max_hold_time still fires when every price read fails
uv run learning-examples/verify_time_exit_without_price.py

# Offline: post_rpc bounds wall time, not just attempts (virtual clock)
uv run learning-examples/verify_rpc_deadline.py
```

**An exit sell is not idempotent, so "it failed" is not enough to act on.**
A sell that reverted changed nothing and should be retried; a sell whose
confirmation never arrived may already have emptied the position, and another
one spends a fee to act on a balance that no longer exists.
`SolanaClient.confirm_transaction_detailed` / `verify_transaction_status`
return `ConfirmationStatus` (`SUCCESS` / `REVERTED` / `UNCONFIRMED`) and the
seller turns that into `TradeResult.failure_reason`, alongside the
`tx_signature` its failure branch used to drop. `_classify_failed_exit_sell`
retries a `REVERTED`, re-checks an `UNCONFIRMED` signature before deciding —
`getTransaction` still answers after signature statuses have aged out — and
stops rather than reselling blind if it is still unresolved. **A missing
`failure_reason` means "unknown", never "reverted"**: a stub seller simulating
a revert has to say `TradeFailureReason.REVERTED` or the retry will not fire.

**`confirm_transaction` and `verify_transaction_succeeded` deliberately stay
bools.** Returning the enum from them would be silent: every enum member is
truthy, so each existing `if await client.confirm_transaction(sig):` would
start passing unconditionally.

**`position.should_exit()` cannot be asked anything without a price**, so a
failed price read used to skip every exit check — including `max_hold_time`,
which needs no price at all. With the read failing repeatedly the monitor loop
span forever: `is_active` never changed and the position was never sold.
`Position.should_exit_on_time()` is the price-free question, asked in the
loop's exception handler; the blind exit is floored against the last price
actually read, or the entry price if none ever was, and is still bounded by
`trade.max_exit_sell_attempts`.

**`post_rpc` bounds attempts, not wall time.** Three error retries backing off
1, 2, 4 … 16s, or ten 429 retries waiting up to 30s each and honouring a
`Retry-After` of any size, is minutes on one call — and on the trade path that
holds up the whole bot. `deadline_seconds` is the separate bound; it defaults
to `None`, which is exactly the historical behaviour. `_get_transaction_result`
passes the time *it* has left on every lookup, so its `budget_seconds` is a real
ceiling instead of `budget + one post_rpc worst case`. Don't reach for
`asyncio.timeout` here: cutting off an in-flight `getTransaction` and returning
None is the "can't see it, so call it failed" conflation that #206 removed.

### Listener and decoder pitfalls

Each of these was a live bug in `learning-examples/`, all of them invisible
offline and only visible after a couple of minutes against mainnet.

- **A `while True: recv()` loop must break out on `websockets.ConnectionClosed`.**
  Catching it in a broad `except Exception` that only logs makes the next `recv()`
  raise immediately, forever: `listen-new-tokens/compare_listeners.py` produced **13,090,862 error
  lines / 888 MB in 150 s** and never reached its own 30-second report. The outer
  reconnect handler with its `sleep` is unreachable in that shape. A narrow
  `except TimeoutError` or `except json.JSONDecodeError` is fine to swallow —
  those are per-message, not per-connection.
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
  subscription instead; see the two `get_graduating_tokens*.py` examples.
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
  machine-checked against the IDL by `learning-examples/verify_v2_account_layout.py`.
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

  `learning-examples/verify_curve_account_sizes.py` checks that all three
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
  (`uv run learning-examples/verify_create_v2_optional_args.py` checks it).
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
  (see "Verifying the listener-to-buy path" above): CreateEvent-sourced tokens
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
  token from this feed. `learning-examples/verify_pumpportal_bonk_fields.py`
  checks this against committed fixtures, and `--live` re-checks it against the
  real feed. The bonk **trade** path past detection is still unverified — see
  issue #201.
- `config_loader.py` validates the platform/listener pairing before startup:
  pump.fun supports `logs`, `blocks`, `geyser`, `pumpportal`; letsbonk.fun
  supports `blocks`, `geyser`, `pumpportal` — **not `logs`**. Adding a listener
  means updating `PLATFORM_LISTENER_COMPATIBILITY` there too.
- Bots with `separate_process: true` run in their own process. One log file per
  bot instance.

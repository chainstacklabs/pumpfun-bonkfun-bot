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

# Mainnet, no funds moved: simulate buy_v2/sell_v2 for one coin, report CU
uv run learning-examples/simulate_v2_trades.py <MINT>

# Mainnet, no funds moved: run the bot's whole buy path against a fresh coin
uv run learning-examples/simulate_bot_buy_path.py
uv run learning-examples/simulate_bot_buy_path.py --no-extreme-fast
```

Run all three after any pump.fun program upgrade. The simulations report
`unitsConsumed`; use it to retune `get_buy_compute_unit_limit` /
`get_sell_compute_unit_limit` in `platforms/pumpfun/instruction_builder.py`.

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

- pump.fun supports quote assets other than SOL. `BondingCurve.quote_mint` is
  `Pubkey::default()` (all zeros) for SOL-paired coins; USDC
  (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) is whitelisted in `Global`.
  **Legacy `buy`/`sell` cannot trade non-SOL-paired coins at all.**
- The bot uses **`buy_v2` (27 accounts)** and **`sell_v2` (26 accounts)**. Every
  account is mandatory and the order is identical for every coin — SOL or USDC
  paired, mayhem or not, cashback or not. `sell_v2` is `buy_v2` minus
  `global_volume_accumulator`. Layouts live in `_BUY_V2_ACCOUNTS` /
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

- The account is **151 bytes**: 8-byte discriminator, then
  `virtual_token_reserves, virtual_quote_reserves, real_token_reserves,
  real_quote_reserves, token_total_supply` (u64 each), `complete` (1B, offset 48),
  `creator` (32B, offset 49), `is_mayhem_mode` (offset 81),
  `is_cashback_coin` (offset 82), `quote_mint` (32B, offset 83), then 36 reserved
  zero bytes. The documented struct is 115 bytes; the extra 36 are padding.
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

- The IDL instruction is `create_v2` (snake_case). Args: `name (str),
  symbol (str), uri (str), creator (pubkey), is_mayhem_mode (bool),
  is_cashback_enabled (OptionBool 1B)`. `OptionBool` is a struct wrapping a single
  bool — serialized as 1 byte, not 2.
- **`is_cashback_enabled` can be absent from the wire entirely.** Two mainnet
  `create_v2` instructions carry `0001` and `00` after `creator`: one sends both
  trailing args, the other omits the last. A decoder that reads a fixed number of
  trailing bytes raises `IndexError` on roughly half of all coins. Decode
  trailing args defensively and report a missing one as unset.
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
- `extreme_fast_mode` skips the curve-state price fetch but still refreshes
  mayhem/cashback/creator/**quote_mint** from chain, because the wrong quote mint
  means spending the wrong balance entirely. Event parsers also populate
  `quote_mint` from `CreateEvent` (which gained `quote_mint` and
  `virtual_quote_reserves` as trailing fields).

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
- `config_loader.py` validates the platform/listener pairing before startup:
  pump.fun supports `logs`, `blocks`, `geyser`, `pumpportal`; letsbonk.fun
  supports `blocks`, `geyser`, `pumpportal` — **not `logs`**. Adding a listener
  means updating `PLATFORM_LISTENER_COMPATIBILITY` there too.
- Bots with `separate_process: true` run in their own process. One log file per
  bot instance.

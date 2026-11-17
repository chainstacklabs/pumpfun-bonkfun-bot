# Agent guide

Solana trading bot for pump.fun and Raydium LaunchLab (any launchpad; letsbonk.fun and StonkFun presets). Snipes newly created tokens and exits on a configured strategy. See [README.md](README.md) for setup and configuration.

## Reference

Read the matching doc before you touch these areas. In Claude Code, the rules in
`.claude/rules/` load the pointer automatically for the files you open.

| Doc | Read it before |
|---|---|
| [docs/pumpfun-protocol.md](docs/pumpfun-protocol.md) | touching account layouts, instruction args, fee recipients or quote assets |
| [docs/launchlab-protocol.md](docs/launchlab-protocol.md) | touching LaunchLab (any launchpad, letsbonk.fun, StonkFun): platform configs, swap accounts, quote assets, transfer fees |
| [docs/listeners-and-geyser.md](docs/listeners-and-geyser.md) | changing a listener, the geyser stubs or a decoder |
| [docs/cookbook-conventions.md](docs/cookbook-conventions.md) | adding or changing anything under `cookbook/` |
| [docs/regression-tests.md](docs/regression-tests.md) | picking which verifiers your change needs |
| [docs/pre-buy-checks.md](docs/pre-buy-checks.md) | adding or changing a pre-buy check (`src/trading/checks/`) |
| [docs/trading-lifecycle.md](docs/trading-lifecycle.md) | changing positions, the positions file, shutdown, cleanup or PnL (`src/trading/`, `src/cleanup/`) |

## Ground rules

- **A change is verified by running a bot with real funds**, only with explicit
  approval and after stating what will run and what it costs. `cookbook/` and the
  simulations skip `bot_runner`, config loading, the trader loop, exits and
  cleanup, so they can't settle it. Cover every mode the change reaches (each
  listener, `extreme_fast_mode` on and off, each `exit_strategy`) and end with
  the wallet holding SOL only.
- **Never** touch `.env` or print its contents. `SOLANA_PRIVATE_KEY` is a live key.

## Layout

```
src/                 bot source — this dir is the import root (see below)
cookbook/            standalone scripts; each runs on its own, no bot config
tests/regression/    one offline verifier per fixed bug; imports src/
tools/               dev harness — simulations, live round trips, benchmarks
bots/                one YAML per bot instance
docs/                reference docs (table above)
idl/                 vendored Anchor IDLs
logs/, trades/, positions/   written by a running bot; never commit them
```

**Imports are rooted at `src/`, not at the repo.** `uv pip install -e .` puts
`src/` on `sys.path`, so it is `from core.client import SolanaClient`, not
`from src.core...`. Cookbook scripts import nothing from `src` but the generated
geyser stubs; don't rewire one to, that is what `tools/` is for.

Dependency layers, low to high — don't introduce an upward import:

`interfaces` → `utils` → `core` → `platforms` → `monitoring` → `trading` → `bot_runner`

`interfaces` is the leaf; `geyser` holds only generated stubs; `cleanup` sits on
`core`/`utils` and is pulled in by `trading`.

Platform differences resolve through `interfaces/core.py` and the registry in
`platforms/__init__.py`; anything platform-shaped goes in `platforms/<name>/`. Trades
go through one `Market`: `curve_market` (`CurveMarket`, builder + curve manager), then
`graduated_market` (`PumpSwapMarket`, `CpmmMarket`: both buy and sell). `UniversalTrader`
coordinates `trading/intake.py`, `platform_aware.py` and `position_manager.py`.

## Commands

```bash
uv sync                      # install runtime deps + the dev group (ruff)
uv pip install -e .          # editable install (required for the imports above)
pump_bot                     # run all enabled bots
uv run src/bot_runner.py     # same, without the console script
pump_bot_positions status <bot yaml>     # what a bot holds; read only
pump_bot_positions sell-all <bot yaml>   # sell it all; refuses while the bot runs
uv run tests/regression/run_all.py   # every offline verifier; moves no funds
```

Lint and format **the files you touched**, not the whole tree:

```bash
uv run ruff check --fix <paths> && uv run ruff format <paths>
```

A bare `uv run ruff check` reports a known backlog of older errors; it is not a
failing build. Ruff config is in `pyproject.toml` (line length 88, double quotes,
py311, `E501` ignored). Type-hint public functions and use `get_logger(__name__)`.

**Docstrings and comments carry what the code cannot.** A one-line summary, then
only what a reader could not get from the signature: units, ranges, what `None`
means, which errors a caller must handle, a constraint that stops the next
person breaking it. An `Args:` entry that restates the parameter name, or a
comment that restates the line under it, is noise and gets deleted. Leave out
rationale nobody acts on, hypotheticals about what might change, measurements,
dates and issue numbers.

### Where the Solana libraries live

solana-py 0.40 moved most of them; an import sweep passes offline and the call
fails against mainnet.
- `TxOpts`, `MemcmpOpts` and `TokenAccountOpts` are defined in
  `solana.rpc.models` and re-exported by `solana.rpc.core` (`TxOpts` as
  `TxOptsModel`); they are no longer in `solana.rpc.types`.
- spl-token's `*Params` (`BurnParams`, `CloseAccountParams`, …) are in
  `spl.token.models`; `spl.token.instructions` still holds the builders.
- Those are pydantic models: keyword-only and validating. `MemcmpOpts(bytes=...)`
  wants the base58 str a Pubkey's `str()` gives; raw `bytes` are decoded as
  UTF-8, so key bytes raise or match the wrong data.
- A bare `commitment="processed"` still works; `Commitment` is a str enum.

## Invariants

Rules that constrain code you might write next. The reasoning behind each lives
in the verifier named in [docs/regression-tests.md](docs/regression-tests.md).

**Listeners and RPC**

- `maxSupportedTransactionVersion` is a **whole-frame** setting. Asking
  `blockSubscribe` for `0` does not skip a block's v1 transactions, it nulls
  `value.block` for the whole notification — indistinguishable from a skipped
  slot, and a near-total outage of the blocks listener. Send `1` everywhere.
- **A listener routes on `meta.logMessages`, never on the envelope decode.** The
  logs read the same in every transaction version. pump.fun's parser decodes the
  create only when they carry no CreateEvent; LaunchLab's always does.
- `src/` **sends** v0 transactions (`MessageV0.try_compile`, no lookup tables);
  nothing builds a legacy `Message`. The cookbook launch sends v1 — 4096 bytes,
  budget inline, no tables — so scope this rule, don't apply it there.
- `post_rpc` must catch `asyncio.TimeoutError` alongside `aiohttp.ClientError`:
  aiohttp raises it on a request timeout, and it is not a `ClientError`.
- `post_rpc` bounds attempts; `deadline_seconds` (default `None`) bounds wall
  time. Don't wrap a lookup in `asyncio.timeout` instead — cutting off an
  in-flight `getTransaction` and returning None conflates "can't see it" with
  "it failed".
- `build_and_send_transaction` returns a solders `Signature`, not a `str`.
  Normalize at the boundary: a `Signature` is neither JSON serializable nor
  sliceable, and solana-py's `confirm_transaction` rejects a `str`.
- **The endpoints carry their API key in the URL, so any log line holding one is
  a leak.** `install_secret_redaction` (`utils/logger.py`) masks the value, its
  host and the traceback at log-record creation; `bot_runner` and `positions_cli`
  call it at startup, and `bot_runner.mask_endpoints` adds a config's own. Redact
  the **rendered** message, not `record.msg` plus the string arguments: httpx2
  passes its URL as a `URL` object, which a type check skips.

**Buying**

- **Resolve a coin's quote mint before pricing or sizing anything.**
  `resolve_quote_token_program` returns the token program and caches the mint's
  decimals off the same read. Both unit helpers then raise rather than guess
  (`quote_units_per_token` in `src/`, `quote_units` in `cookbook/`): a wrong power
  of ten scales the price and the slippage cap together into an overspend. Only
  `cached_quote_units` answers `None`, for the curve decoder; it leaves
  `price_per_token` unset and the caller's quote gate refuses the coin.
- `trade.curve_refresh_budget` (seconds, default 2.0) bounds the pre-buy curve
  read in `extreme_fast_mode`; when it expires the token is **skipped**, because
  a buy built from listener-guessed defaults reverts with `NotAuthorized` (6000),
  `ConstraintSeeds` (2006) or, on letsbonk, `AccountNotInitialized` (3012). The
  sell path keeps the opposite fallback — proceed with cached values — since
  skipping a sell strands the position.
- The refresh is skipped entirely when `TokenInfo.state_from_event` is set and
  `quote_mint` is known. The CreateEvent parsers set it; instruction-parsed
  `TokenInfo` does not, because `args.creator` is user-supplied and may differ
  from the canonical `BC.creator`. The `shreds` listener sets it itself, having
  no curve to read. `trade.trust_create_event: false` forces the refresh back on.

**Selling**

- `PlatformAwareSeller.execute` reads no price: it floors a sell at `amount *
  price * (1 - slippage)` on the `token_price` it is handed, after taking out the
  venue fee and any transfer fee. A pump.fun curve sell its real reserves can't
  pay is cut to what they can, floored at `amount * min(price, payout per token)
  * (1 - slippage)`; the position keeps the rest.
- The seller's `max_retries` covers **transaction submission only**. An on-chain
  revert comes back as `success=False` and is retried in the monitor loop, where
  the price is re-read first, bounded by `trade.max_exit_sell_attempts` (default
  3, validated 1..100).
- An exit sell is **not idempotent**, so "it failed" is not enough to act on.
  `confirm_transaction_detailed` / `verify_transaction_status` return
  `ConfirmationStatus` (`SUCCESS` / `REVERTED` / `UNCONFIRMED`): a `REVERTED`
  retries; an `UNCONFIRMED` one is re-checked, resold only past its last valid
  height unseen. A missing `failure_reason` means "unknown", never "reverted".
- `SUBMIT_FAILED` means "no transaction ever reached the chain" and nothing else
  — the one reason besides `REVERTED` that retries without asking the chain. A
  post-submission throw reports `UNCONFIRMED` with its signature.
- `confirm_transaction` and `verify_transaction_succeeded` deliberately stay
  bools. Returning the enum would be silent: every member is truthy, so every
  `if await client.confirm_transaction(sig):` would start passing unconditionally.
- `calculate_price` returns `0.0` for a curve with no virtual token reserves, and
  raises `CurveGraduatedError` for a graduated one; price, buy and sell go through
  the platform's `graduated_market`. Never store a non-positive read as the last
  known price or floor a sell against one; `0.0` satisfies the stop-loss check.

## Config notes

- Bot YAML supports `${VAR}` interpolation from the file named by `env_file`.
  `true`/`false` becomes a bool only for a setting in `SWITCHES`
  (`config_loader.py`); add a new on/off setting there. The wallet is
  `private_key` or `keystore` (`core/keystore.py`), never both.
- `src/config_loader.py` (package root) validates the platform/listener pairing
  at startup: pump.fun takes all four listeners, LaunchLab platforms all but
  `logs` (their logs never name the coin). A new listener goes in
  `PLATFORM_LISTENER_COMPATIBILITY` there too.
- Bots with `separate_process: true` run in their own process, one log file each.
- `pump_bot` runs every enabled `bots/*.yaml` in the current folder; `--config
  <file>` runs only that one, whatever its `enabled` says. Exit 0 when every
  enabled bot ran without an error, 1 otherwise, 130 after Ctrl+C, 143 after
  SIGTERM.

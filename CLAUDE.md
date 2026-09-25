# Agent guide

Solana trading bot for pump.fun and letsbonk.fun. Snipes newly created tokens and exits on a configured strategy. See [README.md](README.md) for setup and configuration.

`AGENTS.md` is a symlink to this file, so Claude Code, Codex, Cursor and Windsurf read the same guide.

## Reference

Deeper notes, kept out of this file so they load only when needed:

| Doc | Read it before |
|---|---|
| [docs/pumpfun-protocol.md](docs/pumpfun-protocol.md) | touching account layouts, instruction args, fee recipients or quote assets |
| [docs/listeners-and-geyser.md](docs/listeners-and-geyser.md) | changing a listener, the geyser stubs or a decoder |
| [docs/cookbook-conventions.md](docs/cookbook-conventions.md) | adding or changing anything under `cookbook/` |
| [docs/regression-tests.md](docs/regression-tests.md) | picking which verifiers your change needs |

## Ground rules

- **Running a bot with real funds is how a change gets verified**, with explicit
  approval for that session and after stating what will run and what it costs.
  `cookbook/` and the simulations move no funds but skip `bot_runner`, config
  loading, the trader loop, the exit strategy and cleanup, so they cannot close
  the question. A verification run covers every mode the change reaches — each
  listener, `extreme_fast_mode` on and off, each `exit_strategy` — and ends with
  the wallet holding SOL only.
- **Never** touch `.env` or print its contents. `SOLANA_PRIVATE_KEY` is a live key.
- Don't commit anything from `logs/`; test with a cookbook script before `src/`.

## Layout

```
src/                 bot source — this dir is the import root (see below)
cookbook/            standalone scripts; each runs on its own, no bot config
tests/regression/    one offline verifier per fixed bug; imports src/
tools/               dev harness — simulations, live round trips, benchmarks
bots/                one YAML per bot instance
idl/                 vendored Anchor IDLs
logs/                {bot_name}_{timestamp}.log
```

**Imports are rooted at `src/`, not at the repo.** `uv pip install -e .` puts
`src/` on `sys.path`, so it is `from core.client import SolanaClient`, not
`from src.core...`. Cookbook scripts don't import from `src` at all — don't
rewire one to; that is what `tools/` is for.

Dependency layers, low to high — don't introduce an upward import:

`interfaces` → `utils` → `core` → `platforms` → `monitoring` → `trading` → `bot_runner`

`interfaces` is the leaf; `geyser` holds only generated stubs; `cleanup` sits on
`core`/`utils` and is pulled in by `trading`.

Platform differences resolve through `interfaces/core.py` abstractions
(`AddressProvider`, curve manager, event parser, instruction builder) and the
registry in `platforms/__init__.py`. Listeners and the trader are
platform-agnostic (`Universal*`); anything platform-shaped belongs under
`platforms/<name>/`.

## Commands

```bash
uv sync                      # install runtime deps + the dev group (ruff)
uv pip install -e .          # editable install (required for the imports above)
pump_bot                     # run all enabled bots
uv run src/bot_runner.py     # same, without the console script
uv run tests/regression/run_all.py   # every offline verifier; moves no funds
```

Lint and format **the files you touched**, not the whole tree:

```bash
uv run ruff check --fix <paths> && uv run ruff format <paths>
```

A bare `uv run ruff check` reports a large backlog of pre-existing errors — the
known baseline, not a failing build and not something your change caused. Ruff
config is in `pyproject.toml`: line length 88, double quotes, target py311,
`E501` ignored. Type-hint public functions and use `get_logger(__name__)`.

**Docstrings and comments carry what the code cannot.** A one-line summary, then
only what a reader could not get from the signature: units, ranges, what `None`
means, which errors a caller must handle, a constraint that stops the next
person breaking it. An `Args:` entry that restates the parameter name, or a
comment that restates the line under it, is noise and gets deleted. Leave out
rationale nobody acts on, hypotheticals about what might change, measurements,
dates and issue numbers.

### Where the Solana libraries live

solana-py 0.40 moved most of them, and nothing in this class of break shows up
offline — an import sweep passes and the call fails against mainnet.
- `TxOpts` is `TxOptsModel`, and it, `MemcmpOpts` and `TokenAccountOpts` live in
  `solana.rpc.core`, not `solana.rpc.types`.
- spl-token's `*Params` (`BurnParams`, `CloseAccountParams`, …) are in
  `spl.token.models`; `spl.token.instructions` still holds the builders.
- Those are pydantic models: keyword-only and validating. `MemcmpOpts(bytes=...)`
  wants the base58 str a Pubkey's `str()` gives; raw `bytes` raises.
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
  RPC has already decoded the envelope by the time it emits the logs, and they
  read the same for every transaction version; the byte decode is the fallback.
- The bot **sends** v0 transactions (`MessageV0.try_compile` +
  `VersionedTransaction`, no lookup tables). Nothing builds a legacy `Message`.
- `post_rpc` must catch `asyncio.TimeoutError` alongside `aiohttp.ClientError`:
  aiohttp raises the former on a request timeout, it is not a `ClientError`, and
  `str()` on it is empty, so the caller logs a blank reason.
- `post_rpc` bounds attempts; `deadline_seconds` (default `None`) bounds wall
  time. Don't wrap a lookup in `asyncio.timeout` instead — cutting off an
  in-flight `getTransaction` and returning None conflates "can't see it" with
  "it failed".
- `build_and_send_transaction` returns a solders `Signature`, not a `str`.
  Normalize at the boundary: a `Signature` is neither JSON serializable nor
  sliceable, and solana-py's `confirm_transaction` rejects a `str`.
- **The endpoints carry their API key in the URL, so any log line holding one is
  a leak.** `install_secret_redaction` (`utils/logger.py`) masks the value at
  log-record creation, and every entry point calls it before attaching a handler.
  Redact the **rendered** message, not `record.msg` plus the string arguments —
  httpx2 passes its URL as a `URL` object, so a type check skips the one argument
  that matters.

**Buying**

- **Resolve a coin's quote mint before pricing or sizing anything.**
  `resolve_quote_token_program` returns the token program and caches the mint's
  decimals off the same read. Both unit helpers then raise rather than guess —
  `quote_units_per_token` in `src/`, `quote_units` in `cookbook/` — because a
  wrong power of ten inflates the price *and* the slippage cap in the same
  direction, so they compound into an overspend instead of cancelling. Only
  `cached_quote_units` answers `None` instead of raising, for the curve decoder,
  which runs against coins that are never traded; it leaves `price_per_token`
  unset and the caller's quote gate refuses the coin.
- `trade.curve_refresh_budget` (seconds, default 2.0) bounds the pre-buy curve
  read in `extreme_fast_mode`; when it expires the token is **skipped**, because
  a buy built from listener-guessed defaults reverts with `NotAuthorized` (6000),
  `ConstraintSeeds` (2006) or, on letsbonk, `AccountNotInitialized` (3012). The
  sell path keeps the opposite fallback — proceed with cached values — since
  skipping a sell strands the position.
- The refresh is skipped entirely when `TokenInfo.state_from_event` is set, i.e.
  the listener resolved creator/flags/quote_mint for itself. The CreateEvent
  parsers set it; instruction-parsed `TokenInfo` does not, because `args.creator`
  is user-supplied and may differ from the canonical `BC.creator`. The `shreds`
  listener sets the flag itself, having no curve to read.
  `trade.trust_create_event: false` forces the refresh back on; PumpPortal
  payloads always refresh.

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
  `ConfirmationStatus` (`SUCCESS` / `REVERTED` / `UNCONFIRMED`): a `REVERTED`
  retries, an `UNCONFIRMED` signature is re-checked first. A missing
  `failure_reason` means "unknown", never "reverted".
- `SUBMIT_FAILED` means "no transaction ever reached the chain" and nothing else
  — the one reason besides `REVERTED` that retries without asking the chain. A
  post-submission throw reports `UNCONFIRMED` with its signature.
- `confirm_transaction` and `verify_transaction_succeeded` deliberately stay
  bools. Returning the enum would be silent: every member is truthy, so every
  `if await client.confirm_transaction(sig):` would start passing unconditionally.
- `calculate_price` returns `0.0` for a curve with no virtual token reserves — it
  does not raise, and the seller rejects it before its own error handling. Never
  store a non-positive read as the last known price or floor a sell against one;
  `0.0` also satisfies the stop-loss comparison.

## Config notes

- Bot YAML supports `${VAR}` interpolation from the file named by `env_file`:
  `SOLANA_NODE_RPC_ENDPOINT`, `SOLANA_NODE_WSS_ENDPOINT`, `SOLANA_PRIVATE_KEY`,
  `GEYSER_*`.
- `src/config_loader.py` (package root, not under `core/`) validates the
  platform/listener pairing before startup: pump.fun supports `logs`, `blocks`,
  `geyser`, `shreds`, `pumpportal`; letsbonk.fun supports `blocks`, `geyser`,
  `pumpportal` — not `logs`, and not `shreds` (issue #201). Adding a listener
  means updating `PLATFORM_LISTENER_COMPATIBILITY` there too.
- **PumpPortal is a sampling feed, not a complete one, and not one launchpad.**
  It never sends a coin created in a transaction v1, and a bonk `create` carries
  no `name`, `symbol` or `uri` — so only `mint` and `traderPublicKey` are
  required, and `filters.match_string` can never match a bonk token. Each
  payload names its launchpad in `pool`; a consumer that ignores it counts bonk
  coins as pump.fun ones. The bonk trade path past
  detection is unverified (issue #201).
- Bots with `separate_process: true` run in their own process, one log file each.
- `pump_bot` runs **every** `bots/*.yaml`, and three of the four committed configs
  ship `enabled: false`. With all of them disabled it prints nothing and exits 0,
  indistinguishable from a clean run — check that `logs/<name>_<timestamp>.log`
  appeared before reading anything into a run.

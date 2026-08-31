<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/quickstart/">Blockchain API reference</a> • <br> 
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

A Solana trading bot for **pump.fun** and **letsbonk.fun**. Its core feature is sniping new tokens: it watches for token creation, buys, and exits on a strategy you configure. `learning-examples/` includes offline verifiers, read-only listeners, RPC simulations, and live transaction scripts; do not treat that directory as uniformly safe to run.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). It explains the concepts well but lags behind the code, so treat this README and the checked-in sample configs as the source of truth for setup and safety policy.

> **Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:
> - [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading, launching, and managing tokens on pump.fun; buy, sell, wallet management, and smart routing between the bonding curve and PumpSwap AMM.
> - [**pumpclaw**](https://github.com/chainstacklabs/pumpclaw) — agent skill that equips AI assistants (OpenClaw, Claude Code, Cursor, Codex) with the ability to operate pumpfun-cli.

---

**🚨 SCAM ALERT**: The Issues section is regularly targeted by scam bots that try to redirect you to an external site and drain your funds. A GitHub Action tags the common patterns, which is not 100% accurate. Deleted comments in issues are scam bots after your private keys — genuine outside devs are welcome and appreciated.

**⚠️ NOT FOR PRODUCTION**: This code is for learning purposes only. We assume no responsibility for the code or its usage. Modify it for your needs and learn from it — the examples, issues, and PRs contain valuable insights.

**Execution is fail-closed by default.** The sample bots are disabled and set
`execution.mode: "dry_run"`. Dry-run never authorizes transaction submission,
but it can still contact configured RPC/listener services and does not prove
that a later live transaction will succeed.
Live submission requires all of the following: one explicitly selected config,
`execution.mode: "live"`, an exact `expected_wallet`, raw-unit trade and fee
caps, and the separate `--authorize-live` runtime acknowledgement. Bulk startup
never authorizes live bots. This README intentionally provides no copy-paste live
command.

**Current live capability boundary:** the bot refuses live pump.fun bonding-curve
buys and sells because the current dynamic protocol and creator fee schedule is
not yet sourced into its executable quote. It fails before submission rather
than deriving a slippage minimum from a pre-fee quote. The pump.fun dry-run,
decoder, and offline verification paths remain available. LetsBonk execution is
limited to authoritative `blocks` or `geyser` events, funding-state constant
product pools, and the supported WSOL quote asset.

---

## Getting started

### 1. Prerequisites

Install [uv](https://github.com/astral-sh/uv), a fast Python package manager. The project needs **Python 3.11+**; `uv` uses an existing install if it's new enough, otherwise it fetches one for you.

### 2. Clone and install

```bash
git clone https://github.com/chainstacklabs/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot

uv sync                        # create .venv and install dependencies
source .venv/bin/activate      # Unix/macOS — Windows: .venv\Scripts\activate
uv pip install -e .            # install the bot as an editable package
```

### 3. Set your credentials

```bash
cp .env.example .env
```

Fill in `.env`. Use a dedicated, low-value wallet; never paste a seed phrase or
funded primary-wallet key. The key is still parsed to identify the wallet in
dry-run mode, but dry-run policy blocks transaction submission.

| Variable | Purpose |
|---|---|
| `SOLANA_NODE_RPC_ENDPOINT` | HTTPS RPC endpoint |
| `SOLANA_NODE_WSS_ENDPOINT` | WebSocket endpoint (for `logs` / `blocks` listeners) |
| `SOLANA_PRIVATE_KEY` | Base58 private key for a dedicated trading wallet; leave the template blank |
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN`, `GEYSER_AUTH_TYPE` | Only for the `geyser` listener |

Public RPC nodes will not work for this workload — see [throughput](#throughput-and-rate-limits) below.

### 4. Configure a bot

Each YAML file in `bots/` is one bot instance. Every checked-in sample has
`enabled: false`, `execution.mode: "dry_run"`, destructive cleanup disabled,
and live-only policy grants disabled. Start from the listener you want, but keep
those settings until you have reviewed the resolved config and wallet identity:

| File | Listener | Ships with |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported everywhere | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-4-pp.yaml` | `pumpportal` — third-party aggregator | `pump_fun` |

Set `platform: "pump_fun"` or `platform: "lets_bonk"`. pump.fun supports all
four listeners; letsbonk.fun supports `blocks` and `geyser`. PumpPortal's
LetsBonk payload lacks the authoritative pool/config/vault state required by
the executable path, so that pairing is rejected at startup rather than filled
with guessed accounts. The bot validates every pairing before startup.

`enabled: false` prevents startup. To exercise one sample without authorizing
fund movement, keep `execution.mode: "dry_run"`, set only that sample to
`enabled: true`, and select it explicitly:

```bash
uv run src/bot_runner.py --config bots/bot-sniper-2-logs.yaml
```

This command cannot authorize live submission: a config changed to `live`
fails unless the separate live acknowledgement flag is also present. Running
without `--config` scans all samples but still refuses live configs.

Logs land in `logs/{bot_name}_{timestamp}.log`.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`execution`** — defaults to `dry_run`. Live mode requires an exact
  `expected_wallet`, `max_trade_quote_raw`, and `max_total_fee_lamports` (all
  integer raw-unit caps), plus the separate `--authorize-live` acknowledgement.
  Current LetsBonk buy and sell callers always submit with `skip_preflight=True`,
  so `allow_skip_preflight: true` is mandatory for LetsBonk live execution; it
  is an explicit live-risk grant, not an optional latency setting.
  `allow_force_burn` should remain false unless destructive cleanup has been
  separately reviewed.
- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), and `extreme_fast_mode`, which skips the bonding-curve price check and buys a fixed token amount instead. Faster, less precise. See [Extreme fast mode](#extreme-fast-mode-zero-rpc-buys) for the zero-RPC behavior and its two knobs, `trust_create_event` and `curve_refresh_budget`.
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age`, name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`cleanup`** — defaults to `disabled`. `on_fail`, `after_sell`, and `post_session` may submit account-management transactions in authorized live mode. `force_close_with_burn` irreversibly destroys remaining tokens and is blocked unless both the cleanup request and `execution.allow_force_burn` are true.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

Positions are journaled under
`.state/positions/<wallet>-<platform>.json`; live transaction attempts use
`.state/transaction-ledgers/<wallet>-<platform>.sqlite3`. These paths are
relative to the working directory. Back them up and do not delete or share them
while a position or transaction outcome is unresolved: they prevent unsafe
re-execution and drive recovery after restart.

### Extreme fast mode: zero-RPC buys

With `extreme_fast_mode: true` the bot constructs a buy for a fixed token amount
(`extreme_fast_token_amount`) instead of fetching the curve price first. It does
not override execution policy: checked-in dry-run configs still cannot submit.

For pump.fun, zero-RPC preparation is limited to authoritative, correlated
**CreateEvent** observations. Normalized `geyser` and `blocks` transactions
retain `state_from_event` and `metadata_verified` only when the event matches
exactly one create instruction in the same successful transaction. Those
retained fields include the canonical creator, mayhem/cashback flags, and quote
mint, so instruction preparation needs no state RPC between detection and
submission. This is an offline-verifiable latency contract; it does not bypass
the pump.fun live fee-capability boundary above. That is the point of the mode;
a single account read costs ~40–50 ms even on a good endpoint, a tenth of a
slot.

`logsSubscribe` is deliberately **not** a zero-RPC source. Its notification has
logs but no transaction instructions with which to correlate the CreateEvent,
so normalized parser dispatch clears `state_from_event`, `metadata_verified`,
and event-only quote state. A token detected by the `logs` listener must refresh
curve state before a buy even when its logs contain a valid CreateEvent.

The `pumpportal` listener also cannot take the zero-RPC path because its payload
carries none of the authoritative curve state. It performs one batched account
read (bonding curve + mint in a single slot-consistent `getMultipleAccounts`)
before buying. If required curve state is not readable within
`trade.curve_refresh_budget` seconds (default 2.0), the token is **skipped**: a
buy built from guessed accounts reverts on-chain with `NotAuthorized` (6000) or
`ConstraintSeeds` (2006) and still costs the fee. The same refresh-and-skip rule
applies to incomplete, uncorrelated, or downgraded event data.

`trade.trust_create_event: false` turns the zero-RPC path off and forces the
pre-buy read even for verified `geyser` and `blocks` CreateEvents. This is the
more conservative fallback if pump.fun changes what the CreateEvent carries; it
still cannot guarantee a live transaction will succeed.

Machine checks: `learning-examples/verify_extreme_fast_zero_rpc.py` (the
zero-RPC contract per listener) and
`learning-examples/verify_pumpportal_buy_path.py` (the refresh/skip path).
Neither moves funds.

### Non-SOL quote assets

pump.fun v2 has verified metadata only for SOL/WSOL and USDC. Amounts are in
that mint's own whole units, so `usdc: 1.0` is one USDC and is **not**
comparable to `buy_amount`:

```yaml
trade:
  buy_amount: 0.0001    # SOL-paired coins
  quote_amounts:
    usdc: 1.0           # USDC-paired coins

filters:
  allowed_quote_mints: ["sol", "usdc"]
```

Use only the aliases `sol` / `wsol` / `usdc`. Although config parsing accepts a
raw base58 mint, execution rejects quotes without verified decimals and token
program metadata; a configured amount does not make an arbitrary quote asset
supported. A coin with no configured amount is skipped. USDC buys require USDC
plus SOL for fees and ATA rent. letsbonk.fun's current executable buy/sell path
supports WSOL quote only and refuses other quote mints.

## Learning examples

The examples are not one safety class. Offline `verify_*.py` scripts do not use
RPC or keys. Listener/decoder scripts may contact mainnet. `simulate_*.py`
scripts submit RPC simulations but no transactions; simulation can differ from
live execution. Files named `manual_buy*`, `manual_sell*`, `mint_and_buy*`, and
`cleanup_accounts.py` are live/account-mutating tools that can spend funds,
create or close accounts, or destroy assets. Never run them as a verification
step.

| Path | What it covers |
|---|---|
| `listen-new-tokens/` | One listener per method (`logs`, `blocks`, `geyser`, `pumpportal`) plus `compare_listeners.py` to race them |
| `listen-migrations/` | Detect a token graduating from the bonding curve to PumpSwap, via the migration wrapper program or new pool accounts |
| `bonding-curve-progress/` | Curve state, progress polling, and a live watch for coins close to graduating — over WebSocket (`get_graduating_tokens.py`) or Geyser (`get_graduating_tokens_geyser.py`), both taking `--min-progress` |
| `pumpswap/` | Manual **live** buy/sell against the PumpSwap AMM, and pool discovery |
| `letsbonk-buy-sell/` | Manual **live** exact-in / exact-out buys and sells on letsbonk.fun |
| `copy-trading/` | Watch another wallet's transactions |
| `manual_buy.py`, `manual_sell.py`, `fetch_price.py` | Live pump.fun trade tools plus a read-only price path. `manual_buy.py --cu-optimized` adds a `SetLoadedAccountsDataSizeLimit` instruction |
| `mint_and_buy_v2.py` | **Live:** create a coin and buy it in one transaction |
| `decode_from_*.py`, `calculate_discriminator.py` | Decoding account data, transactions, and Anchor discriminators |
| `cleanup_accounts.py` | **Live/account-mutating:** close eligible token accounts; review burn behavior before use |

Most of these take the mint or curve address as the first argument, and print usage if
you leave it off. The `decode_from_*.py` scripts fall back to the saved fixtures beside
them (`raw_*.json`), which are recaptured from mainnet rather than hand-edited — a
stale fixture makes a working decoder look broken and a broken one look fine.

Two offline verifiers and one RPC simulation are useful after a pump.fun
program upgrade:

```bash
uv run learning-examples/verify_v2_account_layout.py    # offline: account layouts, PDAs, encoding
uv run learning-examples/verify_tx_status_checks.py     # offline: examples check transaction meta.err
uv run learning-examples/simulate_v2_trades.py <MINT>   # RPC simulation only; not proof of live success
```

Related docs: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Throughput and rate limits

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up.

One case worth knowing about: `getProgramAccounts` over the whole pump.fun program is no longer served by anyone. That program owns more than 10 million accounts, so providers reject the request or time out no matter which filters you pass. Use a filtered subscription instead — `bonding-curve-progress/get_graduating_tokens.py` shows the pattern.

For Chainstack, the numbers you need are in the [throughput guidelines](https://docs.chainstack.com/docs/limits), kept up to date.

The bot rate-limits itself with a token bucket: `node.max_rps` in the YAML (25 by default) smooths the request rate while allowing short bursts, and 429s are retried with exponential backoff.

For faster execution, Chainstack offers [Solana Trader nodes](https://docs.chainstack.com/docs/trader-nodes) for transaction propagation and the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs under [`idl/`](idl/) are vendored from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) — currently upstream commit `9c82f61`. To refresh, copy `pump.json`, `pump_amm.json`, and `pump_fees.json` into `pump_fun_idl.json`, `pump_swap_idl.json`, and `pump_fees.json`, and note the upstream commit in your commit message. Don't hand-edit them.

The `buy_v2` / `sell_v2` account lists are complete in the IDL — that's the point of the v2 interface. The **legacy** `buy` / `sell` lists are not: the IDL omits PDAs the on-chain program requires. For anything outside v2, cross-check against a recent successful on-chain transaction before trusting the IDL.

[CLAUDE.md](CLAUDE.md) documents the protocol gotchas in detail — account layouts, quote-mint handling, fee recipients, and what the IDL gets wrong.

## Contributing

Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md). Open an **Issue** for feedback or bugs.

Lint and format the files you changed (`uv sync` installs `ruff` for you):

```bash
uv run ruff check --fix path/to/changed.py
uv run ruff format path/to/changed.py
```

Running `ruff check` over the whole repo reports a large backlog of pre-existing
errors — that's a known baseline, so scope it to your own files.

Then test your change with a learning example rather than by running a bot with real funds.

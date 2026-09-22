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

A Solana trading bot for **pump.fun** and **letsbonk.fun**. Its core feature is sniping new tokens: it watches for token creation, buys, and exits on a strategy you configure. [`cookbook/`](cookbook/) holds one standalone script per action — buy a coin, create one, watch for new ones, decode a transaction — useful on their own even if you never run the bot.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). It explains the concepts well but lags behind the code, so treat this README as the source of truth for setup and configuration.

> **Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:
> - [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading, launching, and managing tokens on pump.fun; buy, sell, wallet management, and smart routing between the bonding curve and PumpSwap AMM.
> - [**pumpclaw**](https://github.com/chainstacklabs/pumpclaw) — agent skill that equips AI assistants (OpenClaw, Claude Code, Cursor, Codex) with the ability to operate pumpfun-cli.

---

**🚨 SCAM ALERT**: The Issues section is regularly targeted by scam bots that try to redirect you to an external site and drain your funds. A GitHub Action tags the common patterns, which is not 100% accurate. Deleted comments in issues are scam bots after your private keys — genuine outside devs are welcome and appreciated.

**⚠️ NOT FOR PRODUCTION**: This code is for learning purposes only. We assume no responsibility for the code or its usage. Modify it for your needs and learn from it — the examples, issues, and PRs contain valuable insights.

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

Fill in `.env`:

| Variable | Purpose |
|---|---|
| `SOLANA_NODE_RPC_ENDPOINT` | HTTPS RPC endpoint |
| `SOLANA_NODE_WSS_ENDPOINT` | WebSocket endpoint (for `logs` / `blocks` listeners) |
| `SOLANA_PRIVATE_KEY` | Base58 private key of the trading wallet |
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN`, `GEYSER_AUTH_TYPE` | Only for the `geyser` listener |

Public RPC nodes will not work for this workload — see [throughput](#throughput-and-rate-limits) below.

### 4. Configure a bot

Each YAML file in `bots/` is one bot instance. They ship with commented defaults; start from the one matching the listener you want:

| File | Listener | Ships with |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported everywhere | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-4-pp.yaml` | `pumpportal` — third-party aggregator, misses some coins | `lets_bonk` |

Set `platform: "pump_fun"` or `platform: "lets_bonk"`. pump.fun supports all four listeners; letsbonk.fun supports `blocks`, `geyser`, and `pumpportal` but **not** `logs`. The bot validates the pairing at startup and refuses to run an invalid one.

`pumpportal` is a third-party feed and only reports what it indexes. As of 2026-09-16 it does not push coins whose creation landed in a Solana transaction v1 (a format live since 2026-09-15), so it sees a sample rather than everything. `geyser` and `logs` read the chain directly and are unaffected; `blocks` needs `maxSupportedTransactionVersion: 1`, which it now sends.

Set `enabled: false` to keep a config around without running it. Every bot with `enabled: true` starts when you run the bot.

### 5. Run

```bash
pump_bot                   # as an installed package
uv run src/bot_runner.py   # or directly
```

Logs land in `logs/{bot_name}_{timestamp}.log`.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), and `extreme_fast_mode`, which skips the bonding-curve price check and buys a fixed token amount instead. Faster, less precise. See [Extreme fast mode](#extreme-fast-mode-zero-rpc-buys) for the zero-RPC behavior and its two knobs, `trust_create_event` and `curve_refresh_budget`.
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age`, name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`cleanup`** — when to close leftover token accounts: `disabled`, `on_fail`, `after_sell`, `post_session`.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

### Extreme fast mode: zero-RPC buys

With `extreme_fast_mode: true` the bot buys a fixed token amount
(`extreme_fast_token_amount`) instead of fetching the curve price first. For
tokens detected through the on-chain **CreateEvent** — the `geyser`, `logs`
and `blocks` listeners — the buy is built entirely from the event: the
canonical creator, mayhem/cashback flags and quote mint are all in it, so
**no RPC call happens between detecting the token and submitting the buy**.
That is the point of the mode; a single account read costs ~40–50 ms even on
a good endpoint, a tenth of a slot.

The `pumpportal` listener can't do this — its payload carries none of those
fields — so it performs one batched account read (bonding curve + mint in a
single slot-consistent `getMultipleAccounts`) before buying. If the curve
isn't readable within `trade.curve_refresh_budget` seconds (default 2.0),
the token is **skipped**: a buy built from guessed accounts reverts on-chain
with `NotAuthorized` (6000) or `ConstraintSeeds` (2006) and still costs the
fee. The same skip applies to any token whose event data was incomplete.

`trade.trust_create_event: false` turns the zero-RPC path off and forces the
pre-buy read for every listener — the safe fallback if pump.fun changes what
the CreateEvent carries.

Machine checks: `tests/regression/verify_extreme_fast_zero_rpc.py` (the
zero-RPC contract per listener) and
`tests/regression/verify_pumpportal_buy_path.py` (the refresh/skip path).
Neither moves funds.

### Non-SOL quote assets

pump.fun supports quote assets other than SOL, USDC first. Amounts are in that mint's own whole units, so `usdc: 1.0` is one USDC and is **not** comparable to `buy_amount`:

```yaml
trade:
  buy_amount: 0.0001    # SOL-paired coins
  quote_amounts:
    usdc: 1.0           # USDC-paired coins

filters:
  allowed_quote_mints: ["sol", "usdc"]   # omit to allow any configured quote
```

Keys accept the aliases `sol` / `wsol` / `usdc` or a raw base58 mint. A coin whose quote mint has no configured amount is skipped with a log line rather than bought with a wrongly-scaled amount. SOL always falls back to `buy_amount`, so existing configs keep working untouched. Buying a USDC-paired coin needs USDC in the wallet plus a little SOL for fees and ATA rent.

## Cookbook

[`cookbook/`](cookbook/) is one script per thing you might want to do — buy a coin,
create one, watch for new ones, decode a transaction. Each runs on its own with
`uv run <path>`, reads `.env` directly, and needs no bot config.
[`cookbook/README.md`](cookbook/README.md) is the index; the shape is:

| Directory | What is in it |
|---|---|
| `cookbook/pumpfun/listen/` | One script per detection method — `logs`, `blocks`, `geyser`, `pumpportal` — plus wallet watching |
| `cookbook/pumpfun/read/` | Price, curve state, graduation progress, address derivation, balances, transaction status |
| `cookbook/pumpfun/trade/` | Buy, sell, create, and the two sniping variants |
| `cookbook/pumpfun/graduation/` | Coins approaching graduation, and migrations to PumpSwap |
| `cookbook/pumpfun/decode/` | Account data, transactions, and Anchor discriminators, against committed fixtures |
| `cookbook/pumpswap/` | Pool discovery and manual buy/sell on the AMM |
| `cookbook/letsbonk/` | Exact-in / exact-out buys and sells on letsbonk.fun |
| `cookbook/legacy/` | Instructions pump.fun has moved on from |

The quickest way in:

```bash
uv run cookbook/pumpfun/read/get_balances.py                 # what you hold
uv run cookbook/pumpfun/read/fetch_price.py <CURVE>          # what it costs
uv run cookbook/pumpfun/trade/buy_token.py <MINT> --dry-run  # the buy, simulated
```

Scripts that spend real funds say so on the first line of their docstring. The
`decode_from_*.py` scripts fall back to the fixtures beside them (`raw_*.json`),
which are recaptured from mainnet rather than hand-edited — a stale fixture makes a
working decoder look broken and a broken one look fine.

Related docs: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Regression checks

`tests/regression/` holds one offline script per bug that has been fixed here. They
import the bot's own code, move no funds, and each one's docstring names the bug it
guards. Run them after any pump.fun program upgrade or change to `src/`:

```bash
uv run tests/regression/run_all.py                      # the whole set
uv run tests/regression/verify_v2_account_layout.py     # account layouts, PDAs, encoding
uv run tests/regression/verify_curve_account_sizes.py   # 125/151/256-byte curves all decode
uv run tests/regression/verify_tx_status_checks.py      # every path reads meta.err
```

## Development tools

`tools/` holds the scripts used to exercise the bot rather than teach it. They import
`src/` and several of them spend real funds — read the module docstring first.

```bash
uv run tools/simulate_v2_trades.py <MINT>    # mainnet simulation, no funds moved
uv run tools/simulate_bot_buy_path.py        # the bot's buy path against a fresh coin, no funds moved
uv run tools/compare_listeners.py            # race all four listeners against each other
uv run tools/compare_migration_listeners.py  # race the two migration detection methods
uv run tools/cleanup_accounts.py [MINT]      # close leftover token accounts — submits transactions
uv run tools/live_v2_round_trip.py --yes     # real buy_v2 + sell_v2 — SPENDS REAL FUNDS
uv run tools/live_listener_matrix.py --yes   # real round trip per listener — SPENDS REAL FUNDS
```

## Throughput and rate limits

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up.

One case worth knowing about: `getProgramAccounts` over the whole pump.fun program is no longer served by anyone. That program owns more than 10 million accounts, so providers reject the request or time out no matter which filters you pass. Use a filtered subscription instead — `cookbook/pumpfun/graduation/get_graduating_tokens.py` shows the pattern.

For Chainstack, the numbers you need are in the [throughput guidelines](https://docs.chainstack.com/docs/limits), kept up to date.

The bot rate-limits itself with a token bucket: `node.max_rps` in the YAML (25 by default) smooths the request rate while allowing short bursts, and 429s are retried with exponential backoff.

For faster execution, Chainstack offers [Solana Trader nodes](https://docs.chainstack.com/docs/trader-nodes) for transaction propagation and the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs under [`idl/`](idl/) are vendored from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) — currently upstream commit `8109141`. To refresh, copy `pump.json`, `pump_amm.json`, and `pump_fees.json` into `pump_fun_idl.json`, `pump_swap_idl.json`, and `pump_fees.json`, and note the upstream commit in your commit message. Don't hand-edit them.

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

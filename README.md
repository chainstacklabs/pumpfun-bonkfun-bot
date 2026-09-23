<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/reference/blockchain-apis">Blockchain API reference</a> • <br> 
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

A Solana trading bot for **pump.fun** and **letsbonk.fun**. It watches for token creation, buys, and exits on a strategy you configure. [`cookbook/`](cookbook/) holds one standalone script per action, useful on their own even if you never run the bot.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). It lags behind the code, so treat this README as the source of truth for setup and configuration.

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
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN`, `GEYSER_AUTH_TYPE` | For the `geyser` and `shreds` listeners (same endpoint, different RPC) |

Public RPC nodes will not work for this workload — see [throughput](#throughput-and-rate-limits) below.

### 4. Configure a bot

Each YAML file in `bots/` is one bot instance. They ship with commented defaults; start from the one matching the listener you want:

| File | Listener | Ships with |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported everywhere | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-4-pp.yaml` | `pumpportal` — third-party aggregator, misses some coins | `lets_bonk` |
| `bot-sniper-5-shreds.yaml` | `shreds` — pre-execution, ahead of `geyser`, cannot see router-created coins | `pump_fun` |

Set `platform: "pump_fun"` or `platform: "lets_bonk"`. pump.fun supports all five listeners; letsbonk.fun supports `blocks`, `geyser`, and `pumpportal` but **not** `logs` or `shreds`. The bot validates the pairing at startup and refuses to run an invalid one.

`pumpportal` is a third-party feed and only reports what it indexes — it does not push coins whose creation landed in a Solana transaction v1, so it sees a sample rather than everything. `geyser`, `logs` and `blocks` read the chain directly and are unaffected.

Set `enabled: false` to keep a config around without running it. Every bot with `enabled: true` starts when you run the bot.

### 5. Run

```bash
pump_bot                   # as an installed package
uv run src/bot_runner.py   # or directly
```

Logs land in `logs/{bot_name}_{timestamp}.log`.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), and [`extreme_fast_mode`](#extreme-fast-mode).
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age`, name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`cleanup`** — when to close leftover token accounts: `disabled`, `on_fail`, `after_sell`, `post_session`.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

### Extreme fast mode

With `extreme_fast_mode: true` the bot buys a fixed token amount
(`extreme_fast_token_amount`) instead of reading the curve price first. You give
up knowing what you paid per token; you get the buy submitted sooner.

How much sooner depends on the listener. With `geyser`, `logs` or `blocks` the
bot makes **no RPC call at all between detecting the token and submitting the
buy**. `pumpportal` does one account read first, because its payload is missing
fields the buy needs.

Two knobs:

- **`curve_refresh_budget`** (seconds, default 2.0) — how long that read may
  take before the bot gives up and skips the token. Raise it to buy more coins
  on a slow endpoint, lower it to skip faster.
- **`trust_create_event`** (default `true`) — set `false` to make every listener
  do the read, giving up the zero-RPC path.

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

Keys accept the aliases `sol` / `wsol` / `usdc` or a raw base58 mint. A coin whose quote mint has no configured amount is skipped rather than bought with a wrongly-scaled amount, so a config that lists nothing here trades SOL-paired coins only. Buying a USDC-paired coin needs USDC in the wallet plus a little SOL for fees and ATA rent.

## Cookbook

[`cookbook/`](cookbook/) is one script per thing you might want to do. Each runs on
its own with `uv run <path>`, reads `.env` directly, and needs no bot config.
[`cookbook/README.md`](cookbook/README.md) is the index; the shape is:

| Directory | What is in it |
|---|---|
| `cookbook/pumpfun/listen/` | One script per detection method — `logs`, `blocks`, `geyser`, `pumpportal` — plus wallet watching |
| `cookbook/pumpfun/read/` | Price, curve state, graduation progress, address derivation |
| `cookbook/pumpfun/trade/` | Buy, sell, create, and the two sniping variants |
| `cookbook/pumpfun/graduation/` | Coins approaching graduation, and migrations to PumpSwap |
| `cookbook/pumpfun/decode/` | Account data and transactions, against committed fixtures |
| `cookbook/solana/` | Chain-level basics: balances, transaction status, Anchor discriminators |
| `cookbook/pumpswap/` | Pool discovery and manual buy/sell on the AMM |
| `cookbook/letsbonk/` | Exact-in / exact-out buys and sells on letsbonk.fun |
| `cookbook/legacy/` | Instructions pump.fun has moved on from |

The quickest way in:

```bash
uv run cookbook/solana/solana_read_balances.py               # what you hold
uv run cookbook/pumpfun/read/pumpfun_read_price.py <CURVE>   # what it costs
uv run cookbook/pumpfun/trade/pumpfun_buy_token_v2.py <MINT> --dry-run
```

A filename tells you the chain, the action and the instruction version before you
open it. Scripts that spend real funds say so on the first line of their docstring.
The decode scripts run with no arguments, against the fixtures beside them.

Related docs: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Regression checks

`tests/regression/` holds one offline script per fixed bug, each naming the bug in its
docstring. They move no funds. Run them after any change to `src/`, and after a
pump.fun program upgrade:

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

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up. For Chainstack, see the [throughput guidelines](https://docs.chainstack.com/docs/limits).

`getProgramAccounts` over the whole pump.fun program is no longer served by anyone: it owns more than 10 million accounts, so providers reject the request or time out whatever filters you pass. Use a filtered subscription instead — `cookbook/pumpfun/graduation/pumpfun_watch_graduating_programsubscribe.py` shows the pattern.

The bot rate-limits itself with a token bucket: `node.max_rps` (25 by default) smooths the request rate while allowing short bursts, and 429s are retried with exponential backoff.

For faster execution, Chainstack offers [Solana Trader nodes](https://docs.chainstack.com/docs/solana-trader-nodes) for transaction propagation and the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs under [`idl/`](idl/) are vendored from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) — currently upstream commit `8109141`. To refresh, copy `pump.json`, `pump_amm.json`, and `pump_fees.json` into `pump_fun_idl.json`, `pump_swap_idl.json`, and `pump_fees.json`, and note the upstream commit in your commit message. Don't hand-edit them.

The `buy_v2` / `sell_v2` account lists are complete in the IDL. The **legacy** `buy` / `sell` lists are not — the IDL omits PDAs the on-chain program requires, so cross-check anything outside v2 against a recent successful on-chain transaction.

[docs/pumpfun-protocol.md](docs/pumpfun-protocol.md) documents the protocol gotchas in detail — account layouts, quote-mint handling, fee recipients, and what the IDL gets wrong.

## Contributing

Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md). Open an **Issue** for feedback or bugs.

Lint and format the files you changed (`uv sync` installs `ruff` for you):

```bash
uv run ruff check --fix path/to/changed.py
uv run ruff format path/to/changed.py
```

Running `ruff check` over the whole repo reports a large backlog of pre-existing
errors — that's a known baseline, so scope it to your own files.

Then test your change with a cookbook script rather than by running a bot with real funds.

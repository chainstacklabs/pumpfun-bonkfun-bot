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

A Solana trading bot for **pump.fun** and **letsbonk.fun**. Its core feature is sniping new tokens: it watches for token creation, buys, and exits on a strategy you configure. `learning-examples/` contains standalone scripts covering every piece of the flow — listeners, price math, manual buys and sells — useful on their own even if you never run the bot.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot).

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
| `bot-sniper-4-pp.yaml` | `pumpportal` — third-party aggregator | `lets_bonk` |

Set `platform: "pump_fun"` or `platform: "lets_bonk"`. pump.fun supports all four listeners; letsbonk.fun supports `blocks`, `geyser`, and `pumpportal` but **not** `logs`. The bot validates the pairing at startup and refuses to run an invalid one.

Set `enabled: false` to keep a config around without running it. Every bot with `enabled: true` starts when you run the bot.

### 5. Run

```bash
pump_bot                   # as an installed package
uv run src/bot_runner.py   # or directly
```

Logs land in `logs/{bot_name}_{timestamp}.log`.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), and `extreme_fast_mode`, which skips the bonding-curve price check and buys a fixed token amount instead. Faster, less precise.
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age`, name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`cleanup`** — when to close leftover token accounts: `disabled`, `on_fail`, `after_sell`, `post_session`.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

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

## Learning examples

Standalone scripts, runnable with `uv run <path>`. No bot config needed — they read `.env` directly.

| Path | What it covers |
|---|---|
| `listen-new-tokens/` | One listener per method (`logs`, `blocks`, `geyser`, `pumpportal`) plus `compare_listeners.py` to race them |
| `listen-migrations/` | Detect a token graduating from the bonding curve to PumpSwap |
| `bonding-curve-progress/` | Curve state, progress polling, and tokens close to graduating |
| `pumpswap/` | Manual buy/sell against the PumpSwap AMM, and pool discovery |
| `letsbonk-buy-sell/` | Manual exact-in / exact-out buys and sells on letsbonk.fun |
| `copytrading/` | Watch another wallet's transactions |
| `manual_buy.py`, `manual_sell.py`, `fetch_price.py` | The minimal pump.fun trade and price path |
| `mint_and_buy_v2.py` | Create a coin and buy it in one go |
| `decode_from_*.py`, `calculate_discriminator.py` | Decoding account data, transactions, and Anchor discriminators |
| `cleanup_accounts.py` | Close leftover empty token accounts |

Two examples double as verification scripts to run after any pump.fun program upgrade:

```bash
uv run learning-examples/verify_v2_account_layout.py    # offline: account layouts, PDAs, encoding
uv run learning-examples/simulate_v2_trades.py <MINT>   # mainnet simulation, no funds moved
uv run learning-examples/verify_tx_status_checks.py     # offline: every example checks meta.err
```

Related docs: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Throughput and rate limits

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up.

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

---

**Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:

- [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading, launching, and managing tokens on pump.fun; buy, sell, wallet management, and smart routing between the bonding curve and PumpSwap AMM.
- [**pumpclaw**](https://github.com/chainstacklabs/pumpclaw) — agent skill that equips AI assistants (OpenClaw, Claude Code, Cursor, Codex) with the ability to operate pumpfun-cli.

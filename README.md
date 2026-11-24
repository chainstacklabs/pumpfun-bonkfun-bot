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

A Solana trading bot for **pump.fun** and for **letsbonk.fun**, **StonkFun** and every other launchpad on Raydium LaunchLab. It watches for new coins, buys them, and sells on a strategy you configure. [`cookbook/`](cookbook/) has one standalone script per action, useful even if you never run the bot.

For a step-by-step guide, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). Where the guide and this README differ, follow the README.

> **Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:
> - [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading on pump.fun and Raydium LaunchLab launchpads (letsbonk.fun, StonkFun) and launching pump.fun coins; routes each trade to the bonding curve, PumpSwap or Raydium CPMM.
> - [**pumpclaw**](https://github.com/chainstacklabs/pumpclaw) — agent skill that lets AI assistants (OpenClaw, Claude Code, Cursor, Codex) use pumpfun-cli.

---

**🚨 Scam alert:** scam bots post in Issues with links to sites that drain your wallet. Never share your private key, and don't follow links in issue comments. Deleted comments were scams; genuine contributors are welcome.

**⚠️ Not for production:** this code is for learning. We take no responsibility for the code or how it is used.

---

## Getting started

### 1. Prerequisites

Install [uv](https://github.com/astral-sh/uv), a fast Python package manager. The project needs **Python 3.11+**; uv uses your installed Python if it is new enough and downloads one otherwise.

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
| `SOLANA_NODE_WSS_ENDPOINT` | WebSocket endpoint, for the `logs` and `blocks` listeners |
| `SOLANA_PRIVATE_KEY` | Base58 private key of the trading wallet |
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN` | Geyser endpoint and token, for the `geyser` and `shreds` listeners; the bot takes the auth type from `geyser.auth_type` in its YAML |
| `KEYSTORE_PASSWORD` | Optional: password of an [encrypted keystore](#encrypted-keystore), if you use one instead of `SOLANA_PRIVATE_KEY` |

Public RPC endpoints can't keep up with the bot. See [Throughput and rate limits](#throughput-and-rate-limits).

### 4. Configure a bot

Each YAML file in `bots/` is one bot, and its comments explain the settings. Start from the one for the listener you want:

| File | Listener | Platform |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported by every provider | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-5-shreds.yaml` | `shreds` — sees coins before `geyser` does, but misses coins created through a router | `pump_fun` |
| `bot-sniper-6-stonkfun.yaml` | `geyser` | `stonk_fun` |

`platform` is `"pump_fun"`, `"launchlab"`, `"lets_bonk"` or `"stonk_fun"`. pump.fun works with all four listeners; the LaunchLab platforms work with `blocks`, `geyser` and `shreds`, not `logs`. The bot refuses an invalid pair at startup.

`pump_bot` runs every bot with `enabled: true`. Only `bot-sniper-1-geyser.yaml` ships enabled. Without a Geyser endpoint, disable it and enable another, or run one file with `--config`.

### 5. Run

Run from the folder that holds `bots/`:

```bash
pump_bot                                        # every enabled bot
pump_bot --config bots/bot-sniper-2-logs.yaml   # only this bot, even if it says enabled: false
pump_bot --verbose                              # every log line on the terminal, not only events
uv run src/bot_runner.py                        # same as pump_bot, without the installed command
```

Each bot logs its wallet address at startup. After that, the terminal shows one short colored line per event: a coin found, a buy, an exit, a sale with its profit or loss, a skipped coin, a warning. `--verbose` adds the detail (prices, confirmations, fees, cleanup). Set `NO_COLOR=1` to turn color off.

A bot that can't start (an invalid config, a wallet that won't unlock, an `rpc_endpoint` that doesn't answer) is skipped with one line saying why. Exit status: 0 when every enabled bot ran without an error, 1 otherwise, 130 after Ctrl+C and 143 after SIGTERM. SIGTERM stops the bots the same way as Ctrl+C.

## Output

The bot writes these files in the folder it runs from:

| Path | Contents |
|---|---|
| `logs/<bot name>_<timestamp>.log` | Every log line, in full |
| `trades/trades.log` | One JSON line per buy and sell |
| `trades/sessions.log` | One JSON line per session summary |
| `positions/<bot name>.json` | Open positions, [resumed on the next start](#positions-survive-a-restart) |

Main fields in `trades/trades.log`:

- `bot` — the bot that traded, so several bots can share the file (`null` for `sell-all` sales).
- `platform` — where the coin launched.
- `venue` — where the trade went: `bonding curve`, `PumpSwap` (pump.fun) or `Raydium CPMM` (LaunchLab).
- `price` — for a buy, the fill price that take profit and stop loss count from; for a sell, the price per token received.
- `quote_amount` — what the wallet received, negative when it spent, protocol fees included.
- `network_fee_sol`, `rent_deposit_sol`, `rent_refund_sol` — the transaction fee, and rent paid into or returned from token accounts.
- `pnl` — on a sell, that sale's realized profit or loss in the quote asset.
- `from_chain: false` — the transaction couldn't be read in time, so the figures are estimates.

Profit and loss come from each trade's transaction. Every sale logs its PnL, and the sale that empties a position logs `Final PnL`, which for a SOL quote includes network fees. Rent for the wallet's own token accounts doesn't count as a loss, since cleanup returns it. Rent for an account the wallet can't close, such as pump.fun's creator fee vault, does.

When a bot stops, it prints a session summary — trades, exit reasons, realized PnL per quote asset, fees, rent, and positions left for the next start — and appends it to `trades/sessions.log`.

## Configuration reference

The YAML files are commented inline. The main sections:

- **`private_key`** or **`keystore`** — the trading wallet: a raw key, or the path to an [encrypted keystore](#encrypted-keystore).
- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), [`entry`](#migration-entry), [`max_open_positions`](#several-positions-at-once) and [`extreme_fast_mode`](#extreme-fast-mode).
  - `min_wallet_balance` (SOL, optional) — below this balance the bot stops buying; open positions still exit. Buying resumes once the balance is back above it. The check never delays a buy.
- **`priority_fees`** — fixed or dynamic. Dynamic makes an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, name and creator matching (`match_string`, `bro_address`), `marry_mode` (buy only, never sell) and `yolo_mode` (keep trading instead of stopping after one coin).
  - `max_token_age` (checked once, in seconds since the bot detected the coin, not its age on chain). A slow listener lets older coins through; set `age_from: chain` to count from the slot the coin was created in instead. It adds no RPC call to the buy.
  - `checks` (optional) — more checks before a buy: built-in `mayhem` and `top_holders`, or your own. See [Pre-buy checks](docs/pre-buy-checks.md).
- **`retries`** — submission attempts, and the waits after creation, after a buy and before the next coin.
- **`timing.token_wait_timeout`** — with `yolo_mode: false` the bot trades one coin and exits; this is how long it waits for a coin that passes the filters. Default 120 seconds.
- **`cleanup`** — when to close token accounts and get their rent back: `disabled`, `on_fail` (after a failed buy), `after_sell` (once each coin is sold) or `post_session` (when the session ends).
  - `post_session` closes the accounts of the coins it traded, then every other empty token account in the wallet. It never closes quote-asset accounts (wSOL, USDC) or those of coins still in the positions file.
  - `force_close_with_burn` — burn the tokens left in a traded coin's account before closing it. `post_session` burns nothing under `exit_strategy: manual` or `marry_mode`, since those coins are meant to be held.
  - `burn_given_up_below` (SOL, optional; needs `after_sell` or `post_session`) — at the end of a session, burn a coin the bot gave up selling if it is worth less than this, close its account and drop it from the positions file. Only SOL-quoted coins are burned.
  - `with_priority_fee` (default `true`) — send closes and burns with the bot's priority fee.
- **`node.max_rps`** — requests per second; match it to your provider's plan. Default 25.

### Extreme fast mode

With `extreme_fast_mode: true`, the bot buys a fixed number of tokens (`extreme_fast_token_amount`) without reading the price first. The buy goes out sooner, but you don't know the price per token in advance.

Usually the bot then makes no RPC call between detecting a coin and sending the buy. If the listener couldn't decode everything the buy needs, the bot reads the curve once first:

- `curve_refresh_budget` (seconds, default 2.0) — how long that read may take before the bot skips the coin. Raise it to buy more coins on a slow endpoint; lower it to skip faster.
- `trust_create_event` (default `true`) — set `false` to always read the curve first.

A coin that has already graduated trades in its pool: PumpSwap for pump.fun, Raydium CPMM for LaunchLab. This mode skips such coins, because a pool buy is sized from the pool. With the mode off, the bot buys them there for exactly the configured amount.

### Migration entry

`trade.entry` sets what the bot buys:

- `new_launch` (default) — new coins, on their bonding curve.
- `migration` — coins as they graduate: a pump.fun coin in its new PumpSwap pool, a LaunchLab coin in its Raydium CPMM pool. LaunchLab coins that move to Raydium AMM v4 are not supported.

```yaml
trade:
  entry: "migration"
  extreme_fast_mode: false   # required: a pool buy is sized from the pool
```

Every listener the platform supports reports migrations. Filters, `quote_amounts` and exit strategies work as for launches, except:

- `match_string` checks the coin's on-chain metadata, and `bro_address` the creator its curve records.
- `max_token_age` is checked once, counting from when the bot detected the migration, not from the coin's creation.
- The bot waits for the new pool to be readable, then waits `wait_after_creation` and buys.
- A LaunchLab coin is bought only if its curve uses a platform config the bot trades.

### Several positions at once

By default the bot holds one coin at a time. With `yolo_mode: true`, `trade.max_open_positions` (1–100, default 1) lets it hold more:

```yaml
trade:
  max_open_positions: 3
```

Buys still go out one at a time, and each position then runs its own exit. While every slot is taken, new coins wait in a queue, and `max_token_age` decides whether they are still bought. Positions under `exit_strategy: manual` or `marry_mode` are never sold, so they don't take a slot. On shutdown, open positions stay in the wallet and resume on the next start.

### Positions survive a restart

The bot saves each position it monitors (`tp_sl` or `time_based`) to `positions/<bot name>.json` as soon as the buy is sent, and removes it once sold. On the next start it resumes them before taking new coins, under the current exit strategy (not under `manual` or `marry_mode`, which sell nothing): `max_hold_time` and `wait_after_buy` count from the original buy, take profit and stop loss from the entry price. A `time_based` position whose time ran out while the bot was down sells right away.

- **Pending buys.** A buy whose outcome is unknown, after a timeout or a crash, is saved as `pending` with its signature. The bot looks the signature up and keeps the position at what the transaction shows, or clears it if the buy failed.
- **The wallet decides what's left.** If the wallet no longer holds a coin (sold or burned by hand), the position is cleared, not sold again. If it holds less than was bought, the bot sells what it holds.
- **Given-up positions.** When a sale runs out of attempts, the position is marked `given_up` with the reason and listed in the session summary. It stays in the file and is tried again on the next start.
- **Other wallets and platforms.** Each position records the wallet and platform that bought it. A bot running with another wallet or `platform` leaves it in the file untouched. No coin in the file is bought again.

To stop the bot managing a coin, stop the bot and delete the coin's entry from the file.

The file is named after the bot's `name`. Give each bot its own name: bots that share one get separate files and a warning. If you rename a bot, rename its file too. Only one process can use a positions file at a time, so a bot whose file is in use doesn't start. A file the bot can't read is moved aside to `<name>.json.unreadable`.

### Check and close positions

```bash
pump_bot_positions status bots/bot-sniper-1-geyser.yaml
pump_bot_positions sell-all bots/bot-sniper-1-geyser.yaml
```

- `status` lists each position with its venue, amount, entry price, current price and unrealized PnL, and flags pending buys, given-up positions and positions from another wallet or platform. It only reads, so it can run while the bot runs and needs no keystore password.
- `sell-all` sells every position this wallet holds, except pending buys and positions from another platform, closes the emptied accounts and removes them from the file. It asks first unless you pass `--yes`, and won't run while a bot uses the file. It burns nothing: what it can't sell stays in the wallet and in the file, marked given up. Run it again to finish whatever a run left open.

If two bots in the folder share a name, pass the positions file with `--positions`; the other bot's file is `positions/<name>@<config file>.json`.

### Encrypted keystore

Instead of a raw `private_key`, a bot can trade from an encrypted keystore (scrypt and AES-256-GCM), so the key never sits on disk in plain text. Create one with `pump_bot_wallet`:

```bash
pump_bot_wallet create bots/wallet.enc     # a new keypair
pump_bot_wallet import bots/wallet.enc     # a key you have, typed at a hidden prompt
pump_bot_wallet import bots/wallet.enc --keypair-file ~/.config/solana/id.json
pump_bot_wallet show bots/wallet.enc       # print the public key
```

The password comes from `KEYSTORE_PASSWORD`, then `PUMPFUN_PASSWORD`, then a hidden prompt. The command doesn't read `.env`, so export a variable in your shell or use the prompt. The key is never taken as a command-line argument: use the prompt, `--keypair-file` or `--from-env VAR`. An existing file is replaced only with `--force`. Back up the file and the password: losing either loses the wallet.

Then replace `private_key` in the bot config with the file's path, and set `KEYSTORE_PASSWORD` in the bot's `env_file`:

```yaml
keystore: "./wallet.enc"
```

A relative path is looked up next to the bot config first, then in the folder you run the bot from. The bot unlocks the file at startup and doesn't start if it can't.

pumpfun-cli and pumpclaw use the same file, so one wallet can serve all three, for example with `keystore: "~/.config/pumpfun-cli/wallet.enc"`. The CLI reads only `PUMPFUN_PASSWORD`, so for a shared wallet set that variable.

### Non-SOL quote assets

pump.fun supports quote assets other than SOL, USDC first. Amounts are in that asset's own units, so `usdc: 1.0` is one USDC and is **not** comparable to `buy_amount`:

```yaml
trade:
  buy_amount: 0.0001    # SOL-paired coins
  quote_amounts:
    usdc: 1.0           # USDC-paired coins

filters:
  allowed_quote_mints: ["sol", "usdc"]   # omit to allow any configured quote
```

Keys accept `sol`, `wsol`, `usdc` or a raw base58 mint. A coin whose quote asset has no amount here is skipped, so with no entries the bot trades SOL-paired coins only. Buying a USDC-paired coin needs USDC in the wallet, plus some SOL for fees and token account rent.

### LaunchLab launchpads

letsbonk.fun, StonkFun and many smaller launchpads run on one program, Raydium LaunchLab; each launchpad is a set of *platform configs*. `platform: "launchlab"` buys launches under any of them, and `lets_bonk` and `stonk_fun` keep to their own. To pick launchpads, list their platform configs:

```yaml
platform: "launchlab"
launchlab:
  platform_configs:                 # omit to take every launchpad
    - "5thqcDwKp5QQ8US4XRMoseGeGbmLKMmoKZmS6zHrQAsA"   # letsbonk.fun
    - "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"   # StonkFun reward coins
```

On `lets_bonk` or `stonk_fun` the list can only narrow the preset. Without a list, the listener sees every trade on the program, not just launches. `trades/trades.log` names each coin's platform config. Only constant-product curves are bought; fixed-price and linear launches are skipped.

### StonkFun

StonkFun coins are Raydium LaunchLab pools, and most are paired with tokenized stocks (xStocks) rather than SOL. To buy those, add the stock's mint to `quote_amounts` with an amount in its own units and hold that asset in the wallet; sells pay out in it. Without an entry the bot buys only SOL-paired StonkFun coins.

*Reward* coins charge a Token-2022 transfer fee of 1% or 3% on every transfer, the buy and the sell included. The bot sets its slippage limits net of that fee and of the curve fee. Set `filters.max_transfer_fee_bps` to skip coins above a fee rate (`0` keeps to standard coins only). How LaunchLab prices and settles a trade is in [docs/launchlab-protocol.md](docs/launchlab-protocol.md).

## Cookbook

[`cookbook/`](cookbook/) has one script per thing you might want to do. Each runs on its own with `uv run <path>`, reads `.env` directly, and needs no bot config. [`cookbook/README.md`](cookbook/README.md) is the index:

| Directory | What is in it |
|---|---|
| `cookbook/pumpfun/listen/` | One script per detection method — `logs`, `blocks`, `geyser`, `shreds` — plus wallet watching |
| `cookbook/pumpfun/read/` | Price, curve state, graduation progress, address derivation |
| `cookbook/pumpfun/trade/` | Buy, sell, create, and the two sniping variants |
| `cookbook/pumpfun/graduation/` | Coins approaching graduation, and migrations to PumpSwap |
| `cookbook/pumpfun/decode/` | Account data and transactions, against committed fixtures |
| `cookbook/solana/` | Chain-level basics: balances, transaction status, Anchor discriminators |
| `cookbook/pumpswap/` | Pool discovery and manual buy/sell on the AMM |
| `cookbook/letsbonk/` | Exact-in / exact-out buys and sells on letsbonk.fun |
| `cookbook/stonkfun/` | New StonkFun coins over Geyser and `blockSubscribe`, pool state, and buys and sells in any quote asset |
| `cookbook/legacy/` | Instructions pump.fun has moved on from |

To start:

```bash
uv run cookbook/solana/solana_read_balances.py               # what you hold
uv run cookbook/pumpfun/read/pumpfun_read_price.py <CURVE>   # what it costs
uv run cookbook/pumpfun/trade/pumpfun_buy_token.py <MINT> --dry-run
```

Trade scripts send real transactions; read a script's docstring before you run it. The decode scripts run with no arguments, against the fixtures beside them.

Related guides: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Regression checks

`tests/regression/` has one offline script per fixed bug, each naming the bug in its docstring. They move no funds. Run them after any change to `src/`, and after a pump.fun program upgrade:

```bash
uv run tests/regression/run_all.py                      # the whole set
uv run tests/regression/verify_v2_account_layout.py     # account layouts, PDAs, encoding
uv run tests/regression/verify_curve_account_sizes.py   # 125/151/256-byte curves all decode
uv run tests/regression/verify_tx_status_checks.py      # every path reads meta.err
```

## Development tools

`tools/` has the scripts used to exercise the bot. They import `src/`, and several spend real funds, so read the module docstring first.

```bash
uv run tools/simulate_v2_trades.py <MINT>    # mainnet simulation, no funds moved
uv run tools/simulate_bot_buy_path.py        # the bot's buy path against a fresh coin, no funds moved
uv run tools/compare_listeners.py            # race the pump.fun listeners against each other
uv run tools/compare_migration_listeners.py  # race the two migration detection methods
uv run tools/cleanup_accounts.py <MINT>      # close a leftover token account — submits a transaction
uv run tools/launchlab_create_coin.py --platform-config <ADDRESS>  # LaunchLab launch on a local validator (surfpool)
uv run tools/live_v2_round_trip.py --yes     # real buy_v2 + sell_v2 — SPENDS REAL FUNDS
uv run tools/live_listener_matrix.py --yes   # real round trip per listener — SPENDS REAL FUNDS
```

## Throughput and rate limits

Every node provider has its own limits: available methods, requests per second, plan caps. Check yours before running the bot; public RPC endpoints won't keep up. For Chainstack, see the [throughput guidelines](https://docs.chainstack.com/docs/limits).

The bot caps its own request rate at `node.max_rps` (default 25), allowing short bursts, and retries HTTP 429 responses with exponential backoff.

No provider serves `getProgramAccounts` over the whole pump.fun program anymore; it owns too many accounts. Use a filtered subscription instead, as `cookbook/pumpfun/graduation/pumpfun_watch_graduating_programsubscribe.py` does.

For faster execution, a Chainstack Global Node with the [Warp transactions](https://docs.chainstack.com/docs/warp-transactions) add-on sends transactions through bloXroute's staked relay, and Chainstack offers the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs in [`idl/`](idl/) are vendored; don't hand-edit them.

- `pump_fun_idl.json`, `pump_swap_idl.json`, `pump_fees.json` — `pump.json`, `pump_amm.json` and `pump_fees.json` from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs), upstream commit `8109141`. To refresh, copy them over and note the upstream commit in your commit message.
- `raydium_launchlab_idl.json` — LaunchLab's on-chain IDL account.
- `raydium_cpmm_idl.json` — `raydium_cpmm/raydium_cp_swap.json` from [raydium-io/raydium-idl](https://github.com/raydium-io/raydium-idl), commit `e7e0c96`; used only to name Raydium CPMM errors.

The `buy_v2` / `sell_v2` account lists in the IDL are complete. The **legacy** `buy` / `sell` lists miss accounts the program requires, so check anything outside v2 against a recent successful transaction. [docs/pumpfun-protocol.md](docs/pumpfun-protocol.md) covers this and the other protocol details: account layouts, quote mints and fee recipients.

## Contributing

Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md). Open an issue for feedback or bugs.

Lint and format only the files you changed; the repo has a known backlog of older `ruff` errors. `uv sync` installs `ruff`.

```bash
uv run ruff check --fix path/to/changed.py
uv run ruff format path/to/changed.py
```

Test your change with a cookbook script before you run a bot with real funds.

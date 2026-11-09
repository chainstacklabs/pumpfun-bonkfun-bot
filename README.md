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

A Solana trading bot for **pump.fun** and for **letsbonk.fun**, **StonkFun** and every other launchpad on Raydium LaunchLab. It watches for token creation, buys, and exits on a strategy you configure. [`cookbook/`](cookbook/) holds one standalone script per action, useful on their own even if you never run the bot.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). It lags behind the code, so treat this README as the source of truth for setup and configuration.

> **Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:
> - [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading on pump.fun and Raydium LaunchLab launchpads (letsbonk.fun, StonkFun) and launching pump.fun coins; routes each trade to the bonding curve, PumpSwap or Raydium CPMM.
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
| `KEYSTORE_PASSWORD` | Password of the [encrypted keystore](#encrypted-keystore), when the bot trades from one instead; the bot also tries `PUMPFUN_PASSWORD`, the variable pumpfun-cli reads |
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN`, `GEYSER_AUTH_TYPE` | For the `geyser` and `shreds` listeners (same endpoint, different RPC) |

Public RPC nodes will not work for this workload — see [throughput](#throughput-and-rate-limits) below.

### 4. Configure a bot

Each YAML file in `bots/` is one bot instance. They ship with commented defaults; start from the one matching the listener you want:

| File | Listener | Ships with |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported everywhere | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-5-shreds.yaml` | `shreds` — pre-execution, ahead of `geyser`, cannot see router-created coins | `pump_fun` |
| `bot-sniper-6-stonkfun.yaml` | `geyser` | `stonk_fun`, `enabled: false` |

Set `platform` to `"pump_fun"`, `"launchlab"`, `"lets_bonk"` or `"stonk_fun"`. pump.fun supports all four listeners; the LaunchLab platforms support `blocks`, `geyser` and `shreds` but **not** `logs`. The bot validates the pairing at startup and refuses to run an invalid one.

Set `enabled: false` to keep a config around without running it; such a file is skipped before its settings are read, so unset `${VAR}` values and invalid settings in it do not stop the run, but its YAML must still parse. Every bot with `enabled: true` starts when you run the bot.

### 5. Run

```bash
pump_bot                   # as an installed package
pump_bot --verbose         # every log line on the terminal, not only events
pump_bot --config bots/bot-sniper-2-logs.yaml   # only this bot, even if it says enabled: false
uv run src/bot_runner.py   # or directly
```

Each bot logs the address of the wallet it trades from when it starts, whether the config gives `private_key` or `keystore`.

Logs land in `logs/{bot_name}_{timestamp}.log`, every line in full.

On a terminal each line is the time, the bot's name and the message, coloured by phase: cyan when a coin is found, green for a buy, purple for a sale, yellow when an exit triggers, and the profit or loss in green or red. Addresses are cut to their ends (`DGAify…pump`) and a line is cut to the window; a warning or an error is never cut. By default only events print: a coin found, a buy, an exit, a sale and its profit or loss, a skipped coin, every warning. `--verbose` adds the detail (prices, confirmations, fees, cleanup), dimmed. Piped into a file or another program, every line prints in the log file's format, and `NO_COLOR=1` turns colour off.

Run it from the folder that holds `bots/`: it reads `bots/*.yaml` there and writes `logs/`, `trades/` and `positions/` beside it. `pump_bot --help` says this and starts nothing.

Profit and loss come from each trade's transaction. `trades/trades.log` has one JSON line per buy and sell. A row's `platform` is where the coin launched and its `venue` where the trade went: `bonding curve`, or the pool a graduated coin trades in, `PumpSwap` (pump.fun) or `Raydium CPMM` (LaunchLab). A row's `quote_amount` is what the wallet received, negative when it spent, protocol fees included. Its `network_fee_sol` is the transaction fee, and `rent_deposit_sol` and `rent_refund_sol` are rent put into or taken back from the wallet's token accounts. A buy's `price` is its fill, the price take profit and stop loss count from; a sell's is what it paid per token. A sell row also carries `pnl`, that sale's realised profit or loss in the quote asset. Every sale logs its PnL, and the line for the sale that empties the position is `Final PnL`; for a SOL quote it includes both network fees, and for any other quote the SOL it cost is shown beside it. Rent for the wallet's own token accounts is never counted as a loss: it comes back when cleanup closes the account, in a transaction of its own that this figure leaves out. Rent for an account the wallet can't close is a cost of the trade (see `cleanup` below). When a transaction can't be read in time, its row has `from_chain: false` and the `Final PnL` line says the figure is an estimate. Each row's `bot` names the bot that made it, so bots run from one folder can share the file.

When a bot stops it prints a session summary: buys and sales, what triggered each exit, the realised PnL by quote asset (estimated and unpriced sales named apart), network fees, rent put into token accounts and rent back from trades and closes, and the positions and pending buys left in the positions file for the next start. The same summary is appended as one JSON line to `trades/sessions.log`, so sessions can be compared.

A bot that can't run is not started, with one line saying why: a config that fails validation, a wallet that won't unlock, or an `rpc_endpoint` that doesn't answer or rejects the request. With every bot disabled the bot says so. The exit status is 0 only when every enabled bot started and finished without an error, 1 otherwise, 130 after Ctrl+C and 143 after SIGTERM, which stops the bots the same way. Ctrl+C reaches a bot in its own process through the process group; SIGTERM sent to the main process alone is passed on to each of them.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`private_key`** or **`keystore`** — the trading wallet: a raw key, or the path of an [encrypted keystore](#encrypted-keystore).
- **`trade`** — `buy_amount` (in SOL), slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), [`entry`](#migration-entry), [`max_open_positions`](#several-positions-at-once) and [`extreme_fast_mode`](#extreme-fast-mode).
  - `min_wallet_balance` (SOL, optional): below it the bot buys nothing more and logs one warning; positions it holds still run to their exit. The balance is read at start and after each buy is sent, never before a buy; while it is low it is read again at most every 10 seconds, and buying resumes once a sale brings it back above.
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age` (checked once, when the bot takes a coin up: seconds since it detected the coin, not its age on chain, so a slow listener lets older coins through; set `age_from: chain` to count a launch's age from the slot its create landed in instead, estimated from the newest slot seen, the node's (read every 2 seconds in the background) or the listener's own, so the buy makes no extra call; a coin whose slot the listener did not give still counts from detection, and migrations always do), name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
  - `checks` (optional): more checks a coin must pass before the buy, built-in (`mayhem`, `top_holders`) or your own class. See [Pre-buy checks](docs/pre-buy-checks.md).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`timing.token_wait_timeout`** — with `yolo_mode: false` the bot trades one coin and exits; this is how long it waits for a coin the filters accept (a migration, with `entry: migration`) before exiting without one. Default 120 seconds.
- **`cleanup`** — when to close leftover token accounts: `disabled`, `on_fail`, `after_sell`, `post_session`.
  - `after_sell` closes each coin's account once it is sold, and the empty account of a position cleared because the wallet no longer holds its coin (sold by hand while the bot was down).
  - `post_session` closes, when the session ends, the accounts of the coins it traded, then every other empty token account in the wallet, under SPL Token and Token-2022. It never closes a quote mint's account (wSOL, USDC, any configured quote), or that of a coin still in the positions file, pending buys included: those positions are kept for the next start (see [Positions survive a restart](#positions-survive-a-restart)), and their accounts are cleaned up at the end of the session that sells them.
  - `force_close_with_burn` burns what is left in a traded coin's account before closing it. It never burns anything in the other accounts `post_session` closes, and under `exit_strategy: manual` or `marry_mode` post-session cleanup burns nothing, since those coins are meant to be held.
  - Closes and burns go out with the bot's priority fee unless `with_priority_fee` is `false` (it must be `true` or `false`; left out, it is `true`). A close is sent again once the block height is past its blockhash's last valid height and its signature is not known to have landed (no node knows it, or the lookup failed; one seen but not yet confirmed is waited on), at most 3 attempts in total; then the bot logs one warning naming `tools/cleanup_accounts.py`.
  - Cleanup closes only token accounts the wallet owns. A buy can also pay rent for an account another program owns, such as pump.fun's creator fee vault for a coin's quote asset the first time that vault is needed. The wallet can never close such an account, so its rent is a one-time cost of the trade that no cleanup mode returns. The trade's figures count it: in `quote_amount` for a SOL quote, and among the SOL costs shown beside the PnL for any other quote.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

### Extreme fast mode

With `extreme_fast_mode: true` the bot buys a fixed token amount
(`extreme_fast_token_amount`) instead of reading the curve price first. You give
up knowing what you paid per token; you get the buy submitted sooner.

How much sooner depends on what the listener could decode. When it read the
coin's `CreateEvent`, the bot makes **no RPC call at all between detecting the
token and submitting the buy**. When it fell back to decoding the create
instruction, it does one account read first, because the instruction is missing
fields the buy needs.

Two knobs:

- **`curve_refresh_budget`** (seconds, default 2.0) — how long that read may
  take before the bot gives up and skips the token. Raise it to buy more coins
  on a slow endpoint, lower it to skip faster.
- **`trust_create_event`** (default `true`) — set `false` to make every listener
  do the read, giving up the zero-RPC path.

A coin whose curve has already graduated trades in the pool it migrated to: a
pump.fun coin in PumpSwap, a LaunchLab coin in Raydium CPMM. Outside this mode
the bot buys it there, spending exactly the configured amount; in this mode it
skips the coin, since a pool buy is sized from the pool.

### Migration entry

`trade.entry` picks what the bot buys. `new_launch` (the default) buys coins as
they launch, on their curve. `migration` buys a coin when its curve migrates: a
pump.fun coin in its new PumpSwap pool, a LaunchLab coin in its Raydium CPMM pool.

```yaml
trade:
  entry: "migration"
  extreme_fast_mode: false   # required: a pool buy is sized from the pool
```

Every listener the platform supports reports migrations. The filters,
`quote_amounts` and the exit strategies apply as for launches, with two
differences: `match_string` is checked against the coin's on-chain metadata, and
`bro_address` against the creator its curve records. A LaunchLab coin is bought
only when its curve is under a platform config the bot trades, not just the
migration naming one. `max_token_age` is checked once, when the bot takes the
migration up, in single-token mode too: it counts from the moment the bot
detected the migration, not from the coin's creation, and the waits for the
coin's mint and pool that follow are not counted. `wait_after_creation` is
waited before the buy. The bot buys once the pool is readable, waiting up to 10
seconds for it; `shreds` sees a migration before it runs, so its pool does not
exist yet.
`extreme_fast_mode: true` with `entry: migration` is refused at startup.

### Several positions at once

By default the bot holds one coin at a time: it buys, waits for the exit, and only
then takes the next coin. With `yolo_mode: true`, `trade.max_open_positions`
(1 to 100, default 1) lets it hold more:

```yaml
trade:
  max_open_positions: 3
```

Coins are still bought one after another. Each position then runs its exit
strategy on its own, and the bot keeps taking coins until that many are open;
the next one is taken when a position closes. A coin detected while every slot is
taken waits in the queue, so `max_token_age` decides whether it is still bought.
Positions under `exit_strategy: manual` or `marry_mode` are never sold, so they
take no slot. On shutdown the bot stops monitoring the open positions before
cleanup runs; their tokens stay in the wallet, `post_session` cleanup does not
burn or close their accounts, and the next start resumes them. A position sold
just before the shutdown still closes its empty account (`after_sell`) before the
bot exits.

### Positions survive a restart

Each position the bot monitors (`tp_sl` or `time_based`) is written to
`positions/<bot name>.json` as soon as its buy may have reached the network,
with a `pending` entry and the buy's signature until the buy is resolved, and
removed once it is sold. A buy that reverts, or provably never left the bot (no
connection, or the node refused it), leaves nothing; one whose send failed any
other way, a timeout say, may still have landed and is kept. The bot resolves a
pending buy by its signature, at the next start if it was stopped or killed
while the buy was in flight, and in the same run when its confirmation never
came: a buy that landed is held at the amount and price its transaction shows,
as far as the wallet still holds it (none held clears it, as for any position),
one that reverted is cleared, and one no node knows is looked at again until its
blockhash has expired. Then the wallet's balance decides: none held clears it,
and a balance is held up to what the buy can have delivered, so a coin another
bot holds in the same wallet is not sold with it. A position whose fill price
could not be read takes profit and loss from the first price it reads, and runs
no exit before then. A buy that may have landed is never cleaned up as a failed
buy, under any exit strategy. On
start the bot reads that file and resumes each position before it takes a new
coin, with the bot's current exit strategy: `max_hold_time` and `wait_after_buy`
count from the original buy, take profit and stop loss from its entry price.
Resumed positions take slots like any other. If the file holds more than
`max_open_positions` (the limit was lowered), all of them are resumed, since
their coins are already in the wallet, and no new coin is taken until the count
is below the limit again. A resumed `time_based` position whose wait has run out
sells at once, priced at the market rather than at its old buy price.

The wallet decides what is left to sell. A position whose coin the wallet no
longer holds (sold or burned by hand) is cleared, not sold again, and its empty
account is closed (`after_sell` at once, `post_session` at the end of the session); one holding
less than was bought sells what is held. The bot clears a position only when three balance
reads, a second apart, all find nothing, since an RPC node lagging behind may
not show a fresh buy yet. A position whose balance cannot be read stays in the
file and is tried again on the next start, and so does one whose exit gave up.
Each position names the wallet that bought it: a bot started with another
wallet leaves it in the file, neither sold nor cleared, until that wallet trades
again. Entries written before positions named their wallet resume for any
wallet. Delete an entry from the file to stop the bot managing that coin.

The file is named after the bot's `name`, with any character other than letters,
digits, `.`, `_` and `-` replaced by `_`. Names that differ only in case share a
file, as they do on macOS and Windows. Enabled bots in `bots/` that would share
a file all start: the one whose config file name sorts first keeps
`positions/<name>.json`, each other gets `positions/<name>@<config file>.json`
(with `-2`, `-3`, ... added if two config file names map to one file) and a
warning. So a bot keeps its file when a second bot with its name is added in a
config file that sorts after it. If the plain file holds positions when a bot
does not get it, a second warning names the file: that bot does not manage
those positions. Give each bot its own name to avoid this. Renaming a bot
leaves its old file behind; rename the file too. A position bought on another
`platform` than the bot's current one stays in the file and is not monitored.
No coin in the file is bought again, monitored or not. A file the bot cannot
read is moved aside to `<name>.json.unreadable`.

### Encrypted keystore

Instead of a raw `private_key`, a bot can trade from an encrypted keystore: the
keypair sealed under a password with scrypt and AES-256-GCM, so the key never
sits on disk in clear. `pump_bot_wallet` makes one:

```bash
pump_bot_wallet create bots/wallet.enc     # a new keypair
pump_bot_wallet import bots/wallet.enc     # a key you have: asks for it, hidden
pump_bot_wallet import bots/wallet.enc --keypair-file ~/.config/solana/id.json
pump_bot_wallet show bots/wallet.enc       # the public key, after unlocking
```

Each prints only the wallet's public key; notes, such as which variable gave
the password, go to stderr. A relative path is taken from the folder you run
the command in. `import` takes the key as base58 or as a Solana CLI keypair
file, never on the command line: an argument that looks like a key or seed is
refused. `--from-env VAR` reads the key from a variable exported in your shell
instead (the command does not read `.env`); the hidden prompt keeps the key out
of your shell's environment and history. An existing file is replaced only with
`--force`, and the refusal names the key it holds. The file is written readable
only by you. Back up the file and the password: losing either loses the wallet.

The password comes from `KEYSTORE_PASSWORD`, else `PUMPFUN_PASSWORD`, else a
hidden prompt that asks twice. `create` and `import` refuse the two variables
set to different values, since a new file has one password, and warn about a
password shorter than 8 characters.

Then replace the bot config's `private_key` line with the file's path:

```yaml
keystore: "./wallet.enc"
```

and put the password in the bot's `env_file` as `KEYSTORE_PASSWORD`. The bot
tries `KEYSTORE_PASSWORD` first and, when it is unset or does not unlock the
file, `PUMPFUN_PASSWORD`. Every bot's `env_file` is loaded into one environment,
so with several bots either variable may hold another bot's password; trying
both lets each bot find its own. `pump_bot_wallet show` tries them in the same
order. A relative path is looked up beside the bot config first, then in the
folder you run the bot from, and `~` expands to your home folder. A config may
set `private_key` or `keystore`, not both. The bot unlocks the file at startup;
a missing password, one that neither variable gets right, or a file that is
not a keystore stops that bot before it trades, with a log line saying which.

pumpfun-cli and pumpclaw read and write the same file, so one wallet can serve
all three, for example with `keystore: "~/.config/pumpfun-cli/wallet.enc"`. The
CLI reads only `PUMPFUN_PASSWORD`, so for a shared wallet set that variable, or
both variables to the same value.

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

### LaunchLab launchpads

letsbonk.fun, StonkFun and many smaller launchpads are pools on one program, Raydium LaunchLab; each launchpad is a set of *platform configs*. `platform: "launchlab"` snipes launches under any of them, and `lets_bonk` / `stonk_fun` keep to their own. To pick launchpads, list their platform configs:

```yaml
platform: "launchlab"
launchlab:
  platform_configs:                 # omit to take every launchpad
    - "5thqcDwKp5QQ8US4XRMoseGeGbmLKMmoKZmS6zHrQAsA"   # letsbonk.fun
    - "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"   # StonkFun reward coins
```

On `stonk_fun` or `lets_bonk` the list can only narrow the preset. Without a list, the listener subscribes on the whole program and sees every LaunchLab trade, not just launches. `trades/trades.log` names each coin's platform config. Only constant-product curves are bought; a fixed-price or linear launch is ignored.

### StonkFun

StonkFun coins are Raydium LaunchLab pools, the same program letsbonk.fun runs on, and most are paired with tokenized stocks (xStocks) rather than SOL. The same `quote_amounts` rules apply: list a raw mint with an amount in its own units, hold that asset in the wallet, and expect sells to pay out in it. Without an entry the bot buys SOL-paired StonkFun coins only.

*Reward* coins charge a Token-2022 transfer fee of 1% or 3% on every transfer, the buy and the sell included. The bot sizes its slippage floors net of that fee and of the 1.25% curve fee. Set `filters.max_transfer_fee_bps` to skip coins above a rate (`0` keeps to standard coins only). How LaunchLab prices and settles a trade is in [docs/launchlab-protocol.md](docs/launchlab-protocol.md).

## Cookbook

[`cookbook/`](cookbook/) is one script per thing you might want to do. Each runs on
its own with `uv run <path>`, reads `.env` directly, and needs no bot config.
[`cookbook/README.md`](cookbook/README.md) is the index; the shape is:

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

The quickest way in:

```bash
uv run cookbook/solana/solana_read_balances.py               # what you hold
uv run cookbook/pumpfun/read/pumpfun_read_price.py <CURVE>   # what it costs
uv run cookbook/pumpfun/trade/pumpfun_buy_token.py <MINT> --dry-run
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
uv run tools/compare_listeners.py            # race all five listeners against each other
uv run tools/compare_migration_listeners.py  # race the two migration detection methods
uv run tools/cleanup_accounts.py [MINT]      # close leftover token accounts — submits transactions
uv run tools/launchlab_create_coin.py --platform-config <ADDRESS>  # LaunchLab launch on a local validator (surfpool)
uv run tools/live_v2_round_trip.py --yes     # real buy_v2 + sell_v2 — SPENDS REAL FUNDS
uv run tools/live_listener_matrix.py --yes   # real round trip per listener — SPENDS REAL FUNDS
```

## Throughput and rate limits

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up. For Chainstack, see the [throughput guidelines](https://docs.chainstack.com/docs/limits).

`getProgramAccounts` over the whole pump.fun program is no longer served by anyone: it owns more than 10 million accounts, so providers reject the request or time out whatever filters you pass. Use a filtered subscription instead — `cookbook/pumpfun/graduation/pumpfun_watch_graduating_programsubscribe.py` shows the pattern.

The bot rate-limits itself with a token bucket: `node.max_rps` (25 by default) smooths the request rate while allowing short bursts, and 429s are retried with exponential backoff.

For faster execution, Chainstack offers [Solana Trader nodes](https://docs.chainstack.com/docs/solana-trader-nodes) for transaction propagation and the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs under [`idl/`](idl/) are vendored from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) — currently upstream commit `8109141`. To refresh, copy `pump.json`, `pump_amm.json`, and `pump_fees.json` into `pump_fun_idl.json`, `pump_swap_idl.json`, and `pump_fees.json`, and note the upstream commit in your commit message. Don't hand-edit them. `raydium_launchlab_idl.json` is LaunchLab's on-chain IDL account, and `raydium_cpmm_idl.json` is `raydium_cpmm/raydium_cp_swap.json` from [raydium-io/raydium-idl](https://github.com/raydium-io/raydium-idl), commit `e7e0c96`; the bot reads Raydium CPMM's only to name its errors.

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

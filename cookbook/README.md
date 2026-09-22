# Cookbook

One script per thing you might want to do on pump.fun, PumpSwap and letsbonk.fun.
Each is standalone: run it with `uv run <path>`, no bot config, no framework. They
read `.env` directly, and most take the mint or curve address as the first argument.

**Scripts marked 💸 submit real transactions and spend real funds.** Read the module
docstring before running one. Everything else only reads.

## Naming

Every filename answers three questions before you open it:

```
<protocol>_<verb>_<noun>[_<variant>].py

pumpfun_buy_token_v2.py              buy one coin, using the v2 instructions
pumpfun_snipe_token_geyser.py        wait for a new coin over Geyser, then buy
pumpfun_listen_tokens_logsubscribe.py   watch for new coins over logsSubscribe
letsbonk_sell_token_exact_out.py     sell for a fixed amount received
```

- **protocol** — `pumpfun`, `pumpswap`, `letsbonk`, `solana`, `anchor`. The directory
  says it too, but a filename on its own is what you see in an editor tab, a grep
  result or a link.
- **verb** — what the script does: `buy`, `sell`, `create`, `snipe`, `listen`,
  `watch`, `read`, `derive`, `decode`, `check`, `capture`, `find`.
- **noun** — what it acts on: `token`, `price`, `curve`, `pool`, `balances`,
  `transaction`, `migrations`.
- **variant** — the instruction version (`v1`, `v2`, `exact_in`, `exact_out`) or the
  transport (`geyser`, `logsubscribe`, `blocksubscribe`, `programsubscribe`,
  `pumpportal`, `gettransaction`, `getaccountinfo`).

**One script does one thing.** Buying and selling are never in the same file. The one
exception is `pumpfun_create_and_buy_token_v2.py`, where the two steps together are
the thing people actually ask for — and its name says so.

Fixtures keep their own convention, `raw_<what>_from_<method>.json`, and sit beside
the script that reads them.

## Shared helpers

The scripts are deliberately repetitive: a script that derives an address inline is
easier to read and copy than one that imports a helper you also have to open, so the
same twenty lines appear in several files. Two modules are exempt, because both are
the kind of thing that must never drift between copies. Neither is runnable, so
neither has a verb in its name.

- [`solana/solana_transaction_status.py`](solana/solana_transaction_status.py) — did
  the transaction actually succeed. Every platform here uses it.
- [`pumpfun/trade/pumpfun_instructions_v2.py`](pumpfun/trade/pumpfun_instructions_v2.py)
  — the 27- and 26-account `buy_v2` / `sell_v2` layouts. Only the pump.fun trade
  scripts need it, so it lives with them.

A script that needs one adds its directory to `sys.path` first; the line is at the top
of the file with a comment saying why.

## Solana and Anchor basics

Not specific to any of the launchpads below.

| Script | What it does |
|---|---|
| [`solana/solana_read_balances.py`](solana/solana_read_balances.py) | Your SOL and every token you hold, across both token programs |
| [`solana/solana_check_transaction.py`](solana/solana_check_transaction.py) | Whether a signature succeeded, reverted, or is not visible yet |
| [`solana/solana_read_token2022_mint.py`](solana/solana_read_token2022_mint.py) | A Token-2022 mint's extensions, and which scaled-UI multiplier is really in force |
| [`solana/anchor_calculate_discriminator.py`](solana/anchor_calculate_discriminator.py) | The 8-byte Anchor discriminator for any instruction or account name |

## pump.fun

### Watch for things happening

| Script | What it does |
|---|---|
| [`pumpfun/listen/pumpfun_listen_tokens_logsubscribe.py`](pumpfun/listen/pumpfun_listen_tokens_logsubscribe.py) | New coins over `logsSubscribe` — works on every provider |
| [`pumpfun/listen/pumpfun_listen_tokens_blocksubscribe.py`](pumpfun/listen/pumpfun_listen_tokens_blocksubscribe.py) | New coins over `blockSubscribe` — whole blocks, slower, not on every provider |
| [`pumpfun/listen/pumpfun_listen_tokens_geyser.py`](pumpfun/listen/pumpfun_listen_tokens_geyser.py) | New coins over Geyser gRPC — the fastest of the four |
| [`pumpfun/listen/pumpfun_listen_tokens_pumpportal.py`](pumpfun/listen/pumpfun_listen_tokens_pumpportal.py) | New coins from PumpPortal's feed — third party, misses some coins |
| [`pumpfun/listen/pumpfun_listen_wallet_trades.py`](pumpfun/listen/pumpfun_listen_wallet_trades.py) | One wallet's bonding-curve buys and sells — copy trading |
| [`pumpfun/listen/pumpfun_capture_transactions_blocksubscribe.py`](pumpfun/listen/pumpfun_capture_transactions_blocksubscribe.py) | Save live transactions to disk, to build a fixture |

Racing the four listeners against each other is `tools/compare_listeners.py`.

### Read state

| Script | What it does |
|---|---|
| [`pumpfun/read/pumpfun_read_price.py`](pumpfun/read/pumpfun_read_price.py) | One coin's price, in whatever asset its curve is paired with |
| [`pumpfun/read/pumpfun_read_curve.py`](pumpfun/read/pumpfun_read_curve.py) | A curve's full state, decoded field by field |
| [`pumpfun/read/pumpfun_watch_curve_progress.py`](pumpfun/read/pumpfun_watch_curve_progress.py) | How close a coin is to graduating, polled over time |
| [`pumpfun/read/pumpfun_derive_curve_addresses.py`](pumpfun/read/pumpfun_derive_curve_addresses.py) | Derive a coin's curve and curve ATA offline, under both token programs |
| [`pumpfun/read/pumpfun_read_quote_mints.py`](pumpfun/read/pumpfun_read_quote_mints.py) | Every asset a coin may be priced in, `--stocks` for the tokenized equities |

### Trade

| Script | What it does |
|---|---|
| 💸 [`pumpfun/trade/pumpfun_buy_token_v2.py`](pumpfun/trade/pumpfun_buy_token_v2.py) | Buy a coin you name. `--dry-run` simulates instead of spending |
| 💸 [`pumpfun/trade/pumpfun_buy_token_exact_quote_v2.py`](pumpfun/trade/pumpfun_buy_token_exact_quote_v2.py) | Spend an exact amount of the quote asset. `--dry-run` simulates |
| 💸 [`pumpfun/trade/pumpfun_buy_token_exact_sol_in.py`](pumpfun/trade/pumpfun_buy_token_exact_sol_in.py) | Spend an exact amount of SOL. SOL-paired coins only |
| 💸 [`pumpfun/trade/pumpfun_sell_token_v2.py`](pumpfun/trade/pumpfun_sell_token_v2.py) | Sell your whole position in a coin you name |
| 💸 [`pumpfun/trade/pumpfun_create_token_v2.py`](pumpfun/trade/pumpfun_create_token_v2.py) | Create a coin with `create_v2`, buying none of it |
| 💸 [`pumpfun/trade/pumpfun_create_and_buy_token_v2.py`](pumpfun/trade/pumpfun_create_and_buy_token_v2.py) | Create a coin and buy it |
| 💸 [`pumpfun/trade/pumpfun_snipe_token_blocksubscribe.py`](pumpfun/trade/pumpfun_snipe_token_blocksubscribe.py) | Wait for the next coin created anywhere, then buy it |
| 💸 [`pumpfun/trade/pumpfun_snipe_token_geyser.py`](pumpfun/trade/pumpfun_snipe_token_geyser.py) | The same snipe, detected over Geyser gRPC |

Start at `pumpfun_buy_token_v2.py`. The two snipers are that same trade behind a
listener, which is most of why they are four times longer.

The two buys differ in which side you pin down — `buy_v2` fixes the tokens you
receive and caps the spend, `buy_exact_quote_in_v2` fixes the spend and floors
the tokens. Pin the spend when the quote asset is a budget you hold, which is
usually the case once a coin is priced in something other than SOL.

### Graduation

A coin that fills its bonding curve graduates to the PumpSwap AMM and stops trading
on the curve.

| Script | What it does |
|---|---|
| [`pumpfun/graduation/pumpfun_watch_graduating_programsubscribe.py`](pumpfun/graduation/pumpfun_watch_graduating_programsubscribe.py) | Coins approaching graduation, over a filtered `programSubscribe` |
| [`pumpfun/graduation/pumpfun_watch_graduating_geyser.py`](pumpfun/graduation/pumpfun_watch_graduating_geyser.py) | The same watch over Geyser gRPC |
| [`pumpfun/graduation/pumpfun_listen_migrations_logsubscribe.py`](pumpfun/graduation/pumpfun_listen_migrations_logsubscribe.py) | Migrations as the wrapper program emits them |
| [`pumpfun/graduation/pumpfun_listen_migrations_programsubscribe.py`](pumpfun/graduation/pumpfun_listen_migrations_programsubscribe.py) | Migrations as new pool accounts appear — noisy |

### Decode

Each script decodes one kind of payload and falls back to the fixture beside it, so
they all run with no arguments and no network. The fixtures are captured from
mainnet, never hand-edited — a stale one makes a working decoder look broken.

| Script | What it does |
|---|---|
| [`pumpfun/decode/pumpfun_decode_transaction_gettransaction.py`](pumpfun/decode/pumpfun_decode_transaction_gettransaction.py) | pump.fun instructions inside a `getTransaction` response |
| [`pumpfun/decode/pumpfun_decode_transaction_blocksubscribe.py`](pumpfun/decode/pumpfun_decode_transaction_blocksubscribe.py) | The same, from a `blockSubscribe` frame |
| [`pumpfun/decode/pumpfun_decode_curve_getaccountinfo.py`](pumpfun/decode/pumpfun_decode_curve_getaccountinfo.py) | A bonding curve account's raw bytes |

## PumpSwap

Where a coin trades after it graduates.

| Script | What it does |
|---|---|
| [`pumpswap/pumpswap_find_pool.py`](pumpswap/pumpswap_find_pool.py) | Find a coin's pool and decode it |
| 💸 [`pumpswap/pumpswap_pumpfun_buy_token_v2.py`](pumpswap/pumpswap_pumpfun_buy_token_v2.py) | Buy against the AMM |
| 💸 [`pumpswap/pumpswap_pumpfun_sell_token_v2.py`](pumpswap/pumpswap_pumpfun_sell_token_v2.py) | Sell against the AMM |

## letsbonk.fun

Built on Raydium LaunchLab, which quotes in two directions: *exact in* fixes what you
spend, *exact out* fixes what you receive.

| Script | What it does |
|---|---|
| 💸 [`letsbonk/letsbonk_buy_token_exact_in.py`](letsbonk/letsbonk_buy_token_exact_in.py) | Spend a fixed amount |
| 💸 [`letsbonk/letsbonk_buy_token_exact_out.py`](letsbonk/letsbonk_buy_token_exact_out.py) | Receive a fixed number of tokens |
| 💸 [`letsbonk/letsbonk_sell_token_exact_in.py`](letsbonk/letsbonk_sell_token_exact_in.py) | Sell a fixed number of tokens |
| 💸 [`letsbonk/letsbonk_sell_token_exact_out.py`](letsbonk/letsbonk_sell_token_exact_out.py) | Sell for a fixed amount received |
| [`letsbonk/letsbonk_idl_parser.py`](letsbonk/letsbonk_idl_parser.py) | Loads the LaunchLab IDL for the four above |

## legacy

Instructions pump.fun has moved on from. Kept because they still land on chain and
older coins were made with them — not what to copy for new work.

| Script | What it does |
|---|---|
| 💸 [`legacy/pumpfun_create_and_buy_token_v1.py`](legacy/pumpfun_create_and_buy_token_v1.py) | Create a coin with the pre-Token-2022 `create` instruction |

## Not in here

- `tools/` — scripts that exercise the bot rather than teach it: mainnet
  simulations, live round trips, listener benchmarks, leftover-account cleanup.
- `tests/regression/` — one offline check per bug that has been fixed, each
  importing the bot's own code. `uv run tests/regression/run_all.py`.

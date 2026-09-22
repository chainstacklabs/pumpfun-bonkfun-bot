# Cookbook

One script per thing you might want to do on pump.fun, PumpSwap and letsbonk.fun.
Each is standalone: run it with `uv run <path>`, no bot config, no framework. They
read `.env` directly, and most take the mint or curve address as the first argument.

They are deliberately repetitive. A script that derives an address inline is easier
to read and copy than one that imports a helper you also have to open, so the same
twenty lines appear in several files. Two exceptions live at the root of this
directory — [`pump_v2.py`](pump_v2.py) (the 27- and 26-account `buy_v2`/`sell_v2`
layouts) and [`tx_status.py`](tx_status.py) (did it actually succeed) — because
both are the kind of thing that must never drift between copies. Scripts in a
subdirectory put this directory on `sys.path` before importing them.

**Scripts marked 💸 submit real transactions and spend real funds.** Read the module
docstring before running one. Everything else only reads.

## pump.fun

### Watch for things happening

| Script | What it does |
|---|---|
| [`pumpfun/listen/listen_logsubscribe.py`](pumpfun/listen/listen_logsubscribe.py) | New coins over `logsSubscribe` — works on every provider |
| [`pumpfun/listen/listen_blocksubscribe.py`](pumpfun/listen/listen_blocksubscribe.py) | New coins over `blockSubscribe` — whole blocks, slower, not on every provider |
| [`pumpfun/listen/listen_geyser.py`](pumpfun/listen/listen_geyser.py) | New coins over Geyser gRPC — the fastest of the four |
| [`pumpfun/listen/listen_pumpportal.py`](pumpfun/listen/listen_pumpportal.py) | New coins from PumpPortal's feed — third party, misses some coins |
| [`pumpfun/listen/listen_wallet_transactions.py`](pumpfun/listen/listen_wallet_transactions.py) | One wallet's bonding-curve buys and sells — copy trading |
| [`pumpfun/listen/extract_blocksubscribe_transactions.py`](pumpfun/listen/extract_blocksubscribe_transactions.py) | Save live transactions to disk, to build a fixture |

Racing the four listeners against each other is `tools/compare_listeners.py`.

### Read state

| Script | What it does |
|---|---|
| [`pumpfun/read/fetch_price.py`](pumpfun/read/fetch_price.py) | One coin's price, in whatever asset its curve is paired with |
| [`pumpfun/read/get_bonding_curve_status.py`](pumpfun/read/get_bonding_curve_status.py) | A curve's full state, decoded field by field |
| [`pumpfun/read/poll_bonding_curve_progress.py`](pumpfun/read/poll_bonding_curve_progress.py) | How close a coin is to graduating, polled over time |
| [`pumpfun/read/compute_associated_bonding_curve.py`](pumpfun/read/compute_associated_bonding_curve.py) | Derive a coin's curve and curve ATA offline, under both token programs |
| [`pumpfun/read/get_balances.py`](pumpfun/read/get_balances.py) | Your SOL and every token you hold, across both token programs |
| [`pumpfun/read/check_tx_status.py`](pumpfun/read/check_tx_status.py) | Whether a signature succeeded, reverted, or is not visible yet |

### Trade

| Script | What it does |
|---|---|
| 💸 [`pumpfun/trade/buy_token.py`](pumpfun/trade/buy_token.py) | Buy a coin you name. `--dry-run` simulates instead of spending |
| 💸 [`pumpfun/trade/sell_token.py`](pumpfun/trade/sell_token.py) | Sell your whole position in a coin you name |
| 💸 [`pumpfun/trade/create_token.py`](pumpfun/trade/create_token.py) | Create a coin with `create_v2`, buying none of it |
| 💸 [`pumpfun/trade/mint_and_buy_v2.py`](pumpfun/trade/mint_and_buy_v2.py) | Create a coin and buy it |
| 💸 [`pumpfun/trade/manual_buy.py`](pumpfun/trade/manual_buy.py) | Wait for the next coin created anywhere, then buy it — sniping |
| 💸 [`pumpfun/trade/manual_buy_geyser.py`](pumpfun/trade/manual_buy_geyser.py) | The same snipe, detected over Geyser gRPC |

Start at `buy_token.py`. `manual_buy.py` is that same trade behind a listener, which
is most of why it is four times longer.

### Graduation

A coin that fills its bonding curve graduates to the PumpSwap AMM and stops trading
on the curve.

| Script | What it does |
|---|---|
| [`pumpfun/graduation/get_graduating_tokens.py`](pumpfun/graduation/get_graduating_tokens.py) | Coins approaching graduation, over a filtered `programSubscribe` |
| [`pumpfun/graduation/get_graduating_tokens_geyser.py`](pumpfun/graduation/get_graduating_tokens_geyser.py) | The same watch over Geyser gRPC |
| [`pumpfun/graduation/listen_migration_logsubscribe.py`](pumpfun/graduation/listen_migration_logsubscribe.py) | Migrations as the wrapper program emits them |
| [`pumpfun/graduation/listen_migration_programsubscribe.py`](pumpfun/graduation/listen_migration_programsubscribe.py) | Migrations as new pool accounts appear — noisy |

### Decode

Each script decodes one kind of payload and falls back to the fixture beside it, so
they all run with no arguments and no network. The fixtures are captured from
mainnet, never hand-edited — a stale one makes a working decoder look broken.

| Script | What it does |
|---|---|
| [`pumpfun/decode/decode_from_gettransaction.py`](pumpfun/decode/decode_from_gettransaction.py) | pump.fun instructions inside a `getTransaction` response |
| [`pumpfun/decode/decode_from_blocksubscribe.py`](pumpfun/decode/decode_from_blocksubscribe.py) | The same, from a `blockSubscribe` frame |
| [`pumpfun/decode/decode_from_getaccountinfo.py`](pumpfun/decode/decode_from_getaccountinfo.py) | A bonding curve account's raw bytes |
| [`pumpfun/decode/calculate_discriminator.py`](pumpfun/decode/calculate_discriminator.py) | The 8-byte Anchor discriminator for any instruction or account name |

## PumpSwap

Where a coin trades after it graduates.

| Script | What it does |
|---|---|
| [`pumpswap/get_pumpswap_pools.py`](pumpswap/get_pumpswap_pools.py) | Find a coin's pool and decode it |
| 💸 [`pumpswap/manual_buy_pumpswap.py`](pumpswap/manual_buy_pumpswap.py) | Buy against the AMM |
| 💸 [`pumpswap/manual_sell_pumpswap.py`](pumpswap/manual_sell_pumpswap.py) | Sell against the AMM |

## letsbonk.fun

Built on Raydium LaunchLab, which quotes in two directions: *exact in* fixes what you
spend, *exact out* fixes what you receive.

| Script | What it does |
|---|---|
| 💸 [`letsbonk/manual_buy_exact_in.py`](letsbonk/manual_buy_exact_in.py) | Spend a fixed amount |
| 💸 [`letsbonk/manual_buy_exact_out.py`](letsbonk/manual_buy_exact_out.py) | Receive a fixed number of tokens |
| 💸 [`letsbonk/manual_sell_exact_in.py`](letsbonk/manual_sell_exact_in.py) | Sell a fixed number of tokens |
| 💸 [`letsbonk/manual_sell_exact_out.py`](letsbonk/manual_sell_exact_out.py) | Sell for a fixed amount received |
| [`letsbonk/idl_parser.py`](letsbonk/idl_parser.py) | Loads the LaunchLab IDL for the four above |

## legacy

Instructions pump.fun has moved on from. Kept because they still land on chain and
older coins were made with them — not what to copy for new work.

| Script | What it does |
|---|---|
| 💸 [`legacy/mint_and_buy.py`](legacy/mint_and_buy.py) | Create a coin with the pre-Token-2022 `create` instruction |

## Not in here

- `tools/` — scripts that exercise the bot rather than teach it: mainnet
  simulations, live round trips, listener benchmarks, leftover-account cleanup.
- `tests/regression/` — one offline check per bug that has been fixed, each
  importing the bot's own code. `uv run tests/regression/run_all.py`.

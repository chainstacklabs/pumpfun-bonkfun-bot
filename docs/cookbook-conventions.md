# Cookbook conventions

Rules for adding or changing a script under `cookbook/`.
[cookbook/README.md](../cookbook/README.md) is the reader-facing index.

## What belongs here

- **One script, one action.** Duplication across scripts is the accepted cost —
  don't factor shared helpers out of them. `pumpfun_instructions.py` and
  `solana_transaction_status.py` are the two exceptions and the list is closed.
  Buy and sell never share a file. The create-and-buy scripts are the only
  two-action ones, and they come in a `_txv0` and a `_txv1` form: the Solana
  transaction version changes whether the launch is atomic, so it is the
  script that changes, not a flag.
- **Every script runs on its own**: `uv run cookbook/<path>`, reading `.env`. No
  bot config, no import from `src/`. Anything that needs the bot goes in `tools/`;
  anything that asserts a past bug stays fixed goes in `tests/regression/`.
- **Directories are single lowercase words** under a platform directory:
  `listen`, `read`, `trade`, `graduation`, `decode`.
- **Files are `<protocol>_<verb>_<noun>[_<variant>].py`**, all snake_case. The
  protocol repeats what the directory says, on purpose — a basename is what shows
  up in an editor tab, a grep hit or a docs link.
  - protocol: `pumpfun`, `pumpswap`, `letsbonk`, `stonkfun`, `solana`, `anchor`
  - verb: `buy`, `sell`, `create`, `snipe`, `listen`, `watch`, `read`, `derive`,
    `decode`, `check`, `capture`, `find`
  - noun: `token`, `price`, `curve`, `pool`, `balances`, `transaction`, `migrations`
  - variant: what the reader is choosing between — `exact_in`/`exact_out`, a
    transport, or `txv0`/`txv1` for the Solana transaction version
  - **Don't put the pump instruction version in a basename.** The cookbook ships
    one form of each action, so `_v2` tells the reader nothing and reads as a
    Solana transaction version, which is a different thing entirely. Name the
    instruction in the docstring instead. The retired path is `legacy/`.
  - the two non-runnable helpers take no verb, because they do nothing
- **RPC and service names lowercase into one token**, never camelCase:
  `blocksubscribe`, `logsubscribe`, `programsubscribe`, `getaccountinfo`,
  `gettransaction`.
- **Anything not specific to a launchpad belongs under `solana/`**, not `pumpfun/`.
- **Input is a command-line argument, never a constant you edit.** Every script
  builds an `ArgumentParser` in `main()`: required values are positionals,
  tunables are `--options`, and anything the caller varies per run is one of
  them. `DEFAULT_*` constants are the only place a literal belongs.
  - A placeholder is not a default: `Pubkey.from_string("...")` raises at import,
    so the script dies before printing its own usage.
  - Don't read config from environment variables either — `.env` is for
    endpoints and keys, not for trade parameters no usage line mentions.
  - `sys.argv` never appears at module level. The nine listeners take no input
    and are exempt; they are listed in the verifier.
- Fixtures are `raw_<what>_from_<method>.json`, next to the script that reads them.
- **Cite a URL only after checking it resolves.**
  `uv run tests/regression/verify_documentation_links.py --live` fetches every URL
  in the repo.
- **A script that spends says so on the first line of its docstring**, and the
  cookbook README marks it. The name is not a safety signal: every `*_buy_*`,
  `*_sell_*`, `*_create_*` and `*_snipe_*` script submits real transactions.

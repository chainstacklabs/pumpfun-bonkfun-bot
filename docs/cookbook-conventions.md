# Cookbook conventions

Rules for adding or changing a script under `cookbook/`.
[cookbook/README.md](../cookbook/README.md) is the reader-facing index.

## What belongs here

- **One script, one action.** Duplication across scripts is the accepted cost;
  don't factor shared helpers out of them. The three existing helpers,
  `pumpfun_instructions.py`, `solana_transaction_status.py` and
  `letsbonk_idl_parser.py`, are the only ones. Buy and sell never share a file.
  The create-and-buy scripts are the only two-action ones, and they come in a
  `_txv0` and a `_txv1` form, since the Solana transaction version decides
  whether the launch is atomic.
- **Every script runs on its own**: `uv run cookbook/<path>`, reading `.env`. No
  bot config, and no import from `src/` except the generated geyser stubs
  (`src.geyser.generated`), which the geyser scripts share. Anything that needs
  the bot goes in `tools/`; anything that asserts a past bug stays fixed goes in
  `tests/regression/`.
- **Layout:** `pumpfun/` and `stonkfun/` split by action into `listen`, `read`,
  `trade`, `graduation` and `decode`; the smaller platform folders (`letsbonk/`,
  `pumpswap/`, `solana/`) hold their scripts directly. Retired instructions go in
  `legacy/`.
- **Files are `<protocol>_<verb>_<noun>[_<variant>].py`**, all snake_case, with
  the protocol repeated even though the folder says it.
  - protocol: `pumpfun`, `pumpswap`, `letsbonk`, `stonkfun`, `solana`, `anchor`
  - verb: an action, such as `buy`, `sell`, `create`, `snipe`, `listen`, `watch`,
    `read`, `derive`, `decode`, `check`, `capture`, `find`, `claim`, `collect`
  - variant: what the reader chooses between, such as `exact_in`/`exact_out`, a
    transport, a venue (`cpmm`) or `txv0`/`txv1` for the Solana transaction
    version
  - **Don't put the pump instruction version in a basename**: `_v2` reads as a
    Solana transaction version. Name the instruction in the docstring.
  - the helpers take no verb, because they do nothing
- **RPC and service names lowercase into one token**, never camelCase:
  `blocksubscribe`, `logsubscribe`, `programsubscribe`, `getaccountinfo`,
  `gettransaction`.
- **Anything not specific to a launchpad belongs under `solana/`**, not `pumpfun/`.
- **Input is a command-line argument, never a constant you edit.** A script
  builds an `ArgumentParser` in `main()`: required values are positionals,
  tunables are `--options`. `DEFAULT_*` constants are the only place a literal
  belongs. The scripts that take no input (the listeners and the capture script)
  are exempt from the parser and are listed in `verify_cookbook_arguments.py`;
  no script reads `sys.argv` at module level.
  - A placeholder is not a default: `Pubkey.from_string("...")` raises at import.
  - Don't read trade parameters from environment variables; `.env` is for
    endpoints and keys.
- **Never print an endpoint or any part of it, its host included.** Name the
  variable it came from instead.
- Fixtures are `raw_<what>_from_<method>.json`, next to the script that reads them.
- **Cite a URL only after checking it resolves.**
  `uv run tests/regression/verify_documentation_links.py --live` fetches every URL
  in the repo.
- **A script that submits a transaction says so** with a
  `WARNING: this submits a real transaction and spends real funds.` line right
  after its docstring summary, and the cookbook README marks it 💸. Every
  `*_buy_*`, `*_sell_*`, `*_create_*`, `*_snipe_*`, `*_claim_*` and `*_collect_*`
  script submits one.

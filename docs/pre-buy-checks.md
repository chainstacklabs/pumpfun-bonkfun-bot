# Pre-buy checks

Before the bot buys a coin, it runs a list of checks. Each one passes the coin
or says why it is skipped, and the first skip wins and is logged with its
reason. The `filters` settings (`allowed_quote_mints`, `max_transfer_fee_bps`,
`match_string`, `bro_address`) run first as built-in checks; the list under
`filters.checks` runs after them, in the order it is written.

## Two kinds of check

- **Checks that read nothing** look only at what the listener already knows
  about the coin: its name, creator, quote mint, flags. They run as soon as the
  coin is detected and cost no time on the buy path.
- **Checks that read the chain** set `reads_chain = True`. They run once, just
  before the buy, after `wait_after_creation` when `extreme_fast_mode` is off.
  Each read adds time between detection and the buy, which is why none is on
  by default.

## Configure checks

```yaml
filters:
  checks:
    - name: mayhem            # a built-in, by name
    - name: top_holders
      max_share: 0.3          # options go to the check
      top: 10
      required: false         # when it can't decide, buy anyway (its default)
      budget: 1.0             # seconds before it counts as undecided (default 2)
    - module: my_checks:SymbolDenylist   # your own, as module:Class
      symbols: ["SCAM", "RUG"]
      enabled: false          # listed but off
```

| Field | Meaning |
|---|---|
| `name` | A built-in check: `mayhem`, `top_holders` |
| `module` | Your check, as `module:Class`, imported from the folder you run `pump_bot` in |
| `enabled` | `false` leaves the check out. Default `true` |
| `required` | When the check can't decide (its read failed, it raised, it ran past its budget), `true` skips the coin and `false` lets it through. Default `true`, except `top_holders` |
| `budget` | Seconds the check may take. Default 2 |

Any other key is passed to the check's `__init__`. A check that fails to load
stops the bot at startup with one line saying why. Each check runs once per
coin, so a check may keep count of what it has seen.

## Write your own

A check is a class with a `name` and one async method. A check that can only
decide when its data is there sets `required_by_default = False`. Save this as
`my_checks.py` next to your `bots/` folder:

```python
from trading.checks import CheckContext, PreBuyCheck


class SymbolDenylist(PreBuyCheck):
    """Skip coins whose symbol is on a list."""

    name = "symbol_denylist"

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = {s.upper() for s in symbols}

    async def check(self, coin, ctx: CheckContext) -> str | None:
        if coin.symbol.upper() in self.symbols:
            return f"symbol {coin.symbol} is on the denylist"
        return None
```

Then list it under `filters.checks` with `module: my_checks:SymbolDenylist`.
Your folder is on the import path only while the bot loads the check, at
startup, so import what it needs at the top of the file.
`coin` is the bot's `TokenInfo` (`src/interfaces/core.py`): `mint`, `name`,
`symbol`, `creator`, `quote_mint`, `is_mayhem_mode` and the rest.

A check that reads the chain sets `reads_chain = True` and uses `ctx.client`,
the bot's `SolanaClient`. When its data can't be read, it raises
`CheckUnavailable` and `required` decides. `src/trading/checks/top_holders.py`
is a complete example.

## Built-in checks

- **`mayhem`** skips pump.fun coins in mayhem mode. Reads nothing.
- **`top_holders`** skips a coin when its `top` largest wallets hold more than
  `max_share` of the supply. Accounts that programs own (the bonding curve, a
  pool's vaults) are left out. It reads the chain three times. A fresh launch
  is held by its creator and its curve alone, so it suits `entry: migration`
  and buys that wait after creation. It is best effort unless listed with
  `required: true`: a coin the `shreds` listener reports before its create has
  run has no accounts to read yet.

# Raydium LaunchLab: letsbonk.fun and StonkFun

Both platforms are pools on one program, Raydium LaunchLab
(`LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`). The shared code is
`src/platforms/launchlab/`; `platforms/letsbonk/` and `platforms/stonkfun/` only
name their platform configs. The IDL is `idl/raydium_launchlab_idl.json`, pulled
from the program's on-chain IDL account.

## Platforms are platform configs

A launchpad on LaunchLab is the set of `PlatformConfig` accounts its pools are
created under. Every launch and every trade names one, at account 3.

| Platform | Platform configs |
|---|---|
| letsbonk.fun | `5thqcDwKp5QQ8US4XRMoseGeGbmLKMmoKZmS6zHrQAsA` |
| StonkFun, standard | `4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7` |
| StonkFun, reward | `6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt` |

The geyser and blocks listeners subscribe on these accounts, not on the program
(`EventParser.get_stream_filter_accounts`), so other launchpads on LaunchLab stay
out of the stream. Two platforms on one program also mean nothing may key a
lookup by program id alone.

## Launches

StonkFun launches are `initialize_with_token_2022`, as legacy, v0 and v1
transactions, sometimes issued by a router as an inner instruction. Read
accounts by IDL name: `payer` is 0 and `creator` is 1, and the pool's `creator`
— which keys the creator fee vault — is the `creator` account, not the payer.
The quote mint is account 7 and its token program account 11. The transfer fee
is the `transfer_fee_extension_param` argument.

Everything a buy needs is in the instruction, so the parsers set
`state_from_event`. The program's `PoolCreateEvent` carries no base mint, quote
mint or platform config, which is why there is no logs listener for LaunchLab.

## Trades

`buy_exact_in` and `sell_exact_in` take the IDL's 15 accounts, then three the
program reads positionally: the system program, the platform fee vault
`PDA([platform_config, quote_mint])` and the creator fee vault
`PDA([creator, quote_mint])`. The pool is `PDA(["pool", base_mint, quote_mint])`.
Every one of these is keyed by the pool's quote mint.

- **`buy_exact_in` spends all of `amount_in`.** The trader hands an exact-in
  builder the configured amount, unpadded (`spends_exact_amount_in`).
- **Price is `(virtual_quote + real_quote) / (virtual_base - real_base)`.** The
  virtual reserves are fixed at launch. Pricing off them alone returns the
  launch price forever, and take-profit and stop-loss never fire.
- **Fees:** `trade_fee_rate` (GlobalConfig) + `fee_rate` + `creator_fee_rate`
  (PlatformConfig), in parts per million, read live by the program. A buy pays
  them from its input and a sell from its output, rounded up. Curve outputs
  round down. `platforms/launchlab/curve_math.py` reproduces both to the unit.

## Quote assets

The launcher picks the quote: SOL, an xStock, another coin. A SOL-quoted trade
settles through a throwaway wrapped-SOL account opened and closed in the same
transaction. Any other quote is paid from, and paid into, the wallet's own
account for that mint. The bot never buys a quote asset, so the balance must
already be there, and a sell leaves the proceeds in that asset. The quote's token
program comes from the launch instruction. xStocks are Token-2022 mints.

xStocks carry a pause switch, a permanent delegate, a scaled UI amount and a
transfer-hook extension with no program set. If the issuer sets a hook program,
every transfer needs that program's extra accounts, and the builder does not
pass them.

## Transfer fees

StonkFun reward coins are Token-2022 mints with a 1% or 3% transfer fee.

- A buy delivers its output less the fee, and **`minimum_amount_out` is checked
  against what arrives**. A floor computed without the fee reverts `6004`.
- A sell sends its amount and the pool trades on what arrives, less the fee.
- The fee is withheld **inside the receiving account**: the bot's own account,
  on a buy. Token-2022 refuses to close an account holding withheld fees
  (`0x23`), so cleanup runs the permissionless `HarvestWithheldTokensToMint`
  first. Standard coins' mints have no transfer-fee extension, and harvesting
  against one fails, so only harvest when the account holds withheld fees.

## Graduation

When a curve raises its target, Raydium's migrate wallet moves it to a pool, and
the curve takes no more trades. `PoolState.status` is 1 while it waits and 2 once
migrated; a migrated pool keeps its final reserves, so the curve manager reports
`graduated` and `calculate_price` raises rather than return a frozen price.

`migrate_type` 1 means Raydium CPMM, which is where every StonkFun coin goes. The
CPMM pool is `PDA(["pool", amm_config, token_0, token_1])` under the CPMM program,
with `amm_config` the platform's `PlatformConfig.cpswap_config` and the two mints
ordered by their bytes, so it is derived, never searched for. A sell is
`swap_base_input`, 13 accounts; the creator fee accrues inside the pool and adds
none. Price is the vault balances less the protocol, fund and creator fees still
sitting in them. `migrate_type` 0, Raydium AMM v4, has no market in the bot; a
coin there is reported, not sold.

## Verifiers

`verify_launchlab_trades`, `verify_launchlab_launch_parsing`,
`verify_cleanup_harvests_withheld_fees` and `verify_sell_after_graduation` cover
all of the above against captured
mainnet transactions.

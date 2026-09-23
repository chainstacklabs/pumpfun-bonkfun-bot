# Pump.fun protocol notes

The IDLs under `idl/` are vendored verbatim from `github.com/pump-fun/pump-public-docs`
(`idl/pump.json` → `pump_fun_idl.json`, `pump_amm.json` → `pump_swap_idl.json`,
`pump_fees.json`). Refresh them from upstream rather than hand-editing.

## Quote assets and the v2 trade instructions (current path)

- pump.fun supports quote assets other than SOL, and no longer just SOL and USDC.
  `BondingCurve.quote_mint` is `Pubkey::default()` (all zeros) for SOL-paired
  coins; USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) is one whitelisted
  entry in `Global`, but coins paired with Token-2022 quote mints are live on
  chain too. **Legacy `buy`/`sell` cannot trade non-SOL-paired coins at all.**
- **`Global.whitelisted_quote_mints` is not the authoritative registry.** A
  `QuoteControl` account carries its own mint list, which is how a coin pairs with
  a mint `Global` never lists. Error `6064` accepts SPL Token or Token-2022.
- **The quote mint's token program is resolved from chain, not assumed.**
  `resolve_quote_token_program` (`src/core/pubkeys.py`) reads a mint's owner once
  — pre-seeded with WSOL/USDC so those stay free — and caches it for the process;
  `cached_quote_token_program` is the hot-path read used by event parsing and
  address resolution. The bot warms the cache at startup for every configured
  quote mint, so `extreme_fast_mode`'s zero-RPC contract still holds.
- **The decimals come from the same read.** Amounts like `max_sol_cost` and
  `min_sol_output` are in the quote mint's raw units, so assuming 9 decimals for
  a 6-decimal mint overstates the cap by 1000x and effectively disables slippage
  protection. `getAccountInfo` returns the owner and the mint data together, so
  decimals cost no extra call; they are cached alongside the token program and
  pre-seeded for WSOL (9) and USDC (6). A quote mint whose decimals cannot be
  resolved fails at startup rather than silently mis-scaling a trade. The
  `decimals` byte sits at **offset 44** in both SPL Token and Token-2022 mints —
  extensions are appended after the base struct and never move it.
- The bot uses **`buy_v2` (27 accounts)** and **`sell_v2` (26 accounts)**. Every
  account is mandatory and the order is identical for every coin — whatever quote
  mint, mayhem or not, cashback or not. `sell_v2` is `buy_v2` minus
  `global_volume_accumulator`. Layouts live in `_BUY_V2_ACCOUNTS` /
  `_SELL_V2_ACCOUNTS` in `platforms/pumpfun/instruction_builder.py` and are
  machine-checked against the IDL by `tests/regression/verify_v2_account_layout.py`.
- v2 args carry **no `track_volume` OptionBool** (24-byte data: discriminator +
  two u64). Volume tracking is unconditional now that `user_volume_accumulator`
  is mandatory. `max_sol_cost`/`min_sol_output` are in the **quote mint's** raw
  units — lamports for SOL, 1e-6 for USDC.
- Even for SOL-paired coins you must pass **wrapped SOL** as `quote_mint`, not
  `Pubkey::default()`. Transfers still happen in native SOL, and the
  `associated_quote_*` accounts are only seed-constrained — do **not** create the
  user's WSOL ATA, it would burn ~0.002 SOL of rent for nothing.
- Fee recipients: 24 total, in three sets of 8 (`NORMAL_FEE_RECIPIENTS`,
  `RESERVED_FEE_RECIPIENTS` for mayhem coins, `BUYBACK_FEE_RECIPIENTS`). Every v2
  buy/sell needs a `fee_recipient` **and** a `buyback_fee_recipient`, randomized
  per tx to spread program throughput.
- `sharing_config` (PDA `["sharing-config", base_mint]`) lives under the **pump
  fees program**, not the pump program. Easy to derive against the wrong program.

## BondingCurve account layout

- **`create_v2` allocates 125 bytes.** The 36 reserved padding bytes are gone and
  three fields follow `quote_mint`: `creator_fee_bps` (u64),
  `can_edit_creator_fee` (bool, reserved — always false) and `is_holder_reward`
  (bool). Every field the bot reads sits before where the padding used to start.
- `extend_account` can grow a curve past 125 bytes, and a length allowlist is
  whack-a-mole against that. **Don't filter on account length** — decode any
  length at or above 125 the same way:

  | Length | What it is |
  |---|---|
  | 125 bytes | What `create_v2` allocates |
  | 151 bytes | The old allocation size; still reachable via `extend_account` |
  | 256 bytes | A rarer `extend_account` target |

  `tests/regression/verify_curve_account_sizes.py` checks that all three decode
  and that the graduating-token examples don't filter on length.
- The SOL-named fields were **renamed**: `virtual_sol_reserves` →
  `virtual_quote_reserves`, `real_sol_reserves` → `real_quote_reserves`. The curve
  manager exposes the old names as aliases, so callers keep working for SOL-paired
  coins — but anything doing arithmetic must scale by the quote mint's decimals
  (`quote_units_per_token`), not a hardcoded 1e9.
- PumpSwap `Pool` gained a trailing **`virtual_quote_reserves: i128`** (16 bytes,
  offset 245). Pool fields end at 261; live accounts are **301 bytes** with
  trailing padding. Quote against **effective** reserves:
  `pool_quote_token_account.amount + virtual_quote_reserves`. **Upstream's
  release note claims it is 0 on all pools — that is out of date**, and quoting
  off the raw vault balance under-prices any pool carrying some. It is `i128`,
  not `u64`; reading 8 bytes works only while the high half is zero.
- pump-amm has **no** `buy_v2`/`sell_v2`. The AMM instruction names are
  unchanged; only the pool layout and quoting moved.
- **`Pool.coin_creator` tells you whether a pool is canonical**, i.e. graduated
  from a pump.fun bonding curve. It is `Pubkey::default()` on every other pool.
  Two things follow:
  - `pool-v2` is sent **only for a canonical pool**. The buyback fee recipient and
    its quote ATA are always the last two accounts and are read positionally, so
    adding `pool-v2` on a non-canonical pool shifts them and the program rejects
    the `pool-v2` PDA with `BuybackFeeRecipientNotAuthorized` (6053). Upstream's
    BREAKING_FEE_RECIPIENT.md says the pair goes after `pool-v2` "for coins that
    graduate from bonding curve", and separately that the pair is needed either
    way — the qualifier is the whole rule. Account counts: buy 26/27 canonical,
    25/26 not; sell 24/26 canonical, 23/25 not.
  - **Do not assume 6 base-token decimals.** Every graduated coin has 6, but
    non-canonical pools routinely carry 9, and a factor of 1000 scales the quote
    and the slippage floor together, so a sell reverts `ExceededSlippage` (6004)
    rather than just mispricing. Resolve the mint's owner and decimals from the
    same `getAccountInfo`, as `get_mint_info` does.
- The vendored `pump_swap_idl.json` **under-reports these accounts** and is
  byte-identical to upstream, so it cannot be the reference: it lists 23 for `buy`
  against 25-26 on chain, and 21 for `sell` against 23-26.

## Coin creation

- `create_v2` takes **eight args**: `name (str), symbol (str), uri (str),
  creator (pubkey), is_mayhem_mode (bool), is_cashback_enabled (OptionBool),
  creator_fee_bps (OptionU64), is_holder_reward (OptionBool)`. `OptionBool` and
  `OptionU64` are single-field Anchor structs with no presence tag — each
  serializes as its bare inner value, 1 byte and 8 bytes, never a discriminated
  Option.
- **`is_cashback_enabled = [true]` is rejected** with `6082 CashbackDeprecated`:
  `create_v2` can no longer mint a new cashback coin. Existing cashback coins keep
  trading, accruing and claiming, so every cashback code path in this repo (the
  legacy sell path's cashback branch, `is_cashback_coin` on `TokenInfo`) stays
  live and must not be treated as dead.
- **The trailing args are positional, not independently optional.** Reaching
  `is_holder_reward` (arg 8) means sending `is_cashback_enabled` (6) and
  `creator_fee_bps` (7) first, even when both are false/zero. The committed
  fixtures show three wire lengths: all trailing args omitted (0 bytes after
  `is_mayhem_mode`), `is_cashback_enabled` only (1 byte), and
  `is_cashback_enabled` + `creator_fee_bps` (9 bytes); the three-arg form is sent
  too. Because the args are positional, a shorter form cannot have set a later one
  — a truncated instruction is proof the coin is not holder-reward, not merely
  silence about it, which is what lets a pre-execution listener classify every
  coin. A decoder that reads a fixed number of trailing bytes raises `IndexError`
  on the shorter forms, so decode defensively and report a missing arg as unset;
  `utils/idl_parser.py` does, and
  `tests/regression/verify_create_v2_optional_args.py` checks it.
- `create_v2` accounts 1-16 are in the IDL; accounts **17-19 are optional
  remaining accounts** (`quote_mint`, `associated_quote_bonding_curve`,
  `quote_token_program`), all three or none. This is the only way to read a new
  coin's quote asset from the instruction rather than the event. They are appended
  for **SOL-paired coins too**, carrying wrapped SOL, so a 19-account `create_v2`
  is not proof of a non-SOL quote asset. Read `quote_mint` off the curve instead.
- The **associated bonding curve is an ordinary ATA**, so its address depends on
  which token program owns the mint: Token2022 for `create_v2` coins, SPL Token
  for legacy `create`. Deriving with the wrong program returns a valid-looking
  address that does not exist on chain.
- `extreme_fast_mode` skips the curve-state price fetch. Whether it also reads the
  curve for mayhem/cashback/creator/**quote_mint** depends on provenance (see the
  Invariants in CLAUDE.md): CreateEvent-sourced tokens (`state_from_event`) trade on the event
  data with zero RPC calls, while pumpportal/incomplete-event tokens refresh from
  chain — the wrong quote mint means spending the wrong balance entirely. Event
  parsers populate `quote_mint` from `CreateEvent`, which carries `quote_mint` and
  `virtual_quote_reserves` as trailing fields.

## Holder reward coins

`create_v2` can set `is_holder_reward` so the creator fee is set aside for holders
instead of paid to a creator wallet. On such a coin `BondingCurve.creator` holds a
pump.fun address rather than the actual creator's; the `creator_vault` derivation
is unchanged and still correct, since it derives from whatever `creator` the curve
carries.

**That substituted address is `PDA(["holder-rewards", mint])` under the pump
program** — the same account the IDL declares for `holder_rewards` on
`distribute_fee_to_holders`, which is how the ordinary `creator-vault` route
delivers the fee to the holder pool without a second payout path. It depends on
nothing but the mint, so `args.creator` being unusable on these coins costs no RPC
call to work around: `PumpFunAddresses.find_holder_reward_creator` derives it.
**No trade instruction changed**: `buy`, `sell`, `buy_v2`, `sell_v2` and the
PumpSwap instructions take identical accounts and arguments either way.
`Global.is_holder_reward_enabled` can switch creation off globally.

`TokenInfo.is_holder_reward` and `TokenInfo.creator_fee_bps` surface this to the
bot — see the field comments on `TokenInfo` in `src/interfaces/core.py`. SOL- and
USDC-paired coins keep `creator_fee_bps` at 0; custom-pair coins carry a nonzero
value.

## Legacy instructions (fallback only)

Retained behind `PumpFunInstructionBuilder(..., use_legacy_instructions=True)`.
The IDL under-reports these: `buy` is **18 accounts** on-chain (IDL lists 16) and
`sell` is **16 non-cashback / 17 cashback** (IDL lists 14). The extras are
`bonding-curve-v2` (PDA `["bonding-curve-v2", mint]`) followed by a mutable
buyback fee recipient; the cashback sell path also inserts
`user_volume_accumulator` before `bonding-curve-v2`. On the PumpSwap side the
legacy path needs `pool-v2` (PDA `["pool-v2", base_mint]` under pump-amm) —
without it pump-amm throws `AnchorError 6023 (Overflow)` after the transfers
complete, a misleading code for a missing account. Prefer v2 — it is the interface
pump.fun maintains.

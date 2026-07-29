# Pump Bot Development Guide

This is a trading bot for pump.fun and letsbonk.fun platforms that snipes new tokens and implements various trading strategies.

## Project Structure

- `src/` - Main source code
- `learning-examples/` - Educational scripts and examples
- `bots/` - Bot configuration files (YAML)
- `logs/` - Log files from bot executions
- `idl/` - Interface definition files for Solana programs

## Bash Commands & Development

### Setup Commands
```bash
# Install dependencies
uv sync

# Activate virtual environment (Unix/macOS)
source .venv/bin/activate

# Install as editable package
uv pip install -e .
```

### Running the Bot
```bash
# Run as installed package
pump_bot

# Run directly
uv run src/bot_runner.py
```

### Learning Examples
```bash
# Bonding curve status
uv run learning-examples/bonding-curve-progress/get_bonding_curve_status.py TOKEN_ADDRESS

# Listen to migrations
uv run learning-examples/listen-migrations/listen_logsubscribe.py
uv run learning-examples/listen-migrations/listen_blocksubscribe_old_raydium.py

# Compute associated bonding curve
uv run learning-examples/compute_associated_bonding_curve.py

# Listen to new tokens
uv run learning-examples/listen-new-tokens/listen_logsubscribe_abc.py
uv run learning-examples/listen-new-tokens/compare_listeners.py
```

### Verifying pump.fun v2 trade instructions
```bash
# Offline: cross-check buy_v2/sell_v2 account layouts, PDA/ATA derivations,
# instruction encoding and quote-asset config against idl/pump_fun_idl.json
uv run learning-examples/verify_v2_account_layout.py

# Mainnet, no funds moved: simulate buy_v2/sell_v2 for one coin, report CU
uv run learning-examples/simulate_v2_trades.py <MINT>

# Mainnet, no funds moved: run the bot's whole buy path against a fresh coin
uv run learning-examples/simulate_bot_buy_path.py
uv run learning-examples/simulate_bot_buy_path.py --no-extreme-fast
```
Run all three after any pump.fun program upgrade. The simulations report
`unitsConsumed`; use it to retune `get_buy_compute_unit_limit` /
`get_sell_compute_unit_limit` in `platforms/pumpfun/instruction_builder.py`.

### Verifying transaction-status handling
```bash
# Offline: stub checks plus a scan that every example verifies meta.err
uv run learning-examples/verify_tx_status_checks.py

# Adds a mainnet replay of the reverted signatures from issue #175
uv run learning-examples/verify_tx_status_checks.py --live
```

`confirm_transaction` answers "did this land in a block?", never "did it
succeed". A landed transaction can have reverted, and RPC reports that only in
`meta.err`. Reporting success without reading it is issue #175: buys reverting
with `BuybackFeeRecipientMissing` (6062) printed as confirmed buys.

- Examples use `learning-examples/tx_status.py` — `confirm_and_assert` in place
  of a bare `confirm_transaction`, or `assert_transaction_succeeded` after one.
  The verifier above fails the build if a new example skips it.
- The bot uses `SolanaClient.confirm_transaction`, which folds `meta.err` into
  its return value. **Read the boolean** — discarding it is the same bug.
- `_get_transaction_result` must send `maxSupportedTransactionVersion: 0` or the
  RPC rejects every versioned (v0) transaction with `-32015`, and a good trade
  reads back as unconfirmed.
- `build_and_send_transaction` returns a solders `Signature`, not a `str`. A
  `Signature` is not JSON serializable and does not support slicing; a `str` is
  rejected by solana-py's `confirm_transaction`. Normalize at the boundary.
- `post_rpc` must catch `asyncio.TimeoutError` alongside `aiohttp.ClientError`.
  aiohttp raises the former when the request timeout fires and it is **not** a
  `ClientError`, so leaving it out lets every RPC timeout escape unretried —
  and `str()` on it is empty, so the caller logs a blank reason. A slow
  `getAccountInfo` is enough to take down a whole listener run this way.

### Code Quality
```bash
# Format code
ruff format

# Lint code
ruff check

# Fix linting issues
ruff check --fix
```

## Pump.fun protocol notes (gotchas)

The IDLs under `idl/` are vendored verbatim from `github.com/pump-fun/pump-public-docs`
(`idl/pump.json` → `pump_fun_idl.json`, `pump_amm.json` → `pump_swap_idl.json`,
`pump_fees.json`). Refresh them from upstream rather than hand-editing.

### Quote assets and the v2 trade instructions (current path)

- pump.fun supports quote assets other than SOL. `BondingCurve.quote_mint` is
  `Pubkey::default()` (all zeros) for SOL-paired coins; USDC
  (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) is whitelisted in `Global`.
  **Legacy `buy`/`sell` cannot trade non-SOL-paired coins at all.**
- The bot uses **`buy_v2` (27 accounts)** and **`sell_v2` (26 accounts)**. Every
  account is mandatory and the order is identical for every coin — SOL or USDC
  paired, mayhem or not, cashback or not. `sell_v2` is `buy_v2` minus
  `global_volume_accumulator`. Layouts live in `_BUY_V2_ACCOUNTS` /
  `_SELL_V2_ACCOUNTS` in `platforms/pumpfun/instruction_builder.py` and are
  machine-checked against the IDL by `learning-examples/verify_v2_account_layout.py`.
- v2 args carry **no `track_volume` OptionBool** (24-byte data: discriminator +
  two u64). Volume tracking is unconditional now that `user_volume_accumulator`
  is mandatory. `max_sol_cost`/`min_sol_output` are in the **quote mint's** raw
  units — lamports for SOL, 1e-6 for USDC.
- Even for SOL-paired coins you must pass **wrapped SOL** as `quote_mint`, not
  `Pubkey::default()`. Transfers still happen in native SOL, and the
  `associated_quote_*` accounts are only seed-constrained — do **not** create the
  user's WSOL ATA, it would burn ~0.002 SOL of rent for nothing.
- Fee recipients: 24 total, in three sets of 8 (`NORMAL_FEE_RECIPIENTS`,
  `RESERVED_FEE_RECIPIENTS` for mayhem coins, `BUYBACK_FEE_RECIPIENTS`). Every
  v2 buy/sell needs a `fee_recipient` **and** a `buyback_fee_recipient`.
- `sharing_config` (PDA `["sharing-config", base_mint]`) lives under the **pump
  fees program**, not the pump program. Easy to derive against the wrong program.

### BondingCurve account layout

- The account is **151 bytes**: 8-byte discriminator, then
  `virtual_token_reserves, virtual_quote_reserves, real_token_reserves,
  real_quote_reserves, token_total_supply` (u64 each), `complete` (1B, offset 48),
  `creator` (32B, offset 49), `is_mayhem_mode` (offset 81),
  `is_cashback_coin` (offset 82), `quote_mint` (32B, offset 83), then 36 reserved
  zero bytes. The documented struct is 115 bytes; the extra 36 are padding.
- The SOL-named fields were **renamed**: `virtual_sol_reserves` →
  `virtual_quote_reserves`, `real_sol_reserves` → `real_quote_reserves`. The
  curve manager still exposes the old names as aliases, so pre-existing callers
  keep working for SOL-paired coins — but anything doing arithmetic must scale by
  the quote mint's decimals (`quote_units_per_token`), not a hardcoded 1e9.
- PumpSwap `Pool` gained a trailing **`virtual_quote_reserves: i128`** (16 bytes,
  offset 245). Pool fields end at 261; live accounts are **301 bytes** with
  trailing padding. Quote against **effective** reserves:
  `pool_quote_token_account.amount + virtual_quote_reserves`.
  **Upstream's release note claims it is 0 on all pools — that is out of date.**
  Verified on mainnet: pool `6Bv1JM1deBPe…` carries 17.584505433 SOL of virtual
  reserves against a 148.455 SOL vault, so quoting off the raw vault balance
  under-prices by ~10.6%. It is `i128`, not `u64` — reading 8 bytes happens to
  work only while the high half is zero.

### Coin creation

- The IDL instruction is `create_v2` (snake_case). Args: `name (str),
  symbol (str), uri (str), creator (pubkey), is_mayhem_mode (bool),
  is_cashback_enabled (OptionBool 1B)`. `OptionBool` is a struct wrapping a single
  bool — serialized as 1 byte, not 2.
- `create_v2` accounts 1-16 are in the IDL; accounts **17-19 are optional
  remaining accounts** (`quote_mint`, `associated_quote_bonding_curve`,
  `quote_token_program`) appended only for a non-SOL quote mint. All three or
  none. This is the only way to read a new coin's quote asset from the
  instruction rather than the event.
- `extreme_fast_mode` skips the curve-state price fetch but still refreshes
  mayhem/cashback/creator/**quote_mint** from chain, because the wrong quote mint
  means spending the wrong balance entirely. Event parsers also populate
  `quote_mint` from `CreateEvent` (which gained `quote_mint` and
  `virtual_quote_reserves` as trailing fields).

### Legacy instructions (fallback only)

Retained behind `PumpFunInstructionBuilder(..., use_legacy_instructions=True)`.
The IDL under-reports these: `buy` is **18 accounts** on-chain (IDL lists 16) and
`sell` is **16 non-cashback / 17 cashback** (IDL lists 14). The extras are
`bonding-curve-v2` (PDA `["bonding-curve-v2", mint]`) followed by a buyback fee
recipient (mutable); the cashback sell path also inserts
`user_volume_accumulator` before `bonding-curve-v2`. Prefer v2 — it is the
interface pump.fun maintains.

## Code Style & Conventions

### Python Style (Ruff Configuration)
- **Line length**: 88 characters
- **Indentation**: 4 spaces
- **Target Python**: 3.11+
- **Quote style**: Double quotes
- **Import sorting**: Enabled

### Linting Rules
- Security best practices (S)
- Type annotations (ANN)
- Exception handling (BLE, TRY)
- Code complexity (C90)
- Pylint conventions (PL)
- No commented-out code (ERA)

### Code Organization
- **Imports**: Standard library, third-party, local imports
- **Docstrings**: Google-style for functions and classes
- **Type hints**: Required for all public functions
- **Logging**: Use `get_logger(__name__)` pattern
- **Error handling**: Comprehensive try-catch with proper logging

### File Structure Patterns
- `__init__.py` files for all packages
- Separate concerns: client, trading, monitoring, platforms
- Abstract base classes in `interfaces/`
- Platform-specific implementations in `platforms/`

## Workflow & Development Practices

### Configuration Management
- Environment variables in `.env` file
- Bot configurations in YAML files under `bots/`
- Platform detection from config files
- Validation of platform-listener combinations

### Logging
- Timestamped log files in `logs/` directory
- Format: `{bot_name}_{timestamp}.log`
- Different log levels for development vs production
- Centralized logger utility in `utils/logger.py`

### Trading Architecture
- Universal trader pattern for platform abstraction
- Platform-specific implementations (pumpfun, letsbonk)
- Position tracking and management
- Priority fee management (dynamic/fixed)

### Monitoring Systems
- Multiple listener types: logs, blocks, geyser, pumpportal
- Universal listeners with platform abstraction
- Event parsing and processing
- Real-time data stream handling

### Development Workflow
1. Make changes to source code
2. Run `ruff check --fix` for linting
3. Run `ruff format` for formatting
4. Test with learning examples (standalone scripts) before deploying bots
5. Use separate processes for production bot instances

### Bot Configuration
- YAML-based configuration files
- Environment variable interpolation
- Platform-specific settings
- Trading parameters (slippage, amounts, timeouts)
- Filter configurations for token selection
- Cleanup and account management settings

### Testing Strategy
- Learning examples serve as integration tests
- Manual testing with learning scripts
- Configuration validation before bot startup
- Logging verification for debugging

## Key Features

- **Multi-platform support**: pump.fun and letsbonk.fun
- **Multiple listening methods**: WebSocket logs, block subscription, Geyser
- **Trading strategies**: Time-based, take profit/stop loss, manual
- **Priority fee management**: Dynamic and fixed fee strategies
- **Account cleanup**: Automated token account management
- **Extreme fast mode**: Skip validation for faster execution

## Security Considerations

- Private keys stored in environment variables
- No sensitive data in configuration files
- Comprehensive input validation
- Error handling to prevent crashes
- Rate limiting and retry mechanisms
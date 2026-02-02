#!/usr/bin/env python3
"""
Pump.fun Sniper Bot - Production Grade

Single-token mode sniper with:
- Yellowstone Geyser gRPC for fast detection
- Jito bundles for sandwich resistance
- Telegram alerts on buy/sell
- Environment variable configuration only
- Fast exit after one complete trade

Usage:
    python main.py
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

# Add parent src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
from solders.keypair import Keypair
import base58

from geyser import GeyserListener, TokenInfo
from trader import PumpFunTrader, TelegramNotifier, Position, LAMPORTS_PER_SOL, TOKEN_DECIMALS


class SniperBot:
    """Production-grade Pump.fun sniper bot."""

    def __init__(self):
        """Initialize bot from environment variables."""
        load_dotenv()

        # Validate required env vars
        self._validate_env()

        # Parse configuration
        self.private_key = os.getenv("PRIVATE_KEY")
        self.rpc_url = os.getenv("RPC_URL")
        self.geyser_endpoint = os.getenv("GEYSER_ENDPOINT")
        self.geyser_api_token = os.getenv("GEYSER_API_TOKEN")
        self.geyser_auth_type = os.getenv("GEYSER_AUTH_TYPE", "x-token")

        # Jito settings
        self.jito_url = os.getenv("JITO_BLOCK_ENGINE_URL")
        self.jito_tip_lamports = int(os.getenv("JITO_TIP_LAMPORTS", "50000"))

        # Trading parameters
        self.buy_amount_sol = float(os.getenv("BUY_AMOUNT_SOL", "0.05"))
        self.buy_slippage = float(os.getenv("BUY_SLIPPAGE", "0.3"))
        self.sell_slippage = float(os.getenv("SELL_SLIPPAGE", "0.2"))
        self.compute_units_buy = int(os.getenv("COMPUTE_UNITS_BUY", "120000"))
        self.compute_units_sell = int(os.getenv("COMPUTE_UNITS_SELL", "80000"))
        self.priority_fee = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "500000"))

        # Exit strategy
        self.exit_strategy = os.getenv("EXIT_STRATEGY", "time_based")
        self.max_hold_time = int(os.getenv("MAX_HOLD_TIME_SECONDS", "60"))
        self.take_profit_percent = float(os.getenv("TAKE_PROFIT_PERCENT", "0.5"))
        self.stop_loss_percent = float(os.getenv("STOP_LOSS_PERCENT", "0.2"))
        self.price_check_interval = float(os.getenv("PRICE_CHECK_INTERVAL", "2"))

        # Filters
        self.match_string = os.getenv("MATCH_STRING", "").strip() or None
        self.creator_address = os.getenv("CREATOR_ADDRESS", "").strip() or None
        self.max_token_age = float(os.getenv("MAX_TOKEN_AGE_SECONDS", "5"))

        # Telegram
        self.telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        # Debug
        self.debug = os.getenv("DEBUG", "false").lower() == "true"

        # Initialize keypair
        self.keypair = Keypair.from_bytes(base58.b58decode(self.private_key))

        # Components
        self.listener: GeyserListener | None = None
        self.trader: PumpFunTrader | None = None
        self.telegram: TelegramNotifier | None = None

        # State
        self.position: Position | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    def _validate_env(self):
        """Validate required environment variables."""
        required = [
            "PRIVATE_KEY",
            "RPC_URL",
            "GEYSER_ENDPOINT",
            "GEYSER_API_TOKEN",
        ]

        missing = [var for var in required if not os.getenv(var)]
        if missing:
            print(f"[ERROR] Missing required environment variables: {', '.join(missing)}")
            print("[ERROR] Copy .env.example to .env and fill in your values")
            sys.exit(1)

    async def start(self):
        """Start the sniper bot."""
        print("\n" + "=" * 60)
        print("PUMP.FUN SNIPER BOT - PRODUCTION MODE")
        print("=" * 60)

        # Print configuration
        print(f"\nWallet: {self.keypair.pubkey()}")
        print(f"RPC: {self.rpc_url[:50]}...")
        print(f"Geyser: {self.geyser_endpoint}")
        print(f"Jito: {'Enabled' if self.jito_url else 'Disabled'}")
        print(f"Buy Amount: {self.buy_amount_sol} SOL")
        print(f"Exit Strategy: {self.exit_strategy}")
        print(f"Telegram: {'Enabled' if self.telegram_enabled else 'Disabled'}")

        if self.match_string:
            print(f"Filter: name/symbol contains '{self.match_string}'")
        if self.creator_address:
            print(f"Filter: creator = {self.creator_address}")

        print("\n" + "-" * 60)

        # Initialize Telegram
        if self.telegram_enabled:
            self.telegram = TelegramNotifier(
                self.telegram_bot_token,
                self.telegram_chat_id,
            )
            await self.telegram.send("Sniper bot started")

        # Initialize trader
        self.trader = PumpFunTrader(
            rpc_url=self.rpc_url,
            keypair=self.keypair,
            jito_url=self.jito_url,
            jito_tip_lamports=self.jito_tip_lamports,
            buy_slippage=self.buy_slippage,
            sell_slippage=self.sell_slippage,
            compute_units_buy=self.compute_units_buy,
            compute_units_sell=self.compute_units_sell,
            priority_fee=self.priority_fee,
            telegram=self.telegram,
        )
        await self.trader.start()

        # Initialize Geyser listener
        self.listener = GeyserListener(
            endpoint=self.geyser_endpoint,
            api_token=self.geyser_api_token,
            auth_type=self.geyser_auth_type,
        )

        self._running = True

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        print("\n[BOT] Listening for new tokens...")
        print("[BOT] Press Ctrl+C to stop\n")

        try:
            # Start listening - single token mode
            token = await self.listener.listen(
                callback=self._on_new_token,
                match_string=self.match_string,
                creator_address=self.creator_address,
                max_token_age=self.max_token_age,
                stop_after_first=True,
            )

            if token and self.position:
                # Execute exit strategy
                await self._execute_exit_strategy()

        except asyncio.CancelledError:
            print("\n[BOT] Cancelled")

        finally:
            await self.stop()

    async def stop(self):
        """Stop the bot and clean up."""
        print("\n[BOT] Shutting down...")
        self._running = False

        if self.listener:
            self.listener.stop()

        if self.trader:
            await self.trader.stop()

        if self.telegram:
            await self.telegram.send("Sniper bot stopped")

        print("[BOT] Shutdown complete")

    def _handle_shutdown(self):
        """Handle shutdown signal."""
        print("\n[BOT] Shutdown signal received")
        self._shutdown_event.set()
        if self.listener:
            self.listener.stop()

    async def _on_new_token(self, token: TokenInfo):
        """Handle new token detection."""
        print(f"\n[BOT] NEW TOKEN DETECTED!")
        print(f"[BOT] Name: {token.name}")
        print(f"[BOT] Symbol: {token.symbol}")
        print(f"[BOT] Mint: {token.mint}")
        print(f"[BOT] Creator: {token.creator}")
        print(f"[BOT] Token2022: {token.is_token_2022}")

        # Execute buy
        use_jito = bool(self.jito_url)
        result = await self.trader.buy(token, self.buy_amount_sol, use_jito=use_jito)

        if not result.success:
            print(f"[BOT] Buy failed: {result.error}")
            return

        print(f"[BOT] Buy successful!")
        print(f"[BOT] Signature: {result.signature}")
        print(f"[BOT] Tokens: {(result.tokens_amount or 0) / (10 ** TOKEN_DECIMALS):,.2f}")
        print(f"[BOT] SOL spent: {(result.sol_amount or 0) / LAMPORTS_PER_SOL:.6f}")

        # Create position
        self.position = Position(
            token_info=token,
            entry_price=result.price_per_token or 0,
            tokens_held=result.tokens_amount or 0,
            sol_spent=result.sol_amount or 0,
            entry_time=time.time(),
        )

    async def _execute_exit_strategy(self):
        """Execute the configured exit strategy."""
        if not self.position:
            return

        print(f"\n[BOT] Executing {self.exit_strategy} exit strategy...")

        if self.exit_strategy == "immediate":
            await self._sell_position()

        elif self.exit_strategy == "time_based":
            await self._time_based_exit()

        elif self.exit_strategy == "tp_sl":
            await self._tp_sl_exit()

        else:
            print(f"[BOT] Unknown exit strategy: {self.exit_strategy}")
            await self._sell_position()

    async def _time_based_exit(self):
        """Hold for configured time then sell."""
        if not self.position:
            return

        elapsed = time.time() - self.position.entry_time
        remaining = self.max_hold_time - elapsed

        if remaining > 0:
            print(f"[BOT] Holding for {remaining:.1f} more seconds...")

            # Wait with shutdown check
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=remaining,
                )
                print("[BOT] Shutdown requested, selling immediately")
            except asyncio.TimeoutError:
                pass

        await self._sell_position()

    async def _tp_sl_exit(self):
        """Monitor price and exit on take profit or stop loss."""
        if not self.position:
            return

        entry_price = self.position.entry_price
        tp_price = entry_price * (1 + self.take_profit_percent)
        sl_price = entry_price * (1 - self.stop_loss_percent)

        print(f"[BOT] Entry price: {entry_price:.10f} SOL")
        print(f"[BOT] Take profit at: {tp_price:.10f} SOL ({self.take_profit_percent*100:.0f}%)")
        print(f"[BOT] Stop loss at: {sl_price:.10f} SOL ({self.stop_loss_percent*100:.0f}%)")

        while self._running and not self._shutdown_event.is_set():
            # Check max hold time
            elapsed = time.time() - self.position.entry_time
            if elapsed >= self.max_hold_time:
                print(f"[BOT] Max hold time reached ({self.max_hold_time}s)")
                break

            # Get current price
            pool_state = await self.trader.get_pool_state(
                self.position.token_info.bonding_curve
            )

            if not pool_state:
                print("[BOT] Failed to get price, retrying...")
                await asyncio.sleep(self.price_check_interval)
                continue

            current_price = pool_state["price_per_token"]
            pnl_percent = ((current_price - entry_price) / entry_price) * 100

            print(f"[BOT] Price: {current_price:.10f} | PnL: {pnl_percent:+.2f}%")

            # Check take profit
            if current_price >= tp_price:
                print(f"[BOT] Take profit triggered!")
                break

            # Check stop loss
            if current_price <= sl_price:
                print(f"[BOT] Stop loss triggered!")
                break

            # Check if token graduated (curve completed)
            if pool_state.get("complete"):
                print("[BOT] Token graduated! Selling...")
                break

            # Wait for next check
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.price_check_interval,
                )
                print("[BOT] Shutdown requested")
                break
            except asyncio.TimeoutError:
                pass

        await self._sell_position()

    async def _sell_position(self):
        """Sell the current position."""
        if not self.position:
            return

        print(f"\n[BOT] Selling position...")

        # Get current token balance
        balance = await self.trader.get_token_balance(self.position.token_info)

        if balance <= 0:
            print("[BOT] No tokens to sell")
            return

        # Execute sell
        use_jito = bool(self.jito_url)
        result = await self.trader.sell(
            self.position.token_info,
            token_amount=balance,
            use_jito=use_jito,
        )

        if not result.success:
            print(f"[BOT] Sell failed: {result.error}")
            return

        # Calculate PnL
        sol_received = result.sol_amount or 0
        sol_spent = self.position.sol_spent
        pnl_lamports = sol_received - sol_spent
        pnl_percent = (pnl_lamports / sol_spent * 100) if sol_spent > 0 else 0

        print(f"[BOT] Sell successful!")
        print(f"[BOT] Signature: {result.signature}")
        print(f"[BOT] SOL received: {sol_received / LAMPORTS_PER_SOL:.6f}")
        print(f"[BOT] PnL: {pnl_lamports / LAMPORTS_PER_SOL:+.6f} SOL ({pnl_percent:+.2f}%)")

        # Send Telegram notification
        if self.telegram:
            await self.telegram.notify_sell(
                self.position.token_info,
                result,
                pnl_percent,
            )

        self.position = None


async def main():
    """Main entry point."""
    bot = SniperBot()

    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[ERROR] {e}")
        if os.getenv("DEBUG", "false").lower() == "true":
            import traceback
            traceback.print_exc()
    finally:
        await bot.stop()


if __name__ == "__main__":
    # Use uvloop for better performance if available
    try:
        import uvloop
        uvloop.install()
        print("[INIT] Using uvloop")
    except ImportError:
        pass

    asyncio.run(main())

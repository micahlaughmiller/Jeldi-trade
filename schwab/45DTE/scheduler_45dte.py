import logging
import sys
import time as time_module
from datetime import datetime, time as dt_time

# Configure logging output to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("__main__")

# Import local system components
try:
    from order_manager import OrderManager
except ImportError:
    from order_manager import OrderManager

# Import Schwab Connector from your project context
try:
    from schwab_connector import SchwabConnector
except ImportError:
    class SchwabConnector:
        """Fallback mock wrapper if import path differs."""
        def get_open_positions(self):
            return []

class Scheduler45DTE:
    def __init__(self, schwab_connector=None, logger_instance=None):
        self.schwab = schwab_connector or SchwabConnector()
        self.logger = logger_instance or logger
        self.order_mgr = OrderManager(self.schwab)
    def is_market_open(api):
        clock = api.get_clock()
        return clock.is_open

    def run_trading_bot(api):
        while True:
            try:
                if is_market_open(api):
                    # Run your minute checks, 45DTE management, and scans here
                    minute_check()
                else:
                    # Market is closed; sleep longer to prevent wasted CPU cycles
                    print("Market is closed. Bot is idling...")
                    time.sleep(300)  # Check every 5 minutes
            except Exception as e:
                print(f"Error in main loop: {e}")
            
            time.sleep(60) # Standard tick rate during open hours


    def _load_existing_positions(self):
        """Loads existing positions from Schwab, pairs legs into credit spreads, and fetches fill credits."""
        try:
            positions = self.schwab.get_open_positions()

            if positions:
                log_msg = f"Loaded {len(positions)} raw position legs from Schwab"
                print(f"[POSITIONS_LOADED] {log_msg}")

                reconstructed_spreads = self.order_mgr.reconstruct_spreads_from_alpaca(positions)

                for spread_id, spread_data in reconstructed_spreads.items():
                    self.order_mgr.open_orders[spread_id] = spread_data

                pair_msg = f"Reconstructed {len(reconstructed_spreads)} paired credit spread positions with entry credits"
                print(f"[SPREADS_PAIRED] {pair_msg}")
            else:
                print("[POSITIONS_LOADED] No existing positions found")

        except Exception as e:
            err_msg = f"Error loading existing positions: {e}"
            print(f"[POSITIONS_LOAD_ERROR] {err_msg}")

    def display_positions_and_pnl(self):
        """Prints the portfolio summary including open spread positions and unrealized P/L."""
        open_orders = self.order_mgr.get_open_orders()
        closed_orders = self.order_mgr.get_closed_orders()

        print("\n" + "=" * 70)
        print("CREDIT SPREAD PORTFOLIO P/L SUMMARY")
        print("=" * 70)

        print("\n--- OPEN SPREAD POSITIONS (UNREALIZED P/L) ---")
        total_unrealized_pnl = 0.0

        if open_orders:
            for spread_id, spread in open_orders.items():
                symbol = spread.get('symbol', 'N/A')
                strategy = spread.get('strategy', 'Spread')
                contracts = spread.get('contracts', 0)
                short_strike = spread.get('short_strike', 0.0)
                long_strike = spread.get('long_strike', 0.0)
                entry_credit = spread.get('entry_credit', 0.0)
                current_credit = spread.get('current_credit', 0.0)
                
                unrealized_pnl = (entry_credit - current_credit) * 100.0 * contracts
                total_unrealized_pnl += unrealized_pnl

                print(f"• [{symbol}] {strategy} ({short_strike}/{long_strike}) | Contracts: {contracts}")
                print(f"  Entry Credit: ${entry_credit:.2f} | Current Mark: ${current_credit:.2f} | Unrealized P/L: ${unrealized_pnl:,.2f}")
        else:
            print("No open positions.")

        print("\n--- CLOSED SPREAD TRADES (REALIZED P/L) ---")
        total_realized_pnl = 0.0
        if closed_orders:
            for trade_id, trade in closed_orders.items():
                pnl = trade.get('realized_pnl', 0.0)
                total_realized_pnl += pnl
                print(f"• [{trade.get('symbol')}] Realized P/L: ${pnl:,.2f}")
        else:
            print("No closed trades in current session.")

        net_pnl = total_realized_pnl + total_unrealized_pnl
        print("\n" + "=" * 70)
        print(f"TOTAL REALIZED P&L:    $ {total_realized_pnl:>12,.2f}")
        print(f"TOTAL UNREALIZED P&L: $ {total_unrealized_pnl:>12,.2f}")
        print("-" * 70)
        print(f"NET COMBINED P&L:      $ {net_pnl:>12,.2f}")
        print("=" * 70 + "\n")

    def initial_check_and_scan(self):
        """Checks if current time is at or past market open / scan time, 
        and triggers an immediate scan if so.
        """
        now = datetime.now()
        current_time = now.time()
        
        scan_target_time = dt_time(9, 35)
        market_open_time = dt_time(9, 30)
        market_close_time = dt_time(16, 0)

        print(f"[STARTUP_CHECK] Current local time: {current_time.strftime('%H:%M:%S')}")

        if now.weekday() < 5 and market_open_time <= current_time <= market_close_time:
            if current_time >= scan_target_time:
                print("[STARTUP_CHECK] Market is open and scan time has passed. Triggering immediate scan...")
                # Call your scan method here when active:
                # self.scan_sp500_for_signals() 
            else:
                print("[STARTUP_CHECK] Market is open, but it's before the scan window (09:35). Waiting for scheduled run.")
        else:
            print("[STARTUP_CHECK] Outside regular market hours. Skipping immediate startup scan.")

    def run(self):
        """Initialize and start the trading system execution loop."""
        print("[SYSTEM_START] Scheduler initialized and ready")
        print("[SCHEDULER_STARTED] Trading system now running")
        
        self._load_existing_positions()
        self.display_positions_and_pnl()

        # Run immediate time & scan check
        self.initial_check_and_scan()

        print("45DTE SCHEDULER RUNNING - Press Ctrl+C to stop")
        try:
            while True:
                time_module.sleep(1)
        except KeyboardInterrupt:
            print("\n[SYSTEM_STOP] Scheduler stopped by user.")

if __name__ == "__main__":
    schwab = SchwabConnector()
    scheduler = Scheduler45DTE(schwab_connector=schwab)
    scheduler.run()
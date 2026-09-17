import schedule
import time
from datetime import datetime
import logging
import sys
from schwab_handler import SchwabOrderHandler
from risk_manager import RiskManager
from config import MARKET_TIMEZONE, TRADER_NAME, ACCOUNT_NAME, DEBUG_MODE, LOG_FILE
import pytz

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

CT = pytz.timezone('US/Central')


class TradingScheduler:
    """Schwab 0DTE Trading Scheduler"""

    def __init__(self):
        self.handler = SchwabOrderHandler()
        self.risk = RiskManager()
        self.trading_active = False
        self.can_open_new_trades = False

        account_info = self.handler.get_account_info()
        equity = account_info.get('equity', 0) if account_info else 0

        logger.info("=" * 80)
        logger.info(f"0DTE SCHEDULER INITIALIZED")
        logger.info(f"Trading: {ACCOUNT_NAME}")
        logger.info(f"Mode: SCHWAB PAPER TRADING")
        logger.info("=" * 80)
    
    def start_trading_day(self):
        """Called at 8:00 AM CT"""
        try:
            account_info = self.handler.get_account_info()
            if not account_info:
                logger.error("Failed to get account info")
                return
            
            equity = account_info['buying_power']
            self.risk.reset_day(equity)
            self.trading_active = True
            self.can_open_new_trades = True
            
            logger.info("TRADING DAY STARTED")
            logger.info(f"   Equity: ${equity:,.2f}")
            
            snapshot = self.risk.snapshot()
            logger.info(f"   Tier: {snapshot['tier']}")
            logger.info(f"   Max Risk: ${snapshot['risk_budget']:,.2f}")
            logger.info(f"   Spread Width: {snapshot['spread_width']} pt")
        
        except Exception as e:
            logger.error(f"Error starting trading day: {e}")
    
    def stop_new_entries(self):
        """Called at 12:00 PM CT - Stop opening new trades"""
        self.can_open_new_trades = False
        logger.info("=" * 80)
        logger.info("STOP: No new trades after 12:00 PM CT")
        logger.info("      Existing positions will continue to be managed")
        logger.info("=" * 80)
    
    def minute_check(self):
        """Called every 15 seconds during market hours"""
        if not self.trading_active:
            return
        
        try:
            account_info = self.handler.get_account_info()
            if account_info:
                self.risk.update_equity(account_info['buying_power'])
            
            # Check if we can open new trades
            now = datetime.now(CT).time()
            if now >= datetime.strptime("13:00", "%H:%M").time():
                self.can_open_new_trades = False
            
            # IMPLEMENT YOUR SIGNAL LOGIC HERE
            # Example:
            # if self.can_open_new_trades:
            #     signal = self.strategy.check_for_entry()
            #     if signal:
            #         self.open_trade(signal)
            # 
            # Then manage existing positions:
            # self.manage_open_positions()
        
        except Exception as e:
            logger.error(f"Error in minute check: {e}")
    
    def end_trading_day(self):
        """Called at 12:30 PM CT (1:30 PM ET)"""
        logger.info("TRADING DAY ENDING AT 12:30 PM CT")
        
        # Get and display open positions
        positions = self.handler.get_positions()
        if positions:
            logger.info("=" * 80)
            logger.info("OPEN POSITIONS TO CLOSE:")
            for pos in positions:
                if isinstance(pos, dict):
                    symbol = pos.get('symbol', 'N/A')
                    qty = int(pos.get('qty', 0))
                    entry_price = float(pos.get('avg_fill_price', 0))
                    current_price = float(pos.get('current_price', 0))
                    unrealized_pl = float(pos.get('unrealized_pl', 0))
                    unrealized_plpc = float(pos.get('unrealized_plpc', 0))
                else:
                    symbol = pos.symbol
                    qty = int(pos.qty)
                    entry_price = float(pos.avg_fill_price)
                    current_price = float(pos.current_price)
                    unrealized_pl = float(pos.unrealized_pl)
                    unrealized_plpc = float(pos.unrealized_plpc)
                
                if qty > 0:
                    logger.info(f"  {symbol}: {qty} shares | Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | P&L: ${unrealized_pl:.2f} ({unrealized_plpc:.2f}%)")
                    
                    # Close with limit order $0.05 above midpoint
                    limit_price = current_price + 0.05
                    logger.info(f"  Closing {qty} shares of {symbol} with limit order @ ${limit_price:.2f}")
            logger.info("=" * 80)
        else:
            logger.info("No open positions to close")
        
        # Cancel all orders
        self.handler.cancel_all_orders()
        
        self.trading_active = False
        self.can_open_new_trades = False
        
        # Update equity before requesting snapshot
        account_info = self.handler.get_account_info()
        if account_info:
            self.risk.update_equity(account_info.get('buying_power', account_info.get('equity', 0)))

        try:
            snapshot = self.risk.snapshot()
            logger.info("=" * 80)
            logger.info(f"FINAL P&L: ${snapshot.get('daily_pnl', 0.0):.2f}")
            logger.info(f"Trades Completed: {snapshot.get('trades_today', 0)}")
            logger.info("=" * 80)
        except Exception as e:
            logger.error(f"Error logging final snapshot: {e}")
    
    def setup_schedule(self):
        """Configure trading schedule"""
        schedule.every().day.at("08:00").do(self.start_trading_day)
        schedule.every(15).seconds.do(self.minute_check)
        schedule.every().day.at("12:00").do(self.stop_new_entries)
        schedule.every().day.at("12:30").do(self.end_trading_day)
        
        logger.info("Schedule configured")
        logger.info("  • 8:00 AM CT: Start trading")
        logger.info("  • Every 15 seconds: Check signals & manage positions")
        logger.info("  • 12:00 PM CT: STOP opening new trades (manage existing only)")
        logger.info("  • 12:30 PM CT: Close all positions & end day")
    
    def run(self):
        """Main loop"""
        self.setup_schedule()
        
        logger.info("\nSCHEDULER RUNNING - Press Ctrl+C to stop\n")
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        
        except KeyboardInterrupt:
            logger.info("\nSCHEDULER STOPPED")
            if self.trading_active:
                self.end_trading_day()


if __name__ == "__main__":
    scheduler = TradingScheduler()
    scheduler.run()
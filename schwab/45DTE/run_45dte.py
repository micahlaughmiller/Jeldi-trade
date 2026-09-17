"""Entry point: python run_45dte.py [--dry-run] [--once] [--reset-breaker]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_45dte
from broker import Broker
from logger_system import TradingLogger
from order_manager import OrderManager
from scheduler_45dte import Scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="45-60 DTE S&P 500 credit-spread bot")
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    parser.add_argument("--once", action="store_true", help="exit after today's end-of-day summary instead of idling")
    parser.add_argument("--reset-breaker", action="store_true", help="clear the max-loss circuit breaker before starting")
    args = parser.parse_args()

    log = TradingLogger(config_45dte.LOG_DIR)
    print(f"[SCHWAB 45DTE] Starting bot (log: {config_45dte.LOG_DIR})")
    if args.dry_run:
        print("[SCHWAB 45DTE] DRY-RUN mode: orders logged but not sent")
    if args.once:
        print("[SCHWAB 45DTE] ONCE mode: will exit after end-of-day")
    
    broker = Broker(config_45dte, dry_run=True if args.dry_run else None,
                    log=lambda message: log.log_event("BROKER", message))
    print(f"[SCHWAB 45DTE] Broker initialized")
    
    orders = OrderManager(broker, log, config_45dte)
    print(f"[SCHWAB 45DTE] OrderManager initialized")
    
    if args.reset_breaker:
        orders.risk.reset_breaker()
        print(f"[SCHWAB 45DTE] Circuit breaker reset")
    
    scheduler = Scheduler(broker, orders, log, config_45dte, once=args.once)
    print(f"[SCHWAB 45DTE] Scheduler started\n")
    
    try:
        scheduler.run()
    except KeyboardInterrupt:
        log.log_event("SHUTDOWN", "stopped by user; state saved")
        orders.save()
        print("[SCHWAB 45DTE] Stopped by user")


if __name__ == "__main__":
    main()

"""Entry point: python run_45dte.py [--dry-run] [--once] [--reset-breaker]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_45dte
from broker import Broker, BrokerError
from logger_system import TradingLogger
from order_manager import OrderManager
from scheduler_45dte import Scheduler

# "Alpaca" or "schwab" from the parent folder, so this file is identical in both copies.
LABEL = f"[{Path(__file__).resolve().parents[1].name.upper()} 45DTE]"


def main() -> None:
    parser = argparse.ArgumentParser(description="45-60 DTE S&P 500 credit-spread bot")
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    parser.add_argument("--once", action="store_true", help="exit after today's end-of-day summary instead of idling")
    parser.add_argument("--reset-breaker", action="store_true", help="clear the max-loss circuit breaker before starting")
    args = parser.parse_args()

    log = TradingLogger(config_45dte.LOG_DIR)
    print(f"{LABEL} Starting bot (log folder: {Path(config_45dte.LOG_DIR).resolve()})")
    if args.dry_run:
        print(f"{LABEL} DRY-RUN mode: orders logged but not sent")
    if args.once:
        print(f"{LABEL} ONCE mode: will exit after end-of-day")

    broker = Broker(config_45dte, dry_run=True if args.dry_run else None,
                    log=lambda message: log.log_event("BROKER", message))
    orders = OrderManager(broker, log, config_45dte)
    if args.reset_breaker:
        orders.risk.reset_breaker()
        print(f"{LABEL} Circuit breaker reset")
    scheduler = Scheduler(broker, orders, log, config_45dte, once=args.once)

    try:
        scheduler.run()
    except KeyboardInterrupt:
        log.log_event("SHUTDOWN", "stopped by user; state saved")
        orders.save()
        print(f"{LABEL} Stopped by user")
    except BrokerError as exc:
        log.log_event("STARTUP_ERROR", str(exc))
        print(f"\n{LABEL} BROKER ERROR: {exc}")
        if "401" in str(exc) or "unauthorized" in str(exc).lower():
            print(f"{LABEL} The broker rejected the API keys. Open the .env file in this folder and check "
                  f"the key/secret values (no quotes, no spaces). If you rotated keys, paste the new ones.")
        else:
            print(f"{LABEL} Check the .env file in this folder and your internet connection, then start again.")
        sys.exit(1)


if __name__ == "__main__":
    main()

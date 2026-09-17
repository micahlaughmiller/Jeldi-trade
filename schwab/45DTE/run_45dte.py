#!/usr/bin/env python3
"""
Entry point for 45-60 DTE Credit Spread Trading System
Usage:
    python run_45dte.py          # Run in continuous mode
    python run_45dte.py test     # Run single day backtest
"""

from scheduler_45dte import Scheduler45DTE
import sys


def main():
    """Main entry point."""
    scheduler = Scheduler45DTE()

    if len(sys.argv) > 1:
        if sys.argv[1] == 'test':
            # Single day backtest mode
            scheduler.run_backtest_single_day()
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python run_45dte.py [test]")
            sys.exit(1)
    else:
        # Continuous mode
        try:
            scheduler.run_continuously()
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            scheduler.stop()


if __name__ == "__main__":
    main()

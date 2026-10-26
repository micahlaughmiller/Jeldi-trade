"""Reset the simulated Schwab paper account (SCHWAB_MODE=sim).

    python sim_reset.py [--equity 2000] [--yes]

Deletes LOG_DIR/sim_state.json and LOG_DIR/sim_equity_history.csv, then re-initialises
the account at --equity (default: config SIM_STARTING_EQUITY).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import config as cfg
except ImportError:
    import config_45dte as cfg  # type: ignore

from paper_sim import PaperBroker


def main() -> int:
    parser = argparse.ArgumentParser(description="reset the simulated Schwab paper account")
    parser.add_argument("--equity", type=float, default=None, help="starting equity (default: SIM_STARTING_EQUITY)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    sim = PaperBroker(None, cfg, log=print)  # no data broker needed: reset touches only the state files
    equity = args.equity if args.equity is not None else sim.starting_equity
    print(f"State file:   {sim.state_path}")
    print(f"Equity file:  {sim.equity_path}")
    print(f"Current cash: {sim.state['cash']:,.2f}  positions: {len(sim.state['positions'])}  "
          f"orders: {len(sim.state['orders'])}")
    if not args.yes:
        answer = input(f"Wipe the simulated account and restart at ${equity:,.2f}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    sim.reset(equity)
    return 0


if __name__ == "__main__":
    sys.exit(main())

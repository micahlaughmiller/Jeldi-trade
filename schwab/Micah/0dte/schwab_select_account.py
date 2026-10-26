"""Choose the Schwab account the bots trade (run it after every token renewal).

    python schwab_select_account.py            # list the linked accounts, pick one, confirm with its last 4 digits
    python schwab_select_account.py --show     # show the account selected now

Live orders are refused until an account has been selected, and a selection stops working when that account is no longer linked to the login.
Uses the same token as the bot; needs a valid token -- run schwab_login.py first if not.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import schwab_accounts

import config

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None, broker_factory=None, input_fn=input, print_fn=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="only show the selected account")
    parser.add_argument("--allow-shared-account", action="store_true",
                        help="allow an account that another person folder already trades (two bots on one account: not recommended)")
    args = parser.parse_args(argv)
    current = schwab_accounts.read_selection(schwab_accounts.selection_path(HERE))
    if args.show:
        print_fn(f"Selected account: {schwab_accounts.describe(current)} (chosen {current['selected_at']})" if current
                 else "No account selected yet: run  python schwab_select_account.py")
        return 0
    if broker_factory is None:
        from broker import SchwabBroker
        broker_factory = lambda: SchwabBroker(config, dry_run=True)   # noqa: E731 -- read-only use: it only lists accounts
    try:
        broker = broker_factory()
        client = broker._get_client()
        numbers = broker._json(broker._request(client.get_account_numbers))
    except Exception as exc:                             # noqa: BLE001 -- say what is wrong instead of a traceback
        print_fn(f"Could not list the Schwab accounts: {exc}")
        return 1
    try:
        accounts = broker._json(broker._request(client.get_accounts))
    except Exception:                                    # noqa: BLE001 -- balances are a courtesy; the account numbers alone are enough to choose
        accounts = []
    if current:
        print_fn(f"Currently selected: {schwab_accounts.describe(current)}")
    try:
        entry = schwab_accounts.select_interactively(numbers, accounts, [HERE], input_fn, print_fn,
                                                     allow_shared=args.allow_shared_account)
    except (EOFError, KeyboardInterrupt):
        print_fn("\nCancelled: nothing was changed.")
        return 1
    return 0 if entry else 1


if __name__ == "__main__":
    sys.exit(main())

"""Cancel every open order and liquidate every position on the Alpaca PAPER account.

    python alpaca-reset.py [--yes]

Keys come from this folder's .env via config (ACTIVE_TRADER selects ASTRA or
CLAUDE). Refuses to run against the live URL. To restore the paper account's
starting cash, use Settings -> Reset Paper Account at https://app.alpaca.markets/.
"""

import argparse
import sys

import requests

import config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if "paper-api" not in config.ALPACA_BASE_URL:
        sys.exit(f"Refusing to reset a non-paper account: {config.ALPACA_BASE_URL}")
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        sys.exit(f"No API keys for ACTIVE_TRADER={config.ACTIVE_TRADER} in .env")

    headers = {"APCA-API-KEY-ID": config.ALPACA_API_KEY, "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY}
    base = config.ALPACA_BASE_URL.rstrip("/")

    account = requests.get(f"{base}/v2/account", headers=headers, timeout=30).json()
    print(f"{config.TRADER_NAME} paper account {account.get('id', '?')[:8]}  equity ${float(account.get('equity', 0)):,.2f}")
    if not args.yes and input("Cancel all orders and close all positions? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    r = requests.delete(f"{base}/v2/orders", headers=headers, timeout=30)
    print(f"1. Cancel all orders -> HTTP {r.status_code}")
    r = requests.delete(f"{base}/v2/positions", headers=headers, params={"cancel_orders": "true"}, timeout=60)
    print(f"2. Close all positions -> HTTP {r.status_code}")

    remaining = requests.get(f"{base}/v2/positions", headers=headers, timeout=30).json()
    open_orders = requests.get(f"{base}/v2/orders", headers=headers, params={"status": "open"}, timeout=30).json()
    print(f"Remaining positions: {len(remaining)}   open orders: {len(open_orders)}")
    print("To restore starting cash: https://app.alpaca.markets/ -> Settings -> Reset Paper Account.")


if __name__ == "__main__":
    main()

import requests
from alpaca.trading.client import TradingClient
from config import ALPACA_API_KEY, ALPACA_BASE_URL, ALPACA_SECRET_KEY

headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

# Initialize client for Paper Trading using imported keys
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def reset_paper_account():
    print("1. Cancelling all open orders...")
    try:
        trading_client.cancel_orders()
        print("   ✓ All orders cancelled.")
    except Exception as e:
        print(f"   ✗ Error cancelling orders: {e}")

    print("2. Liquidating all open positions...")
    try:
        trading_client.close_all_positions(cancel_orders=True)
        print("   ✓ All positions closed.")
    except Exception as e:
        print(f"   ✗ Error closing positions: {e}")

    print(
        "\nAccount cleared of active trades! To set your equity back to maximum ($100k+),"
    )
    print(
        "visit: https://app.alpaca.markets/ -> Settings -> Reset Paper Account."
    )


if __name__ == "__main__":
    reset_paper_account()
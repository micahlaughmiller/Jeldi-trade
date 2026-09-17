from alpaca.trading.client import TradingClient

# Replace with your actual Paper Trading API keys
API_KEY = "PKP2L6LQ5WZNQZQPKXQAZFVI4R"
SECRET_KEY = "JD3DniYFdpp7Znr4g5HJzdnCKaaujoJfvjZGjtiB54aR"

# Initialize client for Paper Trading
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

def reset_paper_account():
    print("1. Cancelling all open orders...")
    trading_client.cancel_orders()
    print("   ✓ All orders cancelled.")

    print("2. Liquidating all open positions...")
    trading_client.close_all_positions(cancel_orders=True)
    print("   ✓ All positions closed.")

    print("\nAccount cleared of active trades! To set your equity back to maximum ($100k+),")
    print("visit: https://app.alpaca.markets/ -> Settings -> Reset Paper Account.")

if __name__ == "__main__":
    reset_paper_account()
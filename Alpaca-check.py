import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

# 1. Fetch ALL open orders (shows unfilled $0.01 long legs)
orders = requests.get(f"{ALPACA_BASE_URL}/v2/orders?status=open", headers=headers).json()
print(f"Total Working/Unfilled Orders: {len(orders)}")
for o in orders:
    print(f"  - Order: {o['side'].upper()} {o['qty']}x {o['symbol']} @ ${o['limit_price']}")

# 2. Fetch ALL filled positions
positions = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers).json()
print(f"\nTotal Active Positions: {len(positions)}")
for p in positions:
    print(f"  - Position: {p['qty']}x {p['symbol']} | Market Value: ${p['market_value']}")
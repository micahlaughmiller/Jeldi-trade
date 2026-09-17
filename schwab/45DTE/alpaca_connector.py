"""Alpaca API connector for 45-60 DTE credit spread system."""

import os
from datetime import datetime
import pandas as pd
import requests

try:
    import alpaca_trade_api as tradeapi
except ImportError:
    print("Error: alpaca-trade-api not installed. Run: pip install -r requirements.txt")
    raise

from config_45dte import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    PAPER_TRADING,
)


class AlpacaConnector:
    """Wrapper for Alpaca Trading API."""

    def __init__(self):
        """Initialize Alpaca REST client for trading and data."""
        self.api = tradeapi.REST(
            key_id=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            base_url=ALPACA_BASE_URL,
            api_version='v2'
        )

    def get_account_equity(self):
        """Get current account equity."""
        try:
            account = self.api.get_account()
            return float(account.equity)
        except Exception as e:
            print(f"Error getting account equity: {e}")
            return 0.0

    def get_buying_power(self):
        """Get available buying power."""
        try:
            account = self.api.get_account()
            return float(account.buying_power)
        except Exception as e:
            print(f"Error getting buying power: {e}")
            return 0.0

    def get_open_positions(self):
        """Get all open positions."""
        try:
            positions = self.api.list_positions()
            return positions if positions else []
        except Exception as e:
            print(f"Error getting positions: {e}")
            return []

    def get_position_by_symbol(self, symbol):
        """Get position details for a specific symbol."""
        try:
            position = self.api.get_position(symbol)
            return position
        except Exception:
            return None

    def get_historical_bars(self, symbol, timeframe="day", limit=252):
        """
        Fetch historical bars for a symbol.

        Args:
            symbol: Stock ticker
            timeframe: Candle interval (e.g., "day", "hour")
            limit: Number of bars to fetch

        Returns:
            DataFrame with OHLCV data
        """
        try:
            bars = self.api.get_barset(symbol, timeframe, limit=limit)
            if symbol in bars:
                bar_data = bars[symbol]
                # Convert to DataFrame
                df = pd.DataFrame([
                    {
                        'timestamp': bar.t,
                        'open': bar.o,
                        'high': bar.h,
                        'low': bar.l,
                        'close': bar.c,
                        'volume': bar.v,
                    }
                    for bar in bar_data
                ])
                return df
            return None
        except Exception as e:
            print(f"Error fetching bars for {symbol}: {e}")
            return None

    def place_limit_order(
        self,
        symbol,
        qty,
        side,
        limit_price,
        time_in_force="gtc",
    ):
        """
        Place a limit order.

        Args:
            symbol: Stock ticker
            qty: Quantity
            side: "buy" or "sell"
            limit_price: Limit price
            time_in_force: "gtc", "day", "opg", "cls"

        Returns:
            Order object
        """
        try:
            order = self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type='limit',
                limit_price=limit_price,
                time_in_force=time_in_force,
            )
            return order
        except Exception as e:
            print(f"Error placing limit order for {symbol}: {e}")
            return None

    def place_market_order(self, symbol, qty, side):
        """
        Place a market order.

        Args:
            symbol: Stock ticker
            qty: Quantity
            side: "buy" or "sell"

        Returns:
            Order object
        """
        try:
            order = self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type='market',
            )
            return order
        except Exception as e:
            print(f"Error placing market order for {symbol}: {e}")
            return None

    def cancel_order(self, order_id):
        """Cancel an open order by ID."""
        try:
            self.api.cancel_order(order_id)
            return True
        except Exception as e:
            print(f"Error canceling order {order_id}: {e}")
            return False

    def get_order(self, order_id):
        """Get order details by ID."""
        try:
            order = self.api.get_order(order_id)
            return order
        except Exception as e:
            print(f"Error fetching order {order_id}: {e}")
            return None

    def get_all_orders(self, status="all"):
        """Get all orders with optional status filter."""
        try:
            orders = self.api.list_orders(status=status)
            return orders if orders else []
        except Exception as e:
            print(f"Error fetching orders: {e}")
            return []

    def close_position(self, symbol):
        """Close a position by symbol."""
        try:
            self.api.close_position(symbol)
            return True
        except Exception as e:
            print(f"Error closing position {symbol}: {e}")
            return False

    def place_options_spread_order(self, symbol, qty, expiration, short_strike, long_strike, option_type, limit_price):
        """
        Place a multi-leg options spread order (mleg) via Alpaca REST API.

        Args:
            symbol: Stock ticker (e.g., "HON")
            qty: Quantity in contracts
            expiration: datetime object or string
            short_strike: Short strike price (sold)
            long_strike: Long strike price (bought)
            option_type: "call" or "put"
            limit_price: Net credit for the spread

        Returns:
            Order dict or None
        """
        try:
            # Format expiration date string
            if hasattr(expiration, 'strftime'):
                exp_str = expiration.strftime("%y%m%d")
            else:
                exp_str = str(expiration).replace('-', '')[2:]

            opt_type = "C" if option_type.lower() == "call" else "P"

            # Format OCC symbols for both legs
            short_strike_fmt = f"{int(round(short_strike * 1000)):08d}"
            long_strike_fmt = f"{int(round(long_strike * 1000)):08d}"

            short_symbol = f"{symbol}{exp_str}{opt_type}{short_strike_fmt}"
            long_symbol = f"{symbol}{exp_str}{opt_type}{long_strike_fmt}"

            headers = {
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            }

            # Single multi-leg payload so Alpaca processes both legs atomically
            payload = {
                "order_class": "mleg",
                "type": "limit",
                "limit_price": str(limit_price),
                "time_in_force": "day",
                "qty": str(qty),
                "legs": [
                    {
                        "symbol": short_symbol,
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open"
                    },
                    {
                        "symbol": long_symbol,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_open"
                    }
                ]
            }

            url = f"{ALPACA_BASE_URL}/v2/orders"
            response = requests.post(url, json=payload, headers=headers)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                print(f"❌ Alpaca MLEG API Error ({response.status_code}): {response.text}")
                return None

        except Exception as e:
            print(f"Error placing spread order: {e}")
            return None

        except Exception as e:
            print(f"Error placing spread order: {e}")
            return None

"""Schwab API connector for 45-60 DTE credit spread system (replaces Alpaca)."""

import os
import json
import requests
import logging
import pandas as pd
import warnings
import schwab
from datetime import datetime, timedelta
from typing import Optional, Dict, List

# Suppress deprecation warnings from internal dependencies
warnings.filterwarnings("ignore", category=DeprecationWarning)

from config_45dte import SCHWAB_CLIENT_ID, SCHWAB_SECRET_KEY, TOKEN_PATH, SCHWAB_BASE_URL, PAPER_TRADING

logger = logging.getLogger(__name__)

class SchwabConnector:
    def __init__(self, token_path=TOKEN_PATH):
        self.token_path = token_path
        self.client_id = SCHWAB_CLIENT_ID
        self.secret_key = SCHWAB_SECRET_KEY
        self.paper_trading = PAPER_TRADING
        self.base_url = SCHWAB_BASE_URL or "https://api.schwabapi.com/trader/v1"
        self.account_hash = None

        # Initialize schwab-py client
        self.client = self._init_schwab_client()
        self.account_hash = self._get_account_hash()

    def _init_schwab_client(self):
        """Loads client using token file or falls back to manual OAuth flow."""
        if not self.client_id or not self.secret_key:
            raise ValueError("SCHWAB_CLIENT_ID and SCHWAB_SECRET_KEY must be set in config_45dte.py / .env")

        try:
            client = schwab.auth.client_from_token_file(
                token_path=self.token_path,
                api_key=self.client_id,
                app_secret=self.secret_key,
            )
            print(f"[INFO] SchwabConnector authenticated via token at: {self.token_path}")
            return client
        except Exception as e:
            print(f"[WARNING] Could not load token from {self.token_path}: {e}")
            print("[INFO] Falling back to manual OAuth flow...")
            client = schwab.auth.client_from_manual_flow(
                api_key=self.client_id,
                app_secret=self.secret_key,
                callback_url="https://127.0.0.1",
                token_path=self.token_path,
            )
            return client

    def _get_account_hash(self) -> Optional[str]:
        """Gets primary account hash using schwab-py."""
        if self.account_hash:
            return self.account_hash

        try:
            resp = self.client.get_account_numbers()
            if resp.status_code == 200:
                accounts = resp.json()
                if accounts:
                    self.account_hash = accounts[0].get('hashValue')
                    return self.account_hash
            print(f"Error fetching account numbers: {resp.text}")
        except Exception as e:
            print(f"Error getting account hash: {e}")

        return None

    def get_account_equity(self) -> float:
        """Get current account equity."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return 0.0

            resp = self.client.get_account(account_hash)
            if resp.status_code == 200:
                account = resp.json()
                return float(account.get('securitiesAccount', {}).get('currentBalances', {}).get('liquidationValue', 0))
            return 0.0
        except Exception as e:
            print(f"Error getting account equity: {e}")
            return 0.0

    def get_buying_power(self) -> float:
        """Get available buying power."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return 0.0

            resp = self.client.get_account(account_hash)
            if resp.status_code == 200:
                account = resp.json()
                buying_power = account.get('securitiesAccount', {}).get('currentBalances', {}).get('buyingPower', 0)
                return float(buying_power)
            return 0.0
        except Exception as e:
            print(f"Error getting buying power: {e}")
            return 0.0

    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return []

            resp = self.client.get_account(
                account_hash, 
                fields=self.client.Account.Fields.POSITIONS
            )
            if resp.status_code == 200:
                account = resp.json()
                positions = account.get('securitiesAccount', {}).get('positions', [])
                return positions if positions else []
            return []
        except Exception as e:
            print(f"Error getting positions: {e}")
            return []

    def get_position_by_symbol(self, symbol: str) -> Optional[Dict]:
        """Get position details for a specific symbol."""
        try:
            positions = self.get_open_positions()
            for pos in positions:
                instrument = pos.get('instrument', {})
                if instrument.get('symbol', '').upper() == symbol.upper():
                    return pos
            return None
        except Exception as e:
            print(f"Error getting position {symbol}: {e}")
            return None

    def get_historical_bars(self, symbol: str, timeframe: str = "day", limit: int = 252) -> Optional[pd.DataFrame]:
        """Fetch historical price history using schwab-py market data client."""
        try:
            if timeframe.lower() == "day":
                frequency_type = self.client.PriceHistory.FrequencyType.DAILY
                frequency = self.client.PriceHistory.Frequency.DAILY
            else:
                frequency_type = self.client.PriceHistory.FrequencyType.MINUTE
                frequency = self.client.PriceHistory.Frequency.EVERY_MINUTE

            resp = self.client.get_price_history_every_day(
                symbol=symbol,
                frequency=frequency
            )
            
            if resp.status_code != 200:
                print(f"Error fetching price history: HTTP {resp.status_code}")
                return None

            data = resp.json()
            candles = data.get('candles', [])

            if not candles:
                return None

            df = pd.DataFrame(candles)
            df.rename(columns={'datetime': 'timestamp'}, inplace=True)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].tail(limit)
            return df.reset_index(drop=True)
        except Exception as e:
            print(f"Error fetching bars for {symbol}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return False

            resp = self.client.cancel_order(order_id, account_hash)
            return resp.status_code in [200, 204]
        except Exception as e:
            print(f"Error canceling order {order_id}: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order details by ID."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return None

            resp = self.client.get_order(order_id, account_hash)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            print(f"Error fetching order {order_id}: {e}")
            return None

    def get_all_orders(self, status: str = "all") -> List[Dict]:
        """Get all orders."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return []

            resp = self.client.get_orders_for_account(account_hash)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            print(f"Error fetching orders: {e}")
            return []
"""Schwab API handler for 0DTE options - replaces Alpaca api_handler."""

from datetime import datetime, date, timedelta
import logging
import sys
import requests
import json
import os
import schwab
from config import SCHWAB_CLIENT_ID, SCHWAB_SECRET_KEY, TOKEN_PATH, SCHWAB_BASE_URL

def get_schwab_client():
    # Pass TOKEN_PATH explicitly so schwab-py loads your existing token
    try:
        client = schwab.auth.client_from_token_file(
            token_path=TOKEN_PATH,
            api_key=SCHWAB_CLIENT_ID,
            app_secret=SCHWAB_SECRET_KEY,
        )
        return client
    except Exception as e:
        print(f"Failed to load token from {TOKEN_PATH}: {e}")
        # Fall back to manual auth if token is corrupt/invalid

# Ensure Windows console encoding
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

logger = logging.getLogger(__name__)


class SchwabOrderHandler:
    """Drop-in replacement for AlpacaOrderHandler - uses Schwab API for multi-leg orders."""

    def __init__(self):
        """Initialize connection to Schwab."""
        self.api_key = SCHWAB_CLIENT_ID
        self.secret_key = SCHWAB_SECRET_KEY
        self.base_url = SCHWAB_BASE_URL or "https://api.schwabapi.com/trader/v1"
        self.token_path = TOKEN_PATH
        self.active_orders = {}
        # Initialize schwab-py client using the configured token path
        self.client = self._init_schwab_client()
        self.account_hash = self._get_primary_account_hash()


        self.validate_connection()
    def _init_schwab_client(self):
        """Loads client using existing token file or triggers manual OAuth flow if missing/expired."""
        if not self.api_key or not self.secret_key:
            raise ValueError("SCHWAB_CLIENT_ID and SCHWAB_SECRET_KEY must be set in config.py / .env")

        try:
            # client_from_token_file handles 30-min access token auto-refreshes and token.json updates
            client = schwab.auth.client_from_token_file(
                token_path=self.token_path,
                api_key=self.api_key,
                app_secret=self.secret_key,
            )
            logger.info(f"Schwab client successfully authenticated via token at: {self.token_path}")
            print(f"[INFO] Schwab client successfully authenticated using {self.token_path}")
            return client
        except Exception as e:
            logger.warning(f"Failed to load existing token from {self.token_path}: {e}")
            logger.info("Schwab token expired or missing. Manual OAuth required.")
            print(f"[WARNING] Could not load token from {self.token_path}: {e}")
            print("[INFO] Falling back to manual OAuth flow...")

            # Fall back to manual browser flow (overwrites/creates token.json on completion)
            client = schwab.auth.client_from_manual_flow(
                api_key=self.api_key,
                app_secret=self.secret_key,
                callback_url="https://127.0.0.1",  # Matches your developer app setup
                token_path=self.token_path,
            )
            return client

    def _get_primary_account_hash(self):
        """Retrieves the primary account hash required by Schwab trader endpoints."""
        try:
            resp = self.client.get_account_numbers()
            if resp.status_code == 200:
                accounts = resp.json()
                if len(accounts) > 0:
                    account_hash = accounts[0].get("hashValue")
                    logger.info(f"Loaded primary Schwab account hash: {account_hash}")
                    return account_hash
            raise RuntimeError(f"Failed to fetch account numbers. Response: {resp.text}")
        except Exception as e:
            logger.error(f"Error fetching Schwab account hash: {e}")
            raise

    def validate_connection(self) -> bool:
        """Validates connection by querying account details through schwab-py."""
        try:
            if not self.account_hash:
                return False
            resp = self.client.get_account(self.account_hash)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

    def _get_access_token(self, auth_code: str):
        """Exchange authorization code for access token."""
        url = "https://api.schwabapi.com/v1/oauth/token"
        payload = {
            'grant_type': 'authorization_code',
            'code': auth_code,
            'client_id': self.api_key,
            'client_secret': self.secret_key,
            'redirect_uri': 'http://localhost:8080',
        }

        response = requests.post(url, data=payload)
        if response.status_code != 200:
            raise Exception(f"OAuth token exchange failed: {response.text}")

        tokens = response.json()
        self.access_token = tokens['access_token']
        self.token_expiry = datetime.now() + timedelta(seconds=tokens['expires_in'])

        # Cache tokens
        with open(self.token_file, 'w') as f:
            json.dump({
                'access_token': self.access_token,
                'refresh_token': tokens.get('refresh_token'),
                'expiry': self.token_expiry.isoformat(),
            }, f)

        logger.info(f"Schwab token obtained (expires {self.token_expiry})")

    def _get_headers(self):
        """Get authorization headers for Schwab API."""
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }

    def _get_account_hash(self) -> str:
        """Get account hash (cached after first call)."""
        if self.account_hash:
            return self.account_hash

        try:
            url = f"{self.base_url}/accounts"
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()

            accounts = response.json()
            if accounts:
                self.account_hash = accounts[0].get('hashValue')
                return self.account_hash
        except Exception as e:
            logger.error(f"Error getting account: {e}")

        return None

    def _format_occ_symbol(self, symbol, expiration, opt_type, strike):
        """
        Format option symbol into OCC format for Schwab.
        Matches the same format as Alpaca handler.
        """
        sym = symbol.strip().upper()
        if sym == "SPX":
            sym = "SPXW"

        if isinstance(expiration, (datetime, date)):
            exp_str = expiration.strftime("%y%m%d")
        else:
            clean_exp = str(expiration).replace("-", "")
            exp_str = clean_exp[2:] if len(clean_exp) == 8 else clean_exp

        opt_char = "C" if "CALL" in str(opt_type).upper() else "P"
        strike_fmt = f"{int(round(float(strike) * 1000)):08d}"

        return f"{sym}{exp_str}{opt_char}{strike_fmt}"

    def validate_connection(self) -> bool:
        """Validates connection by querying account details through schwab-py."""
        try:
            if not self.account_hash:
                return False
            resp = self.client.get_account(self.account_hash)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

    def get_account_info(self):
        """Retrieves account details using schwab-py client."""
        try:
            resp = self.client.get_account(self.account_hash)
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"Error getting account info: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return None

    def place_multi_leg_order(self, legs: list, limit_price: float, time_in_force: str = "gtc") -> dict:
        """
        Place a multi-leg options order (spread).

        Args:
            legs: List of {
                'action': 'BUY_TO_OPEN'|'SELL_TO_OPEN'|'BUY_TO_CLOSE'|'SELL_TO_CLOSE',
                'symbol': OCC-formatted option symbol,
                'quantity': contracts
            }
            limit_price: Net credit/debit price
            time_in_force: 'gtc', 'day', etc.

        Returns:
            Order confirmation dict
        """
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return {'success': False, 'error': 'No account hash'}

            # Build Schwab order
            order_legs = []
            for i, leg in enumerate(legs):
                order_legs.append({
                    'instruction': leg['action'],
                    'quantity': leg['quantity'],
                    'instrument': {
                        'symbol': leg['symbol'],
                        'assetType': 'OPTION',
                    }
                })

            order = {
                'orderType': 'LIMIT',
                'session': 'NORMAL',
                'duration': 'GOOD_TILL_CANCEL' if time_in_force == 'gtc' else 'DAY',
                'orderStrategyType': 'VERTICAL',  # Multi-leg spread
                'price': limit_price,
                'orderLegCollection': order_legs,
            }

            url = f"{self.base_url}/accounts/{account_hash}/orders"
            response = requests.post(url, json=order, headers=self._get_headers())

            if response.status_code == 201:
                order_id = response.headers.get('location', '').split('/')[-1]
                logger.info(f"Order placed: {order_id}")
                self.active_orders[order_id] = order

                return {
                    'success': True,
                    'order_id': order_id,
                    'order': order,
                }
            else:
                logger.error(f"Order failed: {response.status_code} - {response.text}")
                return {
                    'success': False,
                    'error': response.text,
                }
        except Exception as e:
            logger.error(f"Error placing multi-leg order: {e}")
            return {
                'success': False,
                'error': str(e),
            }

    def place_spread_order(self, symbol: str, expiration: str, short_strike: float,
                          long_strike: float, opt_type: str, quantity: int,
                          limit_price: float) -> dict:
        """
        Place a credit/debit spread order.

        Convenience wrapper around place_multi_leg_order.
        """
        short_symbol = self._format_occ_symbol(symbol, expiration, opt_type, short_strike)
        long_symbol = self._format_occ_symbol(symbol, expiration, opt_type, long_strike)

        legs = [
            {
                'action': 'SELL_TO_OPEN',
                'symbol': short_symbol,
                'quantity': quantity,
            },
            {
                'action': 'BUY_TO_OPEN',
                'symbol': long_symbol,
                'quantity': quantity,
            },
        ]

        return self.place_multi_leg_order(legs, limit_price)

    def get_open_orders(self) -> list:
        """Get all open orders."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return []

            url = f"{self.base_url}/accounts/{account_hash}/orders"
            response = requests.get(url, params={'status': 'OPEN'}, headers=self._get_headers())
            response.raise_for_status()

            return response.json() if response.json() else []
        except Exception as e:
            logger.error(f"Error getting open orders: {e}")
            return []

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return False

            url = f"{self.base_url}/accounts/{account_hash}/orders/{order_id}"
            response = requests.delete(url, headers=self._get_headers())

            return response.status_code == 204
        except Exception as e:
            logger.error(f"Error canceling order {order_id}: {e}")
            return False

    def get_positions(self) -> list:
        """Get all open positions."""
        try:
            account_hash = self._get_account_hash()
            if not account_hash:
                return []

            url = f"{self.base_url}/accounts/{account_hash}"
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()

            account = response.json()
            positions = account.get('securitiesAccount', {}).get('positions', [])
            return positions if positions else []
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

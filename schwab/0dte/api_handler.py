# ============================================
# ALPACA API HANDLER - 0DTE OPTIONS
# Submits atomic spread orders and tracks positions
# ============================================

from datetime import datetime, date
import logging
import sys
import requests
from config import ALPACA_API_KEY, ALPACA_BASE_URL, ALPACA_SECRET_KEY, TRADER_NAME

# Ensure Windows console encoding handles standard output safely
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

logger = logging.getLogger(__name__)


class AlpacaOrderHandler:
    """Handles all 0DTE options spread orders with Alpaca using multi-leg (mleg)."""

    def __init__(self):
        """Initialize connection to Alpaca"""
        self.api_key = ALPACA_API_KEY
        self.secret_key = ALPACA_SECRET_KEY
        self.base_url = ALPACA_BASE_URL
        self.active_orders = {}
        self.validate_connection()

    def _format_occ_symbol(self, symbol, expiration, opt_type, strike):
        """Formats an option symbol into standard OCC format.
        Automatically converts 'SPX' to 'SPXW' for 0DTE/weekly contracts.
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

    def validate_connection(self):
        """Verify we can connect to Alpaca"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            response = requests.get(
                f"{self.base_url}/v2/account", headers=headers
            )
            if response.status_code == 200:
                account = response.json()
                equity = float(account.get("equity", 0))
                buying_power = float(account.get("buying_power", 0))
                logger.info("Connected to Alpaca")
                logger.info(f"  Equity: ${equity:.2f}")
                logger.info(f"  Buying Power: ${buying_power:.2f}")
                return True
            else:
                logger.error(f"Failed to connect: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

    def get_account_info(self):
        """Get current account balance and buying power"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            response = requests.get(
                f"{self.base_url}/v2/account", headers=headers
            )
            if response.status_code == 200:
                account = response.json()
                return {
                    "cash": float(account.get("cash", 0)),
                    "buying_power": float(account.get("buying_power", 0)),
                    "portfolio_value": float(account.get("portfolio_value", 0)),
                    "equity": float(account.get("equity", 0)),
                }
            else:
                logger.error(f"Error getting account info: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return None

    def place_spread_order(
        self,
        symbol,
        short_strike,
        long_strike,
        spread_type,
        contracts,
        limit_price,
        expiration,
    ):
        """Place a 0DTE options spread order as an atomic multi-leg (mleg) package."""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
            }

            short_symbol = self._format_occ_symbol(
                symbol, expiration, spread_type, short_strike
            )
            long_symbol = self._format_occ_symbol(
                symbol, expiration, spread_type, long_strike
            )

            # Alpaca mleg credit orders MUST have a negative limit price string
            formatted_limit_price = f"-{abs(float(limit_price)):.2f}"

            payload = {
                "order_class": "mleg",
                "type": "limit",
                "limit_price": formatted_limit_price,
                "time_in_force": "day",
                "qty": str(int(contracts)),
                "legs": [
                    {
                        "symbol": short_symbol,
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open",
                    },
                    {
                        "symbol": long_symbol,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_open",
                    },
                ],
            }

            url = f"{self.base_url}/v2/orders"
            response = requests.post(url, json=payload, headers=headers)

            if response.status_code in [200, 201]:
                order_data = response.json()
                order_id = order_data.get("id")
                logger.info(
                    f"[OK] Multi-leg spread order placed: {spread_type} | ${short_strike:.0f}/${long_strike:.0f} | {contracts}x @ ${limit_price:.2f}"
                )
                self.active_orders[order_id] = {
                    "symbol": symbol,
                    "short_symbol": short_symbol,
                    "long_symbol": long_symbol,
                    "short_strike": short_strike,
                    "long_strike": long_strike,
                    "contracts": contracts,
                    "credit": limit_price,
                    "status": "open",
                }
                return order_id
            else:
                logger.error(
                    f"[REJECTED] 0DTE MLEG ORDER ({response.status_code}): {response.text}"
                )
                return None
        except Exception as e:
            logger.error(f"Spread order exception: {e}")
            return None

    def close_spread_order(self, order_id, close_limit_price):
        """Close an active multi-leg position (buy_to_close short, sell_to_close long)."""
        if order_id not in self.active_orders:
            logger.warning(
                f"Order ID {order_id} not found in active order registry."
            )
            return False

        order_info = self.active_orders[order_id]
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

        # Alpaca mleg DEBIT orders (buying back a spread) MUST have a positive limit price string
        formatted_debit_price = f"{abs(float(close_limit_price)):.2f}"

        payload = {
            "order_class": "mleg",
            "type": "limit",
            "limit_price": formatted_debit_price,
            "time_in_force": "day",
            "qty": str(int(order_info["contracts"])),
            "legs": [
                {
                    "symbol": order_info["short_symbol"],
                    "ratio_qty": "1",
                    "side": "buy",
                    "position_intent": "buy_to_close",
                },
                {
                    "symbol": order_info["long_symbol"],
                    "ratio_qty": "1",
                    "side": "sell",
                    "position_intent": "sell_to_close",
                },
            ],
        }

        try:
            url = f"{self.base_url}/v2/orders"
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code in [200, 201]:
                logger.info(
                    f"[OK] Multi-leg exit order submitted for {order_id} at limit price ${abs(float(close_limit_price)):.2f}"
                )
                order_info["status"] = "closed"
                return True
            else:
                logger.error(
                    f"Failed to submit exit order ({response.status_code}): {response.text}"
                )
                return False
        except Exception as e:
            logger.error(f"Error closing multi-leg order: {e}")
            return False

    def cancel_all_orders(self):
        """Cancel all open orders"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            requests.delete(f"{self.base_url}/v2/orders", headers=headers)
            logger.info("All orders canceled")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    def get_open_orders(self):
        """Get all open orders"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            response = requests.get(
                f"{self.base_url}/v2/orders?status=open", headers=headers
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get orders: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []

    def get_positions(self):
        """Get all current positions"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            response = requests.get(
                f"{self.base_url}/v2/positions", headers=headers
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get positions: {response.text}")
                return []
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def close_position(self, symbol):
        """Close an entire position by symbol name"""
        try:
            headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            }
            response = requests.delete(
                f"{self.base_url}/v2/positions/{symbol}", headers=headers
            )
            if response.status_code in [200, 204]:
                logger.info(f"Position closed: {symbol}")
                return True
            else:
                logger.error(f"Failed to close position: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to close position: {e}")
            return False
    def sync_active_positions(self):
        """Fetches open option positions from Alpaca to restore lost order tracking state."""
        positions = self.get_positions()
        if not positions:
            return
    
        logger.info(f"Found {len(positions)} raw legs in Alpaca. Re-building position map...")
        # Map legs back or store state in a persistent JSON file.
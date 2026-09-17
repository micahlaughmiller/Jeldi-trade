import logging
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class OrderManager:
    def __init__(self, alpaca_connector=None):
        self.alpaca = alpaca_connector
        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.closed_orders: Dict[str, Dict[str, Any]] = {}

    def get_open_orders(self) -> Dict[str, Dict[str, Any]]:
        """Returns all currently tracked open spread positions."""
        return self.open_orders

    def get_closed_orders(self) -> Dict[str, Dict[str, Any]]:
        """Returns all closed orders from the current session/tracking."""
        return self.closed_orders

    def reconstruct_spreads_from_alpaca(self, positions: List[Any]) -> Dict[str, Dict[str, Any]]:
        """
        Parses raw Alpaca option positions, groups legs by underlying ticker,
        matches short and long legs into credit spreads, and fetches historical fill credits.
        OCC Symbol format: TICKER + YYMMDD + C/P + 8-digit Strike (x1000)
        """
        grouped_legs: Dict[str, List[Dict[str, Any]]] = {}
        pattern = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

        # Step 1: Parse and group raw OCC symbols
        for pos in positions:
            symbol = getattr(pos, 'symbol', '')
            match = pattern.match(symbol)
            if not match:
                continue

            ticker, exp, opt_type, strike_raw = match.groups()
            strike = float(strike_raw) / 1000.0
            qty = int(getattr(pos, 'qty', 0))
            current_price = float(getattr(pos, 'current_price', 0.0) or 0.0)

            if ticker not in grouped_legs:
                grouped_legs[ticker] = []

            grouped_legs[ticker].append({
                'symbol': symbol,
                'expiration': exp,
                'type': 'CALL' if opt_type == 'C' else 'PUT',
                'strike': strike,
                'qty': qty,
                'current_price': current_price
            })

        # Fetch historical order fills from Alpaca API to compute exact entry credit
        fill_prices = self._fetch_historical_fill_prices()

        spreads: Dict[str, Dict[str, Any]] = {}

        # Step 2: Pair short and long legs into multi-leg credit spreads
        for ticker, legs in grouped_legs.items():
            if len(legs) != 2:
                logger.warning(f"Skipping {ticker}: Expected 2 legs for credit spread, found {len(legs)}")
                continue

            short_leg = next((l for l in legs if l['qty'] < 0), None)
            long_leg = next((l for l in legs if l['qty'] > 0), None)

            if short_leg and long_leg:
                contracts = abs(short_leg['qty'])
                short_symbol = short_leg['symbol']
                long_symbol = long_leg['symbol']

                # Compute historical entry credit from fill prices if available
                short_fill = fill_prices.get(short_symbol, 0.0)
                long_fill = fill_prices.get(long_symbol, 0.0)
                entry_credit = max(0.0, short_fill - long_fill)

                # Current mark credit to close
                current_credit = max(0.0, short_leg['current_price'] - long_leg['current_price'])

                spread_id = f"EXISTING_{ticker}_{short_leg['expiration']}"
                spreads[spread_id] = {
                    'order_id': spread_id,
                    'symbol': ticker,
                    'option_type': short_leg['type'],
                    'short_strike': short_leg['strike'],
                    'long_strike': long_leg['strike'],
                    'short_symbol': short_symbol,
                    'long_symbol': long_symbol,
                    'contracts': contracts,
                    'entry_credit': entry_credit,
                    'current_credit': current_credit,
                    'unrealized_pnl': (entry_credit - current_credit) * 100.0 * contracts,
                    'status': 'open',
                    'strategy': f"{short_leg['type'].title()} Credit Spread"
                }

        return spreads

    def _fetch_historical_fill_prices(self) -> Dict[str, float]:
        """Queries Alpaca account activities to map OCC option symbols to filled prices."""
        fill_prices: Dict[str, float] = {}
        if not self.alpaca:
            return fill_prices

        try:
            # Check for get_account_activities or get_activities on connector
            if hasattr(self.alpaca, 'get_account_activities'):
                activities = self.alpaca.get_account_activities(activity_types=['FILL'])
            elif hasattr(self.alpaca, 'get_activities'):
                activities = self.alpaca.get_activities(activity_types=['FILL'])
            elif hasattr(self.alpaca, 'api') and hasattr(self.alpaca.api, 'get_activities'):
                activities = self.alpaca.api.get_activities(activity_types=['FILL'])
            else:
                activities = []

            for act in activities:
                symbol = getattr(act, 'symbol', '')
                price = float(getattr(act, 'price', 0.0) or 0.0)
                if symbol and price > 0:
                    if symbol not in fill_prices:
                        fill_prices[symbol] = price
        except Exception as e:
            logger.error(f"Failed to fetch historical fill prices: {e}")

        return fill_prices
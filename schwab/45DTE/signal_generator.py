"""Signal generator for 45-60 DTE credit spreads."""

from datetime import datetime, timedelta
from config_45dte import (
    TARGET_DELTA,
    SPREAD_WIDTH,
    MIN_CREDIT_TARGET,
    EXPIRATION_DTE_MIN,
    EXPIRATION_DTE_MAX,
)


class SignalGenerator:
    """Generates credit spread entry signals based on RSI confluence."""

    def __init__(self):
        """Initialize signal generator."""
        self.target_delta = TARGET_DELTA
        self.spread_width = SPREAD_WIDTH
        self.min_credit = MIN_CREDIT_TARGET

    def get_next_expiration_dte(self):
        """
        Find next expiration within 45-60 DTE range.

        Returns:
            datetime object for expiration Friday, dte
        """
        today = datetime.now()

        # Find next Friday
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7

        next_friday = today + timedelta(days=days_until_friday)

        # Find Friday in 45-60 DTE range
        current_friday = next_friday
        for _ in range(12):  # Check up to 12 weeks
            dte = (current_friday - today).days
            if EXPIRATION_DTE_MIN <= dte <= EXPIRATION_DTE_MAX:
                return current_friday, dte
            current_friday += timedelta(days=7)

        # Fallback to closest DTE in range
        dte = (next_friday - today).days
        if dte < EXPIRATION_DTE_MIN:
            later_friday = next_friday + timedelta(days=7)
            return later_friday, (later_friday - today).days
        else:
            return next_friday, dte

    def estimate_options_credit(self, stock_price, signal_type, iv_percentile=50):
        """
        Estimate credit for options spread (simplified model).

        In production, fetch real Greeks from options chain API.
        This uses Black-Scholes approximation for paper trading.

        Args:
            stock_price: Current stock price
            signal_type: "oversold" (put spread) or "overbought" (call spread)
            iv_percentile: IV percentile (0-100)

        Returns:
            Estimated credit or None if below minimum
        """
        try:
            if signal_type == "oversold":
                base_credit = stock_price * 0.01 + (iv_percentile / 100) * 0.5
            else:
                base_credit = max(0.5, stock_price * 0.005 + (iv_percentile / 100) * 0.3)

            credit = base_credit * (self.spread_width / 5.0) * (self.target_delta / 0.30)

            return round(credit, 2)

        except Exception as e:
            print(f"Error estimating credit: {e}")
            return None

    def generate_entry_signal(self, rsi_signal, current_price, iv_percentile=50):
        """
        Generate credit spread entry signal.

        Args:
            rsi_signal: Dict with signal_type, rsi_14, rsi_28, close
            current_price: Current stock price
            iv_percentile: IV percentile (placeholder)

        Returns:
            Signal dict or None
        """
        try:
            signal_type = rsi_signal['signal_type']
            symbol = rsi_signal['symbol']

            # Determine spread direction
            if signal_type == "oversold":
                spread_direction = "put_spread"
                spread_name = "Put Credit Spread (sell put)"
            else:
                spread_direction = "call_spread"
                spread_name = "Call Credit Spread (sell call)"

            # Get expiration
            expiration_date, dte = self.get_next_expiration_dte()

            # Estimate credit
            estimated_credit = self.estimate_options_credit(
                current_price,
                signal_type,
                iv_percentile,
            )

            if estimated_credit is None or estimated_credit < self.min_credit:
                return None  # Credit too low, skip entry

            # Calculate strike prices (simplified)
            if signal_type == "oversold":
                short_strike = round((current_price * 0.97) / 5) * 5
                long_strike = short_strike - self.spread_width
            else:
                short_strike = round((current_price * 1.03) / 5) * 5
                long_strike = short_strike + self.spread_width

            return {
                'symbol': symbol,
                'signal_type': signal_type,
                'spread_direction': spread_direction,
                'spread_name': spread_name,
                'rsi_14': rsi_signal['rsi_14'],
                'rsi_28': rsi_signal['rsi_28'],
                'current_price': current_price,
                'short_strike': short_strike,
                'long_strike': long_strike,
                'spread_width': self.spread_width,
                'short_delta': self.target_delta,
                'estimated_credit': estimated_credit,
                'max_loss': self.spread_width - estimated_credit,
                'expiration_date': expiration_date,
                'dte': dte,
                'entry_timestamp': datetime.now(),
            }

        except Exception as e:
            print(f"Error generating signal for {rsi_signal.get('symbol')}: {e}")
            return None

    def validate_signal(self, signal):
        """Validate signal meets all entry criteria."""
        if signal is None:
            return False

        if signal['estimated_credit'] < self.min_credit:
            return False

        if not (EXPIRATION_DTE_MIN <= signal['dte'] <= EXPIRATION_DTE_MAX):
            return False

        if signal['short_strike'] <= 0 or signal['long_strike'] <= 0:
            return False

        return True

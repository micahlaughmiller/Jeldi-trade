"""Logging system for 45-60 DTE trading strategy."""

import os
import json
from datetime import datetime
from pathlib import Path
from config_45dte import LOG_DIR, LOG_LEVEL


class LoggerSystem:
    """Manages daily trading logs and summaries."""

    def __init__(self):
        """Initialize logger system."""
        self.log_dir = Path(LOG_DIR)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.session_date = datetime.now().strftime("%Y-%m-%d")
        self.session_file = self.log_dir / f"session_{self.session_date}.log"
        self.trades_file = self.log_dir / f"trades_{self.session_date}.csv"

        self.session_log = []
        self.trades_log = []
        self.daily_summary = {
            'date': self.session_date,
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'tickers_analyzed': [],
            'signals_generated': [],
            'trades_opened': [],
            'trades_closed': [],
            'daily_pnl': 0.0,
            'events': [],
        }

    def log_event(self, event_type, message, **kwargs):
        """Log an event with timestamp."""
        timestamp = datetime.now().isoformat()
        # Convert any datetime objects in kwargs to ISO format strings
        clean_kwargs = {}
        for k, v in kwargs.items():
            if isinstance(v, datetime):
                clean_kwargs[k] = v.isoformat()
            elif isinstance(v, dict):
                # Recursively clean dict values
                clean_kwargs[k] = {kk: vv.isoformat() if isinstance(vv, datetime) else vv for kk, vv in v.items()}
            else:
                clean_kwargs[k] = v

        event = {
            'timestamp': timestamp,
            'type': event_type,
            'message': message,
            **clean_kwargs,
        }

        self.session_log.append(event)
        self.daily_summary['events'].append(event)

        print(f"[{event_type}] {message}")

        self._write_session_log()

    def log_scan_start(self, ticker_count):
        """Log start of S&P 500 scan."""
        self.log_event(
            'SCAN_START',
            f'Beginning scan of {ticker_count} S&P 500 stocks',
            ticker_count=ticker_count,
        )

    def log_ticker_analyzed(self, symbol, rsi_14, rsi_28, signal_type=None):
        """Log ticker analysis result."""
        self.daily_summary['tickers_analyzed'].append(symbol)

        if signal_type:
            self.log_event(
                'SIGNAL',
                f'{symbol}: {signal_type.upper()} (RSI14={rsi_14:.2f}, RSI28={rsi_28:.2f})',
                symbol=symbol,
                rsi_14=rsi_14,
                rsi_28=rsi_28,
                signal_type=signal_type,
            )

    def log_signal_generated(self, signal):
        """Log trade signal generation."""
        signal_summary = {
            'symbol': signal['symbol'],
            'spread_direction': signal['spread_direction'],
            'estimated_credit': signal['estimated_credit'],
            'dte': signal['dte'],
            'timestamp': datetime.now().isoformat(),
        }
        self.daily_summary['signals_generated'].append(signal_summary)

        self.log_event(
            'ENTRY_SIGNAL',
            f"{signal['symbol']} {signal['spread_name']} | Credit: ${signal['estimated_credit']:.2f} | DTE: {signal['dte']}",
            signal=signal_summary,
        )

    def log_trade_opened(self, order_metadata):
        """Log trade entry."""
        trade_summary = {
            'order_id': order_metadata['order_id'],
            'symbol': order_metadata['symbol'],
            'direction': order_metadata['spread_direction'],
            'quantity': order_metadata['quantity'],
            'entry_credit': order_metadata['initial_credit'],
            'dte': order_metadata['dte_at_entry'],
            'timestamp': datetime.now().isoformat(),
        }
        self.daily_summary['trades_opened'].append(trade_summary)

        self.log_event(
            'TRADE_OPENED',
            f"{order_metadata['symbol']} {order_metadata['spread_direction']} | Qty: {order_metadata['quantity']} | Credit: ${order_metadata['initial_credit']:.2f}",
            trade=trade_summary,
        )

    def log_trade_closed(self, close_details):
        """Log trade exit and P&L."""
        self.daily_summary['trades_closed'].append(close_details)
        self.daily_summary['daily_pnl'] += close_details['total_pnl']

        self.log_event(
            'TRADE_CLOSED',
            f"{close_details['symbol']} | Exit: ${close_details['exit_price']:.2f} | P&L: ${close_details['total_pnl']:.2f} ({close_details['pnl_pct']:.1f}%) | Reason: {close_details['exit_reason']}",
            trade=close_details,
        )

    def log_price_reduction(self, order_id, old_price, new_price):
        """Log order price reduction."""
        self.log_event(
            'PRICE_REDUCTION',
            f"{order_id}: ${old_price:.2f} → ${new_price:.2f}",
            order_id=order_id,
            old_price=old_price,
            new_price=new_price,
        )

    def log_risk_check(self, symbol, result):
        """Log risk check result."""
        status = "✓ ALLOWED" if result['allowed'] else "✗ REJECTED"
        reason = result['reason']

        self.log_event(
            'RISK_CHECK',
            f"{symbol}: {status} - {reason}",
            symbol=symbol,
            result=result,
        )

    def log_circuit_breaker(self, max_loss_hits):
        """Log circuit breaker activation."""
        self.log_event(
            'CIRCUIT_BREAKER',
            f'🔴 CIRCUIT BREAKER ACTIVATED: {max_loss_hits} trades hit max loss. New entries PAUSED.',
            max_loss_hits=max_loss_hits,
        )

    def log_account_summary(self, equity, buying_power, portfolio_risk):
        """Log account status summary."""
        self.log_event(
            'ACCOUNT_SUMMARY',
            f'Equity: ${equity:.2f} | Buying Power: ${buying_power:.2f} | Portfolio Risk: {portfolio_risk:.1%}',
            equity=equity,
            buying_power=buying_power,
            portfolio_risk_pct=portfolio_risk,
        )

    def _write_session_log(self):
        """Write session log to file."""
        try:
            with open(self.session_file, 'w') as f:
                for event in self.session_log:
                    f.write(json.dumps(event) + '\n')
        except Exception as e:
            print(f"Error writing session log: {e}")

    def write_daily_summary(self):
        """Write end-of-day summary."""
        self.daily_summary['end_time'] = datetime.now().isoformat()

        summary_file = self.log_dir / f"summary_{self.session_date}.json"

        try:
            with open(summary_file, 'w') as f:
                json.dump(self.daily_summary, f, indent=2)

            print(f"\n{'='*70}")
            print(f"DAILY SUMMARY - {self.session_date}")
            print(f"{'='*70}")
            print(f"Tickers Analyzed: {len(self.daily_summary['tickers_analyzed'])}")
            print(f"Signals Generated: {len(self.daily_summary['signals_generated'])}")
            print(f"Trades Opened: {len(self.daily_summary['trades_opened'])}")
            print(f"Trades Closed: {len(self.daily_summary['trades_closed'])}")
            print(f"Daily P&L: ${self.daily_summary['daily_pnl']:.2f}")
            print(f"Log Location: {summary_file}")
            print(f"{'='*70}\n")

        except Exception as e:
            print(f"Error writing daily summary: {e}")

    def get_daily_summary(self):
        """Get current daily summary."""
        return self.daily_summary.copy()

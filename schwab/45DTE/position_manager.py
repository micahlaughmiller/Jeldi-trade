import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class PositionManager45DTE:
    """Manages 45-60 DTE vertical spread positions"""
    
    def __init__(self):
        self.positions = {}
        self.max_concurrent = 6
    
    def add_position(self, symbol, spread_type, short_strike, long_strike, contracts, credit, entry_time):
        """Add a new position"""
        try:
            position = {
                'symbol': symbol,
                'spread_type': spread_type,
                'short_strike': short_strike,
                'long_strike': long_strike,
                'contracts': contracts,
                'entry_credit': credit,
                'entry_time': entry_time,
                'dte_at_entry': 50,  # Assumed 45-60 DTE
                'status': 'OPEN',
                'max_favorable': credit,
                'max_adverse': credit,
                'current_price': credit,
            }
            
            self.positions[symbol] = position
            logger.info(f"Position opened: {symbol} | {spread_type} | {contracts}x | ${credit:.2f}")
            return True
        
        except Exception as e:
            logger.error(f"Error adding position: {e}")
            return False
    
    def update_position(self, symbol, current_price):
        """Update current price and track extremes"""
        if symbol not in self.positions:
            return
        
        pos = self.positions[symbol]
        pos['current_price'] = current_price
        
        # Track max favorable (lowest for credit spreads we're short)
        if current_price < pos['max_favorable']:
            pos['max_favorable'] = current_price
        
        # Track max adverse (highest)
        if current_price > pos['max_adverse']:
            pos['max_adverse'] = current_price
    
    def check_profit_target(self, symbol):
        """Check if position hit 50% profit target"""
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        profit = (pos['entry_credit'] - pos['current_price']) / pos['entry_credit'] * 100
        
        if profit >= 50:
            logger.info(f"PROFIT TARGET HIT: {symbol} | Profit: {profit:.2f}%")
            return True
        
        return False
    
    def check_stop_loss(self, symbol):
        """Check if position hit stop loss"""
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        loss = (pos['current_price'] - pos['entry_credit']) / pos['entry_credit'] * 100
        
        if loss >= 20:  # 20% loss
            logger.info(f"STOP LOSS HIT: {symbol} | Loss: {loss:.2f}%")
            return True
        
        return False
    
    def check_dte_close(self, symbol):
        """Check if position is within 7 DTE (hard close)"""
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        entry = pos['entry_time']
        dte_remaining = 50 - ((datetime.now() - entry).days)
        
        if dte_remaining <= 7:
            logger.info(f"7 DTE CLOSE: {symbol} | {dte_remaining} DTE remaining")
            return True
        
        return False
    
    def close_position(self, symbol, exit_price, reason):
        """Close a position"""
        if symbol not in self.positions:
            return False
        
        pos = self.positions[symbol]
        pnl = (pos['entry_credit'] - exit_price) * 100 * pos['contracts']
        pnl_pct = (pos['entry_credit'] - exit_price) / pos['entry_credit'] * 100
        
        logger.info("=" * 80)
        logger.info(f"POSITION CLOSED: {symbol}")
        logger.info(f"  Entry Credit: ${pos['entry_credit']:.2f}")
        logger.info(f"  Exit Price: ${exit_price:.2f}")
        logger.info(f"  P&L: ${pnl:.2f} ({pnl_pct:.2f}%)")
        logger.info(f"  Reason: {reason}")
        logger.info(f"  Max Favorable: ${pos['max_favorable']:.2f}")
        logger.info(f"  Max Adverse: ${pos['max_adverse']:.2f}")
        logger.info("=" * 80)
        
        del self.positions[symbol]
        return True
    
    def get_open_count(self):
        """Get number of open positions"""
        return len([p for p in self.positions.values() if p['status'] == 'OPEN'])
    
    def can_open_new(self):
        """Check if we can open new positions (max 6)"""
        return self.get_open_count() < self.max_concurrent
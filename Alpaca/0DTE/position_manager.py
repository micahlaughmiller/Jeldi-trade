# ============================================
# POSITION MANAGER
# Tracks open positions, exits, and P&L
# ============================================

import logging
from datetime import datetime
from config import MARKET_TIMEZONE

logger = logging.getLogger(__name__)

ET_TZ = MARKET_TIMEZONE


class PositionManager:
    """Manages a single open position through its lifecycle with Short/Long leg tracking"""
    
    def __init__(self, trade_plan, spx_entry, credit, contracts, entry_time):
        """Initialize a new position"""
        
        self.plan = trade_plan
        self.spx_entry = spx_entry
        self.entry_credit = credit
        self.contracts = contracts
        self.entry_time = entry_time
        
        self.short_strike = trade_plan.short_strike
        self.long_strike = trade_plan.long_strike
        
        self.max_favorable_spread = credit
        self.max_adverse_spread = credit
        self.current_spread_price = credit
        
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.runner = False
        
        logger.info("=" * 80)
        logger.info(f"POSITION OPENED: {contracts} contracts at net credit ${credit:.2f}")
        logger.info(f"  Short Leg Strike: {self.short_strike}")
        logger.info(f"  Long Leg Strike:  {self.long_strike}")
        logger.info("=" * 80)
    
    def update_price(self, new_spread_price):
        """Update current spread price"""
        
        self.current_spread_price = new_spread_price
        
        if new_spread_price < self.max_favorable_spread:
            self.max_favorable_spread = new_spread_price
        
        if new_spread_price > self.max_adverse_spread:
            self.max_adverse_spread = new_spread_price
    
    def check_hard_stop(self):
        """Check if hard stop loss is hit"""
        
        stop_price = self.plan.stop_price
        
        if self.current_spread_price >= stop_price:
            logger.warning(f"HARD STOP HIT at ${self.current_spread_price:.2f}")
            return True
        
        return False
    
    def check_profit_trigger(self):
        """Check if initial profit target is hit"""
        
        profit_trigger = self.plan.profit_trigger
        
        if self.current_spread_price <= profit_trigger and not self.runner:
            self.runner = True
            logger.info(f"Profit trigger hit: ${self.current_spread_price:.2f} - RUNNER ACTIVATED")
            return True
        
        return False
    
    def should_exit(self):
        """Determine if position should exit"""
        
        if self.check_hard_stop():
            return True, "HARD_STOP"
        
        self.check_profit_trigger()
        
        return False, None
    
    def exit(self, exit_price, spx_exit, reason):
        """Close the position and output net + individual leg breakdown"""
        
        profit_per_contract = (self.entry_credit - exit_price) * 100
        total_pnl = profit_per_contract * self.contracts
        pnl_percent = ((self.entry_credit - exit_price) / self.entry_credit) * 100 if self.entry_credit > 0 else 0
        
        logger.info("=" * 80)
        logger.info(f"POSITION CLOSED: {reason}")
        logger.info(f"  Strikes: Short {self.short_strike} / Long {self.long_strike}")
        logger.info(f"  Entry Credit: ${self.entry_credit:.2f}")
        logger.info(f"  Exit Price: ${exit_price:.2f}")
        logger.info(f"  P&L Per Contract: ${profit_per_contract:.2f}")
        logger.info(f"  Total P&L: ${total_pnl:.2f} ({pnl_percent:.2f}%)")
        logger.info(f"  Contracts: {self.contracts}")
        logger.info(f"  Max Favorable: ${self.max_favorable_spread:.2f}")
        logger.info(f"  Max Adverse: ${self.max_adverse_spread:.2f}")
        logger.info(f"  Runner: {self.runner}")
        logger.info("=" * 80)
        
        self.exit_price = exit_price
        self.exit_time = datetime.now(ET_TZ)
        self.exit_reason = reason
        
        return total_pnl
    
    def to_dict(self):
        """Convert to trade record with complete entry, exit, and leg detail for journaling"""
        
        pnl = (self.entry_credit - (self.exit_price or 0)) * 100 * self.contracts if self.exit_price else 0
        
        return {
            'timestamp': self.entry_time.isoformat(),
            'exit_timestamp': self.exit_time.isoformat() if self.exit_time else '',
            'setup_type': self.plan.setup_type if hasattr(self.plan, 'setup_type') else '',
            'direction': self.plan.direction,
            'equity': 0,
            'tier': '',
            'spread_width': self.plan.width,
            'contracts': self.contracts,
            'spx_entry': self.spx_entry,
            'short_strike': self.short_strike,
            'long_strike': self.long_strike,
            'entry_credit': self.entry_credit,
            'stop_price': self.plan.stop_price,
            'exit_price': self.exit_price or 0,
            'pnl': pnl,
            'pnl_pct': ((self.entry_credit - (self.exit_price or 0)) / self.entry_credit * 100) if self.entry_credit > 0 else 0,
            'max_favorable_excursion': self.max_favorable_spread,
            'max_adverse_excursion': self.max_adverse_spread,
            'runner': self.runner,
            'exit_reason': self.exit_reason or '',
        }
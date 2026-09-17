# ============================================
# ASTRA ORB STRATEGY
# 15-minute ORB + Overnight Breakout
# ============================================

from dataclasses import dataclass
from typing import Optional
from config import CONFIG
from risk_manager import RiskManager
import logging

logger = logging.getLogger(__name__)


@dataclass
class PositionPlan:
    """Trade candidate ready for execution"""
    
    direction: str
    spread_type: str
    short_strike: float
    long_strike: float
    width: int
    credit: float
    contracts: int
    stop_price: float
    profit_trigger: float


class AstraStrategy:
    """ORB strategy engine"""
    
    def __init__(self, risk: RiskManager):
        self.risk = risk
        
        # Overnight levels (4 PM - 9:30 AM)
        self.overnight_high = None
        self.overnight_low = None
        
        # Opening range (9:30 - 9:45 AM)
        self.or_high = None
        self.or_low = None
        
        # Confirmation counters
        self.above_count = 0
        self.below_count = 0
        
        # Current setup
        self.setup_type = None
        self.direction = None
    
    def reset_day(self):
        """Reset strategy at market open"""
        self.overnight_high = None
        self.overnight_low = None
        self.or_high = None
        self.or_low = None
        self.above_count = 0
        self.below_count = 0
        self.setup_type = None
        self.direction = None
    
    def set_overnight_levels(self, high: float, low: float):
        """Set overnight high/low from previous session"""
        self.overnight_high = high
        self.overnight_low = low
        logger.info(f"Overnight: High {high} / Low {low}")
    
    def confirm_overnight_breakout(self, es_close: float) -> Optional[str]:
        """
        Check for overnight breakout with 2-candle confirmation
        Called every minute 9:30-9:45
        """
        if self.overnight_high is None or self.overnight_low is None:
            return None
        
        # Track closes relative to overnight levels
        if es_close > self.overnight_high:
            self.above_count += 1
            self.below_count = 0
        elif es_close < self.overnight_low:
            self.below_count += 1
            self.above_count = 0
        else:
            self.above_count = 0
            self.below_count = 0
        
        # Bullish confirmation: 2 closes above overnight high
        if self.above_count >= CONFIG.confirmation_candles:
            self.direction = "BULLISH"
            self.setup_type = "OVERNIGHT_HIGH_BREAKOUT"
            logger.info("✓ Bullish overnight breakout confirmed")
            return self.direction
        
        # Bearish confirmation: 2 closes below overnight low
        if self.below_count >= CONFIG.confirmation_candles:
            self.direction = "BEARISH"
            self.setup_type = "OVERNIGHT_LOW_BREAKDOWN"
            logger.info("✓ Bearish overnight breakout confirmed")
            return self.direction
        
        return None
    
    def set_opening_range(self, high: float, low: float):
        """Set 15-minute opening range"""
        self.or_high = high
        self.or_low = low
        logger.info(f"Opening Range (9:30-9:45): High {high} / Low {low}")
    
    def evaluate_orb(self, spx_close: float, vwap: Optional[float] = None) -> Optional[str]:
        """
        Evaluate ORB breakout (called after 9:45 AM)
        """
        if self.or_high is None or self.or_low is None:
            return None
        
        # Bullish ORB breakout
        if spx_close > self.or_high:
            if vwap is None or spx_close > vwap:
                self.direction = "BULLISH"
                self.setup_type = "15_MIN_ORB_HIGH"
                logger.info("✓ Bullish ORB breakout")
                return self.direction
        
        # Bearish ORB breakout
        if spx_close < self.or_low:
            if vwap is None or spx_close < vwap:
                self.direction = "BEARISH"
                self.setup_type = "15_MIN_ORB_LOW"
                logger.info("✓ Bearish ORB breakout")
                return self.direction
        
        return None
    
    def calculate_strikes(self, spx_price: float, width: int):
        """
        Determine short and long strikes based on direction
        
        Returns:
            (spread_type, short_strike, long_strike)
        """
        if self.direction == "BEARISH":
            # Call credit spread (sell calls above price)
            short = round(
                (spx_price - CONFIG.short_strike_distance) / 5
            ) * 5
            long = short + width
            
            return ("CALL_CREDIT_SPREAD", short, long)
        
        if self.direction == "BULLISH":
            # Put credit spread (sell puts below price)
            short = round(
                (spx_price + CONFIG.short_strike_distance) / 5
            ) * 5
            long = short - width
            
            return ("PUT_CREDIT_SPREAD", short, long)
        
        return None
    
    def build_trade_plan(self, spx_price: float, credit: float):
        """
        Build a PositionPlan ready for execution
        
        Returns:
            (PositionPlan or None, reason)
        """
        
        # Check if trading is allowed
        allowed, reason = self.risk.trading_allowed()
        if not allowed:
            return None, reason
        
        # Check credit is in range
        if not (CONFIG.min_credit <= credit <= CONFIG.max_credit):
            return None, "CREDIT_OUTSIDE_RANGE"
        
        # Get tier and spread width
        tier = self.risk.tier()
        width = tier.spread_width
        
        if width > CONFIG.absolute_max_spread_width:
            return None, "SPREAD_WIDTH_SAFETY_LIMIT"
        
        # Calculate strikes
        strikes = self.calculate_strikes(spx_price, width)
        if strikes is None:
            return None, "NO_DIRECTION"
        
        spread_type, short_strike, long_strike = strikes
        
        # Calculate contracts
        contracts = self.risk.calculate_contracts(
            spread_width=width,
            credit=credit,
            planned_stop=CONFIG.stop_amount,
        )
        
        if contracts < 1:
            return None, "RISK_MANAGER_REJECTED"
        
        return PositionPlan(
            direction=self.direction,
            spread_type=spread_type,
            short_strike=short_strike,
            long_strike=long_strike,
            width=width,
            credit=credit,
            contracts=contracts,
            stop_price=credit + CONFIG.stop_amount,
            profit_trigger=credit - CONFIG.profit_trigger,
        ), "OK"
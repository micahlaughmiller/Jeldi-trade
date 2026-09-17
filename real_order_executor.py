# ============================================
# REAL ORDER EXECUTOR
# Submits and manages actual Alpaca orders
# ============================================

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class RealOrderExecutor:
    """Submits real orders to Alpaca and tracks execution"""
    
    def __init__(self, alpaca_handler):
        self.handler = alpaca_handler
        self.open_orders = {}
    
    def execute_spread(self, trade_plan, spx_price, credit, contracts):
        """Execute a real spread order on Alpaca"""
        try:
            order_id = self.handler.place_spread_order(
                symbol='SPX',
                short_strike=int(trade_plan.short_strike),
                long_strike=int(trade_plan.long_strike),
                spread_type=trade_plan.spread_type,
                contracts=contracts,
                limit_price=credit,
                expiration=datetime.now()
            )
            
            if not order_id:
                return None
            
            self.open_orders[order_id] = {
                'plan': trade_plan,
                'entry_spx': spx_price,
                'entry_credit': credit,
                'contracts': contracts,
                'entry_time': datetime.now(),
                'status': 'PENDING',
            }
            
            logger.info(f"Order submitted: {order_id}")
            return order_id
        
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            return None
    
    def get_position_pnl(self, symbol):
        """Get unrealized P&L for a position from Alpaca"""
        try:
            positions = self.handler.get_positions()
            for pos in positions:
                if pos.get('symbol') == symbol:
                    return {
                        'unrealized_pl': float(pos.get('unrealized_pl', 0)),
                        'unrealized_plpc': float(pos.get('unrealized_plpc', 0)),
                        'current_price': float(pos.get('current_price', 0)),
                        'qty': float(pos.get('qty', 0)),
                    }
            return None
        except Exception as e:
            logger.error(f"Error getting position P&L: {e}")
            return None
    
    def close_spread(self, order_id, exit_price=0.0):
        """Close a multi-leg spread position on Alpaca"""
        try:
            logger.info(f"Closing spread order on Alpaca: {order_id}")
            success = self.handler.close_spread_order(order_id, close_limit_price=exit_price)
            if order_id in self.open_orders:
                del self.open_orders[order_id]
            return success
        except Exception as e:
            logger.error(f"Error closing spread: {e}")
            return False
import logging

logger = logging.getLogger(__name__)


class OptionChainManager:
    
    def __init__(self, alpaca_client):
        self.client = alpaca_client
    
    def find_spread_legs(self, spx_price, short_strike, long_strike, spread_type, target_credit=7.00):
        
        try:
            if spread_type == "CALL_CREDIT_SPREAD":
                short_price = self.simulate_option_price(spx_price, short_strike, 'call', side='sell')
                long_price = self.simulate_option_price(spx_price, long_strike, 'call', side='buy')
            else:
                short_price = self.simulate_option_price(spx_price, short_strike, 'put', side='sell')
                long_price = self.simulate_option_price(spx_price, long_strike, 'put', side='buy')
            
            net_credit = short_price - long_price
            
            return {
                'short_leg': {'strike': short_strike, 'price': short_price},
                'long_leg': {'strike': long_strike, 'price': long_price},
                'net_credit': net_credit,
                'realistic': net_credit > 0,
            }
        
        except Exception as e:
            logger.error(f"Error finding spread legs: {e}")
            return None
    
    def simulate_option_price(self, spx_price, strike, opt_type, side='mid'):
        
        distance = abs(spx_price - strike)
        time_value_factor = 0.75
        
        if opt_type == 'call':
            intrinsic = max(0, spx_price - strike)
        else:
            intrinsic = max(0, strike - spx_price)
        
        base_time_value = spx_price * 0.02 * time_value_factor
        
        if distance > 20:
            time_value = base_time_value * 0.2
        elif distance > 10:
            time_value = base_time_value * 0.4
        else:
            time_value = base_time_value * 0.8
        
        price = intrinsic + time_value
        
        if side == 'sell':
            price *= 0.98
        elif side == 'buy':
            price *= 1.02
        
        return max(0.05, round(price, 2))
"""Risk manager for Kelly sizing and portfolio constraints."""

from config_45dte import (
    MAX_RISK_PER_TRADE_PCT,
    MAX_PORTFOLIO_RISK_PCT,
    MAX_CONTRACTS_PER_TRADE,
    MAX_LOSS_HITS_CIRCUIT_BREAKER,
    EFFECTIVE_MAX_EQUITY,
    POSITION_SIZE_TIERS,
)


class RiskManager:
    """Manages position sizing, risk limits, and circuit breakers."""

    def __init__(self, alpaca_connector):
        """Initialize risk manager."""
        self.alpaca = alpaca_connector
        self.max_loss_hits = 0
        self.circuit_breaker_active = False

    def calculate_contract_size(self, signal, current_equity):
        """Calculate contract size using tiered position sizing for account growth."""
        try:
            # Get tier-based max contracts
            tier_max_contracts = MAX_CONTRACTS_PER_TRADE
            for tier_limit in sorted(POSITION_SIZE_TIERS.keys()):
                if current_equity <= tier_limit:
                    tier_max_contracts = POSITION_SIZE_TIERS[tier_limit]
                    break

            # Use effective equity for risk calculation
            effective_equity = min(current_equity, EFFECTIVE_MAX_EQUITY)
            risk_budget = effective_equity * MAX_RISK_PER_TRADE_PCT
            max_loss_per_contract = signal['max_loss'] * 100

            if max_loss_per_contract <= 0:
                return 1

            # Calculate by risk
            contracts_by_risk = int(risk_budget / max_loss_per_contract)

            # Cap to tier maximum
            contracts = min(contracts_by_risk, tier_max_contracts)

            contracts = max(1, contracts)
            return contracts

        except Exception as e:
            print(f"Error calculating contract size: {e}")
            return 1

    def get_current_portfolio_risk(self, order_manager):
        """Calculate total portfolio risk across all open positions."""
        open_orders = order_manager.get_open_orders()

        total_risk_dollars = 0
        position_count = 0
        risk_by_symbol = {}

        for order_id, order in open_orders.items():
            position_risk = order['quantity'] * order['max_loss'] * 100
            total_risk_dollars += position_risk
            position_count += 1
            risk_by_symbol[order['symbol']] = position_risk

        return {
            'total_risk_dollars': total_risk_dollars,
            'position_count': position_count,
            'risk_by_symbol': risk_by_symbol,
            'open_orders': open_orders,
        }

    def can_enter_new_trade(self, signal, order_manager, current_equity):
        """Check if new trade entry is allowed per risk rules."""
        if self.circuit_breaker_active:
            return {
                'allowed': False,
                'reason': f'Circuit breaker active: {self.max_loss_hits} trades hit max loss',
            }

        new_risk_dollars = signal['max_loss'] * 100
        portfolio_risk = self.get_current_portfolio_risk(order_manager)

        contracts = self.calculate_contract_size(signal, current_equity)
        total_new_risk = portfolio_risk['total_risk_dollars'] + (new_risk_dollars * contracts)

        portfolio_risk_pct = total_new_risk / current_equity if current_equity > 0 else 1.0

        if portfolio_risk_pct > MAX_PORTFOLIO_RISK_PCT:
            return {
                'allowed': False,
                'reason': f'Portfolio risk would be {portfolio_risk_pct:.1%} (max {MAX_PORTFOLIO_RISK_PCT:.1%})',
            }

        return {
            'allowed': True,
            'reason': 'Risk check passed',
            'contracts': contracts,
            'projected_risk': total_new_risk,
            'projected_risk_pct': portfolio_risk_pct,
        }

    def check_max_loss_hit(self, order_id, current_mark_price, signal):
        """Check if position hit max loss threshold."""
        max_loss_per_contract = signal['max_loss']
        current_loss = current_mark_price - signal['estimated_credit']

        if current_loss >= max_loss_per_contract:
            self.max_loss_hits += 1
            print(f"⚠ Order {order_id}: Max loss hit! Current loss: ${current_loss:.2f}")

            if self.max_loss_hits >= MAX_LOSS_HITS_CIRCUIT_BREAKER:
                self.circuit_breaker_active = True
                print(f"🔴 CIRCUIT BREAKER ACTIVATED: {self.max_loss_hits} trades hit max loss")

            return True

        return False

    def reset_circuit_breaker(self):
        """Reset circuit breaker (manual intervention)."""
        self.circuit_breaker_active = False
        self.max_loss_hits = 0
        print("✓ Circuit breaker reset by human intervention")

    def get_risk_summary(self, order_manager, current_equity):
        """Get comprehensive risk summary."""
        portfolio_risk = self.get_current_portfolio_risk(order_manager)

        risk_pct = (
            portfolio_risk['total_risk_dollars'] / current_equity
            if current_equity > 0
            else 0
        )

        return {
            'current_equity': current_equity,
            'total_risk_dollars': portfolio_risk['total_risk_dollars'],
            'total_risk_pct': risk_pct,
            'open_positions': portfolio_risk['position_count'],
            'max_loss_hits': self.max_loss_hits,
            'circuit_breaker_active': self.circuit_breaker_active,
            'risk_by_symbol': portfolio_risk['risk_by_symbol'],
        }

# ============================================
# RISK MANAGER - 0DTE TRADING
# Manages account risk, position sizing, and risk tiers
# ============================================

import logging

logger = logging.getLogger(__name__)


class RiskManager:
    """Handles risk tier evaluations, position sizing, and account equity tracking."""

    def __init__(self, api_handler=None, initial_equity=None):
        self.api_handler = api_handler
        self.current_equity = initial_equity
        self.daily_pnl = 0.0

    def update_equity(self, equity=None):
        """Updates the internal equity tracking state from parameter or API."""
        if equity is not None:
            self.current_equity = float(equity)
            return self.current_equity

        if self.api_handler:
            account_info = self.api_handler.get_account_info()
            if account_info and "equity" in account_info:
                self.current_equity = float(account_info["equity"])
                return self.current_equity

        logger.warning("Could not update equity from API handler.")
        return self.current_equity

    def tier(self):
        """Determines current risk tier based on account equity."""
        if self.current_equity is None:
            self.update_equity()

        if self.current_equity is None:
            raise RuntimeError("Update equity before requesting tier.")

        eq = self.current_equity

        if eq >= 100000:
            return "TIER_1"
        elif eq >= 50000:
            return "TIER_2"
        elif eq >= 25000:
            return "TIER_3"
        else:
            return "TIER_4"

    def calculate_position_size(
        self, short_strike, long_strike, credit_received
    ):
        """Calculates maximum contracts based on risk tier and max loss per spread."""
        width = abs(short_strike - long_strike)
        max_loss_per_contract = (width - credit_received) * 100

        if max_loss_per_contract <= 0:
            logger.error("Invalid spread parameters for position sizing.")
            return 0

        current_tier = self.tier()

        if current_tier == "TIER_1":
            max_risk = 5000.0
        elif current_tier == "TIER_2":
            max_risk = 2500.0
        elif current_tier == "TIER_3":
            max_risk = 1000.0
        else:
            max_risk = 500.0

        contracts = int(max_risk // max_loss_per_contract)
        return max(1, contracts)

    def snapshot(self):
        """Generates a snapshot of the current risk state safely."""
        self.update_equity()

        current_tier = self.tier()
        return {
            "equity": self.current_equity,
            "tier": current_tier,
            "daily_pnl": self.daily_pnl,
        }
    def reset_day(self):
        """Resets daily risk metrics."""
        pass
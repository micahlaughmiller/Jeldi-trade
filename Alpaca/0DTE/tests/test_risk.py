import pytest

import config
from conftest import et
from risk_manager import DayState, RiskManager, contracts_for, limit, tier


@pytest.mark.parametrize("equity, expected", [
    (9_999, 1), (10_000, 2), (29_999, 2), (30_000, 3), (49_999.99, 3), (50_000, 4), (1_000_000, 4),
])
def test_tier_bands(equity, expected):
    assert tier(equity) == expected


def test_min_contract_override_small_account():
    assert contracts_for(2_000, 5, 3.00, 0.0) == 1


def test_override_disabled(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_MIN_CONTRACT_OVERRIDE", False)
    assert contracts_for(2_000, 5, 3.00, 0.0) == 0


def test_normal_sizing():
    assert contracts_for(10_000, 5, 3.00, 0.0) == 2
    assert contracts_for(30_000, 10, 6.00, 0.0) == 3


def test_portfolio_risk_cap():
    assert contracts_for(10_000, 5, 3.00, 5_000.0) == 1
    assert contracts_for(10_000, 5, 3.00, 5_100.0) == 0
    assert contracts_for(2_000, 5, 3.00, 900.0) == 0


def test_b_sizing_uses_width_minus_credit():
    # B: width 10, credit 1.50 -> max loss $850/contract; 5% of 10k = 500 -> override to 1
    assert contracts_for(10_000, 10, 1.50, 0.0) == 1
    assert contracts_for(20_000, 10, 1.50, 0.0) == 1
    assert contracts_for(40_000, 10, 1.50, 0.0) == 2
    # combined cap: A's $400 open risk plus B's $850 would exceed 52% of $2,000
    assert contracts_for(2_000, 10, 1.50, 400.0) == 0


def test_max_contracts_cap():
    assert contracts_for(1_000_000, 5, 3.00, 0.0) == config.MAX_CONTRACTS_PER_TRADE


def test_news_day_half_size(monkeypatch):
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "half_size")
    assert contracts_for(10_000, 5, 3.00, 0.0, news_day=True) == 1
    assert contracts_for(2_000, 5, 3.00, 0.0, news_day=True) == 1
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "orb_only")
    assert contracts_for(10_000, 5, 3.00, 0.0, news_day=True) == 2


def test_invalid_spread_returns_zero():
    assert contracts_for(10_000, 5, 5.50, 0.0) == 0


def test_consecutive_loss_stop_is_per_strategy():
    rm = RiskManager(10_000)
    assert rm.trading_allowed("A") == (True, "OK")
    for _ in range(limit("A", "MAX_CONSECUTIVE_LOSSES") - 1):
        rm.record_trade("A", -60.0)
    assert rm.trading_allowed("A")[0] is True
    rm.record_trade("A", -60.0)
    ok, reason = rm.trading_allowed("A")
    assert ok is False and "CONSECUTIVE" in reason and reason.startswith("A:")
    assert rm.trading_allowed("B") == (True, "OK")


def test_win_resets_consecutive_losses():
    rm = RiskManager(10_000)
    rm.record_trade("B", -60.0)
    rm.record_trade("B", 90.0)
    rm.record_trade("B", -60.0)
    assert rm.state.for_strategy("B").consecutive_losses == 1
    assert rm.trading_allowed("B")[0] is True


def test_a_limits_are_tighter_than_b():
    assert (limit("A", "MAX_TRADES_PER_DAY"), limit("A", "MAX_CONSECUTIVE_LOSSES"), limit("A", "COOLDOWN_MIN")) == (3, 2, 30)
    assert (limit("B", "MAX_TRADES_PER_DAY"), limit("B", "MAX_CONSECUTIVE_LOSSES"), limit("B", "COOLDOWN_MIN", 0)) == (20, 5, 0)
    rm = RiskManager(10_000)
    for _ in range(3):
        rm.record_trade("A", 10.0)
        rm.record_trade("B", 10.0)
    ok, reason = rm.trading_allowed("A")
    assert ok is False and reason == "A: MAX_TRADES_PER_DAY (3)"
    assert rm.trading_allowed("B") == (True, "OK")
    rm2 = RiskManager(10_000)
    rm2.record_trade("A", -10.0)
    rm2.record_trade("A", -10.0)
    assert rm2.trading_allowed("A")[0] is False and "CONSECUTIVE" in rm2.trading_allowed("A")[1]


def test_cooldown_after_exit_applies_to_a_only():
    rm = RiskManager(10_000)
    rm.record_trade("A", -50.0, {"strategy": "A", "pnl": -50.0, "exit_time": et(10, 30)})
    rm.record_trade("B", -50.0, {"strategy": "B", "pnl": -50.0, "exit_time": et(10, 30)})
    ok, reason = rm.trading_allowed("A", now=et(10, 45))
    assert ok is False and reason == "A: COOLDOWN until 11:00"
    assert rm.trading_allowed("A", now=et(10, 59, 59))[0] is False
    assert rm.trading_allowed("A", now=et(11, 0)) == (True, "OK")
    assert rm.trading_allowed("B", now=et(10, 31)) == (True, "OK")
    # no clock given (legacy callers) -> the cool-down is not evaluated
    assert rm.trading_allowed("A") == (True, "OK")
    # the last exit survives a state round-trip
    restored = DayState.from_dict(rm.state.to_dict())
    assert restored.for_strategy("A").last_exit == et(10, 30).isoformat()


def test_max_trades_per_day_is_per_strategy():
    rm = RiskManager(10_000)
    for _ in range(config.MAX_TRADES_PER_DAY):
        rm.record_trade("B", 10.0)
    ok, reason = rm.trading_allowed("B")
    assert ok is False and "MAX_TRADES" in reason
    assert rm.trading_allowed("A")[0] is True
    assert rm.state.trades_today == config.MAX_TRADES_PER_DAY


def test_daily_loss_limit_is_combined():
    rm = RiskManager(10_000)
    rm.record_trade("A", -600.0)
    rm.record_trade("B", -400.0)
    assert rm.state.realized_pnl == pytest.approx(-1_000.0)
    assert rm.trading_allowed("A")[0] is False
    assert rm.trading_allowed("B")[0] is False
    rm2 = RiskManager(10_000)
    assert rm2.trading_allowed("A", equity=9_000.0)[0] is False
    assert rm2.trading_allowed("B", equity=9_500.0)[0] is True


def test_combined_totals_and_pnl_by_strategy():
    rm = RiskManager(10_000)
    rm.record_trade("A", 60.0, {"strategy": "A", "pnl": 60.0, "exit_time": "2026-09-17T10:30:00-04:00"})
    rm.record_trade("B", -20.0, {"strategy": "B", "pnl": -20.0, "exit_time": "2026-09-17T10:20:00-04:00"})
    assert rm.state.trades_today == 2
    assert rm.state.realized_pnl == pytest.approx(40.0)
    assert rm.state.pnl_by_strategy() == {"A": 60.0, "B": -20.0}
    assert [r["strategy"] for r in rm.state.closed_trades] == ["B", "A"]   # ordered by exit time


def test_day_state_roundtrip():
    rm = RiskManager(10_000)
    rm.record_trade("A", -50.0, {"pnl": -50.0})
    rm.record_trade("B", 25.0, {"pnl": 25.0})
    restored = DayState.from_dict(rm.state.to_dict())
    assert restored == rm.state
    assert restored.for_strategy("B").realized_pnl == pytest.approx(25.0)

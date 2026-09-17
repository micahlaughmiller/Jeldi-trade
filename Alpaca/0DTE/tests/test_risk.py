import pytest

import config
from risk_manager import DayState, RiskManager, contracts_for, tier


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


def test_consecutive_loss_stop():
    rm = RiskManager(10_000)
    assert rm.trading_allowed() == (True, "OK")
    rm.record_trade(-60.0)
    assert rm.trading_allowed()[0] is True
    rm.record_trade(-60.0)
    ok, reason = rm.trading_allowed()
    assert ok is False and "CONSECUTIVE" in reason


def test_win_resets_consecutive_losses():
    rm = RiskManager(10_000)
    rm.record_trade(-60.0)
    rm.record_trade(90.0)
    rm.record_trade(-60.0)
    assert rm.state.consecutive_losses == 1
    assert rm.trading_allowed()[0] is True


def test_max_trades_per_day():
    rm = RiskManager(10_000)
    for _ in range(config.MAX_TRADES_PER_DAY):
        rm.record_trade(10.0)
    ok, reason = rm.trading_allowed()
    assert ok is False and "MAX_TRADES" in reason


def test_daily_loss_limit_realized_and_equity():
    rm = RiskManager(10_000)
    rm.record_trade(-1_000.0)
    assert rm.trading_allowed()[0] is False
    rm2 = RiskManager(10_000)
    assert rm2.trading_allowed(equity=9_000.0)[0] is False
    assert rm2.trading_allowed(equity=9_500.0)[0] is True


def test_day_state_roundtrip():
    rm = RiskManager(10_000)
    rm.record_trade(-50.0, {"pnl": -50.0})
    restored = DayState.from_dict(rm.state.to_dict())
    assert restored == rm.state

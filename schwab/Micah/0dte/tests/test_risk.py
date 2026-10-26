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


def test_max_contracts_cap_legacy(monkeypatch):
    monkeypatch.setattr(config, "TESTING_MODE", False)
    assert contracts_for(1_000_000, 5, 3.00, 0.0) == config.MAX_CONTRACTS_PER_TRADE


def test_max_contracts_cap_testing_mode():
    # TESTING_MODE is on by default right now: caps well below MAX_CONTRACTS_PER_TRADE regardless of equity.
    assert contracts_for(1_000_000, 5, 3.00, 0.0) == config.TESTING_MAX_CONTRACTS
    assert contracts_for(1_000_000, 5, 3.00, 0.0, half_size=True) == config.TESTING_HALF_SIZE_MAX_CONTRACTS


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
    assert rm.trading_allowed("B") == (True, "OK")
    # set the streak directly (not via record_trade) so it isolates MAX_CONSECUTIVE_LOSSES from the
    # now much lower MAX_TRADES_PER_DAY, which looping record_trade this many times would trip first.
    rm.state.for_strategy("B").consecutive_losses = limit("B", "MAX_CONSECUTIVE_LOSSES") - 1
    assert rm.trading_allowed("B")[0] is True
    rm.state.for_strategy("B").consecutive_losses += 1
    ok, reason = rm.trading_allowed("B")
    assert ok is False and "CONSECUTIVE" in reason and reason.startswith("B:")
    assert rm.trading_allowed("A") == (True, "OK")


def test_win_resets_consecutive_losses():
    rm = RiskManager(10_000)
    rm.record_trade("B", -60.0)
    rm.record_trade("B", 90.0)
    rm.record_trade("B", -60.0)
    assert rm.state.for_strategy("B").consecutive_losses == 1
    assert rm.trading_allowed("B")[0] is True


def test_a_limits_are_tighter_than_b():
    assert (limit("A", "MAX_TRADES_PER_DAY"), limit("A", "MAX_CONSECUTIVE_LOSSES"), limit("A", "COOLDOWN_MIN")) == (5, 5, 30)
    from strategy import exit_setting
    assert exit_setting("A", "STOP_CONFIRM_SECONDS", 0) == 30 and exit_setting("B", "STOP_CONFIRM_SECONDS", 0) == 0
    # 2026-10-01: every strategy narrowed to the same 5 trades/day cap; A's remaining tighter trait is COOLDOWN_MIN.
    assert (limit("B", "MAX_TRADES_PER_DAY"), limit("B", "MAX_CONSECUTIVE_LOSSES"), limit("B", "COOLDOWN_MIN", 0)) == (5, 5, 0)
    rm = RiskManager(10_000)
    for _ in range(5):
        rm.record_trade("A", 10.0)
        rm.record_trade("B", 10.0)
    ok, reason = rm.trading_allowed("A")
    assert ok is False and reason == "A: MAX_TRADES_PER_DAY (5)"
    assert rm.trading_allowed("B") == (False, "B: MAX_TRADES_PER_DAY (5)")
    rm2 = RiskManager(10_000)
    for _ in range(5):
        rm2.record_trade("A", -10.0)
    assert rm2.trading_allowed("A")[0] is False


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


def test_cooldown_skips_when_the_setup_differs():
    rm = RiskManager(10_000)
    rm.record_trade("A", -50.0, {"strategy": "A", "pnl": -50.0, "exit_time": et(10, 30), "setup": "ON_BREAK"})
    # a different setup within the cool-down window is allowed
    assert rm.trading_allowed("A", now=et(10, 45), setup="ORB") == (True, "OK")
    # the SAME setup is still blocked
    ok, reason = rm.trading_allowed("A", now=et(10, 45), setup="ON_BREAK")
    assert ok is False and reason == "A: COOLDOWN until 11:00"
    # unknown setup on either side stays conservative (cool-down still applies)
    assert rm.trading_allowed("A", now=et(10, 45))[0] is False
    ok, reason = rm.trading_allowed("A", now=et(10, 45), setup=None)
    assert ok is False


def test_cooldown_setup_survives_state_roundtrip():
    rm = RiskManager(10_000)
    rm.record_trade("A", -50.0, {"strategy": "A", "pnl": -50.0, "exit_time": et(10, 30), "setup": "ON_BREAK"})
    restored = DayState.from_dict(rm.state.to_dict())
    assert restored.for_strategy("A").last_exit_setup == "ON_BREAK"


# ------------------------------------------------------------ Strategy F: entry-counted cap, exempt P/L

def test_f_limits():
    assert (limit("F", "MAX_TRADES_PER_DAY"), limit("F", "MAX_CONSECUTIVE_LOSSES"), limit("F", "COOLDOWN_MIN", 0)) == (3, 7, 0)
    assert limit("F", "MAX_CONCURRENT_POSITIONS", 1) == 3 and limit("A", "MAX_CONCURRENT_POSITIONS", 1) == 1
    assert limit("B", "MAX_CONCURRENT_POSITIONS", config.MAX_CONCURRENT_POSITIONS) == 1


def test_entries_cap_counts_recorded_entries_and_open_positions(f_five_a_day):
    rm = RiskManager(10_000)
    assert rm.record_entry("F") == 1 and rm.state.for_strategy("F").entries_today == 1
    for _ in range(3):
        rm.record_entry("F")
    assert rm.trading_allowed("F") == (True, "OK")                                  # 4 entries
    assert rm.trading_allowed("F", open_count=1) == (True, "OK")                    # 0 closed + 1 open < 4 recorded
    rm.record_entry("F")
    assert rm.trading_allowed("F") == (False, "F: MAX_TRADES_PER_DAY (5)")
    rm2 = RiskManager(10_000)
    for _ in range(3):
        rm2.record_trade("F", 5.0)
    assert rm2.trading_allowed("F", open_count=1) == (True, "OK")
    assert rm2.trading_allowed("F", open_count=2) == (False, "F: MAX_TRADES_PER_DAY (5)")
    assert rm2.trading_allowed("A", open_count=3) == (True, "OK")                   # A-E never pass open_count
    assert rm2.state.for_strategy("A").entries_today == 0


def test_daily_loss_limit_excludes_exempt_realized_and_unrealized_pnl():
    rm = RiskManager(10_000)
    rm.record_trade("F", -900.0)
    rm.record_trade("A", -300.0)
    assert rm.state.realized_pnl == pytest.approx(-1_200.0)
    assert rm.state.loss_limit_pnl == pytest.approx(-300.0) and rm.state.exempt_realized_pnl == pytest.approx(-900.0)
    assert rm.trading_allowed("A") == (True, "OK") and rm.trading_allowed("B") == (True, "OK")
    assert rm.trading_allowed("A", equity=8_800.0) == (True, "OK")                  # 8,800 + 900 = 9,700 ex-F
    assert rm.trading_allowed("A", equity=8_300.0)[0] is True                       # exactly -1,000 + 900 = -100 ... fine
    assert rm.trading_allowed("A", equity=8_099.0)[0] is False                      # -1,901 + 900 = -1,001
    assert rm.trading_allowed("A", equity=8_500.0, exempt_open_pnl=-600.0) == (True, "OK")   # -1,500 + 900 + 600 = 0
    ok, reason = rm.trading_allowed("A", equity=8_098.0, exempt_open_pnl=-1.0)
    assert ok is False and reason == "DAILY_LOSS_LIMIT equity=8999.00 start=10000.00"
    rm.record_trade("B", -700.0)
    ok, reason = rm.trading_allowed("A")
    assert ok is False and reason == "DAILY_LOSS_LIMIT realized=-1000.00"


def test_ab_halt_does_not_stop_exempt_strategy_unless_configured(monkeypatch):
    rm = RiskManager(10_000)
    rm.record_trade("A", -1_000.0)
    assert rm.trading_allowed("A")[0] is False and rm.trading_allowed("B")[0] is False
    assert rm.trading_allowed("F", equity=8_000.0) == (True, "OK")
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_HALTS_EXEMPT", True)
    assert rm.trading_allowed("F", equity=8_000.0) == (False, "DAILY_LOSS_LIMIT realized=-1000.00")
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    rm3 = RiskManager(10_000)
    rm3.record_trade("F", -1_000.0)
    assert rm3.trading_allowed("A")[0] is False                                     # no exemption: F counts again


def test_day_state_roundtrip_with_entries_today():
    rm = RiskManager(10_000)
    rm.record_entry("F")
    rm.record_entry("F")
    rm.record_trade("F", -20.0, {"strategy": "F", "pnl": -20.0, "exit_time": et(10, 30), "setup": "STOCK_ORB"})
    restored = DayState.from_dict(rm.state.to_dict())
    assert restored == rm.state and restored.for_strategy("F").entries_today == 2
    legacy = DayState.from_dict({"start_equity": 10_000, "strategies": {"A": {"trades_today": 1, "realized_pnl": 5.0}}})
    assert legacy.for_strategy("A").entries_today == 0


# ------------------------------------------------- 2026-10-15: daily profit target; F under the loss limit (Schwab 0DTE ships both)

def test_the_daily_profit_target_stops_new_entries_for_every_strategy(monkeypatch):
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", 0.02, raising=False)
    rm = RiskManager(1_000)
    assert rm.trading_allowed("B") == (True, "OK")
    rm.record_trade("B", 19.99)
    assert rm.trading_allowed("B") == (True, "OK") and rm.trading_allowed("F", equity=1_000.0) == (True, "OK")
    rm.record_trade("B", 0.01)                                                      # exactly 2% of 1,000
    for strat in ("B", "F"):
        ok, reason = rm.trading_allowed(strat, equity=1_000.0)
        assert ok is False and reason == "DAILY_PROFIT_TARGET realized=20.00 >= 20.00", (strat, reason)


def test_the_profit_target_counts_exempt_strategies_pnl_too(monkeypatch):
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", 0.02, raising=False)
    rm = RiskManager(1_000)
    rm.record_trade("F", 25.0)                                                      # F is exempt from the LOSS limit, not from the day's goal
    assert rm.trading_allowed("B")[0] is False and rm.trading_allowed("F")[0] is False


def test_no_profit_target_means_no_limit_on_gains(monkeypatch):
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", None, raising=False)
    rm = RiskManager(1_000)
    rm.record_trade("B", 10_000.0)
    assert rm.trading_allowed("B") == (True, "OK")


def test_the_target_is_a_fraction_of_the_days_start_equity(monkeypatch):
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", 0.02, raising=False)
    big = RiskManager(10_000)
    big.record_trade("B", 150.0)
    assert big.trading_allowed("B") == (True, "OK")                                 # 200 would be the goal
    big.record_trade("B", 50.0)
    assert big.trading_allowed("B")[0] is False


def test_f_follows_the_daily_loss_limit_when_it_is_not_exempt(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    rm = RiskManager(1_000)
    rm.record_trade("F", -9.99)
    assert rm.trading_allowed("B") == (True, "OK") and rm.trading_allowed("F") == (True, "OK")
    rm.record_trade("F", -0.01)                                                     # exactly -1% of 1,000
    for strat in ("B", "F"):
        ok, reason = rm.trading_allowed(strat)
        assert ok is False and reason == "DAILY_LOSS_LIMIT realized=-10.00", (strat, reason)
    fresh = RiskManager(1_000)                                                      # the equity (unrealized) path stops F as well
    assert fresh.trading_allowed("F", equity=990.01) == (True, "OK")
    ok, reason = fresh.trading_allowed("F", equity=990.0)
    assert ok is False and reason == "DAILY_LOSS_LIMIT equity=990.00 start=1000.00"
    assert fresh.trading_allowed("B", equity=990.0)[0] is False


def test_b_losses_stop_f_when_nothing_is_exempt(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    rm = RiskManager(1_000)
    rm.record_trade("B", -12.0)
    ok, reason = rm.trading_allowed("F", equity=1_000.0)
    assert ok is False and reason.startswith("DAILY_LOSS_LIMIT")


# ------------------------------------------------- 2026-10-15: the loss limit on the bot's own P/L (Schwab's shared account)

def _bot_basis(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())


def test_with_the_bot_basis_the_accounts_value_never_trips_the_loss_limit(monkeypatch):
    _bot_basis(monkeypatch)
    rm = RiskManager(1_000)
    for equity in (1_000.0, 990.0, 900.0, 500.0, 0.0):                  # the owner's holdings or the 45DTE bot moved the account
        assert rm.trading_allowed("B", equity=equity) == (True, "OK"), equity
        assert rm.trading_allowed("F", equity=equity) == (True, "OK"), equity


def test_with_the_bot_basis_realized_and_open_losses_trip_it(monkeypatch):
    _bot_basis(monkeypatch)
    rm = RiskManager(1_000)
    assert rm.trading_allowed("B", own_open_pnl=-9.99) == (True, "OK")
    ok, reason = rm.trading_allowed("B", own_open_pnl=-10.0)             # the open trade alone is down exactly 1%
    assert ok is False and reason == "DAILY_LOSS_LIMIT bot P/L=-10.00 (realized 0.00, open -10.00)"
    rm.record_trade("B", -6.0)
    assert rm.trading_allowed("F", own_open_pnl=-3.99) == (True, "OK")   # -6.00 realized + -3.99 open = -9.99
    ok, reason = rm.trading_allowed("F", own_open_pnl=-4.0)
    assert ok is False and reason == "DAILY_LOSS_LIMIT bot P/L=-10.00 (realized -6.00, open -4.00)"
    rm.record_trade("F", -4.0)                                           # banked: now the realized check alone stops everything
    ok, reason = rm.trading_allowed("B")
    assert ok is False and reason == "DAILY_LOSS_LIMIT realized=-10.00"


def test_with_the_bot_basis_an_open_profit_offsets_a_realized_loss(monkeypatch):
    _bot_basis(monkeypatch)
    rm = RiskManager(1_000)
    rm.record_trade("B", -8.0)
    assert rm.trading_allowed("B", own_open_pnl=15.0) == (True, "OK")
    assert rm.trading_allowed("B", own_open_pnl=-2.0)[0] is False


def test_the_equity_basis_is_unchanged_and_ignores_own_open_pnl(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "equity", raising=False)
    rm = RiskManager(10_000)
    assert rm.trading_allowed("B", equity=9_001.0, own_open_pnl=-5_000.0) == (True, "OK")
    ok, reason = rm.trading_allowed("B", equity=9_000.0)
    assert ok is False and reason == "DAILY_LOSS_LIMIT equity=9000.00 start=10000.00"

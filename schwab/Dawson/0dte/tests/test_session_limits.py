"""2026-10-07 session limits as the shipped config sets them: new entries only until 12:00 ET (the hard stop stays
13:00 ET); trades per day capped per strategy (B 5, C/D/E/F 3). The optional all-strategies total cap is off but works."""

from datetime import time as dtime

import pytest

import config
import strategy
from conftest import et
from risk_manager import RiskManager
from strategy import Phase


def test_shipped_session_times_and_trade_limits():
    from risk_manager import limit
    assert config.LAST_ENTRY_TIME == dtime(12, 0) and config.FORCE_CLOSE_TIME == dtime(13, 0)
    assert config.MAX_TRADES_TOTAL_PER_DAY is None                       # per-strategy limits instead of a combined cap
    assert [limit(s, "MAX_TRADES_PER_DAY") for s in "BCDEF"] == [5, 3, 3, 3, 3]
    assert config.MANUAL_HOLD_DELAYS_FORCE_CLOSE is False


def test_b_gets_five_trades_a_day_and_every_other_strategy_three():
    rm = RiskManager(10_000)
    for _ in range(5):
        assert rm.trading_allowed("B")[0] is True
        rm.record_trade("B", 10.0)
    assert rm.trading_allowed("B")[0] is False and "MAX_TRADES_PER_DAY (5)" in rm.trading_allowed("B")[1]
    for strat in "CDE":
        for n in range(3):
            assert rm.trading_allowed(strat)[0] is True, (strat, n)
            rm.record_trade(strat, 10.0)
        ok, reason = rm.trading_allowed(strat)
        assert ok is False and "MAX_TRADES_PER_DAY (3)" in reason
    for _ in range(3):                                                   # F counts at entry
        assert rm.trading_allowed("F")[0] is True
        rm.record_entry("F")
    assert rm.trading_allowed("F")[0] is False
    assert rm.state.trades_today == 14                                    # 5 + 3 + 3 + 3 closed (F's entries are not closes)


def test_entries_stop_at_noon_eastern_and_everything_closes_at_one():
    assert strategy.phase(et(9, 29)) == Phase.PRE_OPEN
    assert strategy.phase(et(9, 30)) == Phase.OVERNIGHT_ONLY
    assert strategy.phase(et(11, 59, 59)) == Phase.ORB_ONLY
    assert strategy.phase(et(12, 0, 0)) == Phase.NO_NEW_ENTRIES
    assert strategy.phase(et(12, 59, 59)) == Phase.NO_NEW_ENTRIES
    assert strategy.phase(et(13, 0, 0)) == Phase.CLOSED
    assert Phase.ORB_ONLY in strategy.ENTRY_PHASES and Phase.NO_NEW_ENTRIES not in strategy.ENTRY_PHASES


def test_total_cap_counts_every_strategy_together(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    rm = RiskManager(10_000)
    for strat in ("A", "A", "B", "B"):
        rm.record_trade(strat, 10.0)
    assert rm.trading_allowed("A")[0] is True and rm.trading_allowed("B")[0] is True       # 4 of 5
    rm.record_trade("B", 10.0)
    for strat in ("A", "B"):
        ok, reason = rm.trading_allowed(strat)
        assert ok is False and reason.startswith("TOTAL_MAX_TRADES_PER_DAY (5/5)")
    assert rm.trading_allowed("F")[0] is False                                           # F is one of the five too


def test_open_spx_positions_count_toward_the_total_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    rm = RiskManager(10_000)
    for strat in ("A", "A", "A", "B"):
        rm.record_trade(strat, 10.0)
    assert rm.trading_allowed("B")[0] is True
    ok, reason = rm.trading_allowed("B", open_spx=1)             # one more is open: 4 closed + 1 open = 5
    assert ok is False and "TOTAL_MAX_TRADES_PER_DAY (5/5)" in reason


def test_f_entries_count_when_they_are_taken_not_only_when_they_close(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    rm = RiskManager(10_000)
    for _ in range(3):
        rm.record_entry("F")                                      # three F spreads entered, none closed yet
    rm.record_trade("B", 10.0)
    assert rm.trading_allowed("A")[0] is True                     # 3 + 1 = 4
    rm.record_trade("B", 10.0)
    assert rm.trading_allowed("A")[0] is False                    # 3 + 2 = 5
    rm.record_trade("F", 5.0)                                     # an F close does not count again
    assert rm.state.for_strategy("F").entries_today == 3 and rm.trading_allowed("A")[0] is False


def test_with_the_total_cap_off_only_the_per_strategy_caps_apply():
    rm = RiskManager(10_000)
    for strat in ("A", "A", "B", "B", "B", "A"):
        rm.record_trade(strat, 1.0)
    assert config.MAX_TRADES_TOTAL_PER_DAY is None and rm.trading_allowed("A")[0] is True


# ------------------------------------------------- a daily cap shared by B and C (2026-11-12)

def test_b_and_c_share_a_daily_cap(monkeypatch):
    """Micah, 12 Nov 2026: "all B and C strategies to be a total of 1-3 trades a day" -- B and C together take at most 3 a day."""
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("B", "C"): 3})
    rm = RiskManager(10_000)
    rm.record_trade("B", 10.0)
    rm.record_trade("C", 10.0)
    assert rm.trading_allowed("B")[0] is True and rm.trading_allowed("C")[0] is True          # 2 of 3
    ok, reason = rm.trading_allowed("B", open_by_strategy={"C": 1})                            # ... and the third is open in C
    assert ok is False and reason == "SHARED_MAX_TRADES_PER_DAY (B+C 3/3)"
    rm.record_trade("B", 10.0)
    for strat in ("B", "C"):
        ok, reason = rm.trading_allowed(strat)
        assert ok is False and reason == "SHARED_MAX_TRADES_PER_DAY (B+C 3/3)"
    for strat in ("A", "D", "F"):                                                              # not in the group: untouched
        assert rm.trading_allowed(strat)[0] is True, strat


def test_the_shared_cap_counts_open_positions_and_f_style_entries_once(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("B", "C"): 3, ("F",): 2})
    rm = RiskManager(10_000)
    assert rm.trading_allowed("B", open_by_strategy={"B": 1, "C": 1})[0] is True               # two open, none closed
    ok, reason = rm.trading_allowed("C", open_by_strategy={"B": 2, "C": 1})                    # three open
    assert ok is False and reason == "SHARED_MAX_TRADES_PER_DAY (B+C 3/3)"
    rm.record_entry("F")
    rm.record_entry("F")                                                                       # F counts at entry ...
    ok, reason = rm.trading_allowed("F", open_by_strategy={"F": 2})                            # ... its open spreads are those entries
    assert ok is False and reason == "SHARED_MAX_TRADES_PER_DAY (F 2/2)"
    assert rm.trading_allowed("B", open_by_strategy={"F": 2})[0] is True                       # F's cap is F's


def test_without_the_open_counts_the_shared_cap_knows_this_strategy_s_own_open_positions(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("B", "C"): 2})
    rm = RiskManager(10_000)
    rm.record_trade("C", 10.0)
    assert rm.trading_allowed("B")[0] is True
    assert rm.trading_allowed("B", open_count=1)[0] is False                                   # 1 closed C + 1 open B


def test_the_shared_cap_survives_a_restart(monkeypatch):
    """The counts live in the per-strategy day state the state file already holds; a group member with no state yet counts 0."""
    from risk_manager import DayState
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("B", "C"): 3})
    rm = RiskManager(10_000)
    for strat in ("B", "B", "C"):
        rm.record_trade(strat, 10.0)
    restored = RiskManager(10_000, DayState.from_dict(rm.state.to_dict()))
    assert restored.trading_allowed("B") == (False, "SHARED_MAX_TRADES_PER_DAY (B+C 3/3)")
    fresh = RiskManager(10_000, DayState.from_dict({"start_equity": 10_000, "strategies": {"B": {"trades_today": 1}}}))
    assert fresh.trading_allowed("B")[0] is True and "C" not in fresh.state.strategies           # C has no state yet: counts 0, none made for it
    assert fresh.trading_allowed("C")[0] is True


# ------------------------------------------------- at most 2 trades open at one time (2026-10-08)

@pytest.fixture(autouse=True)
def _alpaca_open_cap(monkeypatch):
    """These tests describe the Alpaca default of two trades open at once; Schwab 0DTE ships one (2026-10-15). Each folder's real value is
    guarded in its own test_*_adoption_rules.py."""
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 2)


def test_a_third_trade_is_refused_while_two_are_open_whatever_the_strategy():
    rm = RiskManager(10_000)
    for strat in "BCDEF":
        assert rm.trading_allowed(strat, open_total=0)[0] is True
        assert rm.trading_allowed(strat, open_total=1)[0] is True
        ok, reason = rm.trading_allowed(strat, open_total=2)
        assert ok is False and reason == "MAX_OPEN_POSITIONS (2/2)", (strat, reason)


def test_the_open_cap_is_independent_of_the_daily_trade_counts():
    rm = RiskManager(10_000)
    rm.record_trade("B", 10.0)
    assert rm.trading_allowed("C", open_total=1)[0] is True and rm.trading_allowed("C", open_total=2)[0] is False


def test_entries_are_refused_while_a_close_is_failing():
    rm = RiskManager(10_000)
    for strat in "BCDEF":
        ok, reason = rm.trading_allowed(strat, close_blocked=True)
        assert ok is False and reason.startswith("CLOSE_BLOCKED"), strat
    assert rm.trading_allowed("B", close_blocked=False)[0] is True


def test_shipped_close_failure_settings():
    assert tuple(config.CLOSE_BLOCK_BACKOFF_SEC) == (30, 60, 120, 300) and config.EOD_CLOSE_RETRY_SEC == 60


def test_shipped_data_failure_settings():
    assert (config.MARK_STALE_ALERT_SEC, config.MARK_STALE_FLATTEN_SEC) == (20, 90)
    assert (config.F_MARK_STALE_ALERT_SEC, config.F_MARK_STALE_FLATTEN_SEC) == (60, 180)
    assert config.STALE_MARK_FLATTEN_ENABLED is True and config.PREV_TICK_MAX_AGE_SEC == 300
    assert (config.TOKEN_CHECK_SEC, config.TOKEN_WARN_DAYS, config.TOKEN_ENTRY_MARGIN_MIN) == (60, 5.0, 30)


def test_a_halt_reason_stops_every_strategy():
    rm = RiskManager(10_000)
    for strat in "BCDEF":
        assert rm.trading_allowed(strat, halt="TOKEN_DEAD (x): new entries halted") == (False, "TOKEN_DEAD (x): new entries halted")
    assert rm.trading_allowed("B", halt=None)[0] is True

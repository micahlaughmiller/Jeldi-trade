"""2026-10-07 session limits as the shipped config sets them: new entries only until 12:00 ET (the hard stop stays
13:00 ET) and ONE cap of 5 trades a day across every strategy of a persona."""

from datetime import time as dtime

import config
import strategy
from conftest import et
from risk_manager import RiskManager
from strategy import Phase


def test_shipped_session_times_and_total_cap():
    assert config.LAST_ENTRY_TIME == dtime(12, 0) and config.FORCE_CLOSE_TIME == dtime(13, 0)
    assert config.MAX_TRADES_TOTAL_PER_DAY == 5
    assert config.MANUAL_HOLD_DELAYS_FORCE_CLOSE is False


def test_entries_stop_at_noon_eastern_and_everything_closes_at_one():
    assert strategy.phase(et(9, 29)) == Phase.PRE_OPEN
    assert strategy.phase(et(9, 30)) == Phase.OVERNIGHT_ONLY
    assert strategy.phase(et(11, 59, 59)) == Phase.ORB_ONLY
    assert strategy.phase(et(12, 0, 0)) == Phase.NO_NEW_ENTRIES
    assert strategy.phase(et(12, 59, 59)) == Phase.NO_NEW_ENTRIES
    assert strategy.phase(et(13, 0, 0)) == Phase.CLOSED
    assert Phase.ORB_ONLY in strategy.ENTRY_PHASES and Phase.NO_NEW_ENTRIES not in strategy.ENTRY_PHASES


def test_total_cap_counts_every_strategy_together():
    rm = RiskManager(10_000)
    for strat in ("A", "A", "B", "B"):
        rm.record_trade(strat, 10.0)
    assert rm.trading_allowed("A")[0] is True and rm.trading_allowed("B")[0] is True       # 4 of 5
    rm.record_trade("B", 10.0)
    for strat in ("A", "B"):
        ok, reason = rm.trading_allowed(strat)
        assert ok is False and reason.startswith("TOTAL_MAX_TRADES_PER_DAY (5/5)")
    assert rm.trading_allowed("F")[0] is False                                           # F is one of the five too


def test_open_spx_positions_count_toward_the_total_cap():
    rm = RiskManager(10_000)
    for strat in ("A", "A", "A", "B"):
        rm.record_trade(strat, 10.0)
    assert rm.trading_allowed("B")[0] is True
    ok, reason = rm.trading_allowed("B", open_spx=1)             # one more is open: 4 closed + 1 open = 5
    assert ok is False and "TOTAL_MAX_TRADES_PER_DAY (5/5)" in reason


def test_f_entries_count_when_they_are_taken_not_only_when_they_close():
    rm = RiskManager(10_000)
    for _ in range(3):
        rm.record_entry("F")                                      # three F spreads entered, none closed yet
    rm.record_trade("B", 10.0)
    assert rm.trading_allowed("A")[0] is True                     # 3 + 1 = 4
    rm.record_trade("B", 10.0)
    assert rm.trading_allowed("A")[0] is False                    # 3 + 2 = 5
    rm.record_trade("F", 5.0)                                     # an F close does not count again
    assert rm.state.for_strategy("F").entries_today == 3 and rm.trading_allowed("A")[0] is False


def test_the_total_cap_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", None)
    rm = RiskManager(10_000)
    for strat in ("A", "A", "B", "B", "B", "A"):
        rm.record_trade(strat, 1.0)
    assert rm.trading_allowed("A")[0] is True                     # only the per-strategy caps (5 each) apply

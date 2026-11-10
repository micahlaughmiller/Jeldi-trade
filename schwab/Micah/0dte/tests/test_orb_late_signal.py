"""2026-11-10: an ORB signal found on a replayed candle (several candles arrived at once after a data stall) is taken while it
is recent and the move is still beyond the level, instead of being dropped as stale with the setup marked DONE."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

import config
import market_data
import scheduler
import strategy

ET = config.ET


def _bot(or_high=7500.0, or_low=7480.0):
    bot = scheduler.Bot.__new__(scheduler.Bot)
    bot.orb = strategy.Levels(or_high, or_low, datetime(2026, 11, 10, 9, 45, tzinfo=ET))
    bot.orb_setup = strategy.OrbSetup(or_high, or_low)
    bot.last_orb_candle = None
    return bot


def _frame(rows):
    idx = pd.DatetimeIndex([t for t, *_ in rows])
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1.0} for _, o, h, l, c in rows], index=idx)


def t(h, m):
    return datetime(2026, 11, 10, h, m, tzinfo=ET)


def test_a_recent_signal_on_an_older_replayed_candle_is_taken(monkeypatch, caplog):
    import logging
    bot = _bot()
    # three 5-minute candles arrive together: 09:45 closes above the range (arms), 09:50 closes higher (MOMENTUM fires), 09:55 keeps going
    frame = _frame([(t(9, 45), 7498, 7510, 7497, 7508), (t(9, 50), 7508, 7520, 7506, 7518), (t(9, 55), 7518, 7530, 7516, 7528)])
    monkeypatch.setattr(market_data, "get_candles", lambda *a, **k: frame)
    with caplog.at_level(logging.INFO):
        signal = bot.orb_signal(t(10, 0) + timedelta(seconds=30))
    assert signal is not None and signal[0] == strategy.BULLISH and signal[1] == t(9, 50) and signal[2] == strategy.MOMENTUM
    assert any("Late ORB" in r.getMessage() for r in caplog.records)
    assert bot.last_orb_candle == t(9, 55)


def test_an_old_replayed_signal_is_still_ignored(monkeypatch):
    bot = _bot()
    frame = _frame([(t(9, 45), 7498, 7510, 7497, 7508), (t(9, 50), 7508, 7520, 7506, 7518), (t(9, 55), 7518, 7530, 7516, 7528)])
    monkeypatch.setattr(market_data, "get_candles", lambda *a, **k: frame)
    assert bot.orb_signal(t(10, 30)) is None                              # the 09:50 candle is 40 minutes old: not taken


def test_a_replayed_signal_whose_move_came_back_inside_the_range_is_ignored(monkeypatch):
    bot = _bot()
    frame = _frame([(t(9, 45), 7498, 7510, 7497, 7508), (t(9, 50), 7508, 7520, 7506, 7518), (t(9, 55), 7518, 7519, 7490, 7495)])
    monkeypatch.setattr(market_data, "get_candles", lambda *a, **k: frame)
    assert bot.orb_signal(t(10, 0) + timedelta(seconds=30)) is None       # 09:55 closed back inside: the setup reset
    assert bot.orb_setup.state == strategy.OrbSetup.WAITING


def test_a_signal_on_the_newest_candle_is_unchanged(monkeypatch):
    bot = _bot()
    frame = _frame([(t(9, 45), 7498, 7510, 7497, 7508), (t(9, 50), 7508, 7520, 7506, 7518)])
    monkeypatch.setattr(market_data, "get_candles", lambda *a, **k: frame)
    signal = bot.orb_signal(t(9, 55) + timedelta(seconds=5))
    assert signal is not None and signal[1] == t(9, 50)


def test_the_15_minute_range_puts_the_orb_phase_at_0945(monkeypatch):
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 15)
    monkeypatch.setattr(config, "OVERNIGHT_ENTRY_END_MIN", 15)
    assert strategy.phase(t(9, 44)) == strategy.Phase.OVERNIGHT_ONLY
    assert strategy.phase(t(9, 45)) == strategy.Phase.ORB_ONLY
    assert market_data.get_opening_range(t(9, 44)) is None               # the range is still forming


def test_a_replayed_signal_whose_move_gave_back_the_break_is_ignored(monkeypatch):
    bot = _bot()
    # 09:50 fired (close 7518 > break close 7508); 09:55 closed at 7503: still above the range high 7500, but below the break close
    frame = _frame([(t(9, 45), 7498, 7510, 7497, 7508), (t(9, 50), 7508, 7520, 7506, 7518), (t(9, 55), 7518, 7519, 7501, 7503)])
    monkeypatch.setattr(market_data, "get_candles", lambda *a, **k: frame)
    assert bot.orb_signal(t(10, 0) + timedelta(seconds=30)) is None
    assert bot.orb_setup.state == strategy.OrbSetup.DONE                  # the setup itself is unchanged

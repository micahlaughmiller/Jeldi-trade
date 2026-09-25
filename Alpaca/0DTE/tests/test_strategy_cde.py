from datetime import time

import pandas as pd
import pytest

import strategy
from conftest import et, make_candles


# --------------------------------------------------------------------------- EMA / Bollinger / %B

def test_ema_matches_pandas_ewm():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = strategy.ema(s, span=3)
    expected = s.ewm(span=3, adjust=False).mean()
    assert list(result) == list(expected)


def test_bollinger_default_basis_is_rolling_mean():
    s = pd.Series([10.0, 10.0, 10.0, 20.0])
    basis, upper, lower = strategy.bollinger(s, period=2, num_std=2.0)
    # last window is [10, 20]: mean 15, population std 5 -> upper 25, lower 5
    assert basis.iloc[-1] == pytest.approx(15.0)
    assert upper.iloc[-1] == pytest.approx(25.0)
    assert lower.iloc[-1] == pytest.approx(5.0)


def test_bollinger_custom_basis_overrides_rolling_mean():
    s = pd.Series([10.0, 10.0, 10.0, 20.0])
    custom_basis = pd.Series([1.0, 2.0, 3.0, 4.0])
    basis, upper, lower = strategy.bollinger(s, period=2, num_std=2.0, basis=custom_basis)
    assert basis.iloc[-1] == 4.0
    # std unaffected by the custom basis (still computed from the raw series' rolling std)
    assert upper.iloc[-1] == pytest.approx(4.0 + 2.0 * 5.0)
    assert lower.iloc[-1] == pytest.approx(4.0 - 2.0 * 5.0)


def test_percent_b_at_extremes_and_middle():
    assert strategy.percent_b(100.0, upper=110.0, lower=90.0) == pytest.approx(0.5)
    assert strategy.percent_b(110.0, upper=110.0, lower=90.0) == pytest.approx(1.0)
    assert strategy.percent_b(90.0, upper=110.0, lower=90.0) == pytest.approx(0.0)


def test_percent_b_none_on_zero_width():
    assert strategy.percent_b(100.0, upper=100.0, lower=100.0) is None


# --------------------------------------------------------------------------------- trend / signal

def test_ema_trend_rising_falling_chop():
    assert strategy.ema_trend(105.0, 100.0, chop_threshold=2.0) == "RISING"
    assert strategy.ema_trend(95.0, 100.0, chop_threshold=2.0) == "FALLING"
    assert strategy.ema_trend(101.0, 100.0, chop_threshold=2.0) == "CHOP"
    assert strategy.ema_trend(100.5, 100.0, chop_threshold=1.0) == "CHOP"


def test_mean_reversion_signal_rising_trend_allows_only_bull_side():
    # below MA and near the lower band in a rising trend -> bull-side reversion
    assert strategy.mean_reversion_signal(spot=95.0, ma=100.0, pct_b=0.03, trend="RISING",
                                          band_touch=0.95) == strategy.BULLISH
    # same band read but price is ABOVE the MA -> no signal (direction gate blocks it)
    assert strategy.mean_reversion_signal(spot=105.0, ma=100.0, pct_b=0.03, trend="RISING",
                                          band_touch=0.95) is None
    # rising trend never fires the bear side even if price touches the upper band
    assert strategy.mean_reversion_signal(spot=105.0, ma=100.0, pct_b=0.97, trend="RISING",
                                          band_touch=0.95) is None


def test_mean_reversion_signal_falling_trend_allows_only_bear_side():
    assert strategy.mean_reversion_signal(spot=105.0, ma=100.0, pct_b=0.97, trend="FALLING",
                                          band_touch=0.95) == strategy.BEARISH
    assert strategy.mean_reversion_signal(spot=95.0, ma=100.0, pct_b=0.97, trend="FALLING",
                                          band_touch=0.95) is None
    assert strategy.mean_reversion_signal(spot=95.0, ma=100.0, pct_b=0.03, trend="FALLING",
                                          band_touch=0.95) is None


def test_mean_reversion_signal_chop_always_none():
    assert strategy.mean_reversion_signal(spot=95.0, ma=100.0, pct_b=0.01, trend="CHOP",
                                          band_touch=0.95) is None


def test_mean_reversion_signal_band_not_touched_yet():
    # below MA (right side for a rising-trend bull setup) but not yet near the lower band
    assert strategy.mean_reversion_signal(spot=99.0, ma=100.0, pct_b=0.5, trend="RISING",
                                          band_touch=0.95) is None


def test_mean_reversion_signal_none_pct_b():
    assert strategy.mean_reversion_signal(spot=95.0, ma=100.0, pct_b=None, trend="RISING",
                                          band_touch=0.95) is None


def test_two_candle_confirm():
    up = make_candles(et(10, 0), [(100, 101, 99, 101), (101, 102, 100, 102)], minutes=5)
    down = make_candles(et(10, 0), [(100, 101, 99, 99), (99, 100, 97, 97)], minutes=5)
    mixed = make_candles(et(10, 0), [(100, 101, 99, 101), (101, 102, 99, 99)], minutes=5)
    assert strategy.two_candle_confirm(up, strategy.BULLISH) is True
    assert strategy.two_candle_confirm(down, strategy.BEARISH) is True
    assert strategy.two_candle_confirm(mixed, strategy.BULLISH) is False
    assert strategy.two_candle_confirm(up, strategy.BULLISH, n=3) is False  # not enough candles


# --------------------------------------------------------------------------------- TwoCandleBreak

def test_two_candle_break_fires_on_second_confirming_close():
    tcb = strategy.TwoCandleBreak(level_high=100.0, level_low=90.0)
    c1 = pd.Series({"open": 99.0, "high": 105.0, "low": 98.0, "close": 101.0})   # breaks, closes above
    c2 = pd.Series({"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0})  # confirms
    assert tcb.update(c1) is None
    assert tcb.state == strategy.TwoCandleBreak.ARMED
    assert tcb.update(c2) == strategy.BULLISH
    assert tcb.state == strategy.TwoCandleBreak.DONE


def test_two_candle_break_ignores_wicks():
    """A wick through the level on the break candle doesn't matter -- only closes count."""
    tcb = strategy.TwoCandleBreak(level_high=100.0, level_low=90.0)
    # break candle's LOW wicks all the way down through the level, but it still CLOSES above it
    c1 = pd.Series({"open": 99.0, "high": 105.0, "low": 50.0, "close": 101.0})
    c2 = pd.Series({"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0})
    assert tcb.update(c1) is None
    assert tcb.update(c2) == strategy.BULLISH


def test_two_candle_break_resets_on_close_back_inside():
    tcb = strategy.TwoCandleBreak(level_high=100.0, level_low=90.0)
    c1 = pd.Series({"open": 99.0, "high": 105.0, "low": 98.0, "close": 101.0})
    c2_reset = pd.Series({"open": 101.0, "high": 101.5, "low": 95.0, "close": 95.0})  # closes back inside
    assert tcb.update(c1) is None
    assert tcb.update(c2_reset) is None
    assert tcb.state == strategy.TwoCandleBreak.WAITING


def test_two_candle_break_bearish_and_no_break_case():
    tcb = strategy.TwoCandleBreak(level_high=100.0, level_low=90.0)
    inside = pd.Series({"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0})
    assert tcb.update(inside) is None
    assert tcb.state == strategy.TwoCandleBreak.WAITING
    c1 = pd.Series({"open": 91.0, "high": 92.0, "low": 85.0, "close": 89.0})   # breaks low
    c2 = pd.Series({"open": 89.0, "high": 89.5, "low": 87.0, "close": 88.0})   # confirms below
    assert tcb.update(c1) is None
    assert tcb.update(c2) == strategy.BEARISH


def test_two_candle_break_done_state_resets_only_on_close_inside_range():
    tcb = strategy.TwoCandleBreak(level_high=100.0, level_low=90.0)
    tcb.update(pd.Series({"open": 99.0, "high": 105.0, "low": 98.0, "close": 101.0}))
    tcb.update(pd.Series({"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0}))
    assert tcb.state == strategy.TwoCandleBreak.DONE
    still_outside = pd.Series({"open": 103.0, "high": 106.0, "low": 102.0, "close": 104.0})
    assert tcb.update(still_outside) is None
    assert tcb.state == strategy.TwoCandleBreak.DONE
    back_inside = pd.Series({"open": 100.0, "high": 100.5, "low": 95.0, "close": 95.0})
    assert tcb.update(back_inside) is None
    assert tcb.state == strategy.TwoCandleBreak.WAITING


# --------------------------------------------------------------------------------- exit_levels C/D/E

def test_exit_levels_cde_default_to_b():
    import config
    assert strategy.exit_levels("C") == (config.C_PROFIT_TARGET, config.C_STOP_LOSS)
    assert strategy.exit_levels("D") == (config.D_PROFIT_TARGET, config.D_STOP_LOSS)
    assert strategy.exit_levels("E") == (config.E_PROFIT_TARGET, config.E_STOP_LOSS)
    assert strategy.exit_levels("C") == strategy.exit_levels("B")
    assert strategy.exit_levels("D") == strategy.exit_levels("B")


# -------------------------------------------------------------------------------- news blackout

def test_in_news_blackout_before_during_after_window():
    events = [time(8, 30)]
    assert strategy.in_news_blackout(et(8, 26), events, before_min=5, after_min=5) is True
    assert strategy.in_news_blackout(et(8, 30), events, before_min=5, after_min=5) is True
    assert strategy.in_news_blackout(et(8, 34), events, before_min=5, after_min=5) is True
    assert strategy.in_news_blackout(et(8, 24), events, before_min=5, after_min=5) is False
    assert strategy.in_news_blackout(et(8, 36), events, before_min=5, after_min=5) is False


def test_in_news_blackout_no_events_today():
    assert strategy.in_news_blackout(et(8, 30), [], before_min=5, after_min=5) is False

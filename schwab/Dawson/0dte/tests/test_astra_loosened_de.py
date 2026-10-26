"""ASTRA's loosened D/E thresholds (2026-10-09): the shipped override, what it changes in the real strategy functions, and that the
rest of the matrix is untouched. A signal replay chose them (57 sessions: spec D on 2 sessions and trend-gated E on none; this set D ~1.6
and E ~2 a week); it says nothing about option P/L."""

import pytest

import config
import strategy
from strategy import BEARISH, BULLISH

ASTRA = getattr(config, "PERSONA_OVERRIDES", {}).get("ASTRA")
pytestmark = pytest.mark.skipif(not ASTRA, reason="the Schwab twin has no personas")


def test_the_shipped_astra_override():
    assert ASTRA == {"STRATEGIES": ("B", "C", "D", "E", "F"), "D_BAND_TOUCH_PCT_B": 0.70, "D_EMA_CHOP_THRESHOLD_PTS": 0.5,
                     "E_USE_TREND_GATE": False, "E_CONFIRM_CANDLES": 3}
    assert "D_BB_STD" not in ASTRA                                       # the 2.0 default stays; only the touch level moves


def test_the_override_is_what_the_astra_process_runs_with():
    if config.ACTIVE_TRADER != "ASTRA":
        pytest.skip("another persona is active")
    assert (config.D_BAND_TOUCH_PCT_B, config.D_EMA_CHOP_THRESHOLD_PTS, config.E_USE_TREND_GATE, config.E_CONFIRM_CANDLES) \
        == (0.70, 0.5, False, 3)
    assert config.D_BB_STD == 2.0 and config.D_EMA_SLOW_SPAN == 30 and config.D_E_CANDLE_INTERVAL == "5m"


def test_no_other_persona_changed_the_e_confirm_or_took_astras_touch_level():
    for name, overrides in config.PERSONA_OVERRIDES.items():
        if name != "ASTRA":
            assert "E_CONFIRM_CANDLES" not in overrides and overrides.get("D_BAND_TOUCH_PCT_B") != 0.70, name


def test_a_touch_of_75_percent_b_now_counts_and_the_spec_level_does_not():
    f = strategy.mean_reversion_signal
    assert f(105.0, 100.0, 0.75, "FALLING", ASTRA["D_BAND_TOUCH_PCT_B"]) == BEARISH       # D: above the MA on a falling day
    assert f(105.0, 100.0, 0.75, "FALLING", 0.95) is None                                   # spec: needs the 95% touch
    assert f(95.0, 100.0, 0.25, "RISING", ASTRA["D_BAND_TOUCH_PCT_B"]) == BULLISH
    assert f(95.0, 100.0, 0.25, "RISING", 0.95) is None
    assert f(105.0, 100.0, 0.55, "FALLING", ASTRA["D_BAND_TOUCH_PCT_B"]) is None            # still needs a real touch


def test_a_narrow_ema_gap_is_no_longer_chop_but_a_flat_one_still_is():
    assert strategy.ema_trend(101.0, 100.0, 2.0) == "CHOP"                                   # spec: 1.0 point apart sits out
    assert strategy.ema_trend(101.0, 100.0, ASTRA["D_EMA_CHOP_THRESHOLD_PTS"]) == "RISING"
    assert strategy.ema_trend(100.3, 100.0, ASTRA["D_EMA_CHOP_THRESHOLD_PTS"]) == "CHOP"


def test_e_without_the_trend_gate_decides_on_the_side_of_the_ma_and_the_touch_alone():
    f = strategy.mean_reversion_signal
    assert f(105.0, 100.0, 0.75, "RISING", ASTRA["D_BAND_TOUCH_PCT_B"], use_trend_gate=ASTRA["E_USE_TREND_GATE"]) == BEARISH
    assert f(105.0, 100.0, 0.75, "RISING", ASTRA["D_BAND_TOUCH_PCT_B"]) is None             # D (gated) still refuses a rising day
    assert f(105.0, 100.0, 0.75, "CHOP", ASTRA["D_BAND_TOUCH_PCT_B"], use_trend_gate=False) == BEARISH


def test_three_same_colour_candles_are_needed_by_e_and_two_no_longer_do():
    import pandas as pd
    from conftest import et, make_candles
    up = make_candles(et(9, 30), [(7600.0, 7601.0, 7599.0, 7601.0)] * 3, minutes=5)
    assert strategy.two_candle_confirm(up, BULLISH, ASTRA["E_CONFIRM_CANDLES"]) is True
    assert strategy.two_candle_confirm(up.tail(2), BULLISH, ASTRA["E_CONFIRM_CANDLES"]) is False
    assert strategy.two_candle_confirm(up.tail(2), BULLISH, 2) is True
    assert isinstance(up, pd.DataFrame)

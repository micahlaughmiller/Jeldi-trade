from datetime import date

import pytest

import config
import strategy
from conftest import et, make_candles
from strategy import BEARISH, BULLISH, Phase


@pytest.mark.parametrize("h, m, expected", [
    (9, 29, Phase.PRE_OPEN),
    (9, 30, Phase.OVERNIGHT_ONLY),
    (9, 44, Phase.OVERNIGHT_ONLY),
    (9, 45, Phase.OVERNIGHT_OR_ORB),
    (9, 59, Phase.OVERNIGHT_OR_ORB),
    (10, 0, Phase.ORB_ONLY),
    (10, 37, Phase.ORB_ONLY),
    (11, 59, Phase.ORB_ONLY),
    (12, 0, Phase.NO_NEW_ENTRIES),
    (12, 29, Phase.NO_NEW_ENTRIES),
    (12, 30, Phase.CLOSED),
    (15, 0, Phase.CLOSED),
])
def test_phase_boundaries(h, m, expected):
    assert strategy.phase(et(h, m)) == expected


def test_phase_uses_wall_clock_not_ticks():
    assert strategy.phase(et(9, 44, 59)) == Phase.OVERNIGHT_ONLY
    assert strategy.phase(et(9, 45, 0)) == Phase.OVERNIGHT_OR_ORB
    assert strategy.minutes_since_open(et(10, 37, 30)) == pytest.approx(67.5)


LEVEL_T = et(9, 45)


def test_bullish_breakout_holds_and_extends():
    c = make_candles(et(9, 46), [(99, 100, 98, 99), (99, 102, 99, 101), (101, 104, 100, 103)])
    bo = strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T)
    assert bo is not None and bo.direction == BULLISH
    assert bo.candle_times == (c.index[-2].to_pydatetime(), c.index[-1].to_pydatetime())


def test_bullish_holds_but_does_not_extend():
    c = make_candles(et(9, 46), [(99, 104, 99, 101), (101, 103, 100, 102)])
    assert strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T) is None


def test_bullish_extends_but_does_not_hold():
    c = make_candles(et(9, 46), [(99, 102, 99, 101), (101, 105, 99, 99.5)])
    assert strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T) is None


def test_bearish_breakout_mirror():
    c = make_candles(et(9, 46), [(91, 92, 88, 89), (89, 90, 86, 87)])
    bo = strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T)
    assert bo is not None and bo.direction == BEARISH


def test_bearish_holds_but_does_not_extend():
    c = make_candles(et(9, 46), [(91, 92, 86, 89), (89, 90, 87, 88)])
    assert strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T) is None


def test_candles_before_level_are_ignored():
    c = make_candles(et(9, 42), [(99, 102, 99, 101), (101, 104, 100, 103)])
    assert strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T) is None


def test_inside_range_is_no_signal():
    c = make_candles(et(9, 46), [(95, 96, 94, 95), (95, 97, 94, 96)])
    assert strategy.detect_breakout(c, 100.0, 90.0, LEVEL_T) is None
    assert strategy.detect_breakout(c.iloc[:1], 100.0, 90.0, LEVEL_T) is None


def test_direction_to_spread():
    assert strategy.direction_to_spread(BULLISH) == "P"
    assert strategy.direction_to_spread(BEARISH) == "C"


@pytest.mark.parametrize("spot, right, width, short, long", [
    (7500.0, "P", 5, 7510.0, 7505.0),
    (7500.0, "P", 10, 7515.0, 7505.0),
    (7500.0, "C", 5, 7490.0, 7495.0),
    (7500.0, "C", 10, 7485.0, 7495.0),
    (7502.3, "P", 5, 7510.0, 7505.0),
    (7502.3, "C", 5, 7495.0, 7500.0),
    (7504.9, "P", 5, 7510.0, 7505.0),
    (7505.0, "P", 5, 7515.0, 7510.0),
])
def test_select_strikes(spot, right, width, short, long):
    assert strategy.select_strikes(spot, right, width) == (short, long)


def test_width_and_credit_range():
    assert strategy.width_for_tier(1) == 5
    assert strategy.width_for_tier(3) == 10
    assert strategy.credit_range(5) == (2.75, 3.50)
    assert strategy.credit_range(10) == (5.50, 7.00)


def chain(quotes: dict[float, tuple[float, float]]) -> list[dict]:
    return [{"strike": k, "bid": b, "ask": a, "mid": round((b + a) / 2, 2)} for k, (b, a) in quotes.items()]


def test_entry_credit_in_range():
    q, reason = strategy.entry_credit(chain({7510: (7.9, 8.1), 7505: (4.9, 5.1)}), 7510, 7505, 5)
    assert reason is None
    assert q.mid == pytest.approx(3.00)
    assert q.bid_side == pytest.approx(2.80)
    assert q.ask_side == pytest.approx(3.20)


def test_entry_credit_rejections():
    _, reason = strategy.entry_credit(chain({7510: (7.0, 7.2), 7505: (4.9, 5.1)}), 7510, 7505, 5)
    assert reason.startswith("CREDIT_BELOW_MIN")
    _, reason = strategy.entry_credit(chain({7510: (8.9, 9.1), 7505: (4.9, 5.1)}), 7510, 7505, 5)
    assert reason.startswith("CREDIT_ABOVE_MAX")
    q, reason = strategy.entry_credit(chain({7510: (8.0, 8.2)}), 7510, 7505, 5)
    assert q is None and reason.startswith("STRIKES_NOT_IN_CHAIN")


def test_momentum_signed_by_direction():
    c = make_candles(et(10, 0), [(100, 103, 99, 102), (102, 105, 101, 104), (104, 106, 103, 105)])
    assert strategy.momentum(c, BULLISH, 3) == pytest.approx((2 + 2 + 1) / 3)
    assert strategy.momentum(c, BEARISH, 3) == pytest.approx(-(2 + 2 + 1) / 3)
    assert strategy.momentum(c, BULLISH, 2) == pytest.approx(1.5)


def test_momentum_slowed():
    assert strategy.momentum_slowed(1.5, 2.0, 0.20) is True
    assert strategy.momentum_slowed(1.6, 2.0, 0.20) is False
    assert strategy.momentum_slowed(-0.5, 2.0, 0.20) is True


def test_momentum_continuing():
    up = make_candles(et(10, 0), [(100, 103, 99, 102), (102, 105, 101, 103.7)])
    assert strategy.momentum_continuing(up, BULLISH, 0.80) is True
    weak = make_candles(et(10, 0), [(100, 103, 99, 102), (102, 105, 101, 103.5)])
    assert strategy.momentum_continuing(weak, BULLISH, 0.80) is False
    reversed_ = make_candles(et(10, 0), [(100, 103, 99, 102), (102, 103, 100, 101)])
    assert strategy.momentum_continuing(reversed_, BULLISH, 0.80) is False
    assert strategy.momentum_continuing(up, BEARISH, 0.80) is False


def test_news_day_and_modes(monkeypatch):
    assert strategy.is_news_day(date(2026, 9, 16)) is True
    assert strategy.is_news_day(date(2026, 9, 17)) is False
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "orb_only")
    assert strategy.setup_allowed("OVERNIGHT", date(2026, 9, 16)) is False
    assert strategy.setup_allowed("ORB", date(2026, 9, 16)) is True
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "skip")
    assert strategy.setup_allowed("ORB", date(2026, 9, 16)) is False
    assert strategy.setup_allowed("ORB", date(2026, 9, 17)) is True


def test_levels_shift_to_spx_terms():
    lv = strategy.Levels(6500.0, 6450.0, et(9, 30))
    shifted = lv.shifted(-25.5)
    assert (shifted.high, shifted.low) == (6474.5, 6424.5)
    assert shifted.established_at == lv.established_at

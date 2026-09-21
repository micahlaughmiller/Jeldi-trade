from datetime import date

import pytest

import config
import strategy
from conftest import et, make_candles
from strategy import BEARISH, BULLISH, OrbSetup, Phase


@pytest.mark.parametrize("h, m, expected", [
    (9, 29, Phase.PRE_OPEN),
    (9, 30, Phase.OVERNIGHT_ONLY),
    (9, 44, Phase.OVERNIGHT_ONLY),
    (9, 59, Phase.OVERNIGHT_ONLY),
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
    assert strategy.phase(et(9, 59, 59)) == Phase.OVERNIGHT_ONLY
    assert strategy.phase(et(10, 0, 0)) == Phase.ORB_ONLY
    assert strategy.minutes_since_open(et(10, 37, 30)) == pytest.approx(67.5)


def test_phase_sets():
    assert strategy.OVERNIGHT_PHASES == {Phase.OVERNIGHT_ONLY}
    assert strategy.ORB_PHASES == {Phase.ORB_ONLY}
    assert strategy.ENTRY_PHASES == {Phase.OVERNIGHT_ONLY, Phase.ORB_ONLY}


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


# ------------------------------------------------------------------ ORB setup

ORH, ORL = 7620.0, 7590.0


def feed(setup: OrbSetup, bars: list[tuple[float, float, float, float]]) -> list[str | None]:
    candles = make_candles(et(10, 0), bars, minutes=5)
    return [setup.update(candles.iloc[i]) for i in range(len(candles))]


def test_orb_momentum_entry_without_pullback():
    s = OrbSetup(ORH, ORL)
    # break closes 7624; next candle never touches 7620 and closes above 7624 -> momentum entry
    assert feed(s, [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]) == [None, BULLISH]
    assert s.state == OrbSetup.DONE


def test_orb_break_without_new_high_keeps_waiting():
    s = OrbSetup(ORH, ORL)
    assert feed(s, [(7615, 7625, 7614, 7624), (7624, 7626, 7621, 7623)]) == [None, None]
    assert s.state == OrbSetup.BROKEN


def test_orb_wick_above_level_is_not_a_break():
    s = OrbSetup(ORH, ORL)
    assert feed(s, [(7615, 7625, 7614, 7619), (7619, 7628, 7618, 7620)]) == [None, None]
    assert s.state == OrbSetup.WAITING


def test_orb_pullback_entry_owner_example():
    s = OrbSetup(ORH, ORL)
    bars = [
        (7615, 7625, 7614, 7624),   # break: closes above 7620
        (7624, 7625, 7619, 7621),   # wicks to 7619 (touches the level) and holds above it
        (7621, 7623, 7620.5, 7622), # green candle closing above 7620 -> enter at 7622
    ]
    assert feed(s, bars) == [None, None, BULLISH]


def test_orb_pullback_then_red_candle_does_not_enter():
    s = OrbSetup(ORH, ORL)
    bars = [(7615, 7625, 7614, 7624), (7624, 7625, 7619, 7621), (7623, 7624, 7620.5, 7621)]
    assert feed(s, bars) == [None, None, None]
    assert s.state == OrbSetup.PULLED_BACK
    # a later green candle above the level still enters; another (red) touch in between is fine
    assert feed(s, [(7622, 7622.5, 7619.5, 7621), (7621, 7626, 7620.5, 7625)]) == [None, BULLISH]


def test_orb_pullback_closing_below_level_resets():
    s = OrbSetup(ORH, ORL)
    bars = [(7615, 7625, 7614, 7624), (7624, 7625, 7618, 7619), (7619, 7623, 7618, 7622)]
    assert feed(s, bars) == [None, None, None]
    assert s.state == OrbSetup.BROKEN     # 7622 close is a fresh break, not an entry


def test_orb_bearish_mirror():
    s = OrbSetup(ORH, ORL)
    bars = [
        (7595, 7596, 7585, 7586),   # break: closes below 7590
        (7586, 7591, 7585, 7589),   # wick to 7591 touches the level, holds below
        (7589, 7589.5, 7585, 7587), # red candle closing below 7590 -> enter
    ]
    assert feed(s, bars) == [None, None, BEARISH]
    s2 = OrbSetup(ORH, ORL)
    assert feed(s2, [(7595, 7596, 7585, 7586), (7586, 7588, 7580, 7582)]) == [None, BEARISH]


def test_orb_bullish_break_then_bearish_break_is_evaluated_fresh():
    s = OrbSetup(ORH, ORL)
    bars = [(7615, 7625, 7614, 7624), (7624, 7626, 7585, 7586), (7586, 7588, 7580, 7582)]
    assert feed(s, bars) == [None, None, BEARISH]


def test_orb_times_out_after_configured_candles(monkeypatch):
    monkeypatch.setattr(config, "ORB_SETUP_TIMEOUT_CANDLES", 3)
    s = OrbSetup(ORH, ORL)
    drifting = (7622, 7623.5, 7621, 7623)   # above the level, no touch, no new high
    assert feed(s, [(7615, 7625, 7614, 7624)] + [drifting] * 3) == [None] * 4
    assert s.state == OrbSetup.BROKEN
    # 4th candle after the break: setup expires and this candle re-arms as a fresh break
    assert feed(s, [(7623, 7626, 7622, 7625)]) == [None]
    assert s.state == OrbSetup.BROKEN and s.break_close == 7625
    assert feed(s, [(7625, 7630, 7624, 7629)]) == [BULLISH]


def test_orb_rearms_only_after_close_back_inside_range():
    s = OrbSetup(ORH, ORL)
    assert feed(s, [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]) == [None, BULLISH]
    # continuation candles above the range must not fire again
    assert feed(s, [(7628, 7635, 7627, 7634), (7634, 7640, 7633, 7639)]) == [None, None]
    assert s.state == OrbSetup.DONE
    # a close back inside the range re-arms; the next break can fire again
    assert feed(s, [(7639, 7640, 7610, 7612)]) == [None]
    assert s.state == OrbSetup.WAITING
    assert feed(s, [(7612, 7626, 7611, 7625), (7625, 7632, 7624, 7630)]) == [None, BULLISH]


# -------------------------------------------------------------------- strikes

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
    assert strategy.credit_range_b(5) == (0.65, 1.10)
    assert strategy.credit_range_b(10) == (1.30, 2.20)


def test_exit_levels_per_strategy():
    assert strategy.exit_levels("A") == (0.30, 0.30)
    assert strategy.exit_levels("B") == (0.30, 0.50)


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


def test_expected_move_is_atm_straddle():
    calls = chain({7595: (24.0, 26.0), 7600: (21.0, 23.0), 7605: (18.0, 20.0)})
    puts = chain({7595: (17.0, 19.0), 7600: (19.0, 21.0), 7605: (22.0, 24.0)})
    assert strategy.expected_move(calls, puts, 7601.7) == pytest.approx(42.0)
    assert strategy.expected_move(calls, puts, 7603.0) == pytest.approx(42.0)   # 7605: 19 + 23
    assert strategy.expected_move(calls, chain({7595: (17.0, 19.0)}), 7601.7) is None
    assert strategy.expected_move([], puts, 7601.7) is None


def put_chain_by_mid(mids: dict[float, float]) -> list[dict]:
    return chain({k: (m - 0.1, m + 0.1) for k, m in mids.items()})


def test_select_strikes_b_bullish_one_em_away():
    rows = put_chain_by_mid({7570: 4.0, 7560: 3.0, 7550: 1.5, 7540: 0.8})
    short, long, quote = strategy.select_strikes_b(7600.0, "P", 10, 42.0, rows)
    assert (short, long) == (7560.0, 7550.0)
    assert quote.mid == pytest.approx(1.50)


def test_select_strikes_b_steps_toward_spot_when_credit_too_low():
    rows = put_chain_by_mid({7575: 5.0, 7570: 3.6, 7565: 2.4, 7560: 1.6, 7555: 1.0, 7550: 0.6})
    # 7560/7550 pays 1.00 (< 1.30) -> 7565/7555 pays 1.40 -> in range
    short, long, quote = strategy.select_strikes_b(7600.0, "P", 10, 42.0, rows)
    assert (short, long) == (7565.0, 7555.0) and quote.mid == pytest.approx(1.40)


def test_select_strikes_b_steps_away_when_credit_too_high():
    rows = put_chain_by_mid({7560: 5.0, 7555: 3.2, 7550: 2.5, 7545: 1.6, 7540: 0.5})
    # 7560/7550 pays 2.50 (> 2.20) -> 7555/7545 pays 1.60 -> in range
    short, long, quote = strategy.select_strikes_b(7600.0, "P", 10, 42.0, rows)
    assert (short, long) == (7555.0, 7545.0) and quote.mid == pytest.approx(1.60)


def test_select_strikes_b_gives_up_within_min_distance_of_spot():
    # every 5-wide spread pays under 0.65 all the way in; 7590 (exactly 10 away) is still tried, 7595 is not
    rows = put_chain_by_mid({7595: 2.6, 7590: 2.0, 7585: 1.5, 7580: 1.1, 7575: 0.8, 7570: 0.6, 7565: 0.5,
                             7560: 0.4, 7555: 0.3})
    short, long, reason = strategy.select_strikes_b(7600.0, "P", 5, 42.0, rows)
    assert short is None and long is None
    assert reason.startswith("B_NO_STRIKE_IN_RANGE") and "short=7595 within 10" in reason


def test_select_strikes_b_gives_up_beyond_two_em():
    # every spread pays 2.00 (> 1.10) so the walk steps away: 7590 -> 7585 -> 7580 (20 = 2 EM, ok) -> 7575 gives up
    rows = put_chain_by_mid({7595: 12.0, 7590: 10.0, 7585: 8.0, 7580: 6.0, 7575: 4.0, 7570: 2.0})
    short, long, reason = strategy.select_strikes_b(7600.0, "P", 5, 10.0, rows)
    assert short is None and reason.startswith("B_NO_STRIKE_IN_RANGE") and "short=7575 beyond 2x EM" in reason


def test_select_strikes_b_bearish_mirror():
    rows = chain({7640: (2.4, 2.6), 7650: (0.9, 1.1)})
    short, long, quote = strategy.select_strikes_b(7600.0, "C", 10, 42.0, rows)
    assert (short, long) == (7640.0, 7650.0) and quote.mid == pytest.approx(1.50)


def test_select_strikes_b_missing_strike_in_chain():
    short, long, reason = strategy.select_strikes_b(7600.0, "P", 10, 42.0, chain({7560: (2.9, 3.1)}))
    assert short is None and reason.startswith("STRIKES_NOT_IN_CHAIN")


# ------------------------------------------------------------------- momentum

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

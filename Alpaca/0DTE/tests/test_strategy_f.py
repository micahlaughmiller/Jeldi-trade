"""Strategy F pure helpers: strike increment, width snapping, the quoted-strike walk, width-scaled exits."""

from datetime import date

import pytest

import config
import strategy
from f_fixtures import tsla_call_chain, tsla_put_chain
from strategy import SpreadQuote, f_exit_params, quote_blown_out, select_strikes_f, spx_strategies, \
    strike_increment, width_for_stock


def rows(quotes: dict[float, tuple[float, float]], right: str = "P") -> list[dict]:
    return [{"strike": k, "right": right, "bid": b, "ask": a, "mid": round((b + a) / 2, 2)}
            for k, (b, a) in quotes.items()]


def test_strike_increment_detects_grids_and_none():
    assert strike_increment(rows({430.0: (1, 2), 432.5: (1, 2), 435.0: (1, 2)}), 432.0) == 2.5
    assert strike_increment(rows({99.0: (1, 2), 100.0: (1, 2), 101.0: (1, 2), 105.0: (1, 2)}), 100.0) == 1.0
    assert strike_increment(rows({700.0: (1, 2), 705.0: (1, 2)}), 702.0) == 5.0
    fine_atm = rows({59.5: (1, 2), 60.0: (1, 2), 60.5: (1, 2), 65.0: (1, 2), 70.0: (1, 2)})
    assert strike_increment(fine_atm, 60.0) == 0.5            # the ATM band wins over the coarse far grid
    assert strike_increment(rows({100.0: (1, 2), 110.0: (1, 2)}), 100.0) == 10.0   # <2 in band: every strike
    assert strike_increment(rows({100.0: (1, 2)}), 100.0) is None
    assert strike_increment([], 100.0) is None


@pytest.mark.parametrize("spot,inc,width", [
    (40, 1.0, 1.0), (180, 1.0, 2.0), (430, 2.5, 5.0), (130, 2.5, 2.5), (60, 0.5, 1.0), (700, 5.0, 5.0), (100, 0.1, 1.0),
])
def test_width_for_stock_snaps_one_percent_min_one_dollar(spot, inc, width):
    assert width_for_stock(spot, inc) == width


def test_select_strikes_f_walks_into_band_for_puts_and_calls():
    puts = rows(tsla_put_chain())
    short, long, quote = select_strikes_f(436.0, "P", 5.0, 17.0, puts)
    assert (short, long) == (417.5, 412.5) and quote.mid == pytest.approx(0.80)
    # far start (EM 30) pays too little: walks toward spot until the credit reaches 0.65
    assert select_strikes_f(436.0, "P", 5.0, 30.0, puts)[:2] == (417.5, 412.5)
    # near start (EM 10) pays too much: walks away from spot until under 1.10
    short, long, quote = select_strikes_f(436.0, "P", 5.0, 10.0, puts)
    assert (short, long) == (422.5, 417.5) and quote.mid == pytest.approx(0.95)
    calls = rows(tsla_call_chain(), "C")
    short, long, quote = select_strikes_f(436.0, "C", 5.0, 17.0, calls)
    assert (short, long) == (455.0, 460.0) and quote.mid == pytest.approx(0.80)
    # width 5 reproduces B's credit band exactly
    assert (round(config.F_CREDIT_PCT_RANGE[0] * 5, 2), round(config.F_CREDIT_PCT_RANGE[1] * 5, 2)) == config.B_CREDIT_RANGE_BY_WIDTH[5]


def test_select_strikes_f_steps_over_unquoted_strikes_on_a_sparse_chain():
    # AMZN-like: $1 grid near the money, but 214 and 212 are not listed at all.
    sparse = {219.0: (2.9, 3.1), 218.0: (2.4, 2.6), 217.0: (2.0, 2.2), 216.0: (1.65, 1.85), 215.0: (1.35, 1.55),
              213.0: (1.0, 1.2), 211.0: (0.6, 0.8), 210.0: (0.5, 0.7), 209.0: (0.4, 0.5)}
    assert width_for_stock(220.0, strike_increment(rows(sparse), 220.0)) == 2.0
    # the naive ceil((220-6)/1) = 214 start does not exist; the walk starts at the first QUOTED strike
    # beyond the EM (213) and pairs it with the listed 211
    short, long, quote = select_strikes_f(220.0, "P", 2.0, 6.0, rows(sparse))
    assert (short, long) == (213.0, 211.0) and quote.mid == pytest.approx(0.40)
    # a zero-bid strike is present for the long leg but never becomes the short leg
    sparse[214.0] = (0.0, 0.05)
    assert select_strikes_f(220.0, "P", 2.0, 6.0, rows(sparse))[:2] == (213.0, 211.0)
    # a missing LONG leg is stepped over too: without 211 the walk moves on to 215/213
    del sparse[211.0]
    short, long, quote = select_strikes_f(220.0, "P", 2.0, 6.0, rows(sparse))
    assert (short, long) == (215.0, 213.0) and quote.mid == pytest.approx(0.35)


def test_select_strikes_f_gives_up_with_reasons():
    puts = rows(tsla_put_chain())
    s, l, reason = select_strikes_f(436.0, "P", 5.0, 1.0, puts)
    assert s is None and reason.startswith("F_TOO_CLOSE_TO_SPOT short=435")
    far = rows({400.0: (0.2, 0.3), 395.0: (0.1, 0.2)})
    assert select_strikes_f(436.0, "P", 5.0, 17.0, far)[2].startswith("F_BEYOND_EM_CAP short=400 beyond 1.8x EM")
    near_only = rows({435.0: (7.9, 8.1), 432.5: (6.4, 6.6)})
    assert "no quoted P strike at or beyond EM" in select_strikes_f(436.0, "P", 5.0, 17.0, near_only)[2]
    assert "F_NO_STRIKE_IN_RANGE no credit in 0.65-1.10 after 1 steps" in \
        select_strikes_f(436.0, "P", 5.0, 30.0, puts, max_steps=1)[2]
    with pytest.raises(ValueError):
        select_strikes_f(436.0, "X", 5.0, 17.0, puts)


def test_f_exit_params_scale_with_width_and_floor_at_five_cents():
    assert f_exit_params(5.0) == {"profit_target": 0.30, "stop_loss": 0.50,
                                  "overrides": {"PROFIT_LOCK_ARM": 0.15, "PROFIT_LOCK_GIVEBACK": 0.10, "TRAIL_AMOUNT": 0.30}}
    assert (f_exit_params(5.0)["profit_target"], f_exit_params(5.0)["stop_loss"]) == strategy.exit_levels("B")
    assert f_exit_params(1.0) == {"profit_target": 0.06, "stop_loss": 0.10,
                                  "overrides": {"PROFIT_LOCK_ARM": 0.05, "PROFIT_LOCK_GIVEBACK": 0.05, "TRAIL_AMOUNT": 0.06}}
    two_fifty = f_exit_params(2.5)
    assert (two_fifty["profit_target"], two_fifty["stop_loss"]) == (0.15, 0.25)
    assert all(v >= config.F_EXIT_FLOOR for v in two_fifty["overrides"].values())


def test_quote_blown_out_threshold_and_zero_bid():
    assert config.F_MAX_QUOTE_SPREAD_FRAC == 0.05 and config.F_MAX_QUOTE_SPREAD_STOP_RATIO == 0.5   # was 0.12, no ratio
    assert quote_blown_out(SpreadQuote(mid=0.9, bid_side=0.75, ask_side=1.0), 5.0) is False    # 0.25 == 5% of 5
    assert quote_blown_out(SpreadQuote(mid=0.9, bid_side=0.75, ask_side=1.01), 5.0) is True
    assert quote_blown_out(SpreadQuote(mid=0.3, bid_side=0.0, ask_side=0.6), 5.0) is True
    assert quote_blown_out(SpreadQuote(mid=0.15, bid_side=0.13, ask_side=0.17), 1.0) is False   # 0.04 <= 0.05
    assert quote_blown_out(SpreadQuote(mid=0.15, bid_side=0.1, ask_side=0.2), 1.0) is True      # 0.10 > 0.05 (old gate passed it)


def test_f_stop_distance_and_the_quote_to_stop_ratio_gate():
    from strategy import f_stop_distance
    assert (f_stop_distance(1.0), f_stop_distance(2.5), f_stop_distance(5.0)) == (0.08, 0.20, 0.40)
    # $1 wide, stop 0.08: the quote spread may be at most 0.04 -- UBER's 0.05-0.10 on 10/6 is refused
    assert quote_blown_out(SpreadQuote(mid=0.12, bid_side=0.10, ask_side=0.14), 1.0, stop_distance=0.08) is False
    assert quote_blown_out(SpreadQuote(mid=0.12, bid_side=0.09, ask_side=0.15), 1.0, stop_distance=0.08) is True
    # $2.5 wide, stop 0.20: at most 0.10 -- MRVL's 0.29 on 10/7 is refused, a 0.10 quote still passes
    assert quote_blown_out(SpreadQuote(mid=0.53, bid_side=0.40, ask_side=0.69), 2.5, stop_distance=0.20) is True
    assert quote_blown_out(SpreadQuote(mid=0.53, bid_side=0.50, ask_side=0.60), 2.5, stop_distance=0.20) is False
    # the ratio is optional: without a stop distance only the width gate applies
    sliver = SpreadQuote(mid=0.12, bid_side=0.10, ask_side=0.15)                   # 0.05: inside 5% of width, but > 0.5 x 0.08
    assert quote_blown_out(sliver, 1.0) is False and quote_blown_out(sliver, 1.0, stop_distance=0.08) is True


def test_spx_strategies_excludes_stock_strategies_and_registry_has_f(monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "F"))
    assert spx_strategies() == ("A", "B")
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    assert spx_strategies() == ("D", "E")
    assert strategy.exit_levels("F") == (config.F_PROFIT_TARGET, config.F_STOP_LOSS) == strategy.exit_levels("B")
    assert config.SETUPS_BY_STRATEGY["F"] == ("STOCK_ORB",)
    assert config.RUNNER_CLOSE_FRACTION_BY_STRATEGY["F"] == config.RUNNER_CLOSE_FRACTION_BY_STRATEGY["B"]


def test_orb_only_news_mode_admits_stock_orb(monkeypatch):
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "orb_only")
    news_day = date(2026, 9, 16)
    assert strategy.setup_allowed("STOCK_ORB", news_day) is True
    assert strategy.setup_allowed("ORB", news_day) is True
    assert strategy.setup_allowed("ON_BREAK", news_day) is False
    monkeypatch.setattr(config, "NEWS_DAY_MODE", "skip")
    assert strategy.setup_allowed("STOCK_ORB", news_day) is False


def test_every_persona_runs_f():
    overrides = getattr(config, "PERSONA_OVERRIDES", {})          # Alpaca personas; the Schwab twin has none
    # 2026-10-07: every strategy runs on every persona (F last), each persona keeping its own parameter overrides
    if overrides:
        assert {n: o["STRATEGIES"] for n, o in overrides.items()} == {
            n: ("B", "C", "D", "E", "F") for n in ("ASTRA", "ARIA", "ELENA", "DAVID", "JORDAN")}
    assert config.STOCK_STRATEGIES == ("F",) and config.DAILY_LOSS_LIMIT_EXEMPT == ("F",)
    assert config.DAILY_LOSS_LIMIT_HALTS_EXEMPT is False and config.F_MAX_ENTRIES_PER_TICKER is None
    assert config.LIMITS_BY_STRATEGY["F"] == {"MAX_TRADES_PER_DAY": 3, "MAX_CONSECUTIVE_LOSSES": 7,
                                              "MAX_CONCURRENT_POSITIONS": 3}


# ------------------------------------------------- 2026-10-16: the EM cap is 1.8x, or 2.5x on a strike grid too coarse for the EM

from strategy import f_chain_span, f_em_cap


def test_f_em_cap_is_1_8x_on_a_normal_grid_and_2_5x_when_the_em_spans_under_two_strike_steps():
    assert f_em_cap(17.0, 2.5) == pytest.approx(1.8 * 17.0)                         # EM 17 on a $2.50 grid: 6.8 steps, normal
    assert f_em_cap(2.85, 2.5) == pytest.approx(2.5 * 2.85)                         # the 10/16 case: EM 2.85 = 1.14 steps, coarse
    assert f_em_cap(5.0, 2.5) == pytest.approx(1.8 * 5.0)                           # exactly 2 steps is NOT coarse
    assert f_em_cap(4.99, 2.5) == pytest.approx(2.5 * 4.99)
    assert f_em_cap(2.85, None) == pytest.approx(1.8 * 2.85)                        # unknown grid: the normal cap
    assert f_em_cap(2.85, 0.0) == pytest.approx(1.8 * 2.85)


def test_the_171_dollar_name_from_10_16_now_passes_on_its_coarse_grid():
    spot, em, width = 171.28, 2.85, 2.5                                               # width: 1% of spot snapped up to the $2.50 step
    # the real chain that day: the first quoted CALL strike at or beyond 1 EM was 177.5, already 2.18 EM out (a $2.50 grid on a $2.85 EM)
    chain = rows({172.5: (1.6, 1.8), 177.5: (0.75, 0.85), 180.0: (0.35, 0.45), 182.5: (0.15, 0.25)})
    short, long, quote = select_strikes_f(spot, "C", width, em, chain, credit_pct=(0.13, 0.22), increment=2.5)
    assert (short, long) == (177.5, 180.0), quote
    assert 1.8 * em < abs(spot - short) <= 2.5 * em                                   # only the coarse cap lets this spread through
    reason = select_strikes_f(spot, "C", width, em, chain, credit_pct=(0.13, 0.22), increment=0.5)[2]
    assert isinstance(reason, str) and reason.startswith("F_BEYOND_EM_CAP short=177.5") and "1.8x EM" in reason   # a fine grid keeps 1.8x


def test_the_normal_cap_tightened_from_2x_to_1_8x():
    far_ok = rows({406.0: (1.4, 1.6), 401.0: (0.6, 0.8)})                             # 30.0 away: 1.76 EM, inside 1.8x; credit 0.80 in band
    short, long, quote = select_strikes_f(436.0, "P", 5.0, 17.0, far_ok, increment=5.0)
    assert (short, long) == (406.0, 401.0)
    too_far = rows({404.0: (1.4, 1.6), 399.0: (0.6, 0.8)})                            # 32.0 away: 1.88 EM -- allowed at 2x, refused at 1.8x
    reason = select_strikes_f(436.0, "P", 5.0, 17.0, too_far, increment=5.0)[2]
    assert reason.startswith("F_BEYOND_EM_CAP short=404 beyond 1.8x EM 17.00") and "strike step 5" in reason


def test_the_coarse_cap_comes_from_the_increment_stock_orb_passes_or_is_derived_from_the_chain():
    spot, em = 171.28, 2.85
    chain = rows({165.0: (0.75, 0.85), 162.5: (0.35, 0.45)})                          # only strikes 2.2 EM out: the coarse cap (7.13) lets 165 in
    assert select_strikes_f(spot, "P", 2.5, em, chain, credit_pct=(0.13, 0.22), increment=2.5)[0] == 165.0
    assert select_strikes_f(spot, "P", 2.5, em, chain, credit_pct=(0.13, 0.22))[0] == 165.0       # derived: the quoted strikes are 2.5 apart
    reason = select_strikes_f(spot, "P", 2.5, em, chain, credit_pct=(0.13, 0.22), increment=1.0)[2]
    assert reason.startswith("F_BEYOND_EM_CAP short=165 beyond 1.8x EM")                            # a $1 grid is not coarse for a 2.85 EM


def test_the_chain_window_covers_the_widest_cap_plus_the_long_leg():
    assert f_chain_span(2.85, 2.5) == pytest.approx(2.5 * 2.85 + 5.0)
    assert f_chain_span(17.0, 5.0) == pytest.approx(2.5 * 17.0 + 10.0)                # always the wider cap: the grid is not known before the chain is fetched

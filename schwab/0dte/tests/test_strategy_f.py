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
    assert select_strikes_f(436.0, "P", 5.0, 17.0, far)[2].startswith("F_BEYOND_2EM short=400")
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
    assert quote_blown_out(SpreadQuote(mid=0.9, bid_side=0.6, ask_side=1.2), 5.0) is False    # 0.60 == 12% of 5
    assert quote_blown_out(SpreadQuote(mid=0.9, bid_side=0.6, ask_side=1.21), 5.0) is True
    assert quote_blown_out(SpreadQuote(mid=0.3, bid_side=0.0, ask_side=0.6), 5.0) is True
    assert quote_blown_out(SpreadQuote(mid=0.15, bid_side=0.1, ask_side=0.2), 1.0) is False    # 0.10 < 0.12


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


def test_only_astra_opts_into_f():
    overrides = getattr(config, "PERSONA_OVERRIDES", {})          # Alpaca personas; the Schwab twin has none
    if overrides:
        assert overrides["ASTRA"]["STRATEGIES"] == ("B", "F")   # 2026-10-01: A removed
    assert all("F" not in o.get("STRATEGIES", ()) for name, o in overrides.items() if name != "ASTRA")
    assert config.STOCK_STRATEGIES == ("F",) and config.DAILY_LOSS_LIMIT_EXEMPT == ("F",)
    assert config.DAILY_LOSS_LIMIT_HALTS_EXEMPT is False and config.F_MAX_ENTRIES_PER_TICKER is None
    assert config.LIMITS_BY_STRATEGY["F"] == {"MAX_TRADES_PER_DAY": 10, "MAX_CONSECUTIVE_LOSSES": 7,
                                              "MAX_CONCURRENT_POSITIONS": 3}

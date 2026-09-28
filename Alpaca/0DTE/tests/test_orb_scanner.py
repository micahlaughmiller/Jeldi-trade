"""ORB pre-breakout scanner: synthetic bars only, no network (fetch functions are monkeypatched)."""

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

import orb_scanner as osc
from orb_scanner import (BEARISH, BULLISH, Candidate, Metrics, OpeningRange, ScanConfig,
                         format_ranking, option_quality_score, score_atr_expected_move,
                         score_candidate, score_distance, score_momentum, score_or_compression,
                         score_relative_volume, score_vwap, to_log_row)

ET = osc.ET
CFG = ScanConfig()
SESSION = date(2026, 9, 25)          # a Friday
NOW_0945 = datetime(2026, 9, 25, 9, 45, tzinfo=ET)


# ------------------------------------------------------------------------------ synthetic data

def bars(day: date, closes: list[float], volume: float = 100.0, wick: float = 0.05,
         start: time = time(9, 30)) -> pd.DataFrame:
    """1-min bars: each open = previous close, high/low = body +/- wick, constant volume."""
    idx = pd.DatetimeIndex([datetime.combine(day, start, tzinfo=ET) + timedelta(minutes=i)
                            for i in range(len(closes))])
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "open": opens,
        "high": [max(o, c) + wick for o, c in zip(opens, closes)],
        "low": [min(o, c) - wick for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [float(volume)] * len(closes),
    }, index=idx)


def weekdays_before(day: date, n: int) -> list[date]:
    out, d = [], day - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def intraday(today_closes: list[float], today_volume: float = 150.0, prior_volume: float = 100.0,
             prior_sessions: int = 5, prior_bars: int = 60, base: float = 100.0) -> pd.DataFrame:
    frames = [bars(d, [base] * prior_bars, prior_volume) for d in weekdays_before(SESSION, prior_sessions)]
    frames.append(bars(SESSION, today_closes, today_volume))
    return pd.concat(frames).sort_index()


def daily(n: int = 20, high: float = 102.0, low: float = 98.0, close: float = 100.0) -> pd.DataFrame:
    """Identical daily bars ending the session before SESSION -> ATR == high - low."""
    days = weekdays_before(SESSION, n)
    idx = pd.DatetimeIndex([datetime.combine(d, time(0, 0), tzinfo=ET) for d in days])
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


def rising(n: int = 15, step: float = 0.06, base: float = 100.0) -> list[float]:
    return [base + step * i for i in range(n)]


def falling(n: int = 15, step: float = 0.06, base: float = 100.0) -> list[float]:
    return [base - step * i for i in range(n)]


UNIVERSE = {"index": "SPX", "tiers": {"A": ["TSLA"], "B": ["JPM"], "C": ["PFE"]}}


def patch_fetch(monkeypatch, intraday_by_symbol: dict, daily_by_symbol: dict | None = None):
    calls = {"intraday": [], "daily": []}

    def fake_intraday(symbols, now=None, sessions=None, cfg=CFG):
        calls["intraday"].append(list(symbols))
        return {s: intraday_by_symbol.get(s, pd.DataFrame(columns=osc._COLUMNS)) for s in symbols}

    def fake_daily(symbols, days=None, cfg=CFG):
        calls["daily"].append(list(symbols))
        return {s: (daily_by_symbol or {}).get(s, daily()) for s in symbols}

    monkeypatch.setattr(osc, "fetch_intraday_batch", fake_intraday)
    monkeypatch.setattr(osc, "fetch_daily_batch", fake_daily)
    return calls


def orange(high: float, low: float, minutes: int = 15, complete: bool = True) -> OpeningRange:
    return OpeningRange(high, low, high - low, minutes, complete)


def make_metrics(**overrides) -> Metrics:
    """A near-perfect bullish setup; override fields to probe each component."""
    base = dict(
        symbol="TSLA", session=SESSION, bar_time=NOW_0945 - timedelta(minutes=1), minutes_elapsed=15,
        last=100.9, open=100.0, or5=orange(100.3, 99.95, 5), or10=orange(100.6, 99.95, 10),
        or15=orange(100.95, 99.95), atr=10.0, expected_move=10.0, consumed_move=0.9, remaining_em=9.1,
        rvol=2.5, rvol_sessions=20, vwap=100.4, vwap_slope=0.05, ema9=100.7, ema21=100.4,
        ema9_slope=0.03, rsi=65.0, macd_hist=0.02, momentum_bull=5, momentum_bear=0,
        boundary_direction=BULLISH, direction=BULLISH, distance=0.05, distance_or_ratio=0.05,
        distance_atr_ratio=0.005, already_broken=False, notes=[],
    )
    base.update(overrides)
    return Metrics(**base)


PERFECT_CHAIN = [
    {"strike": 100.0, "right": "C", "bid": 1.00, "ask": 1.02, "mid": 1.01, "delta": 0.52},
    {"strike": 105.0, "right": "C", "bid": 0.20, "ask": 0.30, "mid": 0.25, "delta": 0.15},
    {"strike": 100.0, "right": "P", "bid": 1.00, "ask": 1.02, "mid": 1.01, "delta": -0.48},
]


# ------------------------------------------------------------------------------------ universe

def test_load_universe_from_doc():
    u = osc.load_universe()
    assert u["index"] == "SPX"
    assert [len(u["tiers"][t]) for t in ("A", "B", "C")] == [10, 10, 10]
    assert u["tiers"]["A"][0] == "TSLA" and u["tiers"]["C"][-1] == "PFE"
    members = osc.universe_members(u)
    assert len(members) == 31 and members[0] == ("SPX", osc.INDEX_TIER)
    assert osc.to_yf_symbol("SPX") == "^GSPC" and osc.to_yf_symbol("tsla") == "TSLA"


def test_intraday_pages_capped_at_yahoo_limit():
    now = datetime(2026, 9, 28, 7, 0, tzinfo=ET)
    pages = osc._intraday_pages(now, sessions=25, today=now.date())
    assert pages[0] == {"period": "7d"}
    floor = now.date() - timedelta(days=osc.YF_1M_MAX_CALENDAR_DAYS)
    for p in pages[1:]:
        assert date.fromisoformat(p["start"]) >= floor
        assert date.fromisoformat(p["start"]) < date.fromisoformat(p["end"])
    assert 2 <= len(pages) <= 5
    # re-scoring a past session: the 30-day limit still counts back from the REAL clock
    past = datetime(2026, 9, 10, 9, 45, tzinfo=ET)
    later_pages = osc._intraday_pages(past, sessions=25, today=date(2026, 9, 28))
    for p in later_pages[1:]:
        assert date.fromisoformat(p["start"]) >= floor


def test_normalize_daily_keeps_session_date():
    raw = pd.DataFrame({"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]},
                       index=pd.DatetimeIndex([pd.Timestamp("2026-09-25")]))
    df = osc._normalize(raw, daily=True)
    assert df.index[0].date() == date(2026, 9, 25)
    intra = osc._normalize(raw.set_index(pd.DatetimeIndex([pd.Timestamp("2026-09-25 13:30")])))
    assert intra.index[0].hour == 9 and intra.index[0].minute == 30   # UTC -> ET


# ------------------------------------------------------------------------- component scoring

@pytest.mark.parametrize("rvol,points", [(0.99, 0), (1.0, 5), (1.24, 5), (1.25, 10), (1.5, 15),
                                         (1.99, 15), (2.0, 20), (3.0, 20), (None, 10)])
def test_relative_volume_tiers(rvol, points):
    assert score_relative_volume(rvol, CFG) == points


def test_distance_zero_when_broken_and_full_at_boundary():
    assert score_distance(0.0, 1.0, already_broken=True) == 0
    assert score_distance(0.05, 1.0, False) == 15                # within 10% of the width
    assert score_distance(0.5, 1.0, False) == 0                  # mid-range
    assert 0 < score_distance(0.3, 1.0, False) < 15
    assert score_distance(0.1, 0.0, False) == 0                  # zero-width range can't be normalized


def test_or_compression_floor_penalizes_noise():
    atr = 10.0
    assert score_or_compression(1.0, atr) == 15                  # 10% of ATR: tight
    assert score_or_compression(4.0, atr) == 0                   # 40% of ATR: wide
    assert 0 < score_or_compression(2.5, atr) < 15
    noise = score_or_compression(0.2, atr)                       # 2% of ATR: below the floor
    assert noise == CFG.compression_noise_points < 15
    assert score_or_compression(0.6, atr) == 15                  # just above the floor: full credit


def test_atr_expected_move_split():
    assert score_atr_expected_move(remaining_em=8.0, atr_value=10.0, or15_width=1.0) == 20
    assert score_atr_expected_move(remaining_em=0.0, atr_value=10.0, or15_width=6.0) == 0
    assert score_atr_expected_move(remaining_em=8.0, atr_value=10.0, or15_width=6.0) == 12
    assert score_atr_expected_move(remaining_em=1.0, atr_value=10.0, or15_width=1.0) == 8
    assert score_atr_expected_move(5.0, 0.0, 1.0) == 0


def test_momentum_counts_by_direction():
    assert score_momentum(5, 0, BULLISH) == 10
    assert score_momentum(5, 0, BEARISH) == 0
    assert score_momentum(1, 4, BEARISH) == 8
    assert score_momentum(3, 2, BULLISH) == 6
    assert score_momentum(9, 0, BULLISH) == 10                   # capped at 5 conditions


def test_vwap_position():
    assert score_vwap(101, 100, 0.1, BULLISH) == 10
    assert score_vwap(101, 100, -0.1, BULLISH) == 6
    assert score_vwap(99, 100, 0.1, BULLISH) == 0
    assert score_vwap(99, 100, -0.1, BEARISH) == 10
    assert score_vwap(101, 100, -0.1, BEARISH) == 0


def test_option_quality_pluggable():
    assert option_quality_score(None, 100.0, BULLISH) == 0
    assert option_quality_score([], 100.0, BULLISH) == 0
    assert option_quality_score(PERFECT_CHAIN, 100.0, BULLISH) == 10
    assert option_quality_score(PERFECT_CHAIN, 100.0, BEARISH) == 10
    puts_only = [r for r in PERFECT_CHAIN if r["right"] == "P"]
    assert option_quality_score(puts_only, 100.0, BULLISH) == 0
    no_quotes = [{"strike": 100.0, "right": "C", "bid": 0.0, "ask": 0.0, "mid": 0.0, "delta": 0.5}]
    assert option_quality_score(no_quotes, 100.0, BULLISH) == 0
    wide = [{"strike": 100.0, "right": "C", "bid": 0.80, "ask": 1.20, "mid": 1.00, "delta": 0.5}]
    assert option_quality_score(wide, 100.0, BULLISH) == 5      # quotes 2 + delta 3 + spread 0
    off_delta = [{"strike": 100.0, "right": "C", "bid": 1.0, "ask": 1.02, "mid": 1.01, "delta": 0.1}]
    assert option_quality_score(off_delta, 100.0, BULLISH) == 2
    no_delta_atm = [{"strike": 100.5, "right": "C", "bid": 1.0, "ask": 1.02, "mid": 1.01}]
    assert option_quality_score(no_delta_atm, 100.0, BULLISH) == 10


# ----------------------------------------------------------------------------- full candidate

def test_full_score_sums_components_and_caps_at_100():
    c = score_candidate(make_metrics(), "A", PERFECT_CHAIN, CFG)
    assert c.score == 100
    assert c.score == pytest.approx(sum(c.components.values()))
    assert set(c.components) == {"relative_volume", "atr_expected_move", "or_compression",
                                 "distance_to_breakout", "momentum", "vwap_position", "option_quality"}
    assert c.score_with_tier == 102
    without_chain = score_candidate(make_metrics(), "A", None, CFG)
    assert without_chain.score == 90 and without_chain.components["option_quality"] == 0
    worst = score_candidate(make_metrics(rvol=0.5, remaining_em=0.0, or15=orange(106, 100), last=100.3,
                                         distance=0.0, already_broken=True, momentum_bull=0, vwap=101,
                                         vwap_slope=-1), "C", None, CFG)
    assert worst.score == 0 and worst.score_with_tier == -2


def test_tier_tiebreak_orders_equal_scores(monkeypatch):
    same = intraday(rising())
    calls = patch_fetch(monkeypatch, {"TSLA": same, "JPM": same, "PFE": same})
    universe = {"index": None, "tiers": {"C": ["PFE"], "B": ["JPM"], "A": ["TSLA"]}}
    ranked = osc.scan(universe, NOW_0945)
    assert [c.symbol for c in ranked] == ["TSLA", "JPM", "PFE"]
    assert ranked[0].score == ranked[1].score == ranked[2].score
    assert ranked[0].score_with_tier == ranked[0].score + 2
    assert ranked[1].score_with_tier == ranked[1].score
    assert ranked[2].score_with_tier == ranked[2].score - 2
    assert calls["intraday"] == [["PFE", "JPM", "TSLA"]] and calls["daily"] == [["PFE", "JPM", "TSLA"]]


# --------------------------------------------------------------------------- metrics from bars

def test_compute_metrics_bullish_near_or_high():
    m, reasons = osc.compute_metrics("TSLA", intraday(rising()), daily(), NOW_0945, CFG)
    assert reasons == [] and m is not None
    assert m.minutes_elapsed == 15 and m.bar_time == NOW_0945 - timedelta(minutes=1)
    assert m.or5.width == pytest.approx(0.34) and m.or5.complete
    assert m.or15.high == pytest.approx(100.89) and m.or15.low == pytest.approx(99.95)
    assert m.or15.width == pytest.approx(0.94) and m.or15.complete
    assert m.atr == pytest.approx(4.0) and m.expected_move == pytest.approx(4.0)
    assert m.consumed_move == pytest.approx(0.84) and m.remaining_em == pytest.approx(3.16)
    assert m.rvol == pytest.approx(1.5) and m.rvol_sessions == 5
    assert m.direction == BULLISH and not m.already_broken
    assert m.distance == pytest.approx(0.05) and m.distance_or_ratio == pytest.approx(0.05 / 0.94)
    assert m.momentum_bull == 5 and m.momentum_bear == 0
    assert m.last > m.vwap and m.vwap_slope > 0 and m.ema_state == "BULL" and m.rsi > 50
    c = score_candidate(m, "A", None, CFG)
    assert c.components["relative_volume"] == 15
    assert c.components["distance_to_breakout"] == 15
    assert c.components["momentum"] == 10 and c.components["vwap_position"] == 10
    assert c.components["atr_expected_move"] == pytest.approx(12 + 8 * (0.5 - 0.235) / 0.35, abs=0.05)
    assert c.components["or_compression"] == pytest.approx(15 * (0.4 - 0.235) / 0.3, abs=0.05)
    assert 0 < c.score <= 100


def test_compute_metrics_bearish_mirror():
    m, _ = osc.compute_metrics("TSLA", intraday(falling()), daily(), NOW_0945, CFG)
    assert m.direction == BEARISH and m.momentum_bear == 5
    assert m.distance == pytest.approx(0.05)
    c = score_candidate(m, "B", None, CFG)
    assert c.components["momentum"] == 10 and c.components["vwap_position"] == 10
    assert c.components["distance_to_breakout"] == 15


def test_already_broken_after_or15():
    closes = [100.0] * 15 + [100.2, 100.3, 100.4, 100.5, 100.6]
    now = NOW_0945 + timedelta(minutes=5)
    m, _ = osc.compute_metrics("TSLA", intraday(closes), daily(), now, CFG)
    assert m.or15.high == pytest.approx(100.05) and m.or15.complete
    assert m.already_broken and m.direction == BULLISH and m.distance == 0
    c = score_candidate(m, "A", None, CFG)
    assert c.components["distance_to_breakout"] == 0
    assert c.already_broken and "already outside" in "; ".join(c.notes)


def test_incomplete_or15_flagged_before_0945():
    m, _ = osc.compute_metrics("TSLA", intraday(rising()), daily(), NOW_0945 - timedelta(minutes=5), CFG)
    assert m.minutes_elapsed == 10 and m.or10.complete and not m.or15.complete
    assert m.or15.width == m.or10.width
    assert any("OR15 incomplete" in n for n in m.notes)


def test_index_gets_neutral_rvol(monkeypatch):
    patch_fetch(monkeypatch, {"SPX": intraday(rising(), today_volume=1e9)})
    universe = {"index": "SPX", "tiers": {}}
    ranked = osc.scan(universe, NOW_0945)
    assert ranked[0].symbol == "SPX" and ranked[0].tier == osc.INDEX_TIER
    assert ranked[0].rvol is None and ranked[0].components["relative_volume"] == CFG.rvol_neutral_points
    assert any("neutral RVOL" in n for n in ranked[0].notes)
    assert ranked[0].score_with_tier == ranked[0].score


def test_too_few_bars_is_skipped_with_reason(monkeypatch):
    patch_fetch(monkeypatch, {"TSLA": intraday(rising()), "JPM": intraday(rising(3)), "PFE": pd.DataFrame(columns=osc._COLUMNS)})
    result = osc.scan_detailed({"index": None, "tiers": UNIVERSE["tiers"]}, NOW_0945)
    assert [c.symbol for c in result.candidates] == ["TSLA"]
    assert "3 completed bars" in result.skipped["JPM"][0]
    assert result.skipped["PFE"] == ["no intraday bars"]


def test_missing_daily_bars_is_skipped(monkeypatch):
    patch_fetch(monkeypatch, {"TSLA": intraday(rising())}, {"TSLA": daily(n=5)})
    result = osc.scan_detailed({"index": None, "tiers": {"A": ["TSLA"]}}, NOW_0945)
    assert result.candidates == [] and "ATR" in result.skipped["TSLA"][0]


def test_rvol_needs_min_sessions_then_neutral(monkeypatch):
    m, _ = osc.compute_metrics("TSLA", intraday(rising(), prior_sessions=2), daily(), NOW_0945, CFG)
    assert m.rvol is None and any("only 2 prior sessions" in n for n in m.notes)
    assert score_candidate(m, "A").components["relative_volume"] == CFG.rvol_neutral_points


def test_chain_fn_is_called_with_spot_and_direction(monkeypatch):
    patch_fetch(monkeypatch, {"TSLA": intraday(rising())})
    seen = []

    def chain_fn(symbol, spot, direction):
        seen.append((symbol, spot, direction))
        return PERFECT_CHAIN

    ranked = osc.scan({"index": None, "tiers": {"A": ["TSLA"]}}, NOW_0945, chain_fn=chain_fn)
    assert seen == [("TSLA", pytest.approx(100.84), BULLISH)]
    assert ranked[0].components["option_quality"] == 10

    def boom(symbol, spot, direction):
        raise RuntimeError("chain down")

    ranked = osc.scan({"index": None, "tiers": {"A": ["TSLA"]}}, NOW_0945, chain_fn=boom)
    assert ranked[0].components["option_quality"] == 0 and any("chain unavailable" in n for n in ranked[0].notes)


def test_scan_symbol_subset_keeps_tiers(monkeypatch):
    patch_fetch(monkeypatch, {"PFE": intraday(rising()), "XYZ": intraday(rising())})
    ranked = osc.scan(UNIVERSE, NOW_0945, symbols=["pfe", "XYZ"])
    assert {(c.symbol, c.tier) for c in ranked} == {("PFE", "C"), ("XYZ", "?")}
    assert ranked[0].symbol == "XYZ"    # unknown tier: no penalty, ranks ahead of C on the tiebreak


# ------------------------------------------------------------------------------------ output

def test_format_ranking_layout():
    a = score_candidate(make_metrics(), "A", PERFECT_CHAIN)
    b = score_candidate(make_metrics(symbol="PFE", rvol=None, already_broken=True, distance=0.0,
                                     direction=BEARISH, momentum_bear=2), "C")
    text = format_ranking([a, b], top=10)
    lines = text.splitlines()
    for col in ("Rank", "Ticker", "Tier", "Dir", "Score", "OR Width", "ATR", "Rem EM", "Break Dist", "RVOL"):
        assert col in lines[0]
    assert len(lines) == 4                                       # header, rule, two rows
    assert lines[2].split()[:4] == ["1", "TSLA", "A", "Bullish"]
    assert "102.0" in lines[2] and "100.0" in lines[2] and "2.5x" in lines[2] and "$1.00" in lines[2]
    assert "PFE" in lines[3] and "Bearish" in lines[3] and "n/a" in lines[3] and "BROKEN" in lines[3]
    assert len(format_ranking([a, b], top=1).splitlines()) == 3
    assert "(no candidates)" in format_ranking([])


def test_to_log_row_has_section_18_pretrade_fields():
    row = to_log_row(score_candidate(make_metrics(), "A", PERFECT_CHAIN))
    required = {"ticker", "date", "bar_time", "minutes_elapsed", "tier", "direction", "score",
                "score_with_tier", "atr", "expected_move", "remaining_em", "consumed_move", "rvol",
                "distance", "distance_or_ratio", "distance_atr_ratio", "already_broken", "vwap",
                "vwap_relation", "vwap_slope", "ema9", "ema21", "ema_state", "ema9_slope", "rsi",
                "macd_hist", "momentum_aligned", "last", "open", "notes",
                "or5_width", "or10_width", "or15_width", "or15_high", "or15_low", "or15_complete"}
    assert required <= set(row)
    for name in ("relative_volume", "atr_expected_move", "or_compression", "distance_to_breakout",
                 "momentum", "vwap_position", "option_quality"):
        assert f"component_{name}" in row
    assert row["ticker"] == "TSLA" and row["date"] == "2026-09-25" and row["vwap_relation"] == "ABOVE"
    assert row["ema_state"] == "BULL" and row["momentum_aligned"] == 5
    import json
    json.dumps(row)                                              # flat and serializable

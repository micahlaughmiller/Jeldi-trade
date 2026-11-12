"""One index series, complete candles, a snapshot shared by every persona (2026-10-08)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

import config
import market_data

ET = ZoneInfo("US/Eastern")
DAY = (2026, 10, 7)


def at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=ET)


def bars(start, end, close_base=7800.0, skip=()):
    """1-minute bars from `start` to `end` (exclusive); each bar's close is close_base + its index in minutes."""
    rows, idx, t, i = [], [], start, 0
    while t < end:
        if t not in skip:
            c = close_base + i
            rows.append({"open": c - 0.5, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 100.0})
            idx.append(t)
        t += timedelta(minutes=1)
        i += 1
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def epoch(dt):
    return dt.timestamp()


def test_a_candle_is_withheld_until_its_last_minute_bar_has_arrived():
    four = bars(at(9, 30), at(9, 34))                                               # 09:30..09:33, the 09:34 bar is missing
    out = market_data._candles_from_1m(four, epoch(at(9, 35, 3)), 5, 180, at(9, 35, 3))
    assert out.empty
    five = bars(at(9, 30), at(9, 35))
    out = market_data._candles_from_1m(five, epoch(at(9, 35, 3)), 5, 180, at(9, 35, 3))
    assert len(out) == 1 and out.index[0] == at(9, 30)
    assert out["open"].iloc[0] == 7799.5 and out["close"].iloc[0] == 7804.0          # first open, last close
    assert out["high"].iloc[0] == 7805.0 and out["low"].iloc[0] == 7799.0 and out["volume"].iloc[0] == 500.0


def test_a_snapshot_taken_while_the_last_bar_was_still_forming_does_not_emit_the_candle():
    five = bars(at(9, 30), at(9, 35))
    assert market_data._candles_from_1m(five, epoch(at(9, 34, 59)), 5, 180, at(9, 35, 5)).empty       # fetched before 09:35 + settle
    assert market_data._candles_from_1m(five, epoch(at(9, 35, 1)), 5, 180, at(9, 35, 5)).empty         # inside the 2 s settle window
    assert len(market_data._candles_from_1m(five, epoch(at(9, 35, 3)), 5, 180, at(9, 35, 5))) == 1


def test_the_clock_boundary_alone_no_longer_emits_a_candle():
    five = bars(at(9, 30), at(9, 35))
    # the old rule (candle end <= now) would have emitted at 09:35:00 from whatever bars had arrived
    assert market_data._candles_from_1m(five, epoch(at(9, 34, 30)), 5, 180, at(9, 35, 0)).empty


def test_an_old_incomplete_candle_is_accepted_after_the_gap_tolerance_but_a_young_one_is_not():
    gap = bars(at(9, 30), at(9, 35), skip={at(9, 32)})
    assert market_data._candles_from_1m(gap, epoch(at(9, 36)), 5, 180, at(9, 36)).empty            # 60 s after the end: wait
    out = market_data._candles_from_1m(gap, epoch(at(9, 40)), 5, 180, at(9, 40))
    assert len(out) == 1 and out.index[0] == at(9, 30)                                              # 5 min after: accepted as it is


def test_two_personas_asking_seconds_apart_get_the_identical_candles():
    snap = bars(at(9, 30), at(10, 6))
    fetched = epoch(at(10, 5, 3))
    a = market_data._candles_from_1m(snap, fetched, 5, 180, at(10, 5, 4))
    b = market_data._candles_from_1m(snap, fetched, 5, 180, at(10, 5, 11))
    assert_frame_equal(a, b)
    assert len(a) == 7 and a.index[-1] == at(10, 0)


def test_all_intervals_come_from_the_same_minute_bars():
    snap = bars(at(9, 30), at(9, 40))
    fetched = epoch(at(9, 40, 3))
    one = market_data._candles_from_1m(snap, fetched, 1, 180, at(9, 40, 3))
    two = market_data._candles_from_1m(snap, fetched, 2, 180, at(9, 40, 3))
    five = market_data._candles_from_1m(snap, fetched, 5, 180, at(9, 40, 3))
    assert len(one) == 10 and len(two) == 5 and len(five) == 2
    assert two["close"].iloc[-1] == one["close"].iloc[-1] == five["close"].iloc[-1]                  # one series, one last price
    assert two.index[0] == at(9, 30) and five.index.tolist() == [at(9, 30), at(9, 35)]


def test_the_lookback_window_applies():
    snap = bars(at(9, 30), at(11, 0))
    out = market_data._candles_from_1m(snap, epoch(at(11, 0, 3)), 5, 30, at(11, 0, 3))
    assert out.index[0] >= at(11, 0, 3) - timedelta(minutes=30) and out.index[-1] == at(10, 55)


def test_get_candles_routes_spx_through_the_one_yahoo_series_by_default(monkeypatch):
    snap = bars(at(9, 30), at(9, 40))
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (snap, epoch(at(9, 40, 3))))
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: pytest.fail("the SPY path must not be used"))
    out = market_data.get_candles(config.SPX_SYMBOL, "5m", 180, at(9, 40, 3))
    assert len(out) == 2 and config.SPX_CANDLE_SOURCE == "yahoo"


def test_the_spy_source_stays_available_for_rollback(monkeypatch):
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "spy")
    marker = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
                          index=pd.DatetimeIndex([at(9, 30)]))
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: marker)
    assert market_data.get_candles(config.SPX_SYMBOL, "5m", 180, at(9, 40)) is marker


def test_the_opening_range_comes_from_the_same_series_and_waits_for_a_settled_snapshot(monkeypatch):
    snap = bars(at(9, 30), at(10, 0))
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (snap, epoch(at(9, 59, 59))))
    assert market_data.get_opening_range(at(10, 0, 5)) is None                                      # the 09:59 bar may be forming
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (snap, epoch(at(10, 0, 3))))
    levels = market_data.get_opening_range(at(10, 0, 5))
    assert levels.high == 7800.0 + 29 and levels.low == 7800.0 - 0.5         # candle bodies only: the 09:59 close, the 09:30 open
    assert market_data.get_opening_range(at(9, 59)) is None


def test_the_basis_is_the_first_common_minute_from_the_open_whenever_it_is_asked(monkeypatch):
    spx = bars(at(9, 30), at(10, 30), close_base=7800.0)
    es = bars(at(9, 40), at(10, 30), close_base=7740.0)                                              # ES lags ten minutes
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (spx, epoch(at(10, 30))))
    monkeypatch.setattr(market_data, "_download", lambda symbol, period, interval, prepost: es)
    early = market_data.get_spx_es_basis(at(9, 45))
    late = market_data.get_spx_es_basis(at(10, 25))
    assert early == late == round((7800.0 + 10) - 7740.0, 2)                                         # SPX bar 09:40 minus ES bar 09:40
    assert market_data.get_spx_es_basis(at(9, 35)) is None                                           # no common minute yet


def test_the_snapshot_freshness_rule():
    wall = at(10, 0, 50).timestamp()
    assert market_data._fresh(at(10, 0, 35).timestamp(), "1m", wall)                                 # inside the TTL, after the boundary
    assert not market_data._fresh(at(10, 0, 20).timestamp(), "1m", at(10, 0, 50).timestamp() + 1)    # 31 s old: past the TTL
    assert market_data._fresh(at(10, 0, 55).timestamp(), "1m", at(10, 1, 1).timestamp())             # new minute, boundary + 2 s not reached
    assert not market_data._fresh(at(10, 0, 55).timestamp(), "1m", at(10, 1, 3).timestamp())         # boundary passed: refetch now
    assert market_data._fresh(at(10, 1, 3, ).timestamp(), "1m", at(10, 1, 15).timestamp())
    assert market_data._fresh(at(10, 0, 55).timestamp(), "1d", at(10, 1, 3).timestamp())             # daily data: TTL only


def test_two_processes_share_one_download(monkeypatch, tmp_path):
    """The point of the whole change: two personas, one Yahoo request, the same bytes."""
    monkeypatch.setattr(config, "SHARED_DATA_ENABLED", True)
    monkeypatch.setattr(config, "SHARED_DATA_DIR", str(tmp_path / "shared"))
    downloads = []
    raw = bars(at(9, 30), at(9, 40))
    raw.columns = [c.capitalize() for c in raw.columns]
    raw.index = raw.index.tz_convert("UTC")

    def fake_download(symbol, **kwargs):
        downloads.append(symbol)
        return raw
    monkeypatch.setattr(market_data.yf, "download", fake_download)
    market_data._CACHE.clear()
    first, fetched1 = market_data._download_snapshot("^GSPC", "5d", "1m", False)
    market_data._CACHE.clear()                                                                      # "another process": empty memory
    second, fetched2 = market_data._download_snapshot("^GSPC", "5d", "1m", False)
    assert downloads == ["^GSPC"] and fetched1 == fetched2
    assert_frame_equal(first, second)
    assert len(first) == 10
    market_data._CACHE.clear()


def test_shipped_data_settings():
    assert config.SPX_CANDLE_SOURCE == "yahoo" and config.CANDLE_SETTLE_SEC == 2.0 and config.CANDLE_GAP_TOLERANCE_SEC == 180
    assert config.SHARED_DATA_ENABLED is True and config.SHARED_DATA_DIR == "shared_data"


def test_a_missing_just_closed_bar_triggers_a_quick_redownload_but_only_in_session():
    snap = bars(at(10, 0), at(10, 4))                                                # last bar 10:03: the 10:04 bar has not arrived
    fetched = at(10, 5, 2).timestamp()
    assert market_data._fresh(fetched, "1m", at(10, 5, 4).timestamp(), df=snap)      # 2 s old: too soon to ask again
    assert not market_data._fresh(fetched, "1m", at(10, 5, 6).timestamp(), df=snap)  # 4 s old and the closed bar is still missing
    complete = bars(at(10, 0), at(10, 5))
    assert market_data._fresh(fetched, "1m", at(10, 5, 6).timestamp(), df=complete)  # the bar is there: no refetch
    after_hours = bars(at(15, 50), at(15, 58))
    wall = datetime(*DAY, 18, 0, 30, tzinfo=ET).timestamp()
    assert market_data._fresh(wall - 10, "1m", wall, df=after_hours)                 # 18:00: the market is closed, nothing to wait for
    assert market_data._fresh(at(10, 5, 2).timestamp(), "1m", at(10, 5, 6).timestamp(), df=snap, prepost=True)   # ES (24 h): exempt


def test_the_spot_comes_from_the_one_spx_series(monkeypatch):
    snap = bars(at(9, 30), at(9, 40))
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (snap, epoch(at(9, 40, 3))))
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: pytest.fail("SPX spot must not use a private download"))
    assert market_data.get_spot(config.SPX_SYMBOL, at(9, 40, 3)) == 7809.0           # last 1-minute close, 09:39
    assert market_data.get_spot(config.SPX_SYMBOL, at(9, 35, 0)) == 7805.0           # as of 09:35: the last bar stamped <= 09:35


def test_the_basis_moves_with_the_minute_so_latching_the_first_one_is_what_the_test_pins(monkeypatch):
    spx = bars(at(9, 30), at(10, 30), close_base=7800.0)
    es = bars(at(9, 40), at(10, 30), close_base=7740.0)
    es["close"] = es["close"] + range(len(es))                                       # ES drifts away from SPX: the basis changes every minute
    monkeypatch.setattr(market_data, "_spx_1m", lambda: (spx, epoch(at(10, 30))))
    monkeypatch.setattr(market_data, "_download", lambda symbol, period, interval, prepost: es)
    assert market_data.get_spx_es_basis(at(9, 45)) == market_data.get_spx_es_basis(at(10, 25)) == 70.0

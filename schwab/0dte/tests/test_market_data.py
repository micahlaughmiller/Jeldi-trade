"""Live-ES (Schwab) wiring: must always fail soft to the existing yfinance path, never raise."""

import pandas as pd
import pytest

import config
import market_data


@pytest.fixture(autouse=True)
def _clear_es_cache():
    # get_es_candles_live caches its raw fetch for DATA_CACHE_SEC (20s) -- without clearing between
    # tests, a fast-running suite reuses a previous test's cached result across unrelated tests.
    market_data._CACHE.clear()
    yield
    market_data._CACHE.clear()


def test_schwab_client_none_without_credentials(monkeypatch):
    # _schwab_client reads through config (which itself has a hardcoded SCHWAB_TOKEN_PATH default),
    # not a raw os.getenv -- blank out all three explicitly rather than relying on env absence.
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    monkeypatch.setattr(config, "SCHWAB_APP_SECRET", "")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", "")
    market_data._SCHWAB_CLIENT = None
    assert market_data._schwab_client() is None
    # cached as False (tried once, not retried) rather than None (untried) on the second call
    assert market_data._SCHWAB_CLIENT is False


def test_get_es_candles_live_returns_none_without_client(monkeypatch):
    monkeypatch.setattr(market_data, "_schwab_client", lambda: None)
    assert market_data.get_es_candles_live(2, 180) is None


def test_get_es_candles_live_returns_none_on_api_error(monkeypatch):
    class BoomClient:
        def get_price_history_every_minute(self, *a, **k):
            raise RuntimeError("network error")

    monkeypatch.setattr(market_data, "_schwab_client", lambda: BoomClient())
    assert market_data.get_es_candles_live(2, 180) is None


def test_get_es_candles_live_returns_none_on_empty_candles(monkeypatch):
    class EmptyResp:
        def json(self):
            return {"candles": []}

    class EmptyClient:
        def get_price_history_every_minute(self, *a, **k):
            return EmptyResp()

    monkeypatch.setattr(market_data, "_schwab_client", lambda: EmptyClient())
    assert market_data.get_es_candles_live(2, 180) is None


def test_get_es_candles_live_resamples_to_requested_interval(monkeypatch):
    import config
    from datetime import datetime
    from strategy import at_time

    now = at_time(datetime.now(config.ET), config.MARKET_OPEN).replace(hour=10, minute=0)
    base_ms = int((now.timestamp() - 4 * 60) * 1000)
    minute_bars = [
        {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 10,
         "datetime": base_ms + i * 60_000}
        for i in range(4)
    ]

    class FakeResp:
        def json(self):
            return {"candles": minute_bars}

    class FakeClient:
        def get_price_history_every_minute(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(market_data, "_schwab_client", lambda: FakeClient())
    df = market_data.get_es_candles_live(2, 180, now=now)
    assert df is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # 4 one-minute bars resampled to 2-minute bars -> 2 completed candles
    assert len(df) == 2
    assert df["volume"].iloc[0] == 20


def test_get_candles_falls_back_to_yfinance_when_live_es_unavailable(monkeypatch):
    import config
    monkeypatch.setattr(market_data, "get_es_candles_live", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: called.append(a) or market_data._normalize(None))
    market_data.get_candles(config.ES_SYMBOL, "2m", 180)
    assert called and called[0][0] == config.ES_SYMBOL


# ---------------------------------------------------------------------------------------------------
# 2026-10-01: a real live incident -- yf.download() can come back completely empty with NO exception
# raised (Yahoo throttling/outage), which the old bare try/except around it never caught, so a dead
# SPX feed looked byte-for-byte identical to a quiet market: zero signals, zero log lines, zero journal
# events, for as long as the outage lasted. _download now retries in-call and _report_feed_health logs
# the transition loudly (ERROR, bypasses the console's keep-tag filter) instead of silently caching an
# empty result as if nothing were wrong.

@pytest.fixture(autouse=True)
def _clear_feed_state():
    market_data._FEED_STATE.clear()
    yield
    market_data._FEED_STATE.clear()


def _real_df() -> pd.DataFrame:
    idx = pd.date_range("2026-10-01 10:00", periods=2, freq="5min", tz=config.ET)
    return pd.DataFrame({"Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
                        "Close": [1.0, 1.0], "Volume": [10, 10]}, index=idx)


def test_download_retries_in_call_and_succeeds_on_a_later_attempt(monkeypatch):
    calls = []

    def fake_download(symbol, **kwargs):
        calls.append(symbol)
        return pd.DataFrame() if len(calls) == 1 else _real_df()

    monkeypatch.setattr(market_data.yf, "download", fake_download)
    monkeypatch.setattr(market_data._time, "sleep", lambda s: None)
    df = market_data._download("^GSPC", "5d", "5m", False)
    assert len(calls) == 2            # one empty attempt, one real attempt -- succeeded within budget
    assert not df.empty
    assert market_data._FEED_STATE[("^GSPC", "5m")]["healthy"] is True


def test_download_logs_loudly_when_every_retry_comes_back_empty(monkeypatch, caplog):
    caplog.set_level("ERROR")
    monkeypatch.setattr(market_data.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(market_data._time, "sleep", lambda s: None)
    df = market_data._download("^GSPC", "5d", "5m", False)
    assert df.empty
    assert market_data._FEED_STATE[("^GSPC", "5m")]["healthy"] is False
    assert any("DATA FEED DOWN" in r.message for r in caplog.records)


def test_download_logs_recovery_after_being_down(monkeypatch, caplog):
    market_data._FEED_STATE[("^GSPC", "5m")] = {"healthy": False, "last_notice": 0.0}
    caplog.set_level("WARNING")
    monkeypatch.setattr(market_data.yf, "download", lambda *a, **k: _real_df())
    market_data._download("^GSPC", "5d", "5m", False)
    assert market_data._FEED_STATE[("^GSPC", "5m")]["healthy"] is True
    assert any("DATA FEED RECOVERED" in r.message for r in caplog.records)


def test_report_feed_health_does_not_repeat_down_log_within_reminder_window(monkeypatch, caplog):
    caplog.set_level("ERROR")
    mono = [1000.0]
    monkeypatch.setattr(market_data._time, "monotonic", lambda: mono[0])
    market_data._report_feed_health(("^GSPC", "5m"), False, None)   # first failure: logs
    mono[0] += 5   # well inside DATA_FEED_REMINDER_SEC=120 -- must NOT log again yet
    market_data._report_feed_health(("^GSPC", "5m"), False, None)
    down_logs = [r for r in caplog.records if "DATA FEED DOWN" in r.message]
    assert len(down_logs) == 1


def test_report_feed_health_reannounces_a_persistent_outage_after_the_reminder_window(monkeypatch, caplog):
    caplog.set_level("ERROR")
    mono = [1000.0]
    monkeypatch.setattr(market_data._time, "monotonic", lambda: mono[0])
    market_data._report_feed_health(("^GSPC", "5m"), False, None)
    mono[0] += config.DATA_FEED_REMINDER_SEC + 1   # still down, well past the reminder window
    market_data._report_feed_health(("^GSPC", "5m"), False, None)
    down_logs = [r for r in caplog.records if "DATA FEED DOWN" in r.message]
    assert len(down_logs) == 2

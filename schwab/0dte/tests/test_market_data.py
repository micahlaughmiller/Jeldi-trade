"""Live-ES (Schwab) wiring: must always fail soft to the existing yfinance path, never raise."""

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

"""Live-ES (Schwab) wiring: must always fail soft to the existing yfinance path, never raise."""

import pandas as pd
import pytest
import requests

import config
import market_data


@pytest.fixture(autouse=True)
def _clear_es_cache():
    # get_es_candles_live caches its raw fetch for DATA_CACHE_SEC (20s) -- without clearing between
    # tests, a fast-running suite reuses a previous test's cached result across unrelated tests.
    market_data._CACHE.clear()
    yield
    market_data._CACHE.clear()


def _write_token(path, access_token="atok", expires_in=1800, expires_at=None):
    import time as _time
    if expires_at is None:
        expires_at = _time.time() + expires_in
    path.write_text(__import__("json").dumps({
        "creation_timestamp": int(_time.time()),
        "token": {
            "expires_in": expires_in, "token_type": "Bearer", "scope": "api",
            "refresh_token": "rtok-should-never-be-used-by-this-folder",
            "access_token": access_token, "id_token": "idtok", "expires_at": expires_at,
        },
    }))


def test_schwab_read_only_client_none_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", "")
    assert market_data._schwab_read_only_client() is None


def test_schwab_read_only_client_none_when_token_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(tmp_path / "does_not_exist.json"))
    assert market_data._schwab_read_only_client() is None


def test_schwab_read_only_client_none_when_access_token_expired(monkeypatch, tmp_path):
    # 2026-10-01: this folder must NEVER attempt a refresh (see the module-level comment above --
    # Schwab refresh tokens are single-use and shared across up to 9 personas + the real Schwab bot).
    # An expired token is just "unavailable this cycle," exactly like a missing credential.
    token_path = tmp_path / "token.json"
    _write_token(token_path, expires_at=__import__("time").time() - 10)   # already expired
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(token_path))
    assert market_data._schwab_read_only_client() is None


def test_schwab_read_only_client_builds_a_non_refreshing_session_when_valid(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    _write_token(token_path, access_token="a-real-looking-token")
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(token_path))
    client = market_data._schwab_read_only_client()
    assert client is not None
    # A plain requests.Session with a static Bearer header -- no OAuth2 token/refresh_token attached at
    # all, so there is no code path by which this client could ever call Schwab's refresh endpoint.
    assert isinstance(client.session, requests.Session)
    assert client.session.headers["Authorization"] == "Bearer a-real-looking-token"
    assert not hasattr(client.session, "token")


def test_get_es_candles_live_returns_none_without_client(monkeypatch):
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: None)
    assert market_data.get_es_candles_live(2, 180) is None


def test_get_es_candles_live_returns_none_on_api_error(monkeypatch):
    class BoomClient:
        def get_price_history_every_minute(self, *a, **k):
            raise RuntimeError("network error")

    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: BoomClient())
    assert market_data.get_es_candles_live(2, 180) is None


def test_get_es_candles_live_returns_none_on_empty_candles(monkeypatch):
    class EmptyResp:
        def json(self):
            return {"candles": []}

    class EmptyClient:
        def get_price_history_every_minute(self, *a, **k):
            return EmptyResp()

    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: EmptyClient())
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

    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: FakeClient())
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


# ---------------------------------------------------------------------------------------------------
# 2026-10-01: Alpaca has no genuine SPX index bars on this plan, so get_spx_candles_via_alpaca pulls
# real SPY bars (the ETF Alpaca DOES serve directly) and scales them up by the SPX/SPY ratio -- this
# replaces the high-frequency yf.download("^GSPC", ...) candle path (the actual root cause of the
# live incident above) with a real, non-rate-limited feed, falling back to the existing yfinance path
# only if Alpaca credentials are missing or the call fails for any reason.

@pytest.fixture(autouse=True)
def _clear_spx_ratio_cache():
    market_data._SPX_RATIO_CACHE.update({"ratio": None, "at": 0.0})
    yield
    market_data._SPX_RATIO_CACHE.update({"ratio": None, "at": 0.0})


def _alpaca_bars_page(prices: list[float], base_et: pd.Timestamp, next_token: str | None = None) -> dict:
    """base_et is ET wall-clock (what get_candles' lookback filtering actually compares against) --
    converted to a real UTC ISO string per bar, same as Alpaca's actual response format."""
    bars = []
    for i, p in enumerate(prices):
        t_utc = (base_et + pd.Timedelta(minutes=5 * i)).tz_convert("UTC")
        bars.append({"t": t_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": p, "h": p + 0.1, "l": p - 0.1,
                    "c": p, "v": 1000})
    return {"bars": bars, "symbol": config.SPY_SYMBOL, "next_page_token": next_token}


def test_alpaca_creds_none_without_both_key_and_secret(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "sekrit")
    assert market_data._alpaca_creds() is None


def test_get_spx_candles_via_alpaca_returns_none_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "")
    assert market_data.get_spx_candles_via_alpaca(5, 180) is None


def test_get_spx_candles_via_alpaca_scales_spy_bars_by_the_ratio(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:06:00", tz=config.ET)
    base = pd.Timestamp("2026-10-01 09:55:00", tz=config.ET)
    monkeypatch.setattr(market_data, "_alpaca_data_get",
                        lambda path, params: _alpaca_bars_page([76.0, 76.5], base))
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 10.0)
    df = market_data.get_spx_candles_via_alpaca(5, 180, now=now)
    assert df is not None and not df.empty
    assert df["close"].iloc[0] == pytest.approx(760.0)   # 76.0 SPY * 10.0 ratio
    assert df["close"].iloc[-1] == pytest.approx(765.0)


def test_get_spx_candles_via_alpaca_paginates_across_multiple_pages(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:06:00", tz=config.ET)
    base = pd.Timestamp("2026-10-01 09:55:00", tz=config.ET)
    calls = []

    def fake_get(path, params):
        calls.append(dict(params))
        if "page_token" not in params:
            return _alpaca_bars_page([76.0], base, next_token="tok2")
        return _alpaca_bars_page([76.5], base + pd.Timedelta(minutes=5), next_token=None)

    monkeypatch.setattr(market_data, "_alpaca_data_get", fake_get)
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 10.0)
    df = market_data.get_spx_candles_via_alpaca(5, 180, now=now)
    assert len(calls) == 2 and calls[1]["page_token"] == "tok2"
    assert len(df) == 2


def test_get_spx_candles_via_alpaca_returns_none_on_malformed_bars_shape(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: {"bars": "not-a-list"})
    assert market_data.get_spx_candles_via_alpaca(5, 180) is None


# ---------------------------------------------------------------------------------------------------
# 2026-10-01, same day: moving SPX candles onto Alpaca immediately pushed Alpaca's OWN data-API rate
# limit hard enough to also 429 this bot's unrelated TRADING-api calls (same account). Fixed by (1)
# fetching ONE base 1-minute series and resampling locally to whatever interval a caller needs,
# instead of one separate Alpaca request per interval, and (2) retrying a 429/5xx with backoff
# in-call, same pattern broker.py's own _request() already uses for the trading API.

def test_get_spx_candles_via_alpaca_requests_1min_regardless_of_interval_asked(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    seen = []
    monkeypatch.setattr(market_data, "_alpaca_data_get",
                        lambda path, params: seen.append(params["timeframe"]) or {"bars": [], "next_page_token": None})
    market_data.get_spx_candles_via_alpaca(5, 180)
    assert seen == ["1Min"]   # asked for 5m candles, but the underlying Alpaca request is always 1Min


def test_get_spx_candles_via_alpaca_resamples_1min_bars_to_the_requested_interval(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:11:00", tz=config.ET)
    base = pd.Timestamp("2026-10-01 10:00:00", tz=config.ET)
    # 10 genuine 1-minute bars (not pre-aligned to any coarser bucket by coincidence): 10:00-10:09.
    one_min_closes = [76.0, 76.1, 76.3, 76.05, 76.2, 76.4, 76.15, 76.5, 76.25, 76.6]
    bars = []
    for i, c in enumerate(one_min_closes):
        t = (base + pd.Timedelta(minutes=i)).tz_convert("UTC")
        bars.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c - 0.02, "h": c + 0.05,
                    "l": c - 0.05, "c": c, "v": 100})
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: {"bars": bars, "next_page_token": None})
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 1.0)   # ratio=1 -> compare directly to SPY values
    df = market_data.get_spx_candles_via_alpaca(5, 180, now=now)
    assert len(df) == 2   # two complete 5-minute buckets: 10:00-10:04 and 10:05-10:09
    assert df["close"].iloc[0] == pytest.approx(one_min_closes[4])    # last close in the first bucket
    assert df["high"].iloc[0] == pytest.approx(max(one_min_closes[:5]) + 0.05)
    assert df["volume"].iloc[0] == 500   # 5 one-minute bars summed


def test_get_spx_candles_via_alpaca_shares_one_fetch_across_different_intervals(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:06:00", tz=config.ET)
    base = pd.Timestamp("2026-10-01 09:55:00", tz=config.ET)
    calls = []
    monkeypatch.setattr(market_data, "_alpaca_data_get",
                        lambda path, params: calls.append(1) or _alpaca_bars_page([76.0, 76.5], base))
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 10.0)
    market_data.get_spx_candles_via_alpaca(1, 180, now=now)
    market_data.get_spx_candles_via_alpaca(5, 180, now=now)   # different interval, same tick/cache window
    assert len(calls) == 1   # the second call reused the cached 1-minute base fetch


def test_alpaca_data_get_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(market_data._time, "sleep", lambda s: None)
    attempts = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = "too many requests" if status == 429 else "{}"

        def json(self):
            return {}

    def fake_get(url, headers, params, timeout):
        attempts.append(1)
        return Resp(429) if len(attempts) < 2 else Resp(200)

    monkeypatch.setattr(market_data.requests, "get", fake_get)
    data = market_data._alpaca_data_get("/v2/stocks/SPY/bars", {})
    assert data == {}
    assert len(attempts) == 2


def test_alpaca_data_get_gives_up_after_all_retries_on_persistent_429(monkeypatch, caplog):
    caplog.set_level("ERROR")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(market_data._time, "sleep", lambda s: None)
    attempts = []

    class Resp:
        status_code = 429
        text = "too many requests"

    def fake_get(url, headers, params, timeout):
        attempts.append(1)
        return Resp()

    monkeypatch.setattr(market_data.requests, "get", fake_get)
    assert market_data._alpaca_data_get("/v2/stocks/SPY/bars", {}) is None
    assert len(attempts) == config.ALPACA_DATA_RETRIES
    assert any("HTTP 429" in r.message for r in caplog.records)


def test_alpaca_data_get_does_not_retry_a_non_retryable_4xx(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    attempts = []

    class Resp:
        status_code = 404
        text = "not found"

    def fake_get(url, headers, params, timeout):
        attempts.append(1)
        return Resp()

    monkeypatch.setattr(market_data.requests, "get", fake_get)
    assert market_data._alpaca_data_get("/v2/stocks/SPY/bars", {}) is None
    assert len(attempts) == 1   # a real client error isn't retried


# 2026-10-01, post-review (independent adversarial pass before this ever reached Schwab or was
# pushed): three real gaps found -- a non-dict JSON body would reach .get("bars") as an AttributeError
# (nothing downstream catches that), a non-numeric OHLC value would survive _normalize_alpaca_bars'
# guarded block and crash the un-guarded ratio-scaling step, and Alpaca's SPY bars have no regular-
# trading-hours filter the way every other SPX-series call in this file gets (prepost=False), so
# extended-hours prints with no real SPX analog would otherwise reach D/E's rolling EMA/Bollinger
# window. All three fixed; these tests cover each directly.

def test_alpaca_data_get_returns_none_on_non_dict_json_body(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")

    class FakeResp:
        status_code = 200

        def json(self):
            return ["not", "a", "dict"]

    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResp())
    assert market_data._alpaca_data_get("/v2/stocks/SPY/bars", {}) is None


def test_get_spx_candles_via_alpaca_returns_none_on_non_numeric_ohlc_value(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    bad = {"bars": [{"t": "2026-10-01T14:00:00Z", "o": "not-a-number", "h": 76.1, "l": 75.9,
                    "c": 76.0, "v": 1000}]}
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: bad)
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 10.0)
    assert market_data.get_spx_candles_via_alpaca(5, 180) is None


def test_get_spx_candles_via_alpaca_filters_out_extended_hours_bars(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:06:00", tz=config.ET)
    premarket = pd.Timestamp("2026-10-01 08:00:00", tz=config.ET)   # before MARKET_OPEN -- must be dropped
    rth = pd.Timestamp("2026-10-01 09:55:00", tz=config.ET)          # within RTH -- must survive
    page = _alpaca_bars_page([70.0], premarket)
    page["bars"] += _alpaca_bars_page([76.0], rth)["bars"]
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: page)
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: 10.0)
    df = market_data.get_spx_candles_via_alpaca(5, 180, now=now)
    assert len(df) == 1
    assert df["close"].iloc[0] == pytest.approx(760.0)   # the RTH bar only


def test_get_spx_candles_via_alpaca_returns_none_on_network_failure(monkeypatch):
    import requests
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")

    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(market_data.requests, "get", boom)
    assert market_data.get_spx_candles_via_alpaca(5, 180) is None


def test_get_candles_tries_alpaca_first_then_falls_back_to_yfinance_for_spx(monkeypatch):
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: called.append(a) or market_data._normalize(None))
    market_data.get_candles(config.SPX_SYMBOL, "5m", 180)
    assert called and called[0][0] == config.SPX_SYMBOL


def test_get_candles_uses_alpaca_result_when_available_and_skips_yfinance(monkeypatch):
    idx = pd.date_range("2026-10-01 09:00", periods=3, freq="5min", tz=config.ET)
    live = pd.DataFrame({"open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3,
                        "close": [1.0] * 3, "volume": [1] * 3}, index=idx)
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: live)
    called = []
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: called.append(a) or market_data._normalize(None))
    df = market_data.get_candles(config.SPX_SYMBOL, "5m", 180, now=idx[-1] + pd.Timedelta(minutes=5))
    assert not called   # yfinance never invoked -- Alpaca's result was used directly
    assert len(df) == 3


def test_spx_spy_ratio_computes_and_caches_within_the_refresh_window(monkeypatch):
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            calls.append(symbol)
            self.fast_info = {"last_price": 7600.0 if symbol == config.SPX_SYMBOL else 760.0}

    monkeypatch.setattr(market_data.yf, "Ticker", FakeTicker)
    ratio1 = market_data._spx_spy_ratio()
    ratio2 = market_data._spx_spy_ratio()   # within the refresh window -- no new Ticker() calls
    assert ratio1 == pytest.approx(10.0) and ratio2 == pytest.approx(10.0)
    assert calls == [config.SPX_SYMBOL, config.SPY_SYMBOL]


def test_spx_spy_ratio_refreshes_after_the_window_elapses(monkeypatch):
    prices = [(7600.0, 760.0), (7650.0, 765.0)]

    class FakeTicker:
        def __init__(self, symbol):
            spx, spy = prices[0]
            self.fast_info = {"last_price": spx if symbol == config.SPX_SYMBOL else spy}

    monkeypatch.setattr(market_data.yf, "Ticker", FakeTicker)
    mono = [0.0]
    monkeypatch.setattr(market_data._time, "monotonic", lambda: mono[0])
    assert market_data._spx_spy_ratio() == pytest.approx(10.0)
    prices[0] = prices[1]
    mono[0] += config.SPX_SPY_RATIO_REFRESH_MIN * 60 + 1
    assert market_data._spx_spy_ratio() == pytest.approx(10.0)   # still 10.0 (7650/765) after refresh


def test_spx_spy_ratio_keeps_last_known_good_value_when_a_refresh_fails(monkeypatch):
    market_data._SPX_RATIO_CACHE.update({"ratio": 10.0, "at": market_data._time.monotonic()})

    def boom(symbol):
        raise RuntimeError("yfinance unavailable")

    monkeypatch.setattr(market_data.yf, "Ticker", boom)
    mono = [0.0]
    monkeypatch.setattr(market_data._time, "monotonic", lambda: mono[0] + 10_000)   # force staleness
    assert market_data._spx_spy_ratio() == pytest.approx(10.0)


def test_spx_spy_ratio_none_when_nothing_has_ever_succeeded(monkeypatch):
    def boom(symbol):
        raise RuntimeError("yfinance unavailable")

    monkeypatch.setattr(market_data.yf, "Ticker", boom)
    assert market_data._spx_spy_ratio() is None


def test_get_spx_candles_via_alpaca_falls_back_to_the_ratio_constant_when_calibration_never_succeeded(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    now = pd.Timestamp("2026-10-01 10:06:00", tz=config.ET)
    base = pd.Timestamp("2026-10-01 09:55:00", tz=config.ET)
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: _alpaca_bars_page([76.0], base))
    monkeypatch.setattr(market_data, "_spx_spy_ratio", lambda: None)
    df = market_data.get_spx_candles_via_alpaca(5, 180, now=now)
    assert df["close"].iloc[0] == pytest.approx(76.0 * config.SPX_SPY_RATIO_FALLBACK)


def test_get_spx_candles_via_alpaca_reports_feed_health(monkeypatch, caplog):
    caplog.set_level("ERROR")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(market_data, "_alpaca_data_get", lambda path, params: None)
    market_data.get_spx_candles_via_alpaca(5, 180)
    assert any("DATA FEED DOWN" in r.message and config.SPY_SYMBOL in r.message for r in caplog.records)

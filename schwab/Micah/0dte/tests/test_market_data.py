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
    market_data._SPX_LIVE["fail_until"] = 0.0          # a back-off left by one test must not blind the next
    market_data._SPX_SOURCE["state"] = None
    yield
    market_data._CACHE.clear()
    market_data._SPX_LIVE["fail_until"] = 0.0
    market_data._SPX_SOURCE["state"] = None


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
    _clock(monkeypatch, now + timedelta(seconds=5))     # the snapshot was taken just after the last bar closed
    df = market_data.get_es_candles_live(2, 180, now=now)
    assert df is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # 4 one-minute bars resampled to 2-minute bars -> 2 completed candles
    assert len(df) == 2
    assert df["volume"].iloc[0] == 20


def test_get_candles_falls_back_to_yfinance_when_live_es_unavailable(monkeypatch):
    import config
    monkeypatch.setattr(market_data, "get_es_candles_live", lambda *a, **k: None)
    monkeypatch.setattr(market_data, "live_es_available", lambda: False)      # whatever Schwab token this machine happens to hold
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
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "spy")      # these cover the older Alpaca-SPY path (rollback source)
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: called.append(a) or market_data._normalize(None))
    market_data.get_candles(config.SPX_SYMBOL, "5m", 180)
    assert called and called[0][0] == config.SPX_SYMBOL


def test_get_candles_uses_alpaca_result_when_available_and_skips_yfinance(monkeypatch):
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "spy")      # these cover the older Alpaca-SPY path (rollback source)
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


# ---------------------------------------------------------------------------------------------------
# 2026-10-08: the live-ES path, hardened (ASTRA-only audit): bounded request with a timeout, complete candles only, a failed request
# turns the feed off for a while instead of silently substituting the ~10 min delayed Yahoo series, every state change is logged once.

import types
from datetime import datetime, timedelta

from conftest import et


@pytest.fixture(autouse=True)
def _reset_es_live_state():
    market_data._ES_LIVE.update(state=None, fail_until=0.0)
    yield
    market_data._ES_LIVE.update(state=None, fail_until=0.0)


def _clock(monkeypatch, wall: datetime) -> None:
    """Pin market_data's wall clock (time.time) to `wall`; monotonic/sleep stay real."""
    import time as real
    monkeypatch.setattr(market_data, "_time", types.SimpleNamespace(time=lambda: wall.timestamp(), monotonic=real.monotonic,
                                                                    sleep=real.sleep))


def _minute_rows(first: datetime, n: int, skip=()) -> list[dict]:
    rows = []
    for i in range(n):
        t = first + timedelta(minutes=i)
        if t in skip:
            continue
        rows.append({"open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 10,
                     "datetime": int(t.timestamp() * 1000)})
    return rows


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Client:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.calls = body, error, []

    def get_price_history_every_minute(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return _Resp(self.body)


def _valid_token_config(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    _write_token(token_path)
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(token_path))


def test_the_read_only_session_carries_a_default_timeout(monkeypatch, tmp_path):
    _valid_token_config(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **k: seen.append(k) or types.SimpleNamespace(status_code=200))
    client = market_data._schwab_read_only_client()
    client.session.get("https://example.invalid/x")
    client.session.get("https://example.invalid/y", timeout=1)
    assert seen[0]["timeout"] == (3.0, 8.0) and seen[1]["timeout"] == 1


def test_the_history_request_is_bounded_to_a_day_ending_now(monkeypatch):
    now = et(9, 40)
    client = _Client({"candles": _minute_rows(et(9, 30), 10)})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    _clock(monkeypatch, now + timedelta(seconds=5))
    market_data.get_es_candles_live(2, 180, now=now)
    kwargs = client.calls[0][1]
    assert kwargs["end_datetime"] == now and kwargs["start_datetime"] == now - timedelta(minutes=1440)
    assert kwargs["need_extended_hours_data"] is True


def test_live_es_candles_are_complete_ones_only(monkeypatch):
    now = et(10, 0)
    # 09:56 .. 09:58 present, the 09:59 bar is missing: the 09:58 two-minute candle has one of its two bars
    client = _Client({"candles": _minute_rows(et(9, 56), 4, skip={et(9, 59)})})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    _clock(monkeypatch, now + timedelta(seconds=5))
    df = market_data.get_es_candles_live(2, 180, now=now)
    assert list(df.index) == [et(9, 56)]                                   # the half-built 09:58 candle is withheld
    market_data._CACHE.clear()
    full = _Client({"candles": _minute_rows(et(9, 56), 4)})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: full)
    assert list(market_data.get_es_candles_live(2, 180, now=now).index) == [et(9, 56), et(9, 58)]


def test_a_snapshot_taken_before_the_last_bar_settled_does_not_emit_the_candle(monkeypatch):
    now = et(10, 0, 3)
    client = _Client({"candles": _minute_rows(et(9, 56), 4)})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    _clock(monkeypatch, et(9, 59, 59))                                     # fetched while the 09:59 bar was still forming
    assert list(market_data.get_es_candles_live(2, 180, now=now).index) == [et(9, 56)]


def test_a_failed_request_turns_live_es_off_for_a_while_and_says_so_once(monkeypatch, tmp_path, caplog):
    from schwab.client import Client
    caplog.set_level("INFO")
    _valid_token_config(monkeypatch, tmp_path)

    def boom(self, *a, **k):
        raise RuntimeError("network error")
    monkeypatch.setattr(Client, "get_price_history_every_minute", boom)
    assert market_data.live_es_available() is True
    assert market_data.get_es_candles_live(2, 180, now=et(9, 40)) is None
    assert market_data.live_es_available() is False                        # the proxy takes over at once
    assert market_data.get_es_candles_live(2, 180, now=et(9, 41)) is None  # no second request while it is off
    lines = [r for r in caplog.records if "LIVE_ES" in r.getMessage()]
    assert len(lines) == 1 and lines[0].keep is True and "RuntimeError" in lines[0].getMessage()
    market_data._ES_LIVE["fail_until"] = 0.0                               # the retry window is over
    assert market_data.live_es_available() is True


def test_candles_never_fall_back_to_delayed_yahoo_es_when_live_es_was_usable(monkeypatch, tmp_path):
    from schwab.client import Client
    _valid_token_config(monkeypatch, tmp_path)
    monkeypatch.setattr(Client, "get_price_history_every_minute", lambda self, *a, **k: _Resp({"error": "unauthorized"}))
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: pytest.fail("delayed ES=F must not stand in for live ES"))
    out = market_data.get_candles(config.ES_SYMBOL, "2m", 180, et(9, 40))
    assert out.empty and list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert market_data.live_es_available() is False


def test_live_es_not_usable_at_all_still_reads_yahoo_for_an_explicit_es_source(monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    called = []
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: called.append(a) or market_data._normalize(None))
    market_data.get_candles(config.ES_SYMBOL, "2m", 180, et(9, 40))
    assert called


@pytest.mark.parametrize("body", [[], "nope", None, {"candles": None}, {"candles": [{"not": "a candle"}]}])
def test_a_malformed_history_body_is_a_failure_not_a_crash(monkeypatch, body):
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: _Client(body))
    assert market_data.get_es_candles_live(2, 180, now=et(9, 40)) is None
    assert market_data._ES_LIVE["fail_until"] > 0


def test_a_steady_live_es_state_is_silent_and_a_change_is_logged(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    for _ in range(3):
        market_data._schwab_read_only_client()
    off = [r for r in caplog.records if "LIVE_ES off" in r.getMessage()]
    assert len(off) == 1 and "not configured" in off[0].getMessage() and off[0].keep is True
    _valid_token_config(monkeypatch, tmp_path)
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: _Client({"candles": _minute_rows(et(9, 30), 10)}))
    _clock(monkeypatch, et(9, 40, 5))
    market_data.get_es_candles_live(2, 180, now=et(9, 40))
    market_data._CACHE.clear()
    market_data.get_es_candles_live(2, 180, now=et(9, 40))
    assert len([r for r in caplog.records if "LIVE_ES on" in r.getMessage()]) == 1


def test_live_es_status_says_why_it_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    assert market_data.live_es_status() == (False, "SCHWAB_APP_KEY / SCHWAB_TOKEN_PATH not configured")
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(tmp_path / "missing.json"))
    ok, why = market_data.live_es_status()
    assert ok is False and why.startswith("token file not found")
    token_path = tmp_path / "token.json"
    _write_token(token_path, expires_at=__import__("time").time() - 5)
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(token_path))
    ok, why = market_data.live_es_status()
    assert ok is False and "expired" in why and "Schwab bot" in why
    _write_token(token_path)
    assert market_data.live_es_status()[0] is True
    token_path.write_text("{not json")
    ok, why = market_data.live_es_status()
    assert ok is False and "unreadable" in why


def test_the_status_check_logs_nothing_and_builds_no_state(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "")
    market_data.live_es_status()
    assert [r for r in caplog.records if "LIVE_ES" in r.getMessage()] == [] and market_data._ES_LIVE["state"] is None


# ---- review follow-ups (2026-10-08)

class _StatusResp(_Resp):
    def __init__(self, body, status, error=None):
        super().__init__(body)
        self.status_code, self.error = status, error

    def json(self):
        if self.error:
            raise self.error
        return self.body


def test_a_response_without_candles_names_its_http_status(monkeypatch):
    client = _Client()
    client.get_price_history_every_minute = lambda *a, **k: _StatusResp({"errors": [{"status": 403}]}, 403)
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    assert market_data.get_es_candles_live(2, 180, now=et(9, 40)) is None
    assert market_data._ES_LIVE["state"] == (False, "Schwab /ES returned no candles (HTTP 403)")
    market_data._ES_LIVE.update(state=None, fail_until=0.0)
    client.get_price_history_every_minute = lambda *a, **k: _StatusResp(None, 401, ValueError("not json"))
    assert market_data.get_es_candles_live(2, 180, now=et(9, 40)) is None
    assert market_data._ES_LIVE["state"] == (False, "Schwab /ES request failed (ValueError, HTTP 401)")


def test_a_stalled_live_es_series_counts_as_a_failure_not_as_live(monkeypatch):
    client = _Client({"candles": _minute_rows(et(9, 0), 36)})                  # the newest bar is 09:35
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    now = et(9, 50, 3)
    _clock(monkeypatch, now)
    assert market_data.get_es_candles_live(2, 180, now=now) is None
    assert market_data._ES_LIVE["state"] == (False, "Schwab /ES bars are 15 min old")
    assert market_data.live_es_backoff_active() is True


def test_a_series_that_is_a_few_minutes_old_is_still_live_and_the_check_is_for_the_session_only(monkeypatch):
    now = et(9, 40)
    client = _Client({"candles": _minute_rows(et(9, 30), 7)})                  # newest bar 09:36: four minutes old
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    _clock(monkeypatch, now + timedelta(seconds=5))
    assert market_data.get_es_candles_live(2, 180, now=now) is not None and market_data._ES_LIVE["state"] == (True, "")
    market_data._CACHE.clear()
    evening = datetime(2026, 10, 7, 18, 30, tzinfo=et(9, 0).tzinfo)            # overnight: a quiet tape is not a stall
    old = _Client({"candles": _minute_rows(et(17, 0), 30)})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: old)
    _clock(monkeypatch, evening)
    assert market_data.get_es_candles_live(2, 180, now=evening) is not None


def test_the_live_es_request_is_cached_and_refreshed_when_the_minute_rolls(monkeypatch):
    client = _Client({"candles": _minute_rows(et(9, 30), 10)})
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    now = et(9, 40)
    _clock(monkeypatch, et(9, 40, 5))
    market_data.get_es_candles_live(2, 180, now=now)
    _clock(monkeypatch, et(9, 40, 15))
    market_data.get_es_candles_live(2, 180, now=now)
    assert len(client.calls) == 1                                              # inside DATA_CACHE_SEC and the same minute: one request
    _clock(monkeypatch, et(9, 41, 3))                                          # the minute rolled and settled: ask again
    market_data.get_es_candles_live(2, 180, now=et(9, 41))
    assert len(client.calls) == 2
    _clock(monkeypatch, et(9, 41, 40))                                         # more than DATA_CACHE_SEC old
    market_data.get_es_candles_live(2, 180, now=et(9, 41, 40))
    assert len(client.calls) == 3


def test_a_token_that_expires_within_thirty_seconds_is_not_usable(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    _write_token(token_path, expires_at=__import__("time").time() + 20)
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(token_path))
    ok, why = market_data.live_es_status()
    assert ok is False and "expired" in why
    _write_token(token_path, expires_at=__import__("time").time() + 120)
    assert market_data.live_es_status()[0] is True


def test_live_es_backoff_active_follows_the_failure_window():
    assert market_data.live_es_backoff_active() is False
    market_data._es_failed("x")
    assert market_data.live_es_backoff_active() is True
    market_data._ES_LIVE["fail_until"] = 0.0
    assert market_data.live_es_backoff_active() is False


# ---------------------------------------------------------------------------------------------------
# 2026-11-10: SPX data from Schwab's own $SPX price history (SPX_CANDLE_SOURCE "schwab"), Yahoo the moment it is not readable.

def _spx_minute_bars(start_ms: int, n: int, base: float = 7800.0) -> list[dict]:
    return [{"open": base + i, "high": base + i + 1, "low": base + i - 1, "close": base + i + 0.5, "volume": 10,
             "datetime": start_ms + i * 60_000} for i in range(n)]


class _SpxResp:
    status_code = 200

    def __init__(self, candles):
        self._c = candles

    def json(self):
        return {"candles": self._c}


class _SpxClient:
    def __init__(self, candles):
        self.candles, self.calls = candles, []

    def get_price_history_every_minute(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return _SpxResp(self.candles)


def test_the_schwab_spx_series_is_read_regular_hours_only_and_cached(monkeypatch):
    from datetime import datetime, timedelta
    now = datetime(2026, 11, 10, 9, 50, 5, tzinfo=config.ET)
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    client = _SpxClient(_spx_minute_bars(start_ms, 20))
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "schwab")
    got = market_data._spx_1m_schwab(now)
    assert got is not None
    df1, fetched = got
    assert len(df1) == 20 and list(df1.columns) == ["open", "high", "low", "close", "volume"]
    assert client.calls[0][0] == "$SPX" and client.calls[0][1]["need_extended_hours_data"] is False
    market_data._spx_1m_schwab(now)
    assert len(client.calls) == 1                                        # cached within DATA_CACHE_SEC


def test_the_spx_series_falls_back_to_yahoo_when_schwab_cannot_be_read(monkeypatch, caplog):
    import logging
    from datetime import datetime
    now = datetime(2026, 11, 10, 9, 50, 5, tzinfo=config.ET)
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "schwab")
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: None)
    yahoo = (pd.DataFrame(columns=market_data._COLUMNS), 123.0)
    monkeypatch.setattr(market_data, "_spx_1m", lambda: yahoo)
    with caplog.at_level(logging.WARNING):
        assert market_data._spx_series(now) == yahoo
    assert any("SPX data: Yahoo" in r.getMessage() for r in caplog.records)

    class Boom:
        def get_price_history_every_minute(self, *a, **k):
            raise requests.ConnectionError("down")
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: Boom())
    market_data._CACHE.clear()
    assert market_data._spx_series(now) == yahoo                          # a failing request is also just "use Yahoo"


def test_yahoo_source_never_touches_schwab(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "yahoo")
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: (_ for _ in ()).throw(AssertionError("not called")))
    yahoo = (pd.DataFrame(columns=market_data._COLUMNS), 1.0)
    monkeypatch.setattr(market_data, "_spx_1m", lambda: yahoo)
    assert market_data._spx_series(datetime(2026, 11, 10, 10, 0, tzinfo=config.ET)) == yahoo


def test_the_opening_range_and_spot_read_the_schwab_series(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "schwab")
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 15)
    now = datetime(2026, 11, 10, 9, 46, 5, tzinfo=config.ET)
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    bars = _spx_minute_bars(start_ms, 16)
    bars[4]["close"] = 7900.0                                             # the 09:30 candle closes at the range's body high
    bars[3]["high"], bars[7]["low"] = 9999.0, 1.0                         # wicks: ignored (the range is candle bodies only)
    bars[15]["open"] = 9999.0                                             # 09:45 is outside the range
    client = _SpxClient(bars)
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    monkeypatch.setattr(market_data._time, "time", lambda: now.timestamp())
    levels = market_data.get_opening_range(now)
    assert levels is not None and levels.high == 7900.0 and levels.low == pytest.approx(7800.0)   # the 09:30 candle's open
    assert levels.established_at == datetime(2026, 11, 10, 9, 45, tzinfo=config.ET)
    assert market_data.get_spot(config.SPX_SYMBOL, now) == pytest.approx(7815.5)
    assert len(client.calls) == 1                                         # one series for both


def test_overnight_levels_and_basis_use_live_es_when_available(monkeypatch):
    from datetime import datetime, timedelta
    now = datetime(2026, 11, 10, 9, 40, tzinfo=config.ET)
    es_start = datetime(2026, 11, 9, 18, 0, tzinfo=config.ET)
    es_bars = pd.DataFrame([{"open": 7850.0, "high": 7850.0 + (5 if i == 100 else 0), "low": 7840.0 - (7 if i == 200 else 0),
                             "close": 7845.0, "volume": 1.0} for i in range(16 * 60)],
                           index=pd.DatetimeIndex([es_start + timedelta(minutes=i) for i in range(16 * 60)]))
    monkeypatch.setattr(market_data, "live_es_available", lambda: True)
    monkeypatch.setattr(market_data, "_es_1m_live", lambda lookback, when: (es_bars, now.timestamp()))
    monkeypatch.setattr(market_data, "_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Yahoo must not be read")))
    levels = market_data.get_overnight_levels(now)
    assert levels is not None and levels.high == 7855.0 and levels.low == 7833.0
    spx = pd.DataFrame([{"open": 1, "high": 1, "low": 1, "close": 7900.0, "volume": 1}],
                       index=pd.DatetimeIndex([datetime(2026, 11, 10, 9, 30, tzinfo=config.ET)]))
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (spx, now.timestamp()))
    assert market_data.get_spx_es_basis(now) == pytest.approx(7900.0 - 7845.0)


def test_get_candles_routes_spx_candles_through_schwab_in_schwab_mode(monkeypatch):
    from datetime import datetime
    now = datetime(2026, 11, 10, 9, 50, 5, tzinfo=config.ET)
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    client = _SpxClient(_spx_minute_bars(start_ms, 20))
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "schwab")
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: client)
    monkeypatch.setattr(market_data, "_spx_1m", lambda: pytest.fail("Yahoo must not be read"))
    monkeypatch.setattr(market_data, "get_spx_candles_via_alpaca", lambda *a, **k: pytest.fail("Alpaca must not be read"))
    monkeypatch.setattr(market_data._time, "time", lambda: now.timestamp())
    df = market_data.get_candles(config.SPX_SYMBOL, "5m", 180, now)
    assert len(df) == 4 and df["high"].iloc[0] == pytest.approx(7805.0)     # 20 one-minute bars -> four complete 5-minute candles
    assert df["volume"].iloc[0] == 50


def test_a_failed_schwab_spx_request_backs_off_and_a_stalled_series_counts_as_failed(monkeypatch):
    from datetime import datetime, timedelta
    now = datetime(2026, 11, 10, 10, 0, 5, tzinfo=config.ET)
    monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "schwab")
    monkeypatch.setattr(config, "SPX_SCHWAB_RETRY_SEC", 60, raising=False)

    class Boom:
        def __init__(self):
            self.calls = 0

        def get_price_history_every_minute(self, *a, **k):
            self.calls += 1
            raise requests.ConnectionError("down")
    boom = Boom()
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: boom)
    market_data._SPX_LIVE["fail_until"] = 0.0
    assert market_data._spx_1m_schwab(now) is None
    assert market_data._spx_1m_schwab(now) is None
    assert market_data._spx_1m_schwab(now) is None
    assert boom.calls == 1                                                    # one failed request, then back-off
    market_data._SPX_LIVE["fail_until"] = 0.0
    stale_start = int((now - timedelta(minutes=30)).timestamp() * 1000)
    stalled = _SpxClient(_spx_minute_bars(stale_start, 10))                   # newest bar 20 minutes old, in session
    monkeypatch.setattr(market_data, "_schwab_read_only_client", lambda: stalled)
    assert market_data._spx_1m_schwab(now) is None and len(stalled.calls) == 1
    assert market_data._SPX_LIVE["fail_until"] > 0
    market_data._SPX_LIVE["fail_until"] = 0.0


def test_the_opening_range_waits_for_its_last_five_minute_candle(monkeypatch):
    """The 09:40 candle is complete only with its 09:44 bar; until then (or CANDLE_GAP_TOLERANCE_SEC after the window) the range waits."""
    from datetime import datetime
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 15)
    monkeypatch.setattr(config, "CANDLE_GAP_TOLERANCE_SEC", 180)
    now = datetime(2026, 11, 10, 9, 45, 5, tzinfo=config.ET)
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    bars = _spx_minute_bars(start_ms, 14)                                     # 09:30..09:43 -- the 09:44 bar has not arrived (09:40 candle incomplete)
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (market_data._es_minutes(bars), now.timestamp()))
    assert market_data.get_opening_range(now) is None                        # waits
    later = datetime(2026, 11, 10, 9, 48, 30, tzinfo=config.ET)              # 3.5 min after the window: the gap is accepted
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (market_data._es_minutes(bars), later.timestamp()))
    levels = market_data.get_opening_range(later)
    assert levels is not None and levels.high == pytest.approx(7813.5)      # the body high: the 09:43 close (bodies only, no wicks)
    bars15 = _spx_minute_bars(start_ms, 15)
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (market_data._es_minutes(bars15), now.timestamp()))
    assert market_data.get_opening_range(now).high == pytest.approx(7814.5)  # complete: accepted at once (the 09:44 close)


def test_the_opening_range_is_the_whole_window_long_after_the_open(monkeypatch):
    """A restart at 12:40 ET rebuilds the 09:30-09:45 range from the day's series: the candle lookback never drops its first candles
    (CANDLE_LOOKBACK_MIN alone would leave the 09:40 candle only and a wrong range)."""
    from datetime import datetime
    monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 15)
    monkeypatch.setattr(config, "CANDLE_LOOKBACK_MIN", 180)
    now = datetime(2026, 11, 10, 12, 40, 5, tzinfo=config.ET)
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    bars = _spx_minute_bars(start_ms, 190)                                    # 09:30..12:39
    bars[0]["open"] = 7790.0                                                  # the 09:30 candle opens at the range low
    bars[4]["close"] = 7890.0                                                 # ... and closes at the range high
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (market_data._es_minutes(bars), now.timestamp()))
    levels = market_data.get_opening_range(now)
    assert levels is not None and levels.high == pytest.approx(7890.0) and levels.low == pytest.approx(7790.0)
    assert levels.established_at == datetime(2026, 11, 10, 9, 45, tzinfo=config.ET)


def test_an_index_candle_without_volume_is_still_a_candle():
    from datetime import datetime
    start_ms = int(datetime(2026, 11, 10, 9, 30, tzinfo=config.ET).timestamp() * 1000)
    bars = [{k: v for k, v in b.items() if k != "volume"} for b in _spx_minute_bars(start_ms, 3)]
    df = market_data._es_minutes(bars)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and (df["volume"] == 0).all()


def test_the_feeds_read_the_token_the_broker_is_using(monkeypatch, tmp_path):
    """The broker may have switched to a sibling folder's newer login: the feeds follow it instead of the folder's own stale file."""
    own = tmp_path / "token.json"                                            # the folder's own file: expired
    _write_token(own, expires_at=__import__("time").time() - 10)
    sibling = tmp_path / "sibling" / "token.json"
    sibling.parent.mkdir()
    _write_token(sibling, access_token="newer-login")
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "key")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(own))
    try:
        market_data.set_token_path_provider(None)
        assert market_data._schwab_read_only_client() is None                # own file only: expired
        market_data.set_token_path_provider(lambda: sibling)
        client = market_data._schwab_read_only_client()
        assert client is not None and client.session.headers["Authorization"] == "Bearer newer-login"
        market_data.set_token_path_provider(lambda: (_ for _ in ()).throw(RuntimeError("broker gone")))
        assert market_data._token_path() == str(own)                         # a failing provider falls back to the config path
        market_data.set_token_path_provider(lambda: None)
        assert market_data._token_path() == str(own)
    finally:
        market_data.set_token_path_provider(None)


def test_the_basis_uses_complete_minutes_only(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(config, "CANDLE_SETTLE_SEC", 2.0)
    open_dt = datetime(2026, 11, 10, 9, 30, tzinfo=config.ET)
    idx = pd.DatetimeIndex([open_dt, open_dt + timedelta(minutes=1)])
    spx = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [7900.0, 7910.0], "volume": [1, 1]}, index=idx)
    es = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [7845.0, 7850.0], "volume": [1, 1]}, index=idx)
    monkeypatch.setattr(market_data, "_spx_series", lambda when=None: (spx, 0.0))
    monkeypatch.setattr(market_data, "_es_minutes_any", lambda when: es)
    assert market_data.get_spx_es_basis(open_dt + timedelta(seconds=1)) is None          # the 09:30 bar is still forming
    assert market_data.get_spx_es_basis(open_dt + timedelta(minutes=1, seconds=1)) is None   # not settled yet (2 s)
    assert market_data.get_spx_es_basis(open_dt + timedelta(minutes=1, seconds=3)) == pytest.approx(55.0)   # the 09:30 bar

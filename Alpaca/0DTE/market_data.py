"""yfinance market data: ES overnight levels, SPX/ES basis, completed candles, opening range.

Every function takes `now` (tz-aware ET) so tests can inject time. Downloads are
cached for config.DATA_CACHE_SEC to avoid hammering Yahoo on a 15 s tick.
"""

import logging
import time as _time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

import config
from strategy import Levels, at_time

log = logging.getLogger(__name__)

_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_COLUMNS = ["open", "high", "low", "close", "volume"]
# 2026-10-01: per-feed (symbol, interval) health tracking for _download's loud failure logging --
# {"healthy": bool, "last_notice": monotonic float} -- see config.py's DATA_DOWNLOAD_RETRIES section.
_FEED_STATE: dict[tuple, dict] = {}

# 2026-09-25: optional real-time ES via a Schwab account with futures data entitlement, reusing the
# same schwab-py client the Schwab broker folder already authenticates with (cross-broker DATA-only
# credentials in THIS folder's .env, mirroring the existing reverse pattern where the Schwab folder
# borrows Alpaca's indicative SPX quotes). yfinance's ES=F is ~10 minutes delayed -- fine for the
# overnight LEVEL (a static number established once, well before the open), but not fast enough for
# a genuine live ES-leads-SPX confirmation. Every function below fails soft: any missing credential,
# import error, or API error just returns None, and the caller falls back to the existing yfinance
# path -- this must never be able to crash the bot over an optional speed upgrade.
_SCHWAB_CLIENT = None   # None = not yet tried, False = tried and failed (don't retry every tick)


def _schwab_client():
    global _SCHWAB_CLIENT
    if _SCHWAB_CLIENT is not None:
        return _SCHWAB_CLIENT or None
    # Reads through config (not a raw os.getenv) so this picks up the SAME SCHWAB_APP_KEY/SECRET/
    # TOKEN_PATH resolution config.py already does (including its own sensible per-folder default
    # for TOKEN_PATH) -- a direct os.getenv here would silently miss credentials that are only set
    # via config.py's fallback, not literally present as an environment variable.
    app_key = getattr(config, "SCHWAB_APP_KEY", "").strip()
    app_secret = getattr(config, "SCHWAB_APP_SECRET", "").strip()
    token_path = getattr(config, "SCHWAB_TOKEN_PATH", "").strip()
    if not (app_key and app_secret and token_path):
        _SCHWAB_CLIENT = False
        return None
    try:
        from schwab.auth import client_from_token_file
        _SCHWAB_CLIENT = client_from_token_file(token_path, app_key, app_secret)
    except Exception as e:
        log.warning("Schwab client unavailable for live ES data (falling back to delayed yfinance "
                    "ES=F): %s", e)
        _SCHWAB_CLIENT = False
        return None
    return _SCHWAB_CLIENT


def live_es_available() -> bool:
    """True once this folder's Schwab cross-auth is configured and working -- lets callers switch
    from the ES_TO_SPX proxy (confirm on live SPX candles against a basis-shifted ES level) to
    confirming directly on ES's own (now genuinely live) candles for the real lead-time edge."""
    return _schwab_client() is not None


def get_es_candles_live(interval_min: int, lookback_min: int, now: datetime | None = None) -> pd.DataFrame | None:
    """Real ES futures candles via Schwab (config.ES_SCHWAB_SYMBOL, e.g. "/ES"), resampled to
    interval_min. Returns None -- never raises -- if Schwab cross-auth isn't configured in this
    folder's .env or the call fails for any reason; get_candles() falls back to yfinance in that case."""
    client = _schwab_client()
    if client is None:
        return None
    now = _now(now)
    # Cached like _download's yfinance calls: at TICK_SECONDS=1 (fast-as-Alpaca-allows polling) an
    # uncached call here would hit Schwab's API every single tick across every running persona --
    # this keeps it to at most one real request per DATA_CACHE_SEC regardless of tick rate.
    cache_key = ("es_live", config.ES_SCHWAB_SYMBOL)
    hit = _CACHE.get(cache_key)
    if hit and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC:
        candles = hit[1]
    else:
        try:
            resp = client.get_price_history_every_minute(config.ES_SCHWAB_SYMBOL, need_extended_hours_data=True)
            data = resp.json()
        except Exception as e:
            log.warning("Schwab ES price history failed (falling back to delayed yfinance ES=F): %s", e)
            return None
        candles = data.get("candles") or []
        _CACHE[cache_key] = (_time.monotonic(), candles)
    if not candles:
        return None
    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(config.ET)
    df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]].sort_index()
    if interval_min > 1:
        df = df.resample(f"{interval_min}min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    df = df[(df.index >= now - timedelta(minutes=lookback_min)) & (df.index + timedelta(minutes=interval_min) <= now)]
    return df


# 2026-10-01: real-time SPX-equivalent candles via Alpaca's own SPY bars (see config.py's SPY_SYMBOL
# section for the full rationale -- Alpaca has no genuine SPX index bars on this plan). Every function
# below fails soft: a missing credential, network error, or unexpected response shape just returns
# None, and get_candles() falls back to the existing yfinance ^GSPC path -- same contract as the
# existing ES live-feed fallback. This must never be able to crash the bot over an optional feed swap.
_SPX_RATIO_CACHE: dict = {"ratio": None, "at": 0.0}   # monotonic-timestamped; see _spx_spy_ratio


def _alpaca_creds() -> tuple[str, str] | None:
    key = getattr(config, "ALPACA_API_KEY", "").strip()
    secret = getattr(config, "ALPACA_SECRET_KEY", "").strip()
    return (key, secret) if key and secret else None


def _alpaca_data_get(path: str, params: dict) -> dict | None:
    """2026-10-01: a 429/5xx from Alpaca's data API is often transient -- the SAME class of failure
    broker.py's own _request() already retries with backoff for the trading API -- so this retries
    in-call too rather than immediately reporting the feed down on the very first throttle. Real live
    evidence this matters: moving SPX candles off yfinance onto Alpaca (this same function) pushed
    Alpaca's OWN rate limit hard enough to also throw a 429 on the bot's unrelated trading-API calls
    (broker.py's option-chain/account polling, same account) -- a 429 here is retried immediately
    instead of adding to that same burst of near-simultaneous requests via get_candles()'s yfinance
    fallback path."""
    creds = _alpaca_creds()
    if creds is None:
        return None
    key, secret = creds
    base = getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    resp = None
    for attempt in range(config.ALPACA_DATA_RETRIES):
        try:
            resp = requests.get(f"{base}{path}", headers=headers, params=params, timeout=10)
        except requests.RequestException as e:
            log.warning("Alpaca data API %s failed (falling back to yfinance): %s", path, e)
            return None
        if resp.status_code == 200:
            break
        if resp.status_code != 429 and resp.status_code < 500:
            break   # a real client error (4xx other than 429) won't be fixed by retrying
        if attempt < config.ALPACA_DATA_RETRIES - 1:
            _time.sleep(config.ALPACA_DATA_RETRY_SEC * (attempt + 1))
    if resp.status_code != 200:
        if resp.status_code == 429 or resp.status_code >= 500:
            # ERROR + keep=True (not just a filtered warning) -- the generic "DATA FEED DOWN" line
            # alone doesn't say WHY, and a rate limit on this SAME account is a distinct, actionable
            # signal (it can also start affecting real order/chain calls) worth surfacing on its own.
            log.error("Alpaca data API %s returned HTTP %d after retries (falling back to yfinance): %s",
                     path, resp.status_code, resp.text[:200], extra={"keep": True})
        else:
            log.warning("Alpaca data API %s returned HTTP %d (falling back to yfinance): %s",
                       path, resp.status_code, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError as e:
        log.warning("Alpaca data API %s returned unparseable JSON (falling back to yfinance): %s", path, e)
        return None
    # 2026-10-01, post-review: a malformed body (a JSON array/string/number instead of an object --
    # a proxy/WAF error page, an unexpected endpoint response) would otherwise reach .get("bars") in
    # the caller as an AttributeError, which nothing downstream catches (the tick loop only catches
    # BrokerError) -- checked once, here, so every current and future caller of this function is covered.
    if not isinstance(data, dict):
        log.warning("Alpaca data API %s returned a non-object JSON body (%s) (falling back to yfinance)",
                   path, type(data).__name__)
        return None
    return data


def _spx_spy_ratio() -> float | None:
    """SPX/SPY ratio, refreshed at most every SPX_SPY_RATIO_REFRESH_MIN via ONE lightweight yfinance
    single-price lookup (not a bulk candle download -- see config.py). Returns the last known-good
    ratio if a refresh attempt fails or produces a nonsensical reading, and None only if no refresh has
    EVER succeeded (caller then falls back to SPX_SPY_RATIO_FALLBACK)."""
    stale = _time.monotonic() - _SPX_RATIO_CACHE["at"] >= config.SPX_SPY_RATIO_REFRESH_MIN * 60
    if _SPX_RATIO_CACHE["ratio"] is not None and not stale:
        return _SPX_RATIO_CACHE["ratio"]
    try:
        spx = yf.Ticker(config.SPX_SYMBOL).fast_info["last_price"]
        spy = yf.Ticker(config.SPY_SYMBOL).fast_info["last_price"]
        if spx and spy and float(spx) > 0 and float(spy) > 0:
            _SPX_RATIO_CACHE["ratio"] = float(spx) / float(spy)
            _SPX_RATIO_CACHE["at"] = _time.monotonic()
        else:
            log.warning("SPX/SPY ratio calibration got a non-positive price (spx=%s spy=%s); keeping "
                       "last known ratio", spx, spy)
    except Exception as e:
        log.warning("SPX/SPY ratio calibration failed (%s); keeping last known ratio", e)
    return _SPX_RATIO_CACHE["ratio"]


def _fetch_alpaca_spy_bars_1min(now: datetime) -> pd.DataFrame | None:
    """Raw 1-MINUTE SPY bars (not yet scaled to SPX terms), cached for DATA_CACHE_SEC. Every interval
    get_spx_candles_via_alpaca needs (1m/2m/5m today) resamples locally from this SAME base fetch
    rather than each making its own separate Alpaca request -- 2026-10-01, same day as the original
    yfinance incident: moving SPX candles onto Alpaca immediately pushed Alpaca's OWN rate limit hard
    enough to also 429 this bot's unrelated trading-API calls (same account). One base request per
    cache window instead of one per distinct interval cuts that candle-request volume roughly 3x.
    None on any failure -- caller reports feed health and falls back to yfinance."""
    cache_key = ("spx_via_spy", 1)
    hit = _CACHE.get(cache_key)
    if hit and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC:
        return hit[1]
    # A generous CALENDAR-day lookback (not lookback_min) mirrors _download's own "5d" yfinance period
    # for the same reason: a large lookback_min (D/E's multi-session EMA/Bollinger window) needs to
    # reach back far enough on a Monday morning, and every caller filters down to its own lookback_min
    # afterward regardless -- the extra data is free. Derived from D_E_LOOKBACK_MIN (the largest real
    # lookback_min any caller uses today) plus a 2-day weekend buffer, floored at 6, rather than an
    # independent hardcoded figure that could silently under-fetch if D_E_LOOKBACK_MIN grows later.
    fetch_days = max(6, -(-config.D_E_LOOKBACK_MIN // (24 * 60)) + 2)   # ceil-div minutes -> days
    start = now - timedelta(days=fetch_days)
    params = {"timeframe": "1Min", "start": start.isoformat(), "end": now.isoformat(),
              "feed": "iex", "limit": 10000, "adjustment": "raw"}
    rows: list[dict] = []
    for _ in range(20):   # hard cap on pagination -- never loop forever on a malformed token
        data = _alpaca_data_get(f"/v2/stocks/{config.SPY_SYMBOL}/bars", params)
        if data is None:
            return None
        bars = data.get("bars")
        if not isinstance(bars, list):
            log.warning("Alpaca bars response missing/malformed 'bars' list (falling back to yfinance)")
            return None
        rows.extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
        params = {**params, "page_token": token}
    try:
        df = _normalize_alpaca_bars(rows)
    except (KeyError, ValueError, TypeError) as e:
        log.warning("Alpaca bars response had an unexpected shape (falling back to yfinance): %s", e)
        return None
    _CACHE[cache_key] = (_time.monotonic(), df)
    return df


def get_spx_candles_via_alpaca(interval_min: int, lookback_min: int, now: datetime | None = None) -> pd.DataFrame | None:
    """SPX-equivalent candles: real 1-minute SPY bars via Alpaca's market data API (free IEX feed),
    resampled to interval_min and scaled up by the periodically-calibrated SPX/SPY ratio. Returns
    None -- never raises -- if Alpaca credentials aren't configured or the call fails for any reason;
    get_candles() falls back to yfinance in that case. Reports its own up/down transitions (same
    _report_feed_health used for the yfinance path) so a broken Alpaca credential/feed doesn't stay
    invisible just because yfinance happens to still work."""
    if _alpaca_creds() is None:
        return None
    now = _now(now)
    df = _fetch_alpaca_spy_bars_1min(now)
    _report_feed_health((config.SPY_SYMBOL, "1Min"), df is not None, None)
    if df is None:
        return None
    if df.empty:
        return df
    if interval_min > 1:
        df = df.resample(f"{interval_min}min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    ratio = _spx_spy_ratio() or config.SPX_SPY_RATIO_FALLBACK
    scaled = df.copy()
    for col in ("open", "high", "low", "close"):
        scaled[col] = scaled[col] * ratio
    scaled = scaled[(scaled.index >= now - timedelta(minutes=lookback_min))
                    & (scaled.index + timedelta(minutes=interval_min) <= now)]
    return scaled


def _normalize_alpaca_bars(rows: list[dict]) -> pd.DataFrame:
    """Alpaca bar fields: t (RFC-3339), o/h/l/c/v. Raises KeyError/ValueError/TypeError on an
    unexpected shape -- the caller catches that and fails soft to yfinance rather than ever proceeding
    with reinterpreted/garbage candles. 2026-10-01, post-review: pd.to_numeric(errors="raise") forces
    a non-numeric OHLCV value (a string, a nested object, anything malformed) to surface HERE, inside
    this try/except, rather than surviving into get_spx_candles_via_alpaca's un-guarded ratio-scaling
    step -- the prior version's "never raises" contract wasn't actually upheld for that case. Also
    drops anything outside regular trading hours: Alpaca's SPY bars have no RTH filter the way
    yfinance's SPX_SYMBOL calls always get (prepost=False everywhere else in this file), and feeding
    thin, real-market-has-no-analog pre/post-market SPY prints into D/E's rolling, never-resets
    EMA/Bollinger window would be a genuine behavior change, not just cosmetic noise."""
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    df = pd.DataFrame(rows)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["t", *_COLUMNS]]
    for col in _COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="raise")
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(config.ET)
    df = df.set_index("t").sort_index()
    rth = (df.index.time >= config.MARKET_OPEN) & (df.index.time < config.MARKET_CLOSE)
    return df[rth]


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(config.ET)


def _report_feed_health(feed_key: tuple, healthy: bool, err: Exception | None) -> None:
    """Log a feed's up/down transitions loudly (ERROR, bypasses the console's keep-tag filter since
    ERROR+ always passes) instead of letting a dead data source look identical to a quiet market.
    Re-announces an ongoing outage at most every DATA_FEED_REMINDER_SEC rather than once per tick.
    Shared by both the yfinance path (_download) and the Alpaca/SPY path (get_spx_candles_via_alpaca)
    -- `err` is whatever extra detail that feed wants appended, or None."""
    state = _FEED_STATE.setdefault(feed_key, {"healthy": True, "last_notice": 0.0})
    mono = _time.monotonic()
    symbol, interval = feed_key
    if not healthy:
        if state["healthy"] or mono - state["last_notice"] >= config.DATA_FEED_REMINDER_SEC:
            log.error("DATA FEED DOWN: %s %s returned no data%s", symbol, interval,
                     f" ({err})" if err else "", extra={"keep": True})
            state["last_notice"] = mono
    elif not state["healthy"]:
        log.warning("DATA FEED RECOVERED: %s %s", symbol, interval, extra={"keep": True})
    state["healthy"] = healthy


def _download(symbol: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    """2026-10-01: yfinance can come back EMPTY with no exception raised at all (Yahoo throttling/
    outage -- yfinance prints its own "possibly delisted" message internally, which is not the same
    as a Python exception reaching us), so a plain try/except around yf.download() is not enough to
    detect a dead feed -- see config.py's DATA_DOWNLOAD_RETRIES section for the real incident this
    fixes. A short, in-call retry absorbs a one-off blip; if every attempt still comes back empty,
    _report_feed_health logs it LOUDLY (ERROR, console-visible) instead of silently caching an empty
    result and moving on as if nothing happened.
    """
    key = (symbol, period, interval, prepost)
    hit = _CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC:
        return hit[1]
    raw, last_err = None, None
    for attempt in range(1, config.DATA_DOWNLOAD_RETRIES + 1):
        try:
            raw = yf.download(symbol, period=period, interval=interval, prepost=prepost,
                              auto_adjust=False, progress=False, threads=False)
        except Exception as e:
            last_err = e
            raw = None
        if raw is not None and not raw.empty:
            break
        if attempt < config.DATA_DOWNLOAD_RETRIES:
            _time.sleep(config.DATA_DOWNLOAD_RETRY_SEC)
    if raw is None:
        raw = pd.DataFrame()
    _report_feed_health((symbol, interval), not raw.empty, last_err)
    df = _normalize(raw)
    _CACHE[key] = (_time.monotonic(), df)
    return df


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_COLUMNS)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    df = df[[c for c in _COLUMNS if c in df.columns]].dropna(subset=["open", "high", "low", "close"])
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(config.ET)
    return df.sort_index()


def _prior_session_date(now: datetime) -> datetime:
    d = now - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_overnight_levels(now: datetime | None = None) -> Levels | None:
    now = _now(now)
    start = at_time(_prior_session_date(now), config.OVERNIGHT_SESSION_START)
    end = at_time(now, config.MARKET_OPEN)
    df = _download(config.ES_SYMBOL, "5d", "5m", True)
    window = df[(df.index >= start) & (df.index < end)]
    if window.empty:
        return None
    return Levels(float(window["high"].max()), float(window["low"].min()), end)


def get_spx_es_basis(now: datetime | None = None) -> float | None:
    now = _now(now)
    open_dt = at_time(now, config.MARKET_OPEN)
    spx = _download(config.SPX_SYMBOL, "2d", "1m", False)
    es = _download(config.ES_SYMBOL, "2d", "1m", True)
    common = spx.index.intersection(es.index)
    common = common[(common >= open_dt) & (common <= now)]
    if common.empty:
        return None
    t = common[-1]
    return round(float(spx.loc[t, "close"] - es.loc[t, "close"]), 2)


def get_candles(symbol: str, interval: str = "2m", lookback_min: int = 180,
                now: datetime | None = None) -> pd.DataFrame:
    now = _now(now)
    if symbol == config.ES_SYMBOL:
        live = get_es_candles_live(int(interval.rstrip("m")), lookback_min, now)
        if live is not None and not live.empty:
            return live
    if symbol == config.SPX_SYMBOL:
        live = get_spx_candles_via_alpaca(int(interval.rstrip("m")), lookback_min, now)
        if live is not None and not live.empty:
            return live
    # 2026-09-25: "5d" (not "2d") so a large lookback_min (D/E's multi-session EMA/Bollinger window)
    # can actually reach back far enough on a Monday morning -- yfinance's period is CALENDAR days,
    # so "2d" on a Monday only reaches Saturday and misses Friday's session entirely. The extra data
    # is free: every caller still filters down to its own lookback_min afterward.
    df = _download(symbol, "5d", interval, symbol == config.ES_SYMBOL)
    if df.empty:
        return df
    minutes = int(interval.rstrip("m"))
    df = df[df.index >= now - timedelta(minutes=lookback_min)]
    df = df[df.index + timedelta(minutes=minutes) <= now]
    return df


def get_opening_range(now: datetime | None = None) -> Levels | None:
    now = _now(now)
    start = at_time(now, config.MARKET_OPEN)
    end = start + timedelta(minutes=config.OPENING_RANGE_MINUTES)
    if now < end:
        return None
    df = _download(config.SPX_SYMBOL, "2d", "1m", False)
    window = df[(df.index >= start) & (df.index < end)]
    if window.empty:
        return None
    return Levels(float(window["high"].max()), float(window["low"].min()), end)


def get_spot(symbol: str = "^GSPC", now: datetime | None = None) -> float | None:
    now = _now(now)
    df = _download(symbol, "2d", "1m", symbol == config.ES_SYMBOL)
    df = df[df.index <= now]
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def is_trading_day(now: datetime) -> bool:
    return now.weekday() < 5

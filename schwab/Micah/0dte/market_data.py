"""yfinance market data: ES overnight levels, SPX/ES basis, completed candles, opening range.

Every function takes `now` (tz-aware ET) so tests can inject time. Downloads are
cached for config.DATA_CACHE_SEC to avoid hammering Yahoo on a 15 s tick.
"""

import json
import logging
import time as _time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

import config
import shared_cache
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
#
# 2026-10-01: this folder must NEVER be the one to call Schwab's refresh endpoint. Schwab refresh
# tokens are single-use -- rotate on every call -- and up to 9 Alpaca personas plus the real Schwab
# trading bot were all sharing ONE token.json, each independently letting schwab-py's OAuth2 client
# auto-refresh whenever its own copy's access token expired. Any one of those refreshing invalidated
# every other process's in-memory copy, repeatedly forcing a fresh login on the Schwab bot (which has
# no fallback for its own primary auth, unlike this optional feature). User's explicit decision: only
# the Schwab bot may ever refresh. _schwab_read_only_client() below builds a schwab-py Client around a
# PLAIN, non-OAuth requests.Session carrying a static Bearer header read directly from the token file
# -- BaseClient only ever needs session.get(url, params=...) (confirmed by reading the installed
# schwab-py source directly: schwab.client.base.BaseClient.__init__ takes just an api_key and a
# session, and schwab.client.synchronous.Client._get_request is a bare self.session.get(...)) -- so
# this reuses schwab-py's own proven request-building/enum logic with ZERO refresh capability at all:
# an access token that's expired (checked locally against the file's own expires_at, with a safety
# margin) or that 401s anyway is treated as "unavailable this cycle," exactly like every other
# optional-data-source failure in this file, instead of ever attempting a refresh. Rebuilt fresh every
# time (no permanent client caching like the old design) specifically so it keeps picking up the
# Schwab bot's latest refreshed access token from the file rather than using a token baked in at
# process startup that would itself go stale after ~30 minutes.


class _TimeoutSession(requests.Session):
    """The read-only Schwab session. schwab-py sends its requests with no timeout, and this call runs in the single-threaded tick loop
    that manages every open position: a stalled connection must not be able to freeze it."""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", (3.0, 8.0))
        return super().request(*args, **kwargs)


# 2026-10-08: the fallback from live ES to the delayed proxy used to be invisible (nothing logged when the token was missing, expired
# or the request failed). _ES_LIVE["state"] is the last (usable, reason) reported, so a change is logged once and a steady state
# is silent; "fail_until" is the monotonic time before which a failed request is not repeated.
_ES_LIVE: dict = {"state": None, "fail_until": 0.0}
_ES_BACKOFF = "a Schwab /ES request failed a moment ago, retrying shortly"


def _es_report(ok: bool, reason: str = "") -> None:
    state = (ok, reason)
    if _ES_LIVE["state"] == state:
        return
    _ES_LIVE["state"] = state
    if ok:
        log.warning("LIVE_ES on: breakouts are confirmed on Schwab /ES candles.", extra={"keep": True})
    else:
        log.warning("LIVE_ES off (%s): breakouts are confirmed on SPX candles against ES levels shifted by the SPX-ES basis "
                    "(Yahoo ES=F, about 10 min delayed; the basis is known from about 09:40, no breakout can be confirmed before).",
                    reason, extra={"keep": True})


def _es_failed(reason: str) -> None:
    _ES_LIVE["fail_until"] = _time.monotonic() + float(getattr(config, "ES_LIVE_RETRY_SEC", 60))
    _es_report(False, reason)


_TOKEN_PATH_PROVIDER: dict = {"fn": None}


def set_token_path_provider(fn) -> None:
    """2026-11-10: the data feeds read the token file the BROKER is using. The Schwab broker may switch to a sibling folder's
    newer login (same app key) and from then on only that file is refreshed; this folder's own token.json goes stale within
    the hour and the feeds would fall back to Yahoo while the bot trades live. The scheduler installs the broker's path here."""
    _TOKEN_PATH_PROVIDER["fn"] = fn


def _token_path() -> str:
    fn = _TOKEN_PATH_PROVIDER["fn"]
    if fn is not None:
        try:
            found = fn()
            if found:
                return str(found)
        except Exception:       # noqa: BLE001 -- the provider must never take the feed down; the config path is the fallback
            pass
    return str(getattr(config, "SCHWAB_TOKEN_PATH", "") or "").strip()


def _es_token_check():
    """(client, "") when a read-only Schwab client can be built from the token file right now, else (None, why not). No logging."""
    token_path = _token_path()
    api_key = str(getattr(config, "SCHWAB_APP_KEY", "") or "").strip()
    if not token_path or not api_key:
        return None, "SCHWAB_APP_KEY / SCHWAB_TOKEN_PATH not configured"
    if _time.monotonic() < _ES_LIVE["fail_until"]:
        return None, _ES_BACKOFF
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        token = raw["token"]
        access_token = token["access_token"]
        expires_at = float(token["expires_at"])
    except FileNotFoundError:
        return None, f"token file not found: {token_path}"
    except Exception as e:
        return None, f"token file unreadable ({type(e).__name__})"
    if _time.time() >= expires_at - 30:   # expired, or close enough to expire mid-request -- do NOT refresh
        return None, "access token expired (only a running Schwab bot refreshes it)"
    try:
        from schwab.client import Client
    except Exception:
        return None, "schwab-py is not installed in this environment"
    session = _TimeoutSession()
    session.headers["Authorization"] = f"Bearer {access_token}"
    return Client(api_key, session), ""


def _schwab_read_only_client():
    client, reason = _es_token_check()
    if client is None and reason != _ES_BACKOFF:      # the failure that started the back-off was already reported
        _es_report(False, reason)
    return client


def live_es_status() -> tuple[bool, str]:
    """(usable, reason) for the start-of-day report: whether live ES could be used right now, and if not why not. No client, no logging."""
    client, reason = _es_token_check()
    return client is not None, reason or "token file holds a valid access token"


def live_es_backoff_active() -> bool:
    """True while a failed /ES request keeps live ES switched off (see ES_LIVE_RETRY_SEC)."""
    return _time.monotonic() < _ES_LIVE["fail_until"]


def live_es_available() -> bool:
    """True once this folder's Schwab cross-auth is configured and the shared token file currently
    holds an unexpired access token (and no /ES request failed in the last ES_LIVE_RETRY_SEC) -- lets callers switch from the
    ES_TO_SPX proxy (confirm on live SPX candles against a basis-shifted ES level) to confirming directly on ES's own (now
    genuinely live) candles for the real lead-time edge. Cheap (a local file read, no network call) so this is
    safe to call on every tick without caching, unlike the old design's permanent client cache."""
    return _schwab_read_only_client() is not None


def _es_minutes(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(config.ET)
    if "volume" not in df.columns:            # an index ($SPX) has no volume
        df["volume"] = 0.0
    return df.set_index("datetime")[["open", "high", "low", "close", "volume"]].sort_index()


def _es_1m_live(lookback_min: int, now: datetime) -> tuple[pd.DataFrame, float] | None:
    """The live ES 1-minute series (Schwab, extended hours, at least the last 24 h) and when it was fetched; None -- never
    raises -- when the read-only client cannot be built or the request fails (logged once, feed left alone for ES_LIVE_RETRY_SEC)."""
    client = _schwab_read_only_client()
    if client is None:
        return None
    # Cached like _download's yfinance calls: at TICK_SECONDS=1 (fast-as-Alpaca-allows polling) an
    # uncached call here would hit Schwab's API every single tick -- this keeps it to at most one real request per
    # DATA_CACHE_SEC (sooner right after a minute closes: _fresh), regardless of tick rate.
    cache_key = ("es_live", config.ES_SCHWAB_SYMBOL)
    hit = _CACHE.get(cache_key)
    if hit and len(hit) > 2 and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC and _fresh(hit[2], "1m", df=hit[1]):
        df1, fetched = hit[1], hit[2]
    else:
        status = None
        try:
            # a bounded window (the default is the whole available history) and a request timeout (_TimeoutSession)
            start = now - timedelta(minutes=max(int(lookback_min), 1440))
            resp = client.get_price_history_every_minute(config.ES_SCHWAB_SYMBOL, start_datetime=start, end_datetime=now,
                                                         need_extended_hours_data=True)
            status = getattr(resp, "status_code", None)
            data = resp.json()
            candles = data.get("candles") if isinstance(data, dict) else None
            if not candles:
                _es_failed("Schwab /ES returned no candles" + (f" (HTTP {status})" if status else ""))
                return None
            df1 = _es_minutes(candles)
            max_age = float(getattr(config, "ES_LIVE_MAX_BAR_AGE_SEC", 300))
            age = (now - df1.index[-1]).total_seconds()
            if max_age and _in_session(now.timestamp()) and age > max_age:
                # a stalled or delayed series is as useless as a failed request: switch to the SPX candles, not "LIVE_ES on"
                _es_failed(f"Schwab /ES bars are {age / 60:.0f} min old")
                return None
        except Exception as e:
            _es_failed(f"Schwab /ES request failed ({type(e).__name__}{f', HTTP {status}' if status else ''})")
            return None
        fetched = _time.time()
        _CACHE[cache_key] = (_time.monotonic(), df1, fetched)
        _es_report(True)
    return df1, fetched


def get_es_candles_live(interval_min: int, lookback_min: int, now: datetime | None = None) -> pd.DataFrame | None:
    """Real ES futures candles via Schwab (config.ES_SCHWAB_SYMBOL, e.g. "/ES"), resampled to
    interval_min. Returns None -- never raises -- if Schwab cross-auth isn't configured in this
    folder's .env, the shared token has expired, or the call fails for any reason (the failure is logged once and the feed
    is left alone for ES_LIVE_RETRY_SEC). Candles are COMPLETE ones only, by the same rule as the SPX series (_candles_from_1m)."""
    now = _now(now)
    got = _es_1m_live(lookback_min, now)
    if got is None:
        return None
    df1, fetched = got
    return _candles_from_1m(df1, fetched, interval_min, lookback_min, now)


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


_MINUTE_INTERVALS = {"1m", "2m", "5m"}


def _in_session(wall_now: float) -> bool:
    t = datetime.fromtimestamp(wall_now, tz=config.ET)
    return t.weekday() < 5 and config.MARKET_OPEN <= t.time() < config.MARKET_CLOSE


def _fresh(fetched_wall: float, interval: str, wall_now: float | None = None, df: pd.DataFrame | None = None,
           prepost: bool = False) -> bool:
    """Is a snapshot fetched at `fetched_wall` (epoch s) still usable? Within DATA_CACHE_SEC, and -- for minute-bar data -- not
    fetched before the current minute's boundary + CANDLE_SETTLE_SEC once that moment has passed, so the bar that just closed
    is picked up within a couple of seconds instead of up to DATA_CACHE_SEC later. If that bar is STILL missing from a regular-
    hours snapshot (Yahoo publishes it a few seconds late) the snapshot is re-downloaded every CANDLE_RETRY_SEC until it arrives."""
    wall_now = _time.time() if wall_now is None else wall_now
    if wall_now - fetched_wall >= config.DATA_CACHE_SEC:
        return False
    if interval in _MINUTE_INTERVALS:
        settle = float(getattr(config, "CANDLE_SETTLE_SEC", 2.0))
        boundary = (wall_now // 60) * 60 + settle
        if wall_now >= boundary and fetched_wall < boundary:
            return False
        if (df is not None and not df.empty and not prepost and wall_now >= boundary and _in_session(wall_now - 60)
                and wall_now - fetched_wall >= float(getattr(config, "CANDLE_RETRY_SEC", 3.0))):
            expected_end = datetime.fromtimestamp((wall_now // 60) * 60, tz=config.ET)       # the minute that just closed ends here
            if df.index[-1] + timedelta(minutes=1) < expected_end:
                return False
    return True


def _download(symbol: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    return _download_snapshot(symbol, period, interval, prepost)[0]


def _download_snapshot(symbol: str, period: str, interval: str, prepost: bool) -> tuple[pd.DataFrame, float]:
    """(frame, fetched_at epoch seconds). Three layers: this process's memory cache, the snapshot file shared by every process in
    the folder (shared_cache.py: one fetch for all personas), then Yahoo itself. 2026-10-01: yfinance can come back EMPTY with no exception raised at all (Yahoo throttling/
    outage -- yfinance prints its own "possibly delisted" message internally, which is not the same
    as a Python exception reaching us), so a plain try/except around yf.download() is not enough to
    detect a dead feed -- see config.py's DATA_DOWNLOAD_RETRIES section for the real incident this
    fixes. A short, in-call retry absorbs a one-off blip; if every attempt still comes back empty,
    _report_feed_health logs it LOUDLY (ERROR, console-visible) instead of silently caching an empty
    result and moving on as if nothing happened.
    """
    key = (symbol, period, interval, prepost)
    hit = _CACHE.get(key)
    if hit:
        fetched = hit[2] if len(hit) > 2 else _time.time() - (_time.monotonic() - hit[0])
        if _time.monotonic() - hit[0] < config.DATA_CACHE_SEC and _fresh(fetched, interval, df=hit[1], prepost=prepost):
            return hit[1], fetched

    def fetch() -> pd.DataFrame:
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
        return _normalize(raw)

    snap = shared_cache.shared_get(f"{symbol}_{period}_{interval}_{int(prepost)}",
                                   lambda s: _fresh(s["fetched_at"], interval, df=s["df"], prepost=prepost), fetch)
    if snap is None:                                   # the feed is down and there is nothing to fall back on
        df, fetched = pd.DataFrame(columns=_COLUMNS), _time.time()
    else:
        df, fetched = snap["df"], snap["fetched_at"]
    _CACHE[key] = (_time.monotonic(), df, fetched)
    return df, fetched


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


def _es_minutes_any(now: datetime) -> pd.DataFrame:
    """ES 1-minute bars (extended hours): Schwab's live series when the read-only client can be built, else Yahoo ES=F."""
    got = _es_1m_live(1440, now) if live_es_available() else None
    if got is not None and not got[0].empty:
        return got[0]
    return _download(config.ES_SYMBOL, "2d", "1m", True)


def get_overnight_levels(now: datetime | None = None) -> Levels | None:
    now = _now(now)
    start = at_time(_prior_session_date(now), config.OVERNIGHT_SESSION_START)
    end = at_time(now, config.MARKET_OPEN)
    # 2026-11-10: the live ES series (Schwab) when it is there -- the same bars the breakouts are confirmed on -- else Yahoo
    df = _es_minutes_any(now) if live_es_available() else _download(config.ES_SYMBOL, "5d", "5m", True)
    window = df[(df.index >= start) & (df.index < end)]
    if window.empty:
        return None
    return Levels(float(window["high"].max()), float(window["low"].min()), end)


def get_spx_es_basis(now: datetime | None = None) -> float | None:
    now = _now(now)
    open_dt = at_time(now, config.MARKET_OPEN)
    spx = _spx_series(now)[0]
    es = _es_minutes_any(now)
    common = spx.index.intersection(es.index)
    # 2026-11-10: complete minutes only -- at 09:30:02 both snapshots can still carry the forming 09:30 bar, and the first basis is
    # latched for the day (the same settle rule every candle consumer applies)
    cutoff = now - timedelta(seconds=float(getattr(config, "CANDLE_SETTLE_SEC", 2.0)))
    common = common[(common >= open_dt) & (common + timedelta(minutes=1) <= cutoff)]
    if common.empty:
        return None
    t = common[0]       # 2026-10-08: the FIRST common minute from the open -- the same number for every persona and every
                        # restart (it used to be the latest common minute at whenever each process happened to ask: 1-3 pt apart)
    return round(float(spx.loc[t, "close"] - es.loc[t, "close"]), 2)


def get_candles(symbol: str, interval: str = "2m", lookback_min: int = 180,
                now: datetime | None = None) -> pd.DataFrame:
    now = _now(now)
    if symbol == config.ES_SYMBOL:
        was_live = live_es_available()
        live = get_es_candles_live(int(interval.rstrip("m")), lookback_min, now)
        if live is not None and not live.empty:
            return live
        if was_live:
            # live ES was usable a moment ago but gave nothing now (no candle complete yet, or the request failed): never
            # substitute the ~10 min delayed Yahoo ES=F series for it -- the caller sees "no candles yet", and after a failure
            # live_es_available() is False so the next tick confirms on SPX candles instead
            return pd.DataFrame(columns=_COLUMNS)
    if symbol == config.SPX_SYMBOL and getattr(config, "SPX_CANDLE_SOURCE", "yahoo") in ("yahoo", "schwab"):
        return get_spx_candles_yahoo(int(interval.rstrip("m")), lookback_min, now)
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


def _spx_1m() -> tuple[pd.DataFrame, float]:
    """The Yahoo SPX series: ^GSPC 1-minute bars, 5 days (enough for a Monday's multi-session D/E window), shared by every
    persona through the snapshot file."""
    return _download_snapshot(config.SPX_SYMBOL, "5d", "1m", False)


_SPX_SOURCE: dict = {"state": None}


def _spx_source_report(label: str) -> None:
    """Log the SPX data source once per change (steady state is silent)."""
    if _SPX_SOURCE["state"] == label:
        return
    _SPX_SOURCE["state"] = label
    log.warning("SPX data: %s", label, extra={"keep": True})


_SPX_LIVE: dict = {"fail_until": 0.0}      # a failed $SPX request is not repeated before this monotonic time (SPX_SCHWAB_RETRY_SEC)


def _spx_failed(symbol: str, err: Exception) -> None:
    _SPX_LIVE["fail_until"] = _time.monotonic() + float(getattr(config, "SPX_SCHWAB_RETRY_SEC", 60))
    _report_feed_health((symbol, "1m"), False, err)


def _spx_1m_schwab(now: datetime) -> tuple[pd.DataFrame, float] | None:
    """The SPX 1-minute series from Schwab's price history (config.SPX_SCHWAB_SYMBOL, "$SPX"), regular hours, cached like the
    live ES series (one request per DATA_CACHE_SEC, sooner right after a minute closes). None -- never raises -- when the
    read-only client cannot be built or the request fails; the caller falls back to Yahoo."""
    symbol = str(getattr(config, "SPX_SCHWAB_SYMBOL", "$SPX"))
    key = ("spx_schwab", symbol)
    hit = _CACHE.get(key)
    if hit and len(hit) > 2 and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC and _fresh(hit[2], "1m", df=hit[1]):
        return hit[1], hit[2]
    if _time.monotonic() < _SPX_LIVE["fail_until"]:
        return None                          # a request failed a moment ago: Yahoo until the back-off has passed
    client = _schwab_read_only_client()
    if client is None:
        return None
    status = None
    try:
        start = now - timedelta(minutes=max(int(getattr(config, "SPX_SCHWAB_LOOKBACK_MIN", 1440)), 60))
        resp = client.get_price_history_every_minute(symbol, start_datetime=start, end_datetime=now,
                                                     need_extended_hours_data=False)
        status = getattr(resp, "status_code", None)
        data = resp.json()
        candles = data.get("candles") if isinstance(data, dict) else None
        if not candles:
            _spx_failed(symbol, RuntimeError("no candles" + (f" (HTTP {status})" if status else "")))
            return None
        df1 = _es_minutes(candles)
        max_age = float(getattr(config, "SPX_SCHWAB_MAX_BAR_AGE_SEC", 300))
        age = (now - df1.index[-1]).total_seconds()
        if max_age and _in_session(now.timestamp()) and age > max_age:
            # a series that stopped advancing is as useless as a failed request: Yahoo, not a frozen "live" feed
            _spx_failed(symbol, RuntimeError(f"newest bar is {age / 60:.0f} min old"))
            return None
    except Exception as e:      # noqa: BLE001 -- an optional feed must never take the bot down
        _spx_failed(symbol, e)
        return None
    fetched = _time.time()
    _CACHE[key] = (_time.monotonic(), df1, fetched)
    _report_feed_health((symbol, "1m"), True, None)
    return df1, fetched


def _spx_series(now: datetime | None = None) -> tuple[pd.DataFrame, float]:
    """The ONE SPX 1-minute series: Schwab's $SPX price history when SPX_CANDLE_SOURCE is "schwab" (Yahoo ^GSPC the moment it
    cannot be read), else Yahoo ^GSPC. Candles, the opening range, the spot and the basis all read this."""
    now = _now(now)
    if getattr(config, "SPX_CANDLE_SOURCE", "yahoo") == "schwab":
        got = _spx_1m_schwab(now)
        if got is not None:
            _spx_source_report(f"Schwab {getattr(config, 'SPX_SCHWAB_SYMBOL', '$SPX')} 1-minute bars")
            return got
        _spx_source_report(f"Yahoo {config.SPX_SYMBOL} 1-minute bars (Schwab {getattr(config, 'SPX_SCHWAB_SYMBOL', '$SPX')} not readable)")
    return _spx_1m()


def _candles_from_1m(df1: pd.DataFrame, fetched_wall: float, minutes: int, lookback_min: int, now: datetime) -> pd.DataFrame:
    """Resample 1-minute bars into `minutes`-minute candles that are COMPLETE: every minute bar present, and the snapshot seen
    at least CANDLE_SETTLE_SEC after the candle's last minute ended (so that bar is final, not still forming). A candle still
    missing a bar CANDLE_GAP_TOLERANCE_SEC after it ended is accepted anyway (a data gap must not freeze the series)."""
    if df1.empty:
        return df1
    settle = timedelta(seconds=float(getattr(config, "CANDLE_SETTLE_SEC", 2.0)))
    cutoff = min(now, datetime.fromtimestamp(fetched_wall, tz=config.ET) - settle)
    bars = df1[df1.index + timedelta(minutes=1) <= cutoff]
    if bars.empty:
        return bars
    if minutes <= 1:
        return bars[bars.index >= now - timedelta(minutes=lookback_min)]
    grouped = bars.resample(f"{minutes}min")
    candles = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped["close"].count()
    candles = candles[counts > 0]
    counts = counts[counts > 0]
    ends = candles.index + timedelta(minutes=minutes)
    gap_ok = ends <= cutoff - timedelta(seconds=float(getattr(config, "CANDLE_GAP_TOLERANCE_SEC", 180)))
    keep = (ends <= cutoff) & ((counts.values >= minutes) | gap_ok)
    candles = candles[keep]
    return candles[candles.index >= now - timedelta(minutes=lookback_min)]


def get_spx_candles_yahoo(minutes: int, lookback_min: int, now: datetime | None = None) -> pd.DataFrame:
    """SPX candles from the one SPX 1-minute series (Schwab or Yahoo, see _spx_series), complete candles only (_candles_from_1m)."""
    now = _now(now)
    df1, fetched = _spx_series(now)
    return _candles_from_1m(df1, fetched, minutes, lookback_min, now)


def get_opening_range(now: datetime | None = None) -> Levels | None:
    """Build the opening range from completed 5-minute candle bodies only.

    Uses the OPEN and CLOSE prices, completely ignoring upper and lower wicks.
    """
    now = _now(now)
    start = at_time(now, config.MARKET_OPEN)
    end = start + timedelta(minutes=config.OPENING_RANGE_MINUTES)

    # Do not establish the range until the full opening window has ended.
    if now < end:
        return None

    # Retrieve underlying SPX 1-minute data.
    df1, fetched = _spx_series(now)

    # Wait until the opening range data has settled.
    if fetched < end.timestamp() + float(
        getattr(config, "CANDLE_SETTLE_SEC", 2.0)
    ):
        return None

    # Convert 1-minute data into completed 5-minute candles. 2026-11-12: the lookback always reaches back to the open --
    # CANDLE_LOOKBACK_MIN (180) alone would drop the 09:30 candle from 12:31 ET on and hand a late restart a partial range.
    lookback_min = max(int(config.CANDLE_LOOKBACK_MIN), int((now - start).total_seconds() // 60) + 5)
    df5 = _candles_from_1m(
        df1,
        fetched,
        5,
        lookback_min,
        now
    )

    # Select only the opening range candles (09:30–09:45 ET).
    window = df5[
        (df5.index >= start) &
        (df5.index < end)
    ]

    if window.empty:
        return None

    # Require the final 5-minute candle (09:40–09:45 ET).
    last_candle_start = end - timedelta(minutes=5)

    if (
        window.index[-1] < last_candle_start
        and fetched < end.timestamp() + float(
            getattr(config, "CANDLE_GAP_TOLERANCE_SEC", 180)
        )
    ):
        return None

    # Calculate opening range using OPEN and CLOSE only.
    # Upper and lower wicks are completely ignored.
    body_high = window[["open", "close"]].max(axis=1)
    body_low = window[["open", "close"]].min(axis=1)

    return Levels(
        high=float(body_high.max()),
        low=float(body_low.min()),
        established_at=end
    )


def get_spot(symbol: str = "^GSPC", now: datetime | None = None) -> float | None:
    now = _now(now)
    df = _spx_series(now)[0] if symbol == config.SPX_SYMBOL else _download(symbol, "2d", "1m", symbol == config.ES_SYMBOL)
    df = df[df.index <= now]
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def is_trading_day(now: datetime) -> bool:
    return now.weekday() < 5

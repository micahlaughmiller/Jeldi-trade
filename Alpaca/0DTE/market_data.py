"""yfinance market data: ES overnight levels, SPX/ES basis, completed candles, opening range.

Every function takes `now` (tz-aware ET) so tests can inject time. Downloads are
cached for config.DATA_CACHE_SEC to avoid hammering Yahoo on a 15 s tick.
"""

import logging
import time as _time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import config
from strategy import Levels, at_time

log = logging.getLogger(__name__)

_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_COLUMNS = ["open", "high", "low", "close", "volume"]

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


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(config.ET)


def _download(symbol: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    key = (symbol, period, interval, prepost)
    hit = _CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < config.DATA_CACHE_SEC:
        return hit[1]
    try:
        raw = yf.download(symbol, period=period, interval=interval, prepost=prepost,
                          auto_adjust=False, progress=False, threads=False)
    except Exception as e:
        log.warning("yfinance download failed for %s %s: %s", symbol, interval, e)
        raw = pd.DataFrame()
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

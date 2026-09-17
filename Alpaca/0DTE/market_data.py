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
    df = _download(symbol, "2d", interval, symbol == config.ES_SYMBOL)
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

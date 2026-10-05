"""Opening-range-breakout (ORB) pre-breakout SCANNER: ranking only, no orders.

Implements the 100-point "Pre Breakout Score" from SPX_Stock_ORB_Trading_System.docx over SPX plus
the 30-stock tiered universe in data/orb_universe.json, evaluated on completed 1-minute bars from
09:30 ET through `now`. Read-only: nothing here talks to a broker or places an order.

Factor weights (doc section 2): relative volume 20, ATR / expected move 20, OR compression 15,
distance to breakout 15, momentum 10, VWAP position 10, option quality 10. The tier tiebreak
(A +2, B 0, C -2) is applied AFTER the 100-point score and both numbers are reported.

Assumptions (every number is a ScanConfig field so it can be tuned / backtested):
  * Expected move (EM) ~= ATR(14) of daily bars x em_atr_multiple (1.0). The doc leaves the EM
    estimate open; ATR is the "consistent timeframe" volatility it asks for and the ATM straddle is
    not available without a chain. Remaining EM = max(EM - |last - today's open|, 0).
  * RVOL = today's cumulative volume through the last completed minute / mean cumulative volume
    through the same minute-of-day over the prior rvol_lookback_sessions (20) sessions. Yahoo's
    ^GSPC "volume" is not a traded volume, so the index gets the neutral rvol_neutral_points (10/20)
    and says so in its notes.
  * The 1-minute indicators (EMA9/21, RSI14 Wilder, MACD 12/26/9) run over the last few RTH
    sessions stitched together so they are warm at 09:30; VWAP resets at 09:30 as the doc describes.
    Its slope is measured over the last vwap_slope_minutes (5) minutes.
  * yfinance serves 1-minute bars only for the trailing ~30 calendar days and ~7 days per request,
    so the intraday history is paged in 7-day windows and capped there (~20 prior sessions).
  * Likely direction: the nearer OR15 boundary, unless momentum is strongly aligned the other way
    (>= direction_override_count of the 5 conditions). A close outside OR15 fixes the direction and
    marks the candidate `already_broken` (distance score 0: the setup is no longer "before the break").
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pandas as pd
import yfinance as yf
from pandas.tseries.holiday import (AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
                                    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
                                    USThanksgivingDay, nearest_workday, sunday_to_monday)

import config
from strategy import BEARISH, BULLISH, at_time, ema

log = logging.getLogger(__name__)

ET = config.ET
MARKET_OPEN = config.MARKET_OPEN
MARKET_CLOSE = time(16, 0)
YF_1M_MAX_CALENDAR_DAYS = 29     # Yahoo refuses 1m requests reaching further back than ~30 days
YF_1M_PAGE_DAYS = 7              # and serves at most ~7-8 days of 1m bars per request
DEFAULT_UNIVERSE_PATH = Path(__file__).resolve().parent / "data" / "orb_universe.json"
INDEX_TIER = "IDX"               # tier label given to the index (SPX); tiebreak 0



class _NyseHolidays(AbstractHolidayCalendar):
    """Full-day NYSE closures (early-close days still trade). Saturday holidays are observed the Friday before,
    Sunday ones the Monday after -- except New Year's Day, which is not moved to the prior Friday."""
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


_NYSE_HOLIDAYS = _NyseHolidays()


@lru_cache(maxsize=None)
def _closed_days(year: int) -> frozenset[date]:
    return frozenset(ts.date() for ts in _NYSE_HOLIDAYS.holidays(start=pd.Timestamp(year, 1, 1),
                                                                 end=pd.Timestamp(year, 12, 31)))


def has_session(start: date, end: date) -> bool:
    """True if [start, end) holds at least one weekday that is not an NYSE holiday, i.e. a day Yahoo can have
    1-minute bars for. Asking Yahoo for a window with none (a weekend plus a Monday holiday) returns nothing and
    makes yfinance log an ERROR ("possibly delisted; no price data found") for every symbol in the batch."""
    day = start
    while day < end:
        if day.weekday() < 5 and day not in _closed_days(day.year):
            return True
        day += timedelta(days=1)
    return False


_CACHE: dict[tuple, tuple[float, object]] = {}
_COLUMNS = ["open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------------------------ configuration

@dataclass(frozen=True)
class ScanConfig:
    """Every threshold the doc calls "approximately". Defaults are working assumptions to be
    optimized with historical testing (doc sections 3 and 20), not tuned constants."""

    # --- data window
    yf_symbol_map: dict = field(default_factory=lambda: {"SPX": "^GSPC"})
    intraday_sessions: int = 25          # sessions wanted incl. today (calendar-capped by Yahoo, ~20 prior)
    daily_days: int = 40                 # calendar days of daily bars for ATR
    atr_period: int = 14
    min_bars_today: int = 5              # need at least the 5-minute OR to score anything
    indicator_warmup_bars: int = 780     # ~2 RTH sessions of 1-min closes stitched before today
    ema_fast: int = 9
    ema_slow: int = 21
    ema_slope_minutes: int = 3
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    vwap_slope_minutes: int = 5
    cache_sec: float = float(config.DATA_CACHE_SEC)   # today's page / daily bars
    history_cache_sec: float = 3600.0                 # prior-week 1m pages never change intraday

    # --- relative volume (20): doc section 3 tiers, lower bound inclusive
    rvol_thresholds: tuple = (1.0, 1.25, 1.5, 2.0)
    rvol_points: tuple = (0.0, 5.0, 10.0, 15.0, 20.0)
    rvol_neutral_points: float = 10.0    # SPX / unknown volume: neutral half credit
    rvol_lookback_sessions: int = 20
    rvol_min_sessions: int = 5

    # --- ATR / expected move (20): remaining EM/ATR (12) + OR15/ATR (8)
    em_atr_multiple: float = 1.0         # EM ~= ATR
    em_remaining_points: float = 12.0
    em_remaining_zero_ratio: float = 0.25   # remaining EM / ATR at or below this -> 0
    em_remaining_full_ratio: float = 0.75   # at or above this -> full 12
    em_or_points: float = 8.0
    em_or_zero_ratio: float = 0.50          # OR15 width / ATR at or above this -> 0
    em_or_full_ratio: float = 0.15          # at or below this -> full 8

    # --- OR compression (15): OR15 width / ATR, tight but not noise
    compression_points: float = 15.0
    compression_full_ratio: float = 0.10    # at or below (and above the floor) -> 15
    compression_zero_ratio: float = 0.40    # at or above -> 0
    compression_floor_ratio: float = 0.05   # below this the range is treated as noise ...
    compression_noise_points: float = 5.0   # ... and gets this flat, penalized credit

    # --- distance to breakout (15): distance to the nearer OR15 boundary / OR15 width
    distance_points: float = 15.0
    distance_full_or_ratio: float = 0.10    # within 10% of the OR width of the boundary -> 15
    distance_zero_or_ratio: float = 0.50    # mid-range (as far as you can be inside) -> 0

    # --- momentum (10): 5 aligned conditions x 2
    momentum_points_per_condition: float = 2.0
    direction_override_count: int = 4       # momentum this aligned overrides the nearer-boundary direction

    # --- VWAP position (10)
    vwap_side_points: float = 6.0           # price on the right side of VWAP
    vwap_slope_points: float = 4.0          # ... and VWAP sloping that way

    # --- option quality (10): quotes present (2) + delta band available (3) + spread (5)
    option_quote_points: float = 2.0
    option_delta_points: float = 3.0
    option_delta_min: float = 0.30
    option_delta_max: float = 0.60
    option_atm_band_pct: float = 0.015      # used when the chain carries no delta
    option_spread_points: float = 5.0
    option_spread_full_pct: float = 0.03    # (ask-bid)/mid at or below -> full 5
    option_spread_zero_pct: float = 0.15    # at or above -> 0

    # --- tier tiebreak applied after the 100-point score
    tier_adjust: dict = field(default_factory=lambda: {"A": 2.0, "B": 0.0, "C": -2.0, INDEX_TIER: 0.0})


DEFAULT_CONFIG = ScanConfig()


# ------------------------------------------------------------------------------------------ universe

def load_universe(path: str | Path = DEFAULT_UNIVERSE_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        universe = json.load(f)
    tiers = universe.get("tiers") or {}
    if not tiers:
        raise ValueError(f"universe {path} has no tiers")
    universe["tiers"] = {str(k): [str(s).upper() for s in v] for k, v in tiers.items()}
    universe["index"] = str(universe.get("index") or "SPX").upper()
    return universe


def universe_members(universe: dict) -> list[tuple[str, str]]:
    """[(symbol, tier)] with the index first (tier INDEX_TIER), then tiers in A, B, C order."""
    members = [(universe["index"], INDEX_TIER)] if universe.get("index") else []
    for tier, symbols in universe["tiers"].items():
        members.extend((s, tier) for s in symbols)
    return members


def to_yf_symbol(symbol: str, cfg: ScanConfig = DEFAULT_CONFIG) -> str:
    return cfg.yf_symbol_map.get(symbol.upper(), symbol.upper())


# --------------------------------------------------------------------------------------- data layer

def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(ET)
    return now if now.tzinfo else now.replace(tzinfo=ET)


def _cache_get(key: tuple, ttl: float):
    hit = _CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: tuple, value) -> None:
    _CACHE[key] = (_time.monotonic(), value)


def _yf_download(symbols: list[str], **kwargs) -> pd.DataFrame:
    """Batched, threaded download that never raises; empty frame on any failure."""
    try:
        raw = yf.download(list(symbols), group_by="ticker", threads=True, auto_adjust=False,
                          progress=False, **kwargs)
    except Exception as e:
        log.warning("yfinance download failed for %d symbols %s: %s", len(symbols), kwargs, e)
        return pd.DataFrame()
    return raw if raw is not None else pd.DataFrame()


def _normalize(raw: pd.DataFrame | None, daily: bool = False) -> pd.DataFrame:
    """Lower-case OHLCV columns on a tz-aware ET index. Intraday bars come back UTC-indexed;
    daily bars are session dates (naive), which must NOT be shifted through UTC."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_COLUMNS)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    df = df[[c for c in _COLUMNS if c in df.columns]].dropna(subset=["open", "high", "low", "close"])
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = df["volume"].fillna(0.0)
    idx = pd.DatetimeIndex(df.index)
    if daily:
        idx = idx.tz_convert(ET).tz_localize(None) if idx.tz is not None else idx
        idx = idx.normalize().tz_localize(ET)
    else:
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        idx = idx.tz_convert(ET)
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


def _split_by_symbol(raw: pd.DataFrame, symbols: list[str], daily: bool = False) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        sub = None
        try:
            if raw is None or raw.empty:
                sub = None
            elif isinstance(raw.columns, pd.MultiIndex):
                if s in raw.columns.get_level_values(0):
                    sub = raw[s]
                elif s in raw.columns.get_level_values(1):
                    sub = raw.xs(s, axis=1, level=1)
            elif len(symbols) == 1:
                sub = raw
        except Exception as e:      # pragma: no cover - defensive against yfinance layout changes
            log.warning("could not split %s out of download: %s", s, e)
            sub = None
        out[s] = _normalize(sub, daily=daily)
    return out


def _rth_only(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    tod = df.index.time
    return df[(tod >= MARKET_OPEN) & (tod < MARKET_CLOSE)]


def _intraday_pages(now: datetime, sessions: int, today: date | None = None) -> list[dict]:
    """yf.download kwargs covering ~`sessions` sessions back from `now`: today's rolling 7-day
    window first, then 7-day start/end windows back to Yahoo's ~30-day 1m limit. That limit is
    measured from the real clock (`today`), not from a past `now` being re-scored. A window with no trading
    session in it (e.g. the Sunday + Labor Day sliver the limit can clip the oldest page down to) is skipped."""
    today = today or datetime.now(ET).date()
    floor = max(now.date(), today) - timedelta(days=YF_1M_MAX_CALENDAR_DAYS)
    calendar_days = min(int(math.ceil(sessions * 7 / 5)) + 3, YF_1M_MAX_CALENDAR_DAYS)
    pages: list[dict] = [{"period": f"{YF_1M_PAGE_DAYS}d"}]
    k = 1
    while YF_1M_PAGE_DAYS * k < calendar_days:
        start = now.date() - timedelta(days=YF_1M_PAGE_DAYS * (k + 1))
        end = now.date() - timedelta(days=YF_1M_PAGE_DAYS * k) + timedelta(days=1)   # end is exclusive
        start = max(start, floor)
        if start >= end:
            break
        if has_session(start, end):    # the floor can leave a sliver that is all weekend/holiday: nothing to fetch
            pages.append({"start": start.isoformat(), "end": end.isoformat()})
        k += 1
    return pages


def fetch_intraday_batch(symbols: list[str], now: datetime | None = None,
                         sessions: int | None = None,
                         cfg: ScanConfig = DEFAULT_CONFIG) -> dict[str, pd.DataFrame]:
    """1-minute RTH bars (ET-indexed, prepost=False) per symbol for the last ~`sessions` sessions
    including today. One batched download per 7-day page; prior pages are cached for an hour."""
    now = _now(now)
    sessions = sessions or cfg.intraday_sessions
    yf_symbols = [to_yf_symbol(s, cfg) for s in symbols]
    parts_by_page: list[dict[str, pd.DataFrame]] = []
    for i, page in enumerate(_intraday_pages(now, sessions)):
        key = ("1m", tuple(yf_symbols), tuple(sorted(page.items())))
        ttl = cfg.cache_sec if i == 0 else cfg.history_cache_sec
        parts = _cache_get(key, ttl)
        if parts is None:
            raw = _yf_download(yf_symbols, interval="1m", prepost=False, **page)
            parts = _split_by_symbol(raw, yf_symbols)
            _cache_put(key, parts)
        parts_by_page.append(parts)
    out: dict[str, pd.DataFrame] = {}
    for s, ys in zip(symbols, yf_symbols):
        frames = [p[ys] for p in parts_by_page if ys in p and not p[ys].empty]
        if not frames:
            out[s] = pd.DataFrame(columns=_COLUMNS)
            continue
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        out[s] = _rth_only(df)
    return out


def fetch_intraday(symbol: str, now: datetime | None = None, days: int = 25,
                   cfg: ScanConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """1-min bars for one symbol over the last ~`days` sessions incl. today (Yahoo caps at ~20 prior)."""
    return fetch_intraday_batch([symbol], now, sessions=days, cfg=cfg)[symbol]


def fetch_daily_batch(symbols: list[str], days: int | None = None,
                      cfg: ScanConfig = DEFAULT_CONFIG) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per symbol for the last `days` calendar days, one batched download."""
    days = days or cfg.daily_days
    yf_symbols = [to_yf_symbol(s, cfg) for s in symbols]
    key = ("1d", tuple(yf_symbols), days)
    parts = _cache_get(key, cfg.cache_sec)
    if parts is None:
        raw = _yf_download(yf_symbols, period=f"{days}d", interval="1d")
        parts = _split_by_symbol(raw, yf_symbols, daily=True)
        _cache_put(key, parts)
    return {s: parts.get(ys, pd.DataFrame(columns=_COLUMNS)) for s, ys in zip(symbols, yf_symbols)}


def fetch_daily(symbol: str, days: int = 40, cfg: ScanConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    return fetch_daily_batch([symbol], days, cfg)[symbol]


# --------------------------------------------------------------------------------------- indicators

def atr(daily: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder ATR: SMA of the first `period` true ranges, then recursive smoothing. None if short."""
    if daily is None or len(daily) < period + 1:
        return None
    prev_close = daily["close"].shift(1)
    tr = pd.concat([daily["high"] - daily["low"],
                    (daily["high"] - prev_close).abs(),
                    (daily["low"] - prev_close).abs()], axis=1).max(axis=1).iloc[1:]
    value = float(tr.iloc[:period].mean())
    for x in tr.iloc[period:]:
        value = (value * (period - 1) + float(x)) / period
    return value


def rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Wilder RSI of the last close; None with fewer than period+1 closes."""
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().iloc[1:]
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def macd_histogram(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> float | None:
    if closes is None or len(closes) < slow + signal:
        return None
    line = ema(closes, fast) - ema(closes, slow)
    return float((line - ema(line, signal)).iloc[-1])


def vwap_series(bars: pd.DataFrame) -> tuple[pd.Series, bool]:
    """Cumulative VWAP from the first bar. Falls back to the cumulative mean of the typical price
    when there is no volume (index); the flag says whether real volume was used."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol = bars["volume"].fillna(0.0)
    if float(vol.sum()) <= 0:
        return typical.expanding().mean(), False
    return (typical * vol).cumsum() / vol.cumsum().replace(0.0, float("nan")), True


# ------------------------------------------------------------------------------------------ metrics

@dataclass
class OpeningRange:
    high: float
    low: float
    width: float
    minutes: int
    complete: bool       # False while `now` is still inside the window (range so far)


@dataclass
class Metrics:
    symbol: str
    session: date
    bar_time: datetime           # start of the last completed 1-min bar used
    minutes_elapsed: int         # completed minutes since 09:30
    last: float
    open: float
    or5: OpeningRange
    or10: OpeningRange
    or15: OpeningRange
    atr: float
    expected_move: float
    consumed_move: float
    remaining_em: float
    rvol: float | None
    rvol_sessions: int
    vwap: float
    vwap_slope: float
    ema9: float
    ema21: float
    ema9_slope: float
    rsi: float | None
    macd_hist: float | None
    momentum_bull: int
    momentum_bear: int
    boundary_direction: str
    direction: str
    distance: float
    distance_or_ratio: float | None
    distance_atr_ratio: float | None
    already_broken: bool
    notes: list[str] = field(default_factory=list)

    @property
    def ema_state(self) -> str:
        if self.ema9 > self.ema21:
            return "BULL"
        if self.ema9 < self.ema21:
            return "BEAR"
        return "FLAT"

    @property
    def vwap_relation(self) -> str:
        if self.last > self.vwap:
            return "ABOVE"
        if self.last < self.vwap:
            return "BELOW"
        return "AT"


def _opening_range(today: pd.DataFrame, open_dt: datetime, now: datetime, minutes: int) -> OpeningRange:
    end = open_dt + timedelta(minutes=minutes)
    window = today[today.index < end]
    high, low = float(window["high"].max()), float(window["low"].min())
    return OpeningRange(high, low, high - low, minutes, complete=now >= end)


def _relative_volume(completed: pd.DataFrame, today: pd.DataFrame, session: date,
                     cfg: ScanConfig) -> tuple[float | None, int, str | None]:
    """(rvol, sessions used, note). Time-normalized: cumulative volume through the last completed
    minute-of-day today vs the mean of the same cumulative over the prior sessions."""
    today_cum = float(today["volume"].sum())
    if today_cum <= 0:
        return None, 0, "RVOL unavailable: no volume today"
    cutoff_tod = today.index[-1].time()
    prior = completed[completed.index.date < session]
    prior = prior[(prior.index.time >= MARKET_OPEN) & (prior.index.time <= cutoff_tod)]
    if prior.empty:
        return None, 0, "RVOL unavailable: no prior sessions"
    per_session = prior.groupby(prior.index.date)["volume"].sum()
    per_session = per_session[per_session > 0].sort_index().iloc[-cfg.rvol_lookback_sessions:]
    if len(per_session) < cfg.rvol_min_sessions:
        return None, int(len(per_session)), (f"RVOL unavailable: only {len(per_session)} prior sessions "
                                             f"(need {cfg.rvol_min_sessions})")
    return today_cum / float(per_session.mean()), int(len(per_session)), None


def compute_metrics(symbol: str, intraday: pd.DataFrame | None, daily: pd.DataFrame | None,
                    now: datetime, cfg: ScanConfig = DEFAULT_CONFIG,
                    is_index: bool = False) -> tuple[Metrics | None, list[str]]:
    """Everything the score needs for one symbol at `now` (only completed bars from 09:30 to now).
    Returns (metrics, []) or (None, [reasons]) -- never raises on missing data."""
    now = _now(now)
    reasons: list[str] = []
    if intraday is None or intraday.empty:
        return None, ["no intraday bars"]
    completed = intraday[intraday.index + timedelta(minutes=1) <= now]
    open_dt = at_time(now, MARKET_OPEN)
    session = now.date()
    today = completed[completed.index >= open_dt]
    if len(today) < cfg.min_bars_today:
        return None, [f"only {len(today)} completed bars since 09:30 on {session} (need {cfg.min_bars_today})"]
    if daily is None or daily.empty:
        return None, ["no daily bars"]
    prior_daily = daily[daily.index.date < session]
    atr_value = atr(prior_daily, cfg.atr_period)
    if atr_value is None or not atr_value > 0:
        return None, [f"insufficient daily bars for ATR({cfg.atr_period}): {len(prior_daily)} prior sessions"]

    notes: list[str] = []
    or5 = _opening_range(today, open_dt, now, 5)
    or10 = _opening_range(today, open_dt, now, 10)
    or15 = _opening_range(today, open_dt, now, 15)
    if not or15.complete:
        notes.append(f"OR15 incomplete: range over first {len(today)} min so far")

    last = float(today["close"].iloc[-1])
    open_price = float(today["open"].iloc[0])
    expected_move = atr_value * cfg.em_atr_multiple
    consumed = abs(last - open_price)
    remaining = max(expected_move - consumed, 0.0)

    if is_index:
        rvol, rvol_sessions = None, 0
        notes.append(f"index volume unreliable: neutral RVOL ({cfg.rvol_neutral_points:g}/20)")
    else:
        rvol, rvol_sessions, note = _relative_volume(completed, today, session, cfg)
        if note:
            notes.append(note + f" (neutral {cfg.rvol_neutral_points:g}/20)")

    vw, real_volume = vwap_series(today)
    if not real_volume:
        notes.append("VWAP without volume: cumulative typical-price mean")
    vwap_now = float(vw.iloc[-1])
    back = min(cfg.vwap_slope_minutes, len(vw) - 1)
    vwap_slope = vwap_now - float(vw.iloc[-1 - back]) if back > 0 else 0.0

    closes = completed["close"].iloc[-cfg.indicator_warmup_bars:]
    ema_fast = ema(closes, cfg.ema_fast)
    ema_slow = ema(closes, cfg.ema_slow)
    ema9, ema21 = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
    back = min(cfg.ema_slope_minutes, len(ema_fast) - 1)
    ema9_slope = ema9 - float(ema_fast.iloc[-1 - back]) if back > 0 else 0.0
    rsi_value = rsi(closes, cfg.rsi_period)
    macd_hist = macd_histogram(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    if rsi_value is None or macd_hist is None:
        notes.append(f"RSI/MACD not warm: {len(closes)} closes")

    bull = [last > vwap_now, ema9 > ema21, ema9_slope > 0,
            rsi_value is not None and rsi_value > 50.0,
            macd_hist is not None and macd_hist > 0]
    bear = [last < vwap_now, ema9 < ema21, ema9_slope < 0,
            rsi_value is not None and rsi_value < 50.0,
            macd_hist is not None and macd_hist < 0]
    momentum_bull, momentum_bear = sum(bull), sum(bear)

    already_broken = False
    if last > or15.high:
        boundary_direction = direction = BULLISH
        already_broken = True
    elif last < or15.low:
        boundary_direction = direction = BEARISH
        already_broken = True
    else:
        dist_high, dist_low = or15.high - last, last - or15.low
        if dist_high < dist_low:
            boundary_direction = BULLISH
        elif dist_low < dist_high:
            boundary_direction = BEARISH
        else:
            boundary_direction = BULLISH if momentum_bull >= momentum_bear else BEARISH
        direction = boundary_direction
        opposite = BEARISH if boundary_direction == BULLISH else BULLISH
        opp_count = momentum_bear if opposite == BEARISH else momentum_bull
        own_count = momentum_bull if boundary_direction == BULLISH else momentum_bear
        if opp_count >= cfg.direction_override_count and opp_count > own_count:
            direction = opposite
            notes.append(f"momentum override: {opp_count}/5 {opposite.lower()} vs nearer {boundary_direction.lower()} boundary")
    if already_broken:
        notes.append(f"already outside OR15 ({direction.lower()})")
        distance = 0.0
    else:
        distance = (or15.high - last) if direction == BULLISH else (last - or15.low)
    distance_or_ratio = distance / or15.width if or15.width > 0 else None
    distance_atr_ratio = distance / atr_value

    metrics = Metrics(
        symbol=symbol, session=session, bar_time=today.index[-1].to_pydatetime(),
        minutes_elapsed=len(today), last=last, open=open_price,
        or5=or5, or10=or10, or15=or15, atr=atr_value, expected_move=expected_move,
        consumed_move=consumed, remaining_em=remaining, rvol=rvol, rvol_sessions=rvol_sessions,
        vwap=vwap_now, vwap_slope=vwap_slope, ema9=ema9, ema21=ema21, ema9_slope=ema9_slope,
        rsi=rsi_value, macd_hist=macd_hist, momentum_bull=momentum_bull, momentum_bear=momentum_bear,
        boundary_direction=boundary_direction, direction=direction, distance=distance,
        distance_or_ratio=distance_or_ratio, distance_atr_ratio=distance_atr_ratio,
        already_broken=already_broken, notes=notes,
    )
    return metrics, reasons


# ------------------------------------------------------------------------------------------ scoring

def _linear(x: float, x_zero: float, x_full: float, points: float) -> float:
    """`points` at x_full, 0 at x_zero, linear in between, clipped; either orientation."""
    if x_full == x_zero:
        return points if x == x_full else 0.0
    t = (x - x_zero) / (x_full - x_zero)
    return points * min(max(t, 0.0), 1.0)


def score_relative_volume(rvol: float | None, cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 3: <1.0x 0, 1.0-1.25x 5, 1.25-1.5x 10, 1.5-2.0x 15, >=2.0x 20 (lower bound
    inclusive). None (index / unknown) -> neutral rvol_neutral_points."""
    if rvol is None:
        return cfg.rvol_neutral_points
    points = cfg.rvol_points[0]
    for threshold, pts in zip(cfg.rvol_thresholds, cfg.rvol_points[1:]):
        if rvol >= threshold:
            points = pts
    return float(points)


def score_atr_expected_move(remaining_em: float, atr_value: float, or15_width: float,
                            cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 4: remaining EM / ATR (12) + OR15 width / ATR (8)."""
    if atr_value is None or not atr_value > 0:
        return 0.0
    remaining = _linear(remaining_em / atr_value, cfg.em_remaining_zero_ratio,
                        cfg.em_remaining_full_ratio, cfg.em_remaining_points)
    or_part = _linear(or15_width / atr_value, cfg.em_or_zero_ratio, cfg.em_or_full_ratio, cfg.em_or_points)
    return remaining + or_part


def score_or_compression(or15_width: float, atr_value: float, cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 5: tight OR15 / ATR ranks higher, but a range under the noise floor is not
    rewarded -- it gets the flat compression_noise_points instead."""
    if atr_value is None or not atr_value > 0:
        return 0.0
    ratio = or15_width / atr_value
    if ratio < cfg.compression_floor_ratio:
        return cfg.compression_noise_points
    return _linear(ratio, cfg.compression_zero_ratio, cfg.compression_full_ratio, cfg.compression_points)


def score_distance(distance: float, or_width: float, already_broken: bool,
                   cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 6: close to the boundary without having broken it. 0 once outside the OR."""
    if already_broken or or_width is None or not or_width > 0:
        return 0.0
    return _linear(distance / or_width, cfg.distance_zero_or_ratio, cfg.distance_full_or_ratio, cfg.distance_points)


def score_momentum(momentum_bull: int, momentum_bear: int, direction: str,
                   cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 7: count of the 5 aligned conditions (price vs VWAP, EMA9 vs EMA21, EMA slope,
    RSI vs 50, MACD sign) in the candidate's direction."""
    count = momentum_bull if direction == BULLISH else momentum_bear
    return min(int(count), 5) * cfg.momentum_points_per_condition


def score_vwap(last: float, vwap: float, vwap_slope: float, direction: str,
               cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 8: price on the trade side of VWAP (6), plus VWAP sloping that way (4).
    Price on the wrong side of VWAP is conflicting structure -> 0."""
    sign = 1.0 if direction == BULLISH else -1.0
    if sign * (last - vwap) <= 0:
        return 0.0
    points = cfg.vwap_side_points
    if sign * vwap_slope > 0:
        points += cfg.vwap_slope_points
    return points


def option_quality_score(chain_rows: list[dict] | None, spot: float | None, direction: str,
                         cfg: ScanConfig = DEFAULT_CONFIG) -> float:
    """Doc section 9, pluggable. Rows are BROKER_INTERFACE OptionQuote dicts (strike/right/bid/ask/
    mid/delta). Calls for a bullish candidate, puts for bearish (rows without `right` are taken as
    the wanted side). Quotes present (2) + a contract with |delta| in the 0.30-0.60 band (3) +
    the tightest spread among those as % of mid (5). 0 when no chain is supplied."""
    if not chain_rows or spot is None or not spot > 0:
        return 0.0
    right = "C" if direction == BULLISH else "P"
    rows = [r for r in chain_rows if str(r.get("right", right)).upper()[:1] == right]
    quoted = [r for r in rows if (r.get("bid") or 0) > 0 and (r.get("ask") or 0) > 0]
    if not quoted:
        return 0.0
    points = cfg.option_quote_points
    band = []
    for r in quoted:
        delta = r.get("delta")
        if delta is not None:
            if cfg.option_delta_min <= abs(float(delta)) <= cfg.option_delta_max:
                band.append(r)
        elif r.get("strike") is not None and abs(float(r["strike"]) - spot) / spot <= cfg.option_atm_band_pct:
            band.append(r)
    if not band:
        return points
    points += cfg.option_delta_points
    spreads = []
    for r in band:
        bid, ask = float(r["bid"]), float(r["ask"])
        mid = float(r.get("mid") or (bid + ask) / 2.0)
        if mid > 0:
            spreads.append((ask - bid) / mid)
    if spreads:
        points += _linear(min(spreads), cfg.option_spread_zero_pct, cfg.option_spread_full_pct,
                          cfg.option_spread_points)
    return points


@dataclass
class Candidate:
    symbol: str
    tier: str
    direction: str
    score: float                 # 0-100 pre-breakout score
    score_with_tier: float       # score + tier tiebreak (A +2, B 0, C -2)
    components: dict
    or5: OpeningRange
    or10: OpeningRange
    or15: OpeningRange
    atr: float
    expected_move: float
    consumed_move: float
    remaining_em: float
    rvol: float | None
    distance: float
    distance_or_ratio: float | None
    distance_atr_ratio: float | None
    vwap: float
    vwap_slope: float
    vwap_relation: str
    ema9: float
    ema21: float
    ema9_slope: float
    ema_state: str
    rsi: float | None
    macd_hist: float | None
    momentum_aligned: int        # aligned conditions (0-5) in `direction`
    last: float
    open: float
    session: date
    bar_time: datetime
    minutes_elapsed: int
    already_broken: bool
    notes: list[str] = field(default_factory=list)


def score_candidate(m: Metrics, tier: str, chain_rows: list[dict] | None = None,
                    cfg: ScanConfig = DEFAULT_CONFIG) -> Candidate:
    components = {
        "relative_volume": score_relative_volume(m.rvol, cfg),
        "atr_expected_move": score_atr_expected_move(m.remaining_em, m.atr, m.or15.width, cfg),
        "or_compression": score_or_compression(m.or15.width, m.atr, cfg),
        "distance_to_breakout": score_distance(m.distance, m.or15.width, m.already_broken, cfg),
        "momentum": score_momentum(m.momentum_bull, m.momentum_bear, m.direction, cfg),
        "vwap_position": score_vwap(m.last, m.vwap, m.vwap_slope, m.direction, cfg),
        "option_quality": option_quality_score(chain_rows, m.last, m.direction, cfg),
    }
    components = {k: round(float(v), 2) for k, v in components.items()}
    score = round(sum(components.values()), 2)
    adjust = float(cfg.tier_adjust.get(tier, 0.0))
    return Candidate(
        symbol=m.symbol, tier=tier, direction=m.direction, score=score,
        score_with_tier=round(score + adjust, 2), components=components,
        or5=m.or5, or10=m.or10, or15=m.or15, atr=m.atr, expected_move=m.expected_move,
        consumed_move=m.consumed_move, remaining_em=m.remaining_em, rvol=m.rvol,
        distance=m.distance, distance_or_ratio=m.distance_or_ratio, distance_atr_ratio=m.distance_atr_ratio,
        vwap=m.vwap, vwap_slope=m.vwap_slope, vwap_relation=m.vwap_relation,
        ema9=m.ema9, ema21=m.ema21, ema9_slope=m.ema9_slope, ema_state=m.ema_state,
        rsi=m.rsi, macd_hist=m.macd_hist,
        momentum_aligned=m.momentum_bull if m.direction == BULLISH else m.momentum_bear,
        last=m.last, open=m.open, session=m.session, bar_time=m.bar_time,
        minutes_elapsed=m.minutes_elapsed, already_broken=m.already_broken, notes=list(m.notes),
    )


# --------------------------------------------------------------------------------------------- scan

ChainFn = Callable[[str, float, str], list[dict] | None]   # (symbol, spot, direction) -> chain rows


@dataclass
class ScanResult:
    candidates: list[Candidate]
    skipped: dict[str, list[str]]
    now: datetime
    elapsed_sec: float


def scan_detailed(universe: dict, now: datetime | None = None, chain_fn: ChainFn | None = None,
                  cfg: ScanConfig = DEFAULT_CONFIG, symbols: list[str] | None = None,
                  intraday: dict[str, pd.DataFrame] | None = None,
                  daily: dict[str, pd.DataFrame] | None = None) -> ScanResult:
    """Score every universe member at `now`. Pre-fetched `intraday` / `daily` dicts (symbol ->
    frame) skip the network; otherwise both are fetched in batches. Symbols with missing data land
    in `skipped` with their reasons; one bad symbol never aborts the scan."""
    t0 = _time.perf_counter()
    now = _now(now)
    members = universe_members(universe)
    if symbols:
        wanted = [s.upper() for s in symbols]
        known = dict(members)
        members = [(s, known.get(s, "?")) for s in wanted]
    names = [s for s, _ in members]
    if intraday is None:
        intraday = fetch_intraday_batch(names, now, cfg.intraday_sessions, cfg)
    if daily is None:
        daily = fetch_daily_batch(names, cfg.daily_days, cfg)
    index_symbol = universe.get("index")
    candidates: list[Candidate] = []
    skipped: dict[str, list[str]] = {}
    for symbol, tier in members:
        try:
            metrics, reasons = compute_metrics(symbol, intraday.get(symbol), daily.get(symbol), now, cfg,
                                               is_index=(symbol == index_symbol))
            if metrics is None:
                skipped[symbol] = reasons
                log.info("skip %s: %s", symbol, "; ".join(reasons))
                continue
            chain = None
            if chain_fn is not None:
                try:
                    chain = chain_fn(symbol, metrics.last, metrics.direction)
                except Exception as e:
                    metrics.notes.append(f"chain unavailable: {e}")
            candidates.append(score_candidate(metrics, tier, chain, cfg))
        except Exception as e:
            log.exception("scan failed for %s", symbol)
            skipped[symbol] = [f"error: {type(e).__name__}: {e}"]
    candidates.sort(key=lambda c: (-c.score_with_tier, -c.score, c.symbol))
    return ScanResult(candidates, skipped, now, _time.perf_counter() - t0)


def scan(universe: dict, now: datetime | None = None, chain_fn: ChainFn | None = None,
         cfg: ScanConfig = DEFAULT_CONFIG, **kwargs) -> list[Candidate]:
    """Ranked candidates (score_with_tier desc). See scan_detailed for the skipped list."""
    return scan_detailed(universe, now, chain_fn, cfg, **kwargs).candidates


# ------------------------------------------------------------------------------------------- output

def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def format_ranking(candidates: list[Candidate], top: int = 10) -> str:
    """Text table in the shape of the doc's section-10 example."""
    header = (f"{'Rank':>4}  {'Ticker':<6} {'Tier':<4} {'Dir':<7} {'Score':>6} {'Raw':>6} "
              f"{'OR Width':>9} {'ATR':>8} {'Rem EM':>8} {'Break Dist':>10} {'RVOL':>6}  Flags")
    lines = [header, "-" * len(header)]
    for rank, c in enumerate(candidates[:top], start=1):
        flags = []
        if c.already_broken:
            flags.append("BROKEN")
        if not c.or15.complete:
            flags.append(f"OR{c.minutes_elapsed}m")
        rvol = "n/a" if c.rvol is None else f"{c.rvol:.1f}x"
        lines.append(f"{rank:>4}  {c.symbol:<6} {c.tier:<4} {c.direction.title():<7} "
                     f"{c.score_with_tier:>6.1f} {c.score:>6.1f} {_money(c.or15.width):>9} "
                     f"{_money(c.atr):>8} {_money(c.remaining_em):>8} {_money(c.distance):>10} "
                     f"{rvol:>6}  {' '.join(flags)}".rstrip())
    if len(lines) == 2:
        lines.append("(no candidates)")
    return "\n".join(lines)


def to_log_row(c: Candidate) -> dict:
    """Flat, JSON-serializable row with every pre-trade field from doc section 18."""
    row = {
        "ticker": c.symbol,
        "date": c.session.isoformat(),
        "bar_time": c.bar_time.isoformat(),
        "minutes_elapsed": c.minutes_elapsed,
        "tier": c.tier,
        "direction": c.direction,
        "score": c.score,
        "score_with_tier": c.score_with_tier,
        "last": round(c.last, 4),
        "open": round(c.open, 4),
        "atr": round(c.atr, 4),
        "expected_move": round(c.expected_move, 4),
        "consumed_move": round(c.consumed_move, 4),
        "remaining_em": round(c.remaining_em, 4),
        "rvol": None if c.rvol is None else round(c.rvol, 3),
        "distance": round(c.distance, 4),
        "distance_or_ratio": None if c.distance_or_ratio is None else round(c.distance_or_ratio, 4),
        "distance_atr_ratio": None if c.distance_atr_ratio is None else round(c.distance_atr_ratio, 4),
        "already_broken": c.already_broken,
        "vwap": round(c.vwap, 4),
        "vwap_slope": round(c.vwap_slope, 5),
        "vwap_relation": c.vwap_relation,
        "ema9": round(c.ema9, 4),
        "ema21": round(c.ema21, 4),
        "ema9_slope": round(c.ema9_slope, 5),
        "ema_state": c.ema_state,
        "rsi": None if c.rsi is None else round(c.rsi, 2),
        "macd_hist": None if c.macd_hist is None else round(c.macd_hist, 5),
        "momentum_aligned": c.momentum_aligned,
        "notes": "; ".join(c.notes),
    }
    for name, value in c.components.items():
        row[f"component_{name}"] = value
    for label, orange in (("or5", c.or5), ("or10", c.or10), ("or15", c.or15)):
        row[f"{label}_high"] = round(orange.high, 4)
        row[f"{label}_low"] = round(orange.low, 4)
        row[f"{label}_width"] = round(orange.width, 4)
        row[f"{label}_complete"] = orange.complete
    return row


# ---------------------------------------------------------------------------------------------- CLI

def _latest_session(intraday: dict[str, pd.DataFrame]) -> date | None:
    latest = None
    for df in intraday.values():
        if df is not None and not df.empty:
            d = df.index[-1].date()
            latest = d if latest is None or d > latest else latest
    return latest


def _resolve_now(args, intraday: dict[str, pd.DataFrame], real_now: datetime,
                 notes: list[str]) -> datetime:
    """`now` for the scan: --date/--at override the clock; if the target date has no bars yet
    (pre-market, weekend, holiday) fall back to the last session in the data."""
    now = real_now
    if args.date:
        d = date.fromisoformat(args.date)
        now = now.replace(year=d.year, month=d.month, day=d.day)
    if args.at:
        hh, mm = (int(x) for x in args.at.split(":"))
        now = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    have_target = any(not df.empty and (df.index.date == now.date()).any() for df in intraday.values())
    if not have_target:
        latest = _latest_session(intraday)
        if latest is not None:
            notes.append(f"no bars for {now.date()}; using last session {latest}")
            now = now.replace(year=latest.year, month=latest.month, day=latest.day)
            if not args.at:
                now = now.replace(hour=9, minute=45, second=0, microsecond=0)
                notes.append("no --at given: scoring at 09:45 (end of the 15-minute opening range)")
    return now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ORB pre-breakout scanner (ranking only, no orders).")
    parser.add_argument("--at", help="HH:MM ET; score with bars up to that minute (default: now)")
    parser.add_argument("--date", help="YYYY-MM-DD session to score (default: today / last session)")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--tickers", help="comma-separated subset, e.g. TSLA,AMZN,SPX")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE_PATH))
    parser.add_argument("--log-rows", action="store_true", help="also print to_log_row() JSON per candidate")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    cfg = DEFAULT_CONFIG
    universe = load_universe(args.universe)
    symbols = [s.strip().upper() for s in args.tickers.split(",") if s.strip()] if args.tickers else None
    members = universe_members(universe)
    names = symbols or [s for s, _ in members]

    real_now = datetime.now(ET)
    t0 = _time.perf_counter()
    intraday = fetch_intraday_batch(names, real_now, cfg.intraday_sessions, cfg)
    daily = fetch_daily_batch(names, cfg.daily_days, cfg)
    fetch_sec = _time.perf_counter() - t0

    notes: list[str] = []
    now = _resolve_now(args, intraday, real_now, notes)
    result = scan_detailed(universe, now, cfg=cfg, symbols=symbols, intraday=intraday, daily=daily)

    print(f"ORB pre-breakout ranking @ {now:%Y-%m-%d %H:%M} ET  "
          f"({len(result.candidates)} scored, {len(result.skipped)} skipped; "
          f"fetch {fetch_sec:.1f}s, score {result.elapsed_sec:.2f}s)")
    for n in notes:
        print(f"  note: {n}")
    print()
    print(format_ranking(result.candidates, top=args.top))
    if result.skipped:
        print()
        print("Skipped:")
        for sym, reasons in result.skipped.items():
            print(f"  {sym}: {'; '.join(reasons)}")
    if args.log_rows:
        print()
        for c in result.candidates[:args.top]:
            print(json.dumps(to_log_row(c), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Pure 0DTE strategy logic: phases, breakout detection, strikes, credit, momentum.

No I/O here. Candles are pandas DataFrames indexed by tz-aware ET bar START
time with columns open/high/low/close/volume, containing completed bars only.

Breakout level source (config.BREAKOUT_LEVEL_SOURCE):
  ES_TO_SPX (default): the overnight high/low come from ES futures (the only
    instrument trading 18:00-09:30), are translated into SPX terms with the
    09:30 basis (SPX - ES), and compared against live SPX candles. Yahoo's ES
    quotes are ~10 minutes delayed while ^GSPC is real-time, so confirming on
    SPX candles keeps the 2-candle confirmation on current prices.
  ES: compare ES candles directly against the raw ES levels. Simpler, but the
    signal arrives ~10 minutes late.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from math import ceil, floor

import pandas as pd

import config

BULLISH = "BULLISH"
BEARISH = "BEARISH"


class Phase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    OVERNIGHT_ONLY = "OVERNIGHT_ONLY"
    OVERNIGHT_OR_ORB = "OVERNIGHT_OR_ORB"
    ORB_ONLY = "ORB_ONLY"
    NO_NEW_ENTRIES = "NO_NEW_ENTRIES"
    CLOSED = "CLOSED"


ENTRY_PHASES = {Phase.OVERNIGHT_ONLY, Phase.OVERNIGHT_OR_ORB, Phase.ORB_ONLY}
OVERNIGHT_PHASES = {Phase.OVERNIGHT_ONLY, Phase.OVERNIGHT_OR_ORB}
ORB_PHASES = {Phase.OVERNIGHT_OR_ORB, Phase.ORB_ONLY}


@dataclass(frozen=True)
class Levels:
    high: float
    low: float
    established_at: datetime

    def shifted(self, basis: float) -> "Levels":
        return Levels(self.high + basis, self.low + basis, self.established_at)


@dataclass(frozen=True)
class Breakout:
    direction: str
    candle_times: tuple[datetime, datetime]


@dataclass(frozen=True)
class SpreadQuote:
    mid: float
    bid_side: float
    ask_side: float


def at_time(now: datetime, t: time) -> datetime:
    return now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)


def minutes_since_open(now: datetime) -> float:
    return (now - at_time(now, config.MARKET_OPEN)).total_seconds() / 60.0


def phase(now: datetime) -> Phase:
    if now >= at_time(now, config.FORCE_CLOSE_TIME):
        return Phase.CLOSED
    if now >= at_time(now, config.LAST_ENTRY_TIME):
        return Phase.NO_NEW_ENTRIES
    m = minutes_since_open(now)
    if m < 0:
        return Phase.PRE_OPEN
    if m < config.OPENING_RANGE_MINUTES:
        return Phase.OVERNIGHT_ONLY
    if m < config.OVERNIGHT_ENTRY_END_MIN:
        return Phase.OVERNIGHT_OR_ORB
    return Phase.ORB_ONLY


def detect_breakout(candles: pd.DataFrame, level_high: float, level_low: float,
                    level_time: datetime) -> Breakout | None:
    if candles is None or len(candles) < 2:
        return None
    prev, last = candles.iloc[-2], candles.iloc[-1]
    t_prev, t_last = candles.index[-2].to_pydatetime(), candles.index[-1].to_pydatetime()
    if t_prev < level_time:
        return None
    if prev.close > level_high and last.close > level_high and last.high > prev.high:
        return Breakout(BULLISH, (t_prev, t_last))
    if prev.close < level_low and last.close < level_low and last.low < prev.low:
        return Breakout(BEARISH, (t_prev, t_last))
    return None


def direction_to_spread(direction: str) -> str:
    if direction == BULLISH:
        return "P"
    if direction == BEARISH:
        return "C"
    raise ValueError(f"unknown direction {direction!r}")


def select_strikes(spot: float, right: str, width: int) -> tuple[float, float]:
    """Return (short_strike, long_strike). ITM credit spreads by design."""
    if right == "P":
        long = float(floor(spot / 5) * 5 + 5)
        return long + width, long
    if right == "C":
        long = float(ceil(spot / 5) * 5 - 5)
        return long - width, long
    raise ValueError(f"unknown right {right!r}")


def width_for_tier(tier: int) -> int:
    return config.WIDTH_BY_TIER[tier]


def credit_range(width: int) -> tuple[float, float]:
    return config.CREDIT_RANGE_BY_WIDTH[width]


def spread_quote(chain_quotes: list[dict], short_strike: float, long_strike: float) -> SpreadQuote | None:
    by_strike = {round(q["strike"], 2): q for q in chain_quotes}
    short, long = by_strike.get(round(short_strike, 2)), by_strike.get(round(long_strike, 2))
    if short is None or long is None:
        return None
    return SpreadQuote(
        mid=round(short["mid"] - long["mid"], 2),
        bid_side=round(short["bid"] - long["ask"], 2),
        ask_side=round(short["ask"] - long["bid"], 2),
    )


def entry_credit(chain_quotes: list[dict], short_strike: float, long_strike: float,
                 width: int) -> tuple[SpreadQuote | None, str | None]:
    """(quote, None) when the mid is inside the credit range, else (quote, reason)."""
    quote = spread_quote(chain_quotes, short_strike, long_strike)
    if quote is None:
        return None, f"STRIKES_NOT_IN_CHAIN short={short_strike} long={long_strike}"
    lo, hi = credit_range(width)
    if quote.mid < lo:
        return quote, f"CREDIT_BELOW_MIN mid={quote.mid:.2f} < {lo:.2f}"
    if quote.mid > hi:
        return quote, f"CREDIT_ABOVE_MAX mid={quote.mid:.2f} > {hi:.2f}"
    return quote, None


def _signed_bodies(candles: pd.DataFrame, direction: str) -> pd.Series:
    sign = 1.0 if direction == BULLISH else -1.0
    return (candles["close"] - candles["open"]) * sign


def momentum(candles: pd.DataFrame, direction: str, n: int | None = None) -> float:
    """Average body (close-open) of the last n completed candles, positive = with the trade."""
    n = n or config.MOMENTUM_CANDLES
    if candles is None or candles.empty:
        return 0.0
    return float(_signed_bodies(candles.tail(n), direction).mean())


def momentum_continuing(candles: pd.DataFrame, direction: str, ratio: float | None = None) -> bool:
    """Last candle body is in the trade direction and at least `ratio` of the previous body."""
    ratio = ratio if ratio is not None else config.MOMENTUM_CONTINUE_RATIO
    if candles is None or len(candles) < 2:
        return False
    bodies = _signed_bodies(candles.tail(2), direction)
    prev, last = float(bodies.iloc[0]), float(bodies.iloc[1])
    return last > 0 and last >= ratio * prev


def momentum_slowed(current: float, reference: float, pct: float | None = None) -> bool:
    pct = pct if pct is not None else config.MOMENTUM_SLOWDOWN_PCT
    return current < reference * (1.0 - pct)


def is_news_day(d: date) -> bool:
    return d.isoformat() in config.NEWS_DAYS


def news_mode(d: date) -> str | None:
    return config.NEWS_DAY_MODE if is_news_day(d) else None


def setup_allowed(setup: str, d: date) -> bool:
    mode = news_mode(d)
    if mode == "skip":
        return False
    if mode == "orb_only":
        return setup == "ORB"
    return True


def candle_end(start: datetime, interval: str) -> datetime:
    return start + timedelta(minutes=int(interval.rstrip("m")))

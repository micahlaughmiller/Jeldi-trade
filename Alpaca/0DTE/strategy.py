"""Pure 0DTE strategy logic: phases, breakout detection, ORB setup, strikes, credit, momentum.

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
from datetime import date, datetime, time
from enum import StrEnum
from math import ceil, floor

import pandas as pd

import config

BULLISH = "BULLISH"
BEARISH = "BEARISH"
MOMENTUM = "MOMENTUM"     # ORB entry kinds
PULLBACK = "PULLBACK"


class Phase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    OVERNIGHT_ONLY = "OVERNIGHT_ONLY"
    ORB_ONLY = "ORB_ONLY"
    NO_NEW_ENTRIES = "NO_NEW_ENTRIES"
    CLOSED = "CLOSED"


ENTRY_PHASES = {Phase.OVERNIGHT_ONLY, Phase.ORB_ONLY}
OVERNIGHT_PHASES = {Phase.OVERNIGHT_ONLY}
ORB_PHASES = {Phase.ORB_ONLY}


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
    if m < config.OVERNIGHT_ENTRY_END_MIN:
        return Phase.OVERNIGHT_ONLY
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


class OrbSetup:
    """Opening-range break state machine fed one completed ORB candle at a time.

    WAITING -> BROKEN (close beyond a level) -> PULLED_BACK (wick touches the level)
    -> ENTRY, or straight from BROKEN to ENTRY when a candle closes beyond the break
    close. Any close back through the level resets; after an entry the setup stays
    DONE until a candle closes back inside the range.
    """

    WAITING = "WAITING"
    BROKEN = "BROKEN"
    PULLED_BACK = "PULLED_BACK"
    DONE = "DONE"

    def __init__(self, or_high: float, or_low: float):
        self.or_high = or_high
        self.or_low = or_low
        self.state = self.WAITING
        self.direction: str | None = None
        self.break_close: float | None = None
        self.candles_since_break = 0
        self.entry_kind: str | None = None   # MOMENTUM or PULLBACK for the entry just fired

    def _reset(self) -> None:
        self.state = self.WAITING
        self.direction = None
        self.break_close = None
        self.candles_since_break = 0
        self.entry_kind = None

    def _arm(self, direction: str, close: float) -> None:
        self.state = self.BROKEN
        self.direction = direction
        self.break_close = close
        self.candles_since_break = 0

    def _enter(self, kind: str) -> str:
        self.state = self.DONE
        self.entry_kind = kind
        return self.direction

    def update(self, candle: pd.Series) -> str | None:
        """Feed one completed candle; returns BULLISH/BEARISH exactly once per entry."""
        close, open_ = float(candle.close), float(candle.open)
        if self.state == self.DONE:
            if self.or_low <= close <= self.or_high:
                self._reset()
            return None
        if self.state == self.WAITING:
            if close > self.or_high:
                self._arm(BULLISH, close)
            elif close < self.or_low:
                self._arm(BEARISH, close)
            return None
        self.candles_since_break += 1
        if self.candles_since_break > config.ORB_SETUP_TIMEOUT_CANDLES:
            self._reset()
            return self.update(candle)
        # Mirror the bearish case onto the bullish one: positive = beyond the broken level.
        sign = 1.0 if self.direction == BULLISH else -1.0
        level = self.or_high if self.direction == BULLISH else self.or_low
        touch = float(candle.low) if self.direction == BULLISH else float(candle.high)
        if sign * (close - level) <= 0:
            self._reset()
            return self.update(candle)
        if self.state == self.BROKEN:
            if sign * (touch - level) <= 0:
                self.state = self.PULLED_BACK
            elif sign * (close - self.break_close) > 0:
                return self._enter(MOMENTUM)
            return None
        if sign * (close - open_) > 0:
            return self._enter(PULLBACK)
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


def credit_range_b(width: int) -> tuple[float, float]:
    return config.B_CREDIT_RANGE_BY_WIDTH[width]


def exit_setting(strat: str, name: str, default=None):
    """Per-strategy exit knob from EXIT_TUNING_BY_STRATEGY, else the module-level config value."""
    v = config.EXIT_TUNING_BY_STRATEGY.get(strat, {}).get(name)
    return v if v is not None else getattr(config, name, default)


def exit_levels(strat: str) -> tuple[float, float]:
    """(profit_target, stop_loss) in spread-price points for strategy A or B."""
    if strat == "A":
        return config.PROFIT_TARGET, config.STOP_LOSS
    if strat == "B":
        return config.B_PROFIT_TARGET, config.B_STOP_LOSS
    raise ValueError(f"unknown strategy {strat!r}")


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


def credit_bias(width: int) -> float:
    return config.A_CREDIT_BIAS_BY_WIDTH[width]


def select_strikes_a(spot: float, right: str, width: int,
                     chain_rows: list[dict]) -> tuple[float | None, float | None, SpreadQuote | str]:
    """Strategy A strikes: the ITM spread from select_strikes, walked toward spot while too deep.

    A mid above credit_bias(width) -- or above the band's max -- means the short strike is deeper
    ITM than wanted. Step both strikes one strike toward spot, up to A_MAX_STRIKE_WALK times, as
    long as the short strike stays ITM. The first quote at or under the bias wins; if the walk runs
    out (or would drop the credit below the band's minimum) the last in-band quote is used.
    Returns (short, long, quote) or (None, None, reason).
    """
    short, long = select_strikes(spot, right, width)
    lo, hi = credit_range(width)
    bias = credit_bias(width)
    step = -5.0 if right == "P" else 5.0
    best: tuple[float, float, SpreadQuote] | None = None
    reason = ""
    for _ in range(config.A_MAX_STRIKE_WALK + 1):
        quote = spread_quote(chain_rows, short, long)
        if quote is None:
            reason = f"STRIKES_NOT_IN_CHAIN short={short:g} long={long:g}"
            break
        if quote.mid < lo:
            reason = f"CREDIT_BELOW_MIN mid={quote.mid:.2f} < {lo:.2f} at {short:g}/{long:g}"
            break
        if quote.mid <= bias:
            return short, long, quote
        if quote.mid <= hi:
            best = (short, long, quote)
        reason = f"CREDIT_ABOVE_MAX mid={quote.mid:.2f} > {hi:.2f} at {short:g}/{long:g}"
        next_short = short + step
        if not (next_short > spot if right == "P" else next_short < spot):
            reason += " (next strike would not be ITM)"
            break
        short, long = next_short, long + step
    else:
        reason += f" after {config.A_MAX_STRIKE_WALK} strike walk(s)"
    if best is not None:
        return best
    return None, None, reason


def expected_move(chain_calls: list[dict], chain_puts: list[dict], spot: float) -> float | None:
    """ATM straddle mid (call + put at the strike nearest spot); None if a side is missing."""
    strikes = {round(q["strike"], 2) for q in chain_calls} | {round(q["strike"], 2) for q in chain_puts}
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: (abs(k - spot), k))
    call = next((q for q in chain_calls if round(q["strike"], 2) == atm), None)
    put = next((q for q in chain_puts if round(q["strike"], 2) == atm), None)
    if call is None or put is None:
        return None
    return round(call["mid"] + put["mid"], 2)


def select_strikes_b(spot: float, right: str, width: int, em: float,
                     chain_rows: list[dict]) -> tuple[float | None, float | None, SpreadQuote | str]:
    """OTM spread one expected move from spot, walked strike by strike into B's credit range.

    Returns (short, long, quote) or (None, None, reason).
    """
    if right == "P":
        short, toward_spot = float(ceil((spot - em) / 5) * 5), 5.0
    elif right == "C":
        short, toward_spot = float(floor((spot + em) / 5) * 5), -5.0
    else:
        raise ValueError(f"unknown right {right!r}")
    lo, hi = credit_range_b(width)
    for _ in range(20):
        distance = abs(spot - short)
        if distance < config.B_MIN_DISTANCE_FROM_SPOT:
            return None, None, f"B_NO_STRIKE_IN_RANGE short={short:g} within {config.B_MIN_DISTANCE_FROM_SPOT} of spot {spot:.2f}"
        if distance > 2 * em:
            return None, None, f"B_NO_STRIKE_IN_RANGE short={short:g} beyond 2x EM {em:.2f} from spot {spot:.2f}"
        long = short - width if right == "P" else short + width
        quote = spread_quote(chain_rows, short, long)
        if quote is None:
            return None, None, f"STRIKES_NOT_IN_CHAIN short={short:g} long={long:g}"
        if quote.mid < lo:
            short += toward_spot
        elif quote.mid > hi:
            short -= toward_spot
        else:
            return short, long, quote
    return None, None, f"B_NO_STRIKE_IN_RANGE no credit in {lo:.2f}-{hi:.2f} after 20 steps"


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


def candle_against(candles: pd.DataFrame, direction: str) -> bool:
    """Last completed candle closed against the trade (red for BULLISH, green for BEARISH)."""
    if candles is None or candles.empty:
        return False
    return float(_signed_bodies(candles.tail(1), direction).iloc[0]) < 0


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

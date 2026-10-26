"""Shared scaffolding for the Strategy F tests: synthetic 1-min/daily bars around a $430 stock, stub
scan/bars/daily functions with call logs, a synchronous executor, and a Bot with F enabled."""

from concurrent.futures import Future
from datetime import date, datetime, time, timedelta

import pandas as pd

import config
import scheduler
from conftest import et
from fake_broker import FakeBroker
from orb_scanner import Candidate, OpeningRange, ScanResult
from risk_manager import RiskManager
from stock_orb import COLUMNS, StockOrbTrader
from strategy import BULLISH

TODAY = date(2026, 9, 17)
BASE = 430.0
UNIVERSE = {"index": "SPX", "tiers": {"A": ["TSLA", "AMZN"], "B": ["META"], "C": ["PFE"]}}
STOCKS = ["TSLA", "AMZN", "META", "PFE"]
OR_HIGH, OR_LOW = 433.55, 429.95          # or15_closes() with a 0.05 wick
BREAK = [433.8, 434.0, 434.2, 434.4, 434.6]        # 09:45 candle closes above the OR high -> armed
MOMENTUM = [434.7, 434.8, 434.9, 435.0, 435.1]     # 09:50 candle holds above and beats the break close -> MOMENTUM
HOLD = [435.0, 435.05, 435.1, 435.15, 435.2]       # 09:55 candle stays outside the range (setup DONE)
INSIDE = [433.0, 433.1, 433.0, 432.9, 433.0]       # a candle closing back inside the range (setup resets)
RANK_AT = et(9, 45, 45)
SPX_PUTS = {7610.0: (7.9, 8.1), 7605.0: (4.9, 5.1), 7600.0: (19.0, 21.0),
            7565.0: (1.9, 2.1), 7560.0: (1.0, 1.2), 7555.0: (0.4, 0.6), 7550.0: (0.2, 0.3)}


def bars(day: date, closes: list[float], volume: float = 100.0, wick: float = 0.05,
         start: time = time(9, 30)) -> pd.DataFrame:
    """1-min bars: each open = previous close, high/low = body +/- wick, constant volume."""
    idx = pd.DatetimeIndex([datetime.combine(day, start, tzinfo=config.ET) + timedelta(minutes=i)
                            for i in range(len(closes))])
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "open": opens,
        "high": [max(o, c) + wick for o, c in zip(opens, closes)],
        "low": [min(o, c) - wick for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [float(volume)] * len(closes),
    }, index=idx)


def weekdays_before(day: date, n: int) -> list[date]:
    out, d = [], day - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def or15_closes(base: float = BASE, step: float = 0.25) -> list[float]:
    return [base + step * i for i in range(15)]


def today_closes(*blocks: list[float]) -> list[float]:
    return or15_closes() + [c for block in blocks for c in block]


def intraday_frame(closes: list[float], today_volume: float = 150.0, prior_volume: float = 100.0,
                   prior_sessions: int = 5, prior_bars: int = 60, day: date = TODAY) -> pd.DataFrame:
    frames = [bars(d, [BASE] * prior_bars, prior_volume) for d in weekdays_before(day, prior_sessions)]
    frames.append(bars(day, closes, today_volume))
    return pd.concat(frames).sort_index()


def daily_frame(n: int = 20, high: float = 440.0, low: float = 420.0, close: float = BASE) -> pd.DataFrame:
    """Identical daily bars ending the session before TODAY -> ATR == high - low == 20."""
    days = weekdays_before(TODAY, n)
    idx = pd.DatetimeIndex([datetime.combine(d, time(0, 0), tzinfo=config.ET) for d in days])
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


def candidate(symbol: str, tier: str = "A", score: float = 80.0, direction: str = BULLISH,
              or_high: float = OR_HIGH, or_low: float = OR_LOW, already_broken: bool = False,
              minutes_elapsed: int = 15) -> Candidate:
    orange = OpeningRange(or_high, or_low, or_high - or_low, 15, True)
    adjust = {"A": 2.0, "B": 0.0, "C": -2.0}.get(tier, 0.0)
    return Candidate(
        symbol=symbol, tier=tier, direction=direction, score=score, score_with_tier=score + adjust,
        components={"relative_volume": score}, or5=orange, or10=orange, or15=orange, atr=20.0,
        expected_move=20.0, consumed_move=3.5, remaining_em=16.5, rvol=1.5, distance=0.05,
        distance_or_ratio=0.01, distance_atr_ratio=0.0025, vwap=431.0, vwap_slope=0.1, vwap_relation="ABOVE",
        ema9=433.0, ema21=432.0, ema9_slope=0.05, ema_state="BULL", rsi=60.0, macd_hist=0.1, momentum_aligned=5,
        last=433.5, open=BASE, session=TODAY, bar_time=et(9, 44), minutes_elapsed=minutes_elapsed,
        already_broken=already_broken, notes=[],
    )


def scan_result(cands: list[Candidate], now: datetime = RANK_AT) -> ScanResult:
    ranked = sorted(cands, key=lambda c: (-c.score_with_tier, -c.score, c.symbol))
    return ScanResult(ranked, {}, now, 0.1)


def tsla_put_chain() -> dict[float, tuple[float, float]]:
    # $2.50 grid; 417.5/412.5 pays 0.80 (13-22% of a $5 width = 0.65-1.10)
    return {437.5: (9.4, 9.6), 435.0: (7.9, 8.1), 432.5: (6.4, 6.6), 430.0: (5.2, 5.4), 427.5: (4.2, 4.4),
            425.0: (3.4, 3.6), 422.5: (2.7, 2.9), 420.0: (2.15, 2.35), 417.5: (1.75, 1.95), 415.0: (1.25, 1.45),
            412.5: (0.95, 1.15), 410.0: (0.7, 0.9), 407.5: (0.5, 0.7), 405.0: (0.35, 0.55), 402.5: (0.25, 0.4),
            400.0: (0.15, 0.3)}


def tsla_call_chain() -> dict[float, tuple[float, float]]:
    # ATM straddle at 435: call 9.00 + put 8.00 = EM 17.00; 455/460 pays 0.80
    return {435.0: (8.9, 9.1), 437.5: (7.6, 7.8), 440.0: (6.4, 6.6), 442.5: (5.3, 5.5), 445.0: (4.3, 4.5),
            447.5: (3.5, 3.7), 450.0: (2.8, 3.0), 452.5: (2.2, 2.4), 455.0: (1.75, 1.95), 457.5: (1.35, 1.55),
            460.0: (0.95, 1.15), 462.5: (0.7, 0.9), 465.0: (0.5, 0.7)}


class SyncExecutor:
    """Runs submitted jobs inline; the engine still consumes the finished future on its next step."""

    def __init__(self):
        self.submitted: list[str] = []

    def submit(self, fn, *args, **kwargs) -> Future:
        self.submitted.append(fn.__name__)
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


class DataStub:
    """Programmable bars_fn / daily_fn / scan_fn with call logs. Frames hold the WHOLE synthetic day;
    the engine's own `now` filtering decides what is visible."""

    def __init__(self):
        self.frames: dict[str, pd.DataFrame] = {}
        self.daily: dict[str, pd.DataFrame] = {}
        self.result: ScanResult | None = None
        self.calls: dict[str, list] = {"bars": [], "daily": [], "scan": []}
        self.bars_error: Exception | None = None
        self.scan_error: Exception | None = None

    def set_frames(self, closes: list[float], symbols: list[str] = STOCKS, **kw) -> None:
        for s in symbols:
            self.frames[s] = intraday_frame(closes, **kw)

    def bars_fn(self, symbols, now=None, sessions=None):
        self.calls["bars"].append((list(symbols), now, sessions))
        if self.bars_error is not None:
            raise self.bars_error
        return {s: self.frames.get(s, pd.DataFrame(columns=COLUMNS)) for s in symbols}

    def daily_fn(self, symbols, days=None):
        self.calls["daily"].append(list(symbols))
        return {s: self.daily.get(s, daily_frame()) for s in symbols}

    def scan_fn(self, universe, now, chain_fn=None, intraday=None, daily=None):
        self.calls["scan"].append({"now": now, "chain_fn": chain_fn, "intraday": intraday, "daily": daily})
        if self.scan_error is not None:
            raise self.scan_error
        return self.result


def make_broker(equity: float = 10_000.0) -> FakeBroker:
    broker = FakeBroker(equity=equity, today=TODAY)
    broker.spot = 7600.0
    broker.set_chain("P", SPX_PUTS)
    broker.set_chain("C", {7600.0: (21.0, 23.0)})
    for sym in STOCKS:
        broker.set_chain("P", tsla_put_chain(), underlying=sym)
        broker.set_chain("C", tsla_call_chain(), underlying=sym)
        broker.expirations[sym] = [TODAY + timedelta(days=2)]
        broker.spots[sym] = 436.0
    return broker


def make_bot_f(tmp_path, monkeypatch, data: DataStub, equity: float = 10_000.0,
               strategies: tuple[str, ...] = ("A", "B", "F"), broker: FakeBroker | None = None) -> scheduler.Bot:
    """A Bot with F wired to the stubbed data functions. Pass `broker` to restart against the same
    (still-holding) broker."""
    monkeypatch.setattr(config, "STRATEGIES", strategies)
    monkeypatch.setattr(config, "F_CHAIN_CALL_GAP_SEC", 0)
    broker = broker or make_broker(equity)
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: broker)
    bot = scheduler.Bot(dry_run=False, once=True)
    bot.today = TODAY
    bot.risk = RiskManager(equity)
    if "F" in strategies:
        bot.f.shutdown()
        bot.f = StockOrbTrader(bot, universe=UNIVERSE, scan_fn=data.scan_fn, bars_fn=data.bars_fn,
                               daily_fn=data.daily_fn, chain_broker_factory=lambda: broker, executor=SyncExecutor())
    return bot


def rank(bot: scheduler.Bot, data: DataStub, cands: list[Candidate] | None = None,
         now: datetime = RANK_AT) -> None:
    """Submit and consume the 09:45 scan (two steps with the synchronous executor)."""
    data.result = scan_result(cands if cands is not None else [candidate("TSLA")], now)
    bot.f.step(now)
    bot.f.step(now)

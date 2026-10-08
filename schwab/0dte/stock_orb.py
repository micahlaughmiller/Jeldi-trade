"""Strategy F engine: stock opening-range-breakout credit spreads (ASTRA only).

Ranks data/orb_universe.json once at F_RANK_TIME through orb_scanner in a background thread (the
09:45 fetch never stalls manage()), watches the top qualifiers for a completed 5-minute candle
closing through their 15-minute opening range (one strategy.OrbSetup per ticker, whichever boundary
breaks), confirms the break on volume / VWAP side / EMA alignment / remaining expected move, queues
confirmed signals and enters at most one per tick as a B-style OTM credit spread on the stock's
nearest 0-7 DTE expiration. Every broker and data call runs inside step()'s exception guard: F can
only disable itself for the day, never take the A/B process down. Only this module imports
orb_scanner.
"""

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Callable

import pandas as pd

import config
import orb_scanner
import strategy
from broker import BrokerError
from journal import format_entry_card
from position_manager import OpenSpread
from risk_manager import contracts_for, limit
from strategy import BULLISH, OrbSetup, at_time

log = logging.getLogger("0dte.f")

STRATEGY = "F"
SETUP = "STOCK_ORB"
MOMENTUM_REENTRY = "MOMENTUM_REENTRY"
COLUMNS = ["open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------------------ pure helpers

def _today_bars(bars: pd.DataFrame | None, open_dt: datetime) -> pd.DataFrame:
    if bars is None or bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
        return pd.DataFrame(columns=COLUMNS)
    return bars[bars.index >= open_dt]


def _completed(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    return bars[bars.index + timedelta(minutes=1) <= now]


def _bars_in_window(bars: pd.DataFrame | None, open_dt: datetime, end: datetime, now: datetime) -> int | None:
    """Completed 1-min bars inside [open_dt, end); None when the symbol has no bars today at all."""
    today = _today_bars(bars, open_dt)
    return None if today.empty else int(len(_completed(today[today.index < end], now)))


def _candle_dict(candle: pd.Series) -> dict:
    return {c: float(candle[c]) for c in COLUMNS}


def completed_candles(bars: pd.DataFrame | None, now: datetime, minutes: int) -> tuple[pd.DataFrame, list[datetime]]:
    """Resample 1-minute bars into `minutes`-minute candles. A candle counts only once its LAST 1-minute
    bar is present (without it the close and volume are another bar's), so the second value lists the
    ends of candles that have ended by `now` but are still missing that bar."""
    empty = pd.DataFrame(columns=COLUMNS)
    if bars is None or bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
        return empty, []
    done = _completed(bars, now)
    if done.empty:
        return empty, []
    starts = done.index.floor(f"{minutes}min")
    agg = done.groupby(starts).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                   close=("close", "last"), volume=("volume", "sum"))
    last_bar = pd.DatetimeIndex(done.index.to_series().groupby(starts).max().reindex(agg.index))
    span = timedelta(minutes=minutes)
    ended = agg.index + span <= now
    complete = ended & (last_bar == agg.index + span - timedelta(minutes=1))
    waiting = [t.to_pydatetime() + span for t in agg.index[ended & ~complete]]
    return agg[complete].astype(float), waiting


def normal_volume(bars: pd.DataFrame | None, session, start: datetime, minutes: int) -> float | None:
    """Mean volume of the same [start, start + minutes) time-of-day window over the prior sessions in
    the frame; None without prior sessions."""
    if bars is None or bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
        return None
    prior = bars[bars.index.date < session]
    if prior.empty:
        return None
    tod = prior.index.time
    lo, hi = start.time(), (start + timedelta(minutes=minutes)).time()
    window = prior[(tod >= lo) & (tod < hi)]
    if window.empty:
        return None
    return float(window.groupby(window.index.date)["volume"].sum().mean())


def confirm_break(direction: str, candle: dict | None, metrics: orb_scanner.Metrics | None,
                  normal_vol: float | None) -> list[str]:
    """Rejection reasons for a break (empty = confirmed). The volume check needs the break candle and a
    baseline; a re-check of a queued signal passes neither and re-evaluates only the live conditions."""
    if metrics is None:
        return ["NO_METRICS"]
    reasons: list[str] = []
    if candle is not None and normal_vol is not None \
            and float(candle["volume"]) < config.F_BREAK_VOLUME_RATIO * normal_vol:
        reasons.append("LOW_VOLUME")
    bull = direction == BULLISH
    if not (metrics.last > metrics.vwap if bull else metrics.last < metrics.vwap):
        reasons.append("WRONG_SIDE_OF_VWAP")
    if not (metrics.ema9 > metrics.ema21 if bull else metrics.ema9 < metrics.ema21):
        reasons.append("EMA_NOT_ALIGNED")
    if metrics.remaining_em < config.F_MIN_REMAINING_EM_FRAC * metrics.expected_move:
        reasons.append("EM_EXHAUSTED")
    return reasons


# -------------------------------------------------------------------------------- state types

@dataclass
class WatchItem:
    symbol: str
    tier: str
    rank: int
    direction: str            # the scanner's likely break direction (journaled; enforced only if configured)
    score: float
    score_with_tier: float
    or_high: float
    or_low: float
    scan_row: dict
    setup: OrbSetup
    last_candle: datetime | None = None
    reentry_from: datetime | None = None   # set when an F position in this ticker closes: momentum re-entry watch


@dataclass
class StockSignal:
    symbol: str
    tier: str
    rank: int
    direction: str
    kind: str
    candle_time: datetime
    candle_minutes: int
    candle: dict
    break_close: float | None
    or_high: float
    or_low: float
    scanner_direction: str
    scan_row: dict
    confirm: dict | None = None
    confirmed_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{SETUP}:{self.symbol}:{self.candle_time.isoformat()}"

    @property
    def candle_end(self) -> datetime:
        return self.candle_time + timedelta(minutes=self.candle_minutes)

    @property
    def direction_match(self) -> bool:
        return self.direction == self.scanner_direction


@dataclass
class ScanOutcome:
    result: orb_scanner.ScanResult | None   # None while the 1-min bars still lag the opening range
    daily: dict[str, pd.DataFrame]
    median_minutes: float | None            # median completed 1-min bars INSIDE the opening range (max F_OR_MINUTES)
    now: datetime                           # the scan's clock: decides whether this was a late start
    or_bars: dict[str, int]                 # per symbol: completed 1-min bars inside the opening-range window


# ------------------------------------------------------------------------------------- engine

class StockOrbTrader:
    def __init__(self, bot, *, universe: dict | None = None,
                 scan_fn: Callable = orb_scanner.scan_detailed,
                 bars_fn: Callable = orb_scanner.fetch_intraday_batch,
                 daily_fn: Callable = orb_scanner.fetch_daily_batch,
                 chain_broker_factory: Callable[[], object] | None = None, executor=None):
        self.bot = bot
        self.universe = universe or orb_scanner.load_universe(
            Path(config.__file__).resolve().parent / config.F_UNIVERSE_PATH)
        self.scan_fn, self.bars_fn, self.daily_fn = scan_fn, bars_fn, daily_fn
        self.chain_broker_factory = chain_broker_factory
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="f-scan")
        self.names = [s for s, _ in orb_scanner.universe_members(self.universe)]
        self.disabled_reason: str | None = None
        self.ranked_at: datetime | None = None
        self.watch: list[WatchItem] = []
        self.daily: dict[str, pd.DataFrame] = {}
        self.bars: dict[str, pd.DataFrame] = {}
        self.candles5: dict[str, pd.DataFrame] = {}
        self.next_check: datetime | None = None          # _monitor pacing (watch list + open roots)
        self._candles_due: datetime | None = None        # refresh_candles pacing (open roots only)
        self.pending: list[StockSignal] = []
        self._waiting_on: str | None = None           # the transient entry block the pending signals are waiting out (logged once)
        self.entries_by_ticker: dict[str, int] = {}
        self.errors = 0
        self._scan_future: Future | None = None
        self._scan_attempts = 0
        self._next_scan_at: datetime | None = None
        self._prewarmed = False
        self._late_warned: set[tuple[str, datetime]] = set()

    # ------------------------------------------------------------------ lifecycle

    def reset_setups(self) -> None:
        """News-blackout clear: every ticker's setup back to WAITING for a fresh post-news read, and the
        momentum re-entry watch of an earlier exit goes with it (it was tied to the break being reset)."""
        for w in self.watch:
            w.setup._reset()
            w.reentry_from = None

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def disable(self, reason: str, now: datetime) -> None:
        if self.disabled_reason:
            return
        self.disabled_reason = reason
        self.pending.clear()
        log.error("Strategy F DISABLED for the day: %s", reason)
        self.bot.journal.event("F_DISABLED", now, reason=reason)

    def report_lines(self) -> list[str]:
        tiers = ", ".join(f"{t}:{len(v)}" for t, v in self.universe["tiers"].items())
        stocks = len(self.names) - (1 if self.universe.get("index") else 0)
        cap = limit(STRATEGY, "MAX_CONCURRENT_POSITIONS", config.MAX_CONCURRENT_POSITIONS)
        lines = [
            f"Strategy F: universe {stocks} names ({tiers}), rank at {config.F_RANK_TIME:%H:%M} "
            f"+{config.F_RANK_GRACE_SEC}s, min score {config.F_MIN_SCORE:g}, watch top {config.F_WATCH_TOP}, "
            f"max open {cap}, limits {limit(STRATEGY, 'MAX_TRADES_PER_DAY')}/day "
            f"{limit(STRATEGY, 'MAX_CONSECUTIVE_LOSSES')} losses, exits {config.F_TARGET_PCT:.0%}/"
            f"{config.F_STOP_PCT:.0%} of width (floor ${config.F_EXIT_FLOOR:.2f}), DTE {config.F_MIN_DTE}-{config.F_MAX_DTE}",
        ]
        if self.disabled_reason:
            lines.append(f"Strategy F DISABLED: {self.disabled_reason}")
        if self.watch:
            lines.append("Strategy F watch (restored): " + self._watch_line())
        return lines

    def step(self, now: datetime) -> None:
        """One tick of F's entry path (ranking, monitoring, pending entries). Anything raised in here is
        journaled as F_ERROR and swallowed; the SPX strategies never see it (Bot.loop only catches
        BrokerError). A no-op once F has disabled itself for the day."""
        if self.disabled_reason:
            return
        self._guarded(now, self._run)

    def refresh_candles(self, now: datetime) -> None:
        """Keep the cached 5-minute frame of every OPEN F root current for position_manager's runner /
        profit-lock rules (candles_by_key). Bot.tick calls this before manage() in every phase --
        NO_NEW_ENTRIES included, when step() no longer runs -- and it ignores a self-disable, because an
        open position still has to be managed on live candles. Never fires signals."""
        roots = sorted(self._open_roots())
        if not roots or (self._candles_due is not None and now < self._candles_due):
            return
        self._guarded(now, self._refresh_candles, roots)

    def _guarded(self, now: datetime, fn: Callable, *args) -> None:
        try:
            fn(now, *args)
        except Exception as e:
            self.errors += 1
            log.exception("Strategy F error %d/%d in %s (A/B unaffected): %s", self.errors,
                          config.F_MAX_ERRORS_PER_DAY, fn.__name__, e)
            self.bot.journal.event("F_ERROR", now, where=fn.__name__, error=str(e), type=type(e).__name__,
                                   consecutive=self.errors)
            if self.errors >= config.F_MAX_ERRORS_PER_DAY:
                self.disable(f"{self.errors} consecutive errors, last: {type(e).__name__}: {e}", now)
            return
        self.errors = 0

    def _run(self, now: datetime) -> None:
        self.prewarm_if_due(now)
        self._rank_if_due(now)
        if self.ranked_at is None:
            return
        self._monitor(now)
        self._drain_pending(now)

    # ------------------------------------------------------------------ ranking

    def _rank_due(self, now: datetime) -> datetime:
        return at_time(now, config.F_RANK_TIME) + timedelta(seconds=config.F_RANK_GRACE_SEC)

    def prewarm_if_due(self, now: datetime) -> None:
        if self._prewarmed or now < at_time(now, config.F_PREWARM_TIME):
            return
        self._prewarmed = True
        if now >= self._rank_due(now):
            return
        log.info("F: pre-warming scanner history for %d names", len(self.names))
        self.executor.submit(self._prewarm_job, now)

    def _prewarm_job(self, now: datetime) -> None:
        cfg = orb_scanner.DEFAULT_CONFIG
        try:
            self.bars_fn(self.names, now, cfg.intraday_sessions)
            self.daily_fn(self.names, cfg.daily_days)
        except Exception as e:
            log.warning("F pre-warm failed (the scan will fetch again): %s", e)

    def _rank_if_due(self, now: datetime) -> None:
        if self.ranked_at is not None or now < self._rank_due(now):
            return
        if self._scan_future is not None:
            if not self._scan_future.done():
                return
            future, self._scan_future = self._scan_future, None
            try:
                outcome = future.result()
            except Exception as e:
                self._scan_attempts += 1
                log.error("F scan failed (attempt %d/%d): %s", self._scan_attempts, config.F_SCAN_MAX_RETRIES, e)
                self.bot.journal.event("F_SCAN_FAILED", now, attempt=self._scan_attempts, error=str(e),
                                       type=type(e).__name__)
                if self._scan_attempts >= config.F_SCAN_MAX_RETRIES:
                    self.disable(f"scan failed {self._scan_attempts} times, last: {e}", now)
                else:
                    self._next_scan_at = now + timedelta(seconds=config.F_SCAN_RETRY_SEC)
                return
            self._consume_scan(outcome, now)
            return
        if self._next_scan_at is not None and now < self._next_scan_at:
            return
        log.info("F: submitting scan of %d names at %s", len(self.names), now.strftime("%H:%M:%S"))
        self._scan_future = self.executor.submit(self._scan_job, now)

    def _scan_job(self, now: datetime) -> ScanOutcome:
        """Worker thread: fetch, check that the 1-min bars have reached the end of the opening range,
        then score. A lagging feed returns without scoring so the retry costs no chain calls."""
        cfg = orb_scanner.DEFAULT_CONFIG
        intraday = self.bars_fn(self.names, now, cfg.intraday_sessions)
        open_dt = at_time(now, config.MARKET_OPEN)
        or_end = open_dt + timedelta(minutes=config.F_OR_MINUTES)
        index = self.universe.get("index")
        # Completeness is judged on the bars INSIDE the opening range, not on how many bars exist since
        # 09:30: a retry or a late start has plenty of later bars and would still hide a hole in the OR15.
        or_bars = {s: n for s, n in ((s, _bars_in_window(intraday.get(s), open_dt, or_end, now))
                                     for s in self.names if s != index) if n is not None}
        median_minutes = median(or_bars.values()) if or_bars else None
        if median_minutes is None or median_minutes < config.F_OR_MINUTES:
            return ScanOutcome(None, {}, median_minutes, now, or_bars)
        daily = self.daily_fn(self.names, cfg.daily_days)
        chain_fn = self._make_chain_fn() if config.F_SCAN_WITH_CHAINS else None
        return ScanOutcome(self.scan_fn(self.universe, now, chain_fn, intraday=intraday, daily=daily),
                           daily, median_minutes, now, or_bars)

    def _make_chain_fn(self) -> orb_scanner.ChainFn:
        """Option-quality chains for the scan, through a broker instance owned by the scan thread."""
        broker = self.chain_broker_factory() if self.chain_broker_factory is not None else self.bot.broker

        def chain(symbol: str, spot: float, direction: str) -> list[dict] | None:
            time.sleep(config.F_CHAIN_CALL_GAP_SEC)
            exps = broker.get_expirations(symbol, config.F_MIN_DTE, config.F_MAX_DTE)
            if not exps:
                return None
            right = "C" if direction == BULLISH else "P"
            return broker.get_option_chain(symbol, exps[0], right, spot * (1 - config.F_EM_WINDOW_PCT),
                                           spot * (1 + config.F_EM_WINDOW_PCT), spot=spot)
        return chain

    def _consume_scan(self, outcome: ScanOutcome, now: datetime) -> None:
        if outcome.result is None:
            log.warning("F scan: 1-min bars lag the opening range (median %s completed minutes < %d); "
                        "retrying in %ds", outcome.median_minutes, config.F_OR_MINUTES, config.F_SCAN_RETRY_SEC,
                        extra={"keep": True})
            self.bot.journal.event("F_SCAN_LAGGING", now, median_minutes=outcome.median_minutes,
                                   retry_sec=config.F_SCAN_RETRY_SEC)
            self._next_scan_at = now + timedelta(seconds=config.F_SCAN_RETRY_SEC)
            return
        result = outcome.result
        index = self.universe.get("index")
        stock = [c for c in result.candidates if c.symbol != index]
        skipped = {s: list(r) for s, r in result.skipped.items()}
        # The scanner's already_broken is the newest completed 1-min close outside the range. On the
        # on-time scan (or a lag retry before 09:50) that close belongs to the first 5-min candle the
        # OrbSetup is about to judge in full -- excluding the name there would drop exactly the strongest
        # breakouts. Only a genuinely late scan, past the end of that first candle, treats an outside
        # close as a break that already happened without us (the owner's late-start rule).
        first_candle_end = at_time(outcome.now, config.MARKET_OPEN) + timedelta(
            minutes=config.F_OR_MINUTES + config.F_CANDLE_MINUTES)
        late_start = outcome.now >= first_candle_end
        qualifiers = []
        for c in stock:
            if c.score < config.F_MIN_SCORE:
                continue
            n = outcome.or_bars.get(c.symbol) or 0
            if n < config.F_OR_MINUTES:
                skipped[c.symbol] = [f"partial OR15: {n} of {config.F_OR_MINUTES} opening-range bars"]
            elif late_start and c.already_broken:
                skipped[c.symbol] = ["already outside OR15 at rank time (late start)"]
            else:
                qualifiers.append(c)
        self.watch = [
            WatchItem(symbol=c.symbol, tier=c.tier, rank=rank, direction=c.direction, score=c.score,
                      score_with_tier=c.score_with_tier, or_high=c.or15.high, or_low=c.or15.low,
                      scan_row=orb_scanner.to_log_row(c), setup=OrbSetup(c.or15.high, c.or15.low))
            for rank, c in enumerate(qualifiers[:config.F_WATCH_TOP], 1)
        ]
        self.daily = {w.symbol: outcome.daily[w.symbol] for w in self.watch if w.symbol in outcome.daily}
        self.ranked_at = now
        self.next_check = at_time(now, config.MARKET_OPEN) + timedelta(minutes=config.F_OR_MINUTES + config.F_CANDLE_MINUTES)
        log.info("\nF RANKING at %s (%d scored, %d skipped, median %s opening-range bars%s)\n%s\n"
                 "F WATCH (score >= %g, top %d): %s", now.strftime("%H:%M:%S"), len(stock), len(skipped),
                 outcome.median_minutes, ", late start" if late_start else "", orb_scanner.format_ranking(stock, 10),
                 config.F_MIN_SCORE, config.F_WATCH_TOP, self._watch_line(), extra={"keep": True})
        self.bot.journal.event(
            "F_RANKING", now, ranked_at=now, elapsed_sec=result.elapsed_sec, median_minutes=outcome.median_minutes,
            watch=[{"symbol": w.symbol, "tier": w.tier, "rank": w.rank, "direction": w.direction, "score": w.score,
                    "score_with_tier": w.score_with_tier, "or_high": w.or_high, "or_low": w.or_low} for w in self.watch],
            ranking=[orb_scanner.to_log_row(c) for c in stock[:10]], skipped=skipped)
        self.bot.save_state()

    def _watch_line(self) -> str:
        return ", ".join(f"{w.rank}. {w.symbol} {w.direction.lower()} {w.score:g} (OR {w.or_low:.2f}-{w.or_high:.2f})"
                         for w in self.watch) or "(none qualified)"

    # ------------------------------------------------------------------ monitoring

    def _open_roots(self) -> set[str]:
        return {p.root for p in self.bot.pm.positions.values() if p.strategy == STRATEGY}

    def _candles_for(self, symbol: str, bars: dict[str, pd.DataFrame], open_dt: datetime,
                     now: datetime) -> tuple[pd.DataFrame, pd.DataFrame, list[datetime]]:
        """Today's 1-min bars, the complete 5-min candles (cached in candles5) and the ends of candles
        still missing their last bar, for one symbol."""
        today = _today_bars(bars.get(symbol), open_dt)
        candles, late = completed_candles(today, now, config.F_CANDLE_MINUTES)
        self.candles5[symbol] = candles
        self._warn_late(symbol, late, now)
        return today, candles, late

    @staticmethod
    def _still_polling(late: list[datetime], now: datetime) -> bool:
        """Poll every DATA_CACHE_SEC only while a withheld candle is younger than F_CANDLE_MAX_WAIT_SEC:
        a bar that has not shown up by then is a hole in the feed, not lag, and is re-checked at the
        normal candle cadence instead of turning the main loop into a 20-second download loop."""
        return any(now < end + timedelta(seconds=config.F_CANDLE_MAX_WAIT_SEC) for end in late)

    @staticmethod
    def _next_boundary(open_dt: datetime, now: datetime, candle: timedelta) -> datetime:
        return open_dt + candle * (int((now - open_dt) // candle) + 1)

    def _next_check_at(self, now: datetime, open_dt: datetime, newest: datetime | None, poll: bool) -> datetime:
        """When to look at the bars again: DATA_CACHE_SEC ahead while something is genuinely pending,
        else the end of the candle after the newest one fed -- never earlier than the next candle
        boundary, so a feed stuck on a withheld candle waits a whole candle between looks."""
        candle = timedelta(minutes=config.F_CANDLE_MINUTES)
        or_end = open_dt + timedelta(minutes=config.F_OR_MINUTES)
        due = (newest + candle if newest is not None else or_end) + candle
        due = max(due, self._next_boundary(open_dt, now, candle))
        soon = now + timedelta(seconds=config.DATA_CACHE_SEC)
        return soon if poll else max(due, soon)

    def _refresh_candles(self, now: datetime, roots: list[str]) -> None:
        bars = self.bars_fn(roots, now, sessions=config.F_BARS_SESSIONS)
        self.bars.update(bars)
        open_dt = at_time(now, config.MARKET_OPEN)
        poll, newest = False, None
        for sym in roots:
            _, candles, late = self._candles_for(sym, bars, open_dt, now)
            poll |= self._still_polling(late, now)
            if not candles.empty:
                t = candles.index[-1].to_pydatetime()
                newest = t if newest is None or t > newest else newest
        self._candles_due = self._next_check_at(now, open_dt, newest, poll)

    def _monitor(self, now: datetime) -> None:
        watched = {w.symbol for w in self.watch}
        extra = sorted(self._open_roots() - watched)
        symbols = [w.symbol for w in self.watch] + extra
        if not symbols or (self.next_check is not None and now < self.next_check):
            return
        self.bars = self.bars_fn(symbols, now, sessions=config.F_BARS_SESSIONS)
        open_dt = at_time(now, config.MARKET_OPEN)
        span = timedelta(minutes=config.F_CANDLE_MINUTES)
        or_end = open_dt + timedelta(minutes=config.F_OR_MINUTES)
        signals: list[StockSignal] = []
        poll = False
        for w in self.watch:
            today, candles, late = self._candles_for(w.symbol, self.bars, open_dt, now)
            poll |= self._still_polling(late, now)
            feed = candles[candles.index >= or_end]
            if w.last_candle is not None:
                feed = feed[feed.index > w.last_candle]
            # The setup must see every candle in order: a candle withheld for its missing last bar is
            # never stepped over (a skipped reset candle would turn the next close into a false MOMENTUM).
            # The feed stops in front of it until the bar arrives, and the watermark stays behind it.
            held = [end - span for end in late
                    if end - span >= or_end and (w.last_candle is None or end - span > w.last_candle)]
            if held:
                feed = feed[feed.index < min(held)]
            for t, candle in feed.iterrows():
                was_done = w.setup.state == OrbSetup.DONE
                fired = w.setup.update(candle)
                w.last_candle = t.to_pydatetime()
                if was_done and w.setup.state != OrbSetup.DONE and w.reentry_from is not None:
                    # The break that the exit's momentum re-entry watch belonged to has reset: only a
                    # fresh confirmed break (or a later exit) can re-signal this ticker now.
                    log.info("[F:%s] setup reset on candle %s: momentum re-entry watch cleared",
                             w.symbol, t.strftime("%H:%M"))
                    w.reentry_from = None
                if fired is None:
                    continue
                if t != feed.index[-1]:
                    log.info("Stale STOCK_ORB %s %s signal on replayed candle %s ignored.", w.symbol, fired,
                             t.strftime("%H:%M"))
                    continue
                signals.append(self._signal(w, fired, w.setup.entry_kind, w.last_candle, _candle_dict(candle),
                                            config.F_CANDLE_MINUTES))
            reentry = self._momentum_reentry(w, today, now)
            if reentry is not None:
                signals.append(reentry)
            poll |= w.reentry_from is not None and w.setup.state == OrbSetup.DONE
        for sym in extra:
            _, _, late = self._candles_for(sym, self.bars, open_dt, now)
            poll |= self._still_polling(late, now)
        newest = max((w.last_candle for w in self.watch if w.last_candle is not None), default=None)
        self.next_check = self._next_check_at(now, open_dt, newest, poll)
        self._candles_due = self.next_check   # the open roots were just refreshed too
        for sig in signals:
            self._confirm_and_queue(sig, now)

    def _warn_late(self, symbol: str, late: list[datetime], now: datetime) -> None:
        for end in late:
            if now >= end + timedelta(seconds=config.F_CANDLE_MAX_WAIT_SEC) and (symbol, end) not in self._late_warned:
                self._late_warned.add((symbol, end))
                log.warning("[F:%s] %d-min candle ending %s still missing its last 1-min bar %ds after the close; "
                            "withheld and re-checked each candle (never scored early)", symbol, config.F_CANDLE_MINUTES,
                            end.strftime("%H:%M"), int((now - end).total_seconds()), extra={"keep": True})

    def _signal(self, w: WatchItem, direction: str, kind: str, candle_time: datetime, candle: dict,
                minutes: int) -> StockSignal:
        return StockSignal(symbol=w.symbol, tier=w.tier, rank=w.rank, direction=direction, kind=kind,
                           candle_time=candle_time, candle_minutes=minutes, candle=candle,
                           break_close=w.setup.break_close, or_high=w.or_high, or_low=w.or_low,
                           scanner_direction=w.direction, scan_row=w.scan_row)

    def _momentum_reentry(self, w: WatchItem, today: pd.DataFrame, now: datetime) -> StockSignal | None:
        """After an F exit in this ticker, while its break is still standing (setup DONE): re-signal once
        F_REENTRY_MOMENTUM_MINUTES consecutive completed 1-min bars -- adjacent minutes, all after the
        exit -- each close beyond the previous close in the original direction and the last close is
        still beyond the broken OR level. The watch dies with the break (see _monitor)."""
        if w.reentry_from is None or w.setup.state != OrbSetup.DONE or self.bot.pm.has_root(w.symbol):
            return None
        n = config.F_REENTRY_MOMENTUM_MINUTES
        done = _completed(today, now)
        after = done[done.index > w.reentry_from]
        if len(after) < n:
            return None
        streak = after.tail(n)
        if not bool((streak.index[1:] - streak.index[:-1] == timedelta(minutes=1)).all()):
            return None   # a missing minute inside the run: not "consecutive"
        closes = pd.concat([done[done.index <= w.reentry_from]["close"].tail(1), after["close"]])
        if len(closes) < n + 1:
            return None
        sign = 1.0 if w.setup.direction == BULLISH else -1.0
        steps = closes.diff().iloc[1:] * sign
        if not bool((steps.tail(n) > 0).all()):
            return None
        level = w.or_high if w.setup.direction == BULLISH else w.or_low
        if sign * (float(streak["close"].iloc[-1]) - level) <= 0:
            return None
        w.reentry_from = None
        candle = {"open": float(streak["open"].iloc[0]), "high": float(streak["high"].max()),
                  "low": float(streak["low"].min()), "close": float(streak["close"].iloc[-1]),
                  "volume": float(streak["volume"].sum())}
        return self._signal(w, w.setup.direction, MOMENTUM_REENTRY, streak.index[0].to_pydatetime(), candle, n)

    def on_exit(self, root: str, exit_time: datetime) -> None:
        for w in self.watch:
            if w.symbol == root:
                w.reentry_from = exit_time

    # ------------------------------------------------------------------ confirmation

    def _confirm_and_queue(self, sig: StockSignal, now: datetime) -> None:
        if sig.key in self.bot.acted:
            return
        same = [p for p in self.pending if p.symbol == sig.symbol]
        if any(p.key == sig.key for p in same):
            return
        for old in same:       # a signal waiting out a block (see _drain_pending) is replaced by the newer break on its ticker
            self.pending.remove(old)
            self._skip(now, old, f"SUPERSEDED by the {sig.kind} break of {sig.candle_time.strftime('%H:%M')}")
        log.info("SIGNAL STOCK_ORB %s %s (%s) on %dm candle %s vs OR %.2f/%.2f (scanner %s, rank %d)",
                 sig.symbol, sig.direction, sig.kind, sig.candle_minutes, sig.candle_time.strftime("%H:%M"),
                 sig.or_high, sig.or_low, sig.scanner_direction, sig.rank)
        self.bot.journal.event("SIGNAL", now, setup=SETUP, strategy=STRATEGY, ticker=sig.symbol,
                               direction=sig.direction, trigger=sig.kind, candles=[sig.candle_time],
                               high=sig.or_high, low=sig.or_low, break_close=sig.break_close,
                               scanner_direction=sig.scanner_direction, direction_match=sig.direction_match,
                               rank=sig.rank, tier=sig.tier, score=sig.scan_row.get("score"))
        if not strategy.setup_allowed(SETUP, now.date()):
            self._skip(now, sig, "NEWS_DAY_MODE")
            return
        if config.F_REQUIRE_SCANNER_DIRECTION and not sig.direction_match:
            self._skip(now, sig, f"DIRECTION_MISMATCH scanner {sig.scanner_direction}")
            return
        snapshot, reasons = self._confirmation(sig, now, check_volume=True)
        if reasons:
            self._confirm_rejected(now, sig, reasons, snapshot, stale=False)
            return
        sig.confirm, sig.confirmed_at = snapshot, now
        self.pending.append(sig)
        log.info("[F:%s] break confirmed: queued (%d pending)", sig.symbol, len(self.pending))

    def _confirmation(self, sig: StockSignal, now: datetime, check_volume: bool,
                      refresh: bool = False) -> tuple[dict, list[str]]:
        if refresh:
            self.bars.update(self.bars_fn([sig.symbol], now, sessions=config.F_BARS_SESSIONS))
        if sig.symbol not in self.daily:
            self.daily[sig.symbol] = self.daily_fn([sig.symbol]).get(sig.symbol)
        bars = self.bars.get(sig.symbol)
        m, _ = orb_scanner.compute_metrics(sig.symbol, bars, self.daily.get(sig.symbol), now)
        nv = normal_volume(bars, now.date(), sig.candle_time, sig.candle_minutes) if check_volume else None
        reasons = confirm_break(sig.direction, sig.candle if check_volume else None, m, nv)
        snapshot = {
            "checked_at": now.isoformat(), "candle_volume": sig.candle["volume"], "normal_volume": nv,
            "last": m.last if m else None, "vwap": m.vwap if m else None, "ema9": m.ema9 if m else None,
            "ema21": m.ema21 if m else None, "remaining_em": m.remaining_em if m else None,
            "expected_move": m.expected_move if m else None, "atr": m.atr if m else None,
        }
        return snapshot, reasons

    def _confirm_rejected(self, now: datetime, sig: StockSignal, reasons: list[str], snapshot: dict,
                          stale: bool) -> None:
        self.bot.acted.add(sig.key)
        log.info("[F:%s] break %s %s NOT confirmed%s: %s", sig.symbol, sig.direction, sig.kind,
                 " (re-check before entry)" if stale else "", ", ".join(reasons))
        self.bot.journal.event("F_CONFIRM_REJECTED", now, ticker=sig.symbol, direction=sig.direction,
                               trigger=sig.kind, candle_time=sig.candle_time, reasons=reasons, stale=stale, **snapshot)

    def _skip(self, now: datetime, sig: StockSignal, reason: str) -> None:
        self.bot.acted.add(sig.key)
        log.info("[F:%s] F_SKIPPED %s (%s %s at %s)", sig.symbol, reason, sig.direction, sig.kind,
                 sig.candle_time.strftime("%H:%M"))
        self.bot.journal.event("F_SKIPPED", now, strategy=STRATEGY, ticker=sig.symbol, direction=sig.direction,
                               trigger=sig.kind, candle_time=sig.candle_time, reason=reason)

    # ------------------------------------------------------------------ entry

    def _drain_pending(self, now: datetime) -> None:
        """At most one F entry per tick, in rank order; a signal that waited longer than one candle is
        re-confirmed on fresh bars first. Capacity waits keep their signals pending."""
        ttl = timedelta(minutes=config.F_SIGNAL_TTL_MIN)
        for sig in list(self.pending):
            if now > sig.candle_end + ttl:
                self.pending.remove(sig)
                self._skip(now, sig, "SIGNAL_EXPIRED")
        if not self.pending:
            self._waiting_on = None
            return
        if self.bot.news_blackout_active(now):
            return
        # 2026-10-08: a block that clears by itself (the 2-open cap with B and another strategy holding the slots, a failing
        # close, stale prices) keeps the confirmed signals pending until it clears or they expire (F_SIGNAL_TTL_MIN); they used
        # to be taken off the list and written off as acted the moment they met it.
        block = self.bot.risk.transient_block(open_total=self.bot.pm.open_total_count(),
                                              close_blocked=self.bot.pm.any_close_blocked(), halt=self.bot.entry_halt_reason())
        if block:
            if block != self._waiting_on:
                self._waiting_on = block
                log.info("[F] %d confirmed signal(s) waiting: %s (they expire after %d min)", len(self.pending), block,
                         config.F_SIGNAL_TTL_MIN)
                self.bot.journal.event("F_WAITING", now, strategy=STRATEGY, reason=block,
                                       tickers=[s.symbol for s in self.pending])
            return
        self._waiting_on = None
        cap = limit(STRATEGY, "MAX_CONCURRENT_POSITIONS", config.MAX_CONCURRENT_POSITIONS)
        for sig in list(self.pending):
            if self.bot.pm.has_root(sig.symbol):
                self.pending.remove(sig)
                self._skip(now, sig, "TICKER_OPEN")
                continue
            per_ticker = config.F_MAX_ENTRIES_PER_TICKER
            if per_ticker and self.entries_by_ticker.get(sig.symbol, 0) >= per_ticker:
                self.pending.remove(sig)
                self._skip(now, sig, f"TICKER_CAP {per_ticker}")
                continue
            if self.bot.pm.open_count(STRATEGY) >= cap:
                return
            if now - sig.confirmed_at >= timedelta(minutes=config.F_CANDLE_MINUTES):
                snapshot, reasons = self._confirmation(sig, now, check_volume=False, refresh=True)
                if reasons:
                    self.pending.remove(sig)
                    self._confirm_rejected(now, sig, reasons, snapshot, stale=True)
                    continue
                sig.confirm, sig.confirmed_at = snapshot, now
            self.pending.remove(sig)
            self.bot.acted.add(sig.key)
            try:
                summary = self.enter(now, sig)
            except BrokerError as e:
                log.error("[F:%s] broker error during entry: %s", sig.symbol, e)
                self.bot.journal.event("BROKER_ERROR", now, strategy=STRATEGY, ticker=sig.symbol, error=str(e))
                summary = f"broker error {e}"
            log.warning("SIGNAL STOCK_ORB %s %s (%s) at %s -- F: %s", sig.symbol, sig.direction, sig.kind,
                        now.strftime("%H:%M:%S"), summary)
            return

    def _reject(self, now: datetime, sig: StockSignal, reason: str, quote: strategy.SpreadQuote | None = None) -> str:
        log.info("[F:%s] ENTRY REJECTED %s: %s", sig.symbol, sig.direction, reason)
        self.bot.journal.event("ENTRY_REJECTED", now, strategy=STRATEGY, setup=SETUP, ticker=sig.symbol,
                               direction=sig.direction, trigger=sig.kind, reason=reason, quote=quote)
        return f"skip: {reason}"

    def enter(self, now: datetime, sig: StockSignal) -> str:
        """Place one F spread for a confirmed signal. Returns the operator summary."""
        bot, broker, sym = self.bot, self.bot.broker, sig.symbol
        if bot.pm.manual_hold():
            return "skip: MANUAL_HOLD file present"
        equity = broker.get_account()["equity"]
        allowed, reason = bot.risk.trading_allowed(
            STRATEGY, equity, now, SETUP, open_count=bot.pm.open_count(STRATEGY),
            exempt_open_pnl=bot.pm.open_pnl(config.DAILY_LOSS_LIMIT_EXEMPT), open_spx=bot.pm.open_spx_count(),
            open_total=bot.pm.open_total_count(), close_blocked=bot.pm.any_close_blocked(),
            halt=bot.entry_halt_reason())
        if not allowed:
            log.info("[F] No new entries: %s", reason)
            self._skip(now, sig, reason)
            for other in list(self.pending):
                self.pending.remove(other)
                self._skip(now, other, reason)
            return f"skip: {reason}"
        exps = broker.get_expirations(sym, config.F_MIN_DTE, config.F_MAX_DTE)
        if not exps:
            return self._reject(now, sig, f"NO_EXPIRATION within {config.F_MIN_DTE}-{config.F_MAX_DTE} DTE")
        exp = exps[0]
        spot = broker.get_spot(sym)
        if not (spot > sig.or_high if sig.direction == BULLISH else spot < sig.or_low):
            reason = f"BREAK_FAILED spot {spot:.2f} back inside OR {sig.or_low:.2f}-{sig.or_high:.2f}"
            self._skip(now, sig, reason)
            return f"skip: {reason}"
        right = strategy.direction_to_spread(sig.direction)
        lo_w, hi_w = spot * (1 - config.F_EM_WINDOW_PCT), spot * (1 + config.F_EM_WINDOW_PCT)
        calls = broker.get_option_chain(sym, exp, "C", lo_w, hi_w, spot=spot)
        puts = broker.get_option_chain(sym, exp, "P", lo_w, hi_w, spot=spot)
        em = strategy.expected_move(calls, puts, spot)
        if em is None:
            return self._reject(now, sig, f"NO_EXPECTED_MOVE no ATM straddle around spot {spot:.2f}")
        inc = strategy.strike_increment(calls + puts, spot)
        if inc is None:
            return self._reject(now, sig, f"NO_STRIKE_INCREMENT fewer than two strikes around spot {spot:.2f}")
        width = strategy.width_for_stock(spot, inc)
        span = 2 * em + 2 * width
        chain = broker.get_option_chain(sym, exp, right, spot - span, spot + span, spot=spot)
        short, long, result = strategy.select_strikes_f(spot, right, width, em, chain)
        if short is None:
            return self._reject(now, sig, result)
        quote = result
        detail = f" width {width:g} EM {em:.2f} exp {exp.isoformat()}"
        stop_distance = strategy.f_stop_distance(width)
        if strategy.quote_blown_out(quote, width, stop_distance=stop_distance):
            return self._reject(now, sig, f"BID_ASK_BLOWN bid-side {quote.bid_side:.2f} ask-side {quote.ask_side:.2f} "
                                          f"at {short:g}/{long:g} (width {width:g}, quote spread "
                                          f"{quote.ask_side - quote.bid_side:.2f} vs stop {stop_distance:.2f})", quote)
        collision = bot.pm.find_collision(sym, exp, right, short, long)
        if collision:
            return self._reject(now, sig, bot.pm.collision_reason(collision, right, short, long), quote)
        open_risk = bot.pm.total_open_risk()
        qty = contracts_for(equity, width, quote.mid, open_risk, strategy.is_news_day(now.date()),
                            half_size=config.F_HALF_SIZE)
        if qty == 0:
            return self._reject(now, sig, f"SIZING_ZERO equity {equity:.2f} width {width:g} credit {quote.mid:.2f} "
                                          f"open risk {open_risk:.2f}", quote)
        log.warning("[F:%s] ENTRY %s %s spot %.2f: sell %s%s buy %s%s x%d @ %.2f (bid-side %.2f)%s",
                    sym, sig.kind, sig.direction, spot, short, right, long, right, qty, quote.mid, quote.bid_side, detail)
        worked_from = time.monotonic()
        order = broker.place_credit_spread(
            sym, exp, right, short, long, qty, quote.mid, time_in_force="day", root=sym,
            client_tag=f"0dte-{config.TRADER_NAME.lower()}-f-stockorb-{sym.lower()}")
        order = bot.work_entry(order, quote.mid, round(config.F_CREDIT_PCT_RANGE[0] * width, 2),
                               step=max(0.01, round(config.F_ENTRY_STEP_PCT * width, 2)),
                               wait_sec=config.F_ENTRY_STEP_SEC, timeout_sec=config.F_ENTRY_TIMEOUT_SEC)
        filled = qty if order.get("status") == "dry_run" else int(order.get("filled_qty") or 0)
        if filled == 0:
            log.info("[F:%s] ENTRY NOT FILLED (%s): order %s", sym, order.get("status"), order.get("id"))
            bot.journal.event("ENTRY_UNFILLED", now, strategy=STRATEGY, ticker=sym, order_id=order.get("id"),
                              status=order.get("status"))
            return f"not filled ({order.get('status')}){detail}"
        credit = float(order.get("filled_avg_price") or order.get("limit_price") or quote.mid)
        # the entry hold (STOP_MIN_HOLD_SEC) runs from the FILL, not from the tick that placed the order: work_entry can
        # block for up to F_ENTRY_TIMEOUT_SEC before the order fills
        filled_at = now + timedelta(seconds=round(time.monotonic() - worked_from))
        ex = strategy.f_exit_params(width)
        spread = OpenSpread(
            strategy=STRATEGY, direction=sig.direction, setup=SETUP, right=right, root=sym, expiration=exp,
            short_strike=short, long_strike=long, width=width, qty=filled, entry_credit=credit, entry_time=filled_at,
            current_price=credit, best_price=credit, profit_target=ex["profit_target"], stop_loss=ex["stop_loss"],
            entry_mid=quote.mid, entry_bid=quote.bid_side, entry_ask=quote.ask_side, overrides=ex["overrides"],
            scan={**sig.scan_row, "rank": sig.rank, "scanner_direction": sig.scanner_direction,
                  "direction_match": sig.direction_match, "trigger": sig.kind,
                  "break_candle": {**sig.candle, "time": sig.candle_time.isoformat(), "minutes": sig.candle_minutes},
                  "break_close": sig.break_close, "confirm": sig.confirm, "em_straddle": em, "increment": inc,
                  "expiration": exp.isoformat()})
        entry_no = bot.risk.record_entry(STRATEGY)
        self.entries_by_ticker[sym] = self.entries_by_ticker.get(sym, 0) + 1
        bot.pm.open(spread)
        card = format_entry_card(spread, entry_no)
        log.info("\n%s", card, extra={"keep": True})
        bot.journal.card(card, now)
        bot.save_state()
        return f"sell {short:g}{right} buy {long:g}{right} x{filled} @ {credit:.2f}{detail}"

    # ------------------------------------------------------------------ manage / state

    def candles_by_key(self) -> dict[str, pd.DataFrame]:
        """Each open F position's own cached 5-minute frame (never fetches inside manage())."""
        return {key: self.candles5[p.root] for key, p in self.bot.pm.positions.items()
                if p.strategy == STRATEGY and p.root in self.candles5}

    def to_state(self) -> dict:
        return {
            "ranked_at": self.ranked_at.isoformat() if self.ranked_at else None,
            "disabled": self.disabled_reason,
            "entries_by_ticker": dict(self.entries_by_ticker),
            "scan_attempts": self._scan_attempts,
            "watch": [{"symbol": w.symbol, "tier": w.tier, "rank": w.rank, "direction": w.direction,
                       "score": w.score, "score_with_tier": w.score_with_tier, "or_high": w.or_high,
                       "or_low": w.or_low, "scan_row": w.scan_row,
                       "reentry_from": w.reentry_from.isoformat() if w.reentry_from else None} for w in self.watch],
        }

    def restore(self, d: dict | None, now: datetime) -> None:
        """Rebuild today's watch list from state.json with fresh (WAITING) setups; the first _monitor
        replays every completed candle since the opening range, so each setup returns to its true state
        and only a fire on the newest candle -- not yet acted -- can trade."""
        if not d or not d.get("ranked_at"):
            return
        ranked_at = datetime.fromisoformat(d["ranked_at"])
        if ranked_at.date() != now.date():
            return
        self.ranked_at = ranked_at
        self.disabled_reason = d.get("disabled")
        self.entries_by_ticker = dict(d.get("entries_by_ticker") or {})
        self._scan_attempts = int(d.get("scan_attempts") or 0)
        self.watch = [
            WatchItem(symbol=w["symbol"], tier=w["tier"], rank=w["rank"], direction=w["direction"], score=w["score"],
                      score_with_tier=w["score_with_tier"], or_high=w["or_high"], or_low=w["or_low"],
                      scan_row=w.get("scan_row") or {}, setup=OrbSetup(w["or_high"], w["or_low"]),
                      reentry_from=datetime.fromisoformat(w["reentry_from"]) if w.get("reentry_from") else None)
            for w in d.get("watch", [])
        ]
        self.next_check = at_time(now, config.MARKET_OPEN) + timedelta(minutes=config.F_OR_MINUTES + config.F_CANDLE_MINUTES)
        log.info("Restored F state (ranked %s): %s", ranked_at.strftime("%H:%M:%S"), self._watch_line(),
                 extra={"keep": True})

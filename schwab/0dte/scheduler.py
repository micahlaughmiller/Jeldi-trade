"""0DTE SPX credit-spread bot main loop.

    python scheduler.py [--dry-run] [--once]

All clock logic is US/Eastern wall time; phases derive from minutes since 09:30
so a late start lands in the right phase immediately. Every entry signal is
traded twice: strategy A (ITM spread) and strategy B (OTM spread one expected
move away), each with its own position, sizing and daily counters.
"""

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import market_data
import strategy
from broker import Broker, BrokerError
from journal import Journal, format_close_card, format_day_table, format_entry_card
from position_manager import OpenSpread, PositionManager, spxw_legs
from risk_manager import DayState, RiskManager, contracts_for, tier
from strategy import Phase

@dataclass
class PendingMomentum:
    """A MOMENTUM signal awaiting 1-minute follow-through before the listed strategies enter.
    In-memory only -- lost on restart, matching the entry it is guarding: a missed confirmation
    just means that signal was never traded, not a broken position."""
    setup: str
    direction: str
    break_close: float
    strategies: tuple[str, ...]
    signal_time: datetime
    confirmed: int = 0
    last_candle: datetime | None = None


HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / config.LOG_DIR
TERMINAL = {"filled", "canceled", "rejected", "expired", "replaced", "dry_run"}

log = logging.getLogger("0dte")


def now_et() -> datetime:
    return datetime.now(config.ET)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, config.ET).timetuple()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    # Raw broker payload dumps and per-request chatter go to the file only.
    console.addFilter(lambda r: not (r.getMessage().startswith("[broker]") and ": {" in r.getMessage()))
    console.addFilter(lambda r: not r.name.startswith(("httpx", "urllib3", "yfinance", "peewee")))
    file_handler = logging.FileHandler(LOG_DIR / f"0dte_{config.TRADER_NAME.lower()}.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    for handler in (console, file_handler):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def fmt_money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


class Bot:
    def __init__(self, dry_run: bool, once: bool):
        self.once = once
        self.done = False
        self.broker = Broker(config, dry_run=dry_run, log=log.info)
        self.journal = Journal(LOG_DIR, config.TRADER_NAME)
        self.pm = PositionManager(self.broker, self.journal)
        self.risk: RiskManager | None = None
        self.acted: set[str] = set()
        self.overnight: strategy.Levels | None = None
        self.basis: float | None = None
        self.orb: strategy.Levels | None = None
        self.orb_setup: strategy.OrbSetup | None = None
        self.on_setup: strategy.OrbSetup | None = None       # overnight levels, ORB-style state machine (A)
        self.last_on_candle: datetime | None = None
        self.last_candle: datetime | None = None
        self.last_orb_candle: datetime | None = None
        self.pending_momentum: dict[str, PendingMomentum] = {}
        self.today: date = now_et().date()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        now = now_et()
        acct = self.broker.get_account()
        saved = self.journal.load_state()
        saved_today = saved if saved and saved.get("date") == self.today.isoformat() else None
        if saved_today:
            self.risk = RiskManager(acct["equity"], DayState.from_dict(saved_today["day"]))
            self.acted = set(saved_today.get("acted", []))
            log.info("Restored state for %s: %s", self.today, saved_today["day"])
        else:
            self.risk = RiskManager(acct["equity"])
        positions = self.broker.get_positions()
        if spxw_legs(positions):
            self.pm.adopt_from_broker(positions, now, self.broker.get_spot(config.UNDERLYING),
                                      saved_today.get("positions") if saved_today else None)
        self.report_start(now, acct, positions)
        self.broker.record_daily_equity()
        self.save_state()
        self.run(now)

    def run(self, now: datetime) -> None:
        if not market_data.is_trading_day(now) and not self.pm.positions:
            log.info("Weekend (%s): nothing to do, exiting.", now.strftime("%A"))
            return
        ph = strategy.phase(now)
        if ph == Phase.CLOSED:
            if not self.pm.positions:
                log.info("Started after FORCE_CLOSE_TIME %s with no open position: exiting.",
                         config.FORCE_CLOSE_TIME)
                return
            log.warning("Started after FORCE_CLOSE_TIME with open positions: closing them now.")
            self.end_of_day(now_et())
            return
        if ph == Phase.PRE_OPEN:
            self.wait_for_open()
        else:
            log.warning("LATE START at %s (phase %s): computing levels now.", now.strftime("%H:%M:%S"), ph)
            self.refresh_levels(now)
        self.loop()

    def wait_for_open(self) -> None:
        levels_at = strategy.at_time(now_et(), config.MARKET_OPEN) - timedelta(minutes=1)
        log.info("Pre-open: waiting for %s to compute overnight levels.", levels_at.strftime("%H:%M"))
        while True:
            now = now_et()
            if now >= levels_at and self.overnight is None:
                self.overnight = market_data.get_overnight_levels(now)
                self.log_levels("OVERNIGHT(ES)", self.overnight)
            if now >= strategy.at_time(now, config.MARKET_OPEN):
                return
            time.sleep(config.TICK_SECONDS)

    def loop(self) -> None:
        while not self.done:
            now = now_et()
            try:
                self.tick(now)
            except BrokerError as e:
                log.error("Broker error during tick: %s", e)
                self.journal.event("BROKER_ERROR", now, error=str(e))
            if self.once:
                log.info("--once: single tick complete.")
                return
            time.sleep(config.TICK_SECONDS)

    def tick(self, now: datetime) -> None:
        ph = strategy.phase(now)
        if ph == Phase.CLOSED:
            self.end_of_day(now)
            self.done = True
            return
        self.refresh_levels(now)
        candles = market_data.get_candles(config.SPX_SYMBOL, config.CANDLE_INTERVAL,
                                          config.CANDLE_LOOKBACK_MIN, now)
        new_candle = not candles.empty and (self.last_candle is None or candles.index[-1] > self.last_candle)
        if new_candle:
            self.last_candle = candles.index[-1].to_pydatetime()
            last = candles.iloc[-1]
            log.debug("Candle %s O %.2f H %.2f L %.2f C %.2f | phase %s",
                     self.last_candle.strftime("%H:%M"), last.open, last.high, last.low, last.close, ph)
        # The ORB state machine must see every completed 5-min candle, whether or not we can trade.
        orb_signal = self.orb_signal(now) if ph in strategy.ORB_PHASES else None
        if self.pm.positions:
            self.manage(now, candles)
        if self.pending_momentum:
            self.check_pending_momentum(now, ph)
        if ph in strategy.ENTRY_PHASES:
            if new_candle and ph in strategy.OVERNIGHT_PHASES:
                self.try_on_break_entry(now, candles)   # OVERNIGHT retired 2026-09-24; ON_BREAK covers both A and B
            if orb_signal is not None:
                self.try_orb_entry(now, *orb_signal)
        self.save_state()

    # ---------------------------------------------------------------- levels

    def refresh_levels(self, now: datetime) -> None:
        if self.overnight is None:
            self.overnight = market_data.get_overnight_levels(now)
            self.log_levels("OVERNIGHT(ES)", self.overnight)
        if self.basis is None and now >= strategy.at_time(now, config.MARKET_OPEN):
            self.basis = market_data.get_spx_es_basis(now)
            if self.basis is not None:
                log.info("SPX-ES basis %.2f", self.basis)
                if self.overnight is not None:
                    self.log_levels("OVERNIGHT(SPX terms)", self.overnight.shifted(self.basis))
        if self.orb is None:
            self.orb = market_data.get_opening_range(now)
            self.log_levels("OPENING RANGE", self.orb)
            if self.orb is not None:
                self.orb_setup = strategy.OrbSetup(self.orb.high, self.orb.low)

    @staticmethod
    def log_levels(label: str, levels: strategy.Levels | None) -> None:
        if levels is not None:
            log.info("%s high %.2f low %.2f (established %s)", label, levels.high, levels.low,
                     levels.established_at.strftime("%H:%M"))

    def overnight_levels_for_signal(self) -> tuple[strategy.Levels, str] | None:
        if self.overnight is None:
            return None
        if config.BREAKOUT_LEVEL_SOURCE == "ES":
            return self.overnight, config.ES_SYMBOL
        if self.basis is None:
            return None
        return self.overnight.shifted(self.basis), config.SPX_SYMBOL

    # --------------------------------------------------------------- signals

    def entry_possible(self, equity: float, setup: str | None = None,
                       strategies: tuple[str, ...] | None = None) -> bool:
        reasons = []
        for strat in (strategies or config.STRATEGIES):
            if strat in self.pm.positions:
                reasons.append(f"{strat}: position open")
                continue
            allowed, reason = self.risk.trading_allowed(strat, equity, now_et(), setup)
            if allowed:
                return True
            reasons.append(reason)
        log.info("No new entries: %s", "; ".join(reasons))
        return False

    def try_overnight_entry(self, now: datetime, spx_candles: pd.DataFrame) -> None:
        if not strategy.setup_allowed("OVERNIGHT", now.date()):
            return
        found = self.overnight_levels_for_signal()
        if found is None:
            return
        levels, symbol = found
        candles = spx_candles if symbol == config.SPX_SYMBOL else \
            market_data.get_candles(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK_MIN, now)
        bo = strategy.detect_breakout(candles, levels.high, levels.low, levels.established_at)
        if bo is None:
            return
        key = f"OVERNIGHT:{bo.candle_times[1].isoformat()}"
        if key in self.acted:
            return
        self.acted.add(key)
        log.info("SIGNAL OVERNIGHT %s on candles %s/%s vs %.2f/%.2f", bo.direction,
                 bo.candle_times[0].strftime("%H:%M"), bo.candle_times[1].strftime("%H:%M"),
                 levels.high, levels.low)
        self.journal.event("SIGNAL", now, setup="OVERNIGHT", direction=bo.direction,
                           candles=list(bo.candle_times), high=levels.high, low=levels.low)
        self.enter(now, "OVERNIGHT", bo.direction)

    def try_on_break_entry(self, now: datetime, spx_candles: pd.DataFrame) -> None:
        """Overnight high/low through the ORB state machine (break, then pullback or momentum). A only."""
        if not strategy.setup_allowed("ON_BREAK", now.date()):
            return
        found = self.overnight_levels_for_signal()
        if found is None:
            return
        levels, symbol = found
        if self.on_setup is None:
            self.on_setup = strategy.OrbSetup(levels.high, levels.low)
        candles = spx_candles if symbol == config.SPX_SYMBOL else \
            market_data.get_candles(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK_MIN, now)
        candles = candles[candles.index >= strategy.at_time(now, config.MARKET_OPEN)]
        if self.last_on_candle is not None:
            candles = candles[candles.index > self.last_on_candle]
        for t, candle in candles.iterrows():
            fired = self.on_setup.update(candle)
            self.last_on_candle = t.to_pydatetime()
            if fired is None:
                continue
            if t != candles.index[-1]:
                log.info("Stale ON_BREAK %s signal on replayed candle %s ignored.", fired, t.strftime("%H:%M"))
                continue
            key = f"ON_BREAK:{self.last_on_candle.isoformat()}"
            if key in self.acted:
                continue
            self.acted.add(key)
            kind = self.on_setup.entry_kind
            log.info("SIGNAL ON_BREAK %s (%s) on %s candle %s vs overnight %.2f/%.2f", fired, kind,
                     config.CANDLE_INTERVAL, t.strftime("%H:%M"), levels.high, levels.low)
            self.journal.event("SIGNAL", now, setup="ON_BREAK", direction=fired, trigger=kind,
                               candles=[self.last_on_candle], high=levels.high, low=levels.low)
            self.dispatch_entry(now, "ON_BREAK", fired, kind, self.on_setup.break_close)

    def orb_signal(self, now: datetime) -> tuple[str, datetime, str | None] | None:
        """Feed new completed ORB candles to the setup; a signal counts only from the newest one.

        Returns (direction, candle_time, entry_kind) with entry_kind MOMENTUM or PULLBACK.
        """
        if self.orb_setup is None:
            return None
        candles = market_data.get_candles(config.SPX_SYMBOL, config.ORB_CANDLE_INTERVAL,
                                          config.CANDLE_LOOKBACK_MIN, now)
        candles = candles[candles.index >= self.orb.established_at]
        if self.last_orb_candle is not None:
            candles = candles[candles.index > self.last_orb_candle]
        signal = None
        for t, candle in candles.iterrows():
            fired = self.orb_setup.update(candle)
            self.last_orb_candle = t.to_pydatetime()
            log.debug("ORB candle %s O %.2f H %.2f L %.2f C %.2f -> %s", t.strftime("%H:%M"),
                      candle.open, candle.high, candle.low, candle.close, self.orb_setup.state)
            if fired is None:
                continue
            if t == candles.index[-1]:
                signal = (fired, self.last_orb_candle, self.orb_setup.entry_kind)
            else:
                log.info("Stale ORB %s signal on replayed candle %s ignored.", fired, t.strftime("%H:%M"))
        return signal

    def try_orb_entry(self, now: datetime, direction: str, candle_time: datetime,
                      kind: str | None = None) -> None:
        if not strategy.setup_allowed("ORB", now.date()):
            return
        key = f"ORB:{candle_time.isoformat()}"
        if key in self.acted:
            return
        self.acted.add(key)
        log.info("SIGNAL ORB %s (%s) on 5m candle %s vs %.2f/%.2f", direction, kind or "?",
                 candle_time.strftime("%H:%M"), self.orb.high, self.orb.low)
        self.journal.event("SIGNAL", now, setup="ORB", direction=direction, trigger=kind, candles=[candle_time],
                           high=self.orb.high, low=self.orb.low)
        self.dispatch_entry(now, "ORB", direction, kind, self.orb_setup.break_close)

    # ----------------------------------------------------------------- entry

    def dispatch_entry(self, now: datetime, setup: str, direction: str, kind: str | None,
                       break_close: float) -> None:
        """Route a fired signal to enter() now, except a MOMENTUM signal is split per strategy: any
        strategy with MOMENTUM_CONFIRM_ENABLED waits for 1-minute follow-through (see PendingMomentum)
        while the rest enter immediately, exactly as before."""
        if kind != "MOMENTUM":
            self.enter(now, setup, direction, kind)
            return

        def wants_confirmation(strat: str) -> bool:
            # A strategy that isn't even eligible for this setup or this entry kind goes through the
            # immediate path instead, so enter_leg's existing gate rejects it right away (with its usual
            # log line) instead of parking a candidate that was always going to be rejected later anyway.
            if setup not in config.SETUPS_BY_STRATEGY.get(strat, (setup,)):
                return False
            if kind not in config.ORB_ENTRY_KINDS_BY_STRATEGY.get(strat, (kind,)):
                return False
            return strategy.exit_setting(strat, "MOMENTUM_CONFIRM_ENABLED", False)

        confirm = tuple(s for s in config.STRATEGIES if wants_confirmation(s))
        immediate = tuple(s for s in config.STRATEGIES if s not in confirm)
        if immediate:
            self.enter(now, setup, direction, kind, strategies=immediate)
        if confirm:
            log.info("[%s] MOMENTUM confirmation pending for %s: need %d candle(s) beating %.2f by %.2f (%s)",
                     "/".join(confirm), direction, config.MOMENTUM_CONFIRM_CANDLES, break_close,
                     config.MOMENTUM_CONFIRM_MARGIN, setup)
            self.journal.event("MOMENTUM_PENDING", now, setup=setup, direction=direction,
                               break_close=break_close, strategies=list(confirm))
            self.pending_momentum[setup] = PendingMomentum(
                setup=setup, direction=direction, break_close=break_close, strategies=confirm, signal_time=now)

    def check_pending_momentum(self, now: datetime, ph: Phase) -> None:
        m1 = market_data.get_candles(config.SPX_SYMBOL, "1m", config.CANDLE_LOOKBACK_MIN, now)
        for setup, p in list(self.pending_momentum.items()):
            sign = 1.0 if p.direction == strategy.BULLISH else -1.0
            new_bars = m1[m1.index > (p.last_candle or p.signal_time)] if not m1.empty else m1
            done = False
            for t, bar in new_bars.iterrows():
                t = t.to_pydatetime()
                p.last_candle = t
                beat = sign * (float(bar.close) - p.break_close)
                if beat < config.MOMENTUM_CONFIRM_MARGIN:
                    log.info("[%s] MOMENTUM confirmation FAILED at %s (close %.2f, break %.2f, beat %+.2f): "
                             "entry abandoned", setup, t.strftime("%H:%M"), bar.close, p.break_close, beat)
                    self.journal.event("MOMENTUM_ABORTED", now, setup=setup, direction=p.direction,
                                       at=t, close=float(bar.close), break_close=p.break_close, reason="reversed")
                    del self.pending_momentum[setup]
                    done = True
                    break
                p.confirmed += 1
                if p.confirmed >= config.MOMENTUM_CONFIRM_CANDLES:
                    del self.pending_momentum[setup]
                    if ph not in strategy.ENTRY_PHASES:
                        log.info("[%s] MOMENTUM confirmed for %s but the entry window is closed: skipped",
                                 setup, p.direction)
                        self.journal.event("MOMENTUM_ABORTED", now, setup=setup, direction=p.direction,
                                           break_close=p.break_close, reason="entry window closed")
                    else:
                        log.info("[%s] MOMENTUM confirmed for %s (%s): entering now", setup, p.direction,
                                 "/".join(p.strategies))
                        self.journal.event("MOMENTUM_CONFIRMED", now, setup=setup, direction=p.direction,
                                           break_close=p.break_close, strategies=list(p.strategies))
                        self.enter(now, setup, p.direction, "MOMENTUM", strategies=p.strategies)
                    done = True
                    break
            if done:
                continue
            if now - p.signal_time > timedelta(minutes=config.MOMENTUM_CONFIRM_TIMEOUT_MIN):
                log.info("[%s] MOMENTUM confirmation TIMED OUT for %s after %d min: entry abandoned",
                         setup, p.direction, config.MOMENTUM_CONFIRM_TIMEOUT_MIN)
                self.journal.event("MOMENTUM_ABORTED", now, setup=setup, direction=p.direction,
                                   break_close=p.break_close, reason="timeout")
                del self.pending_momentum[setup]

    def enter(self, now: datetime, setup: str, direction: str, kind: str | None = None,
             strategies: tuple[str, ...] | None = None) -> None:
        equity = self.broker.get_account()["equity"]
        if not self.entry_possible(equity, setup, strategies):
            return
        if self.today not in self.broker.get_expirations(config.UNDERLYING, 0, 1):
            log.warning("No SPXW expiration for today %s: no trade.", self.today)
            return
        spot = self.broker.get_spot(config.UNDERLYING)
        width = strategy.width_for_tier(tier(equity))
        right = strategy.direction_to_spread(direction)
        em = self.expected_move(spot)
        short_a, long_a = strategy.select_strikes(spot, right, width)
        lo, hi = min(short_a, long_a), max(short_a, long_a)
        # A may walk up to A_MAX_STRIKE_WALK strikes toward spot.
        lo, hi = lo - 5 * config.A_MAX_STRIKE_WALK, hi + 5 * config.A_MAX_STRIKE_WALK
        if em is not None:
            # One chain wide enough for A and for B's whole strike walk (spot +/- 2 EM + width).
            lo, hi = min(lo, spot - 2 * em - width), max(hi, spot + 2 * em + width)
        chain = self.broker.get_option_chain(config.UNDERLYING, self.today, right, lo - 1, hi + 1, spot=spot)
        strategies = strategies or config.STRATEGIES
        summaries: dict[str, str] = {}
        open_risk = self.pm.total_open_risk()
        for strat in strategies:
            try:
                summaries[strat], added = self.enter_leg(strat, now, setup, direction, equity, spot, width,
                                                         right, chain, em, open_risk, kind)
            except BrokerError as e:
                log.error("[%s] broker error during entry: %s", strat, e)
                self.journal.event("BROKER_ERROR", now, strategy=strat, error=str(e))
                summaries[strat], added = f"broker error {e}", 0.0
            open_risk += added
        log.warning("SIGNAL %s %s at %s -- %s", setup, direction, now.strftime("%H:%M:%S"),
                    " | ".join(f"{s}: {summaries[s]}" for s in strategies))
        self.save_state()

    def expected_move(self, spot: float) -> float | None:
        calls = self.broker.get_option_chain(config.UNDERLYING, self.today, "C", spot - 5, spot + 5, spot=spot)
        puts = self.broker.get_option_chain(config.UNDERLYING, self.today, "P", spot - 5, spot + 5, spot=spot)
        em = strategy.expected_move(calls, puts, spot)
        if em is None:
            log.warning("No ATM straddle quote around spot %.2f: expected move unavailable.", spot)
        else:
            log.info("Expected move (ATM straddle) %.2f at spot %.2f", em, spot)
        return em

    def enter_leg(self, strat: str, now: datetime, setup: str, direction: str, equity: float, spot: float,
                  width: int, right: str, chain: list[dict], em: float | None,
                  open_risk: float, kind: str | None = None) -> tuple[str, float]:
        """Place one strategy's spread. Returns (operator summary, risk dollars added)."""
        if strat in self.pm.positions:
            return "skip: position open", 0.0
        if setup not in config.SETUPS_BY_STRATEGY.get(strat, (setup,)):
            return f"skip: {setup} setup disabled for {strat}", 0.0
        # Any kind-tagged setup (ORB or ON_BREAK -- OVERNIGHT never sets a kind) is gated the same way.
        if kind is not None and kind not in config.ORB_ENTRY_KINDS_BY_STRATEGY.get(strat, (kind,)):
            return f"skip: {setup} {kind} entry disabled for {strat}", 0.0
        allowed, reason = self.risk.trading_allowed(strat, equity, now, setup)
        if not allowed:
            return f"skip: {reason}", 0.0
        if strat == "A":
            short_strike, long_strike, result = strategy.select_strikes_a(spot, right, width, chain)
            quote, reject = (result, None) if short_strike is not None else (None, result)
            floor_credit = strategy.credit_range(width)[0]
            base_short, _ = strategy.select_strikes(spot, right, width)
            walked = int(abs((short_strike if short_strike is not None else base_short) - base_short) // 5)
            detail = f" walked {walked} strike(s) toward spot" if walked else ""
        else:
            if em is None:
                return "skip: NO_EXPECTED_MOVE", 0.0
            short_strike, long_strike, result = strategy.select_strikes_b(spot, right, width, em, chain)
            quote, reject = (result, None) if short_strike is not None else (None, result)
            floor_credit = strategy.credit_range_b(width)[0]
            detail = f" EM {em:.2f}"
        if reject:
            log.info("[%s] ENTRY REJECTED %s %s: %s", strat, direction, right, reject)
            self.journal.event("ENTRY_REJECTED", now, strategy=strat, setup=setup, direction=direction,
                               right=right, short=short_strike, long=long_strike, reason=reject, quote=quote)
            return f"skip: {reject}{detail}", 0.0
        qty = contracts_for(equity, width, quote.mid, open_risk, strategy.is_news_day(self.today))
        if qty == 0:
            log.info("%s_SKIPPED sizing: 0 contracts (equity %.2f width %d credit %.2f open risk %.2f)",
                     strat, equity, width, quote.mid, open_risk)
            return f"skip: sizing 0 contracts{detail}", 0.0
        log.warning("[%s] ENTRY %s %s spot %.2f: sell %s%s buy %s%s x%d @ %.2f (bid-side %.2f, floor %.2f)%s",
                    strat, setup, direction, spot, short_strike, right, long_strike, right, qty, quote.mid,
                    quote.bid_side, floor_credit, detail)
        order = self.broker.place_credit_spread(
            config.UNDERLYING, self.today, right, short_strike, long_strike, qty, quote.mid,
            time_in_force="day", root=config.OPTION_ROOT,
            client_tag=f"0dte-{config.TRADER_NAME.lower()}-{strat.lower()}-{setup.lower()}")
        order = self.work_entry(order, quote.mid, floor_credit)
        filled_qty = qty if order.get("status") == "dry_run" else int(order.get("filled_qty") or 0)
        if filled_qty == 0:
            log.info("[%s] ENTRY NOT FILLED (%s): order %s", strat, order.get("status"), order.get("id"))
            self.journal.event("ENTRY_UNFILLED", now, strategy=strat, order_id=order.get("id"),
                               status=order.get("status"))
            return f"not filled ({order.get('status')}){detail}", 0.0
        credit = float(order.get("filled_avg_price") or order.get("limit_price") or quote.mid)
        target, stop = strategy.exit_levels(strat)
        spread = OpenSpread(
            strategy=strat, direction=direction, setup=setup, right=right, root=config.OPTION_ROOT,
            expiration=self.today, short_strike=short_strike, long_strike=long_strike, width=width,
            qty=filled_qty, entry_credit=credit, entry_time=now_et(), current_price=credit,
            best_price=credit, profit_target=target, stop_loss=stop, entry_mid=quote.mid)
        self.pm.open(spread)
        card = format_entry_card(spread, self.risk.state.for_strategy(strat).trades_today + 1)
        log.info("\n%s", card)
        self.journal.card(card, now)
        return f"sell {short_strike:g}{right} buy {long_strike:g}{right} x{filled_qty} @ {credit:.2f}{detail}", spread.open_risk

    def work_entry(self, order: dict, start_limit: float, floor_limit: float) -> dict:
        if order.get("status") == "dry_run":
            return order
        limit = start_limit
        deadline = time.monotonic() + config.ENTRY_TIMEOUT_SEC
        while True:
            order = self.broker.wait_for_fill(order["id"], config.ENTRY_STEP_SEC)
            if order["status"] in TERMINAL:
                return order
            next_limit = round(limit - config.ENTRY_PRICE_STEP, 2)
            if time.monotonic() >= deadline or next_limit < floor_limit - 1e-9:
                break
            log.info("Entry not filled at %.2f, stepping to %.2f", limit, next_limit)
            order = self.broker.replace_order_price(order["id"], next_limit)
            limit = next_limit
        log.info("Entry timeout: cancelling order %s", order["id"])
        self.broker.cancel_order(order["id"])
        return self.broker.get_order(order["id"]) or order

    # ---------------------------------------------------------------- manage

    def spread_prices(self) -> dict[str, float]:
        """Current mid per open strategy; positions sharing expiration/right share one chain call."""
        groups: dict[tuple, list[OpenSpread]] = {}
        for p in self.pm.positions.values():
            groups.setdefault((p.expiration, p.right), []).append(p)
        prices: dict[str, float] = {}
        for (expiration, right), group in groups.items():
            strikes = [s for p in group for s in (p.short_strike, p.long_strike)]
            chain = self.broker.get_option_chain(config.UNDERLYING, expiration, right,
                                                 min(strikes) - 1, max(strikes) + 1)
            for p in group:
                quote = strategy.spread_quote(chain, p.short_strike, p.long_strike)
                if quote is None:
                    log.warning("[%s] No chain quote for %s %s/%s; skipping manage this tick.",
                                p.strategy, p.right, p.short_strike, p.long_strike)
                else:
                    prices[p.strategy] = quote.mid
        return prices

    def manage(self, now: datetime, candles: pd.DataFrame) -> None:
        prices = self.spread_prices()
        for strat, p in self.pm.positions.items():
            if strat in prices:
                log.info("[%s] Position %s%s/%s x%d entry %.2f now %.2f best %.2f%s", strat, p.short_strike,
                         p.right, p.long_strike, p.remaining, p.entry_credit, prices[strat], p.best_price,
                         f" RUNNER best {p.runner_best:.2f}" if p.runner else "")
        self.record_fills(self.pm.on_tick(now, prices, candles))

    def record_fills(self, fills: list[dict]) -> None:
        for row in fills:
            self.journal.trade(row)
            strat = row["strategy"]
            s = self.risk.state
            trade_no = s.for_strategy(strat).trades_today + 1
            if row["position_closed"]:
                self.risk.record_trade(strat, row["position_pnl"], row)
                remaining = 0
                day_pnl = s.pnl_by_strategy()
            else:
                remaining = self.pm.positions[strat].remaining
                day_pnl = s.pnl_by_strategy()
                day_pnl[strat] = round(day_pnl.get(strat, 0.0) + row["position_pnl"], 2)
            card = format_close_card(row, trade_no, day_pnl, s.trades_today, remaining)
            log.info("\n%s", card)
            self.journal.card(card, now_et())
        if fills:
            self.save_state()

    # ------------------------------------------------------------ end of day

    def end_of_day(self, now: datetime) -> None:
        log.warning("FORCE CLOSE at %s: cancelling orders and closing everything.", now.strftime("%H:%M:%S"))
        cancelled = self.broker.cancel_all_orders()
        log.info("Cancelled %d open orders.", cancelled)
        for attempt in range(1, config.CLOSE_MAX_RETRIES + 1):
            positions = self.broker.get_positions()
            if spxw_legs(positions):
                self.pm.adopt_from_broker(positions, now, self.broker.get_spot(config.UNDERLYING))
            if self.pm.positions:
                self.record_fills(self.pm.force_close(now, self.spread_prices()))
            legs = spxw_legs(self.broker.get_positions())
            if not legs and not self.pm.positions:
                break
            log.critical("CLOSE_FAILED: %s legs still at broker after attempt %d: %s",
                         config.OPTION_ROOT, attempt, [(l["symbol"], l["qty"]) for l in legs])
            time.sleep(config.CLOSE_RETRY_SEC)
        self.report_end(now_et())
        self.save_state()

    # --------------------------------------------------------------- reports

    def report_start(self, now: datetime, acct: dict, positions: list[dict]) -> None:
        pnl = self.broker.get_pnl_summary()
        t = tier(acct["equity"])
        width = strategy.width_for_tier(t)
        lo, hi = strategy.credit_range(width)
        lo_b, hi_b = strategy.credit_range_b(width)
        orders = self.broker.get_open_orders()
        day = self.risk.state
        lines = [
            "=" * 72, f"START-OF-DAY REPORT  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"Trader: {config.TRADER_NAME}   Broker: {self.broker.name} "
            f"({'PAPER' if self.broker.is_paper else 'LIVE'}){'  DRY-RUN' if self.broker.dry_run else ''}",
            f"Equity {fmt_money(acct['equity'])}  Cash {fmt_money(acct['cash'])}  "
            f"Options BP {fmt_money(acct['options_buying_power'])}",
            f"P/L  YTD {fmt_money(pnl['ytd'])}  MTD {fmt_money(pnl['mtd'])}  Today {fmt_money(pnl['today'])}",
            f"Open positions: {len(positions)}" + "".join(
                f"\n   {p['symbol']} qty {p['qty']} avg {p['avg_price']}" for p in positions),
            f"Open orders: {len(orders)}" + "".join(
                f"\n   {o['id']} {o['status']} {o['symbol']} qty {o['qty']} @ {o['limit_price']}" for o in orders),
            f"Tier {t}  width {width}  credit range A {lo:.2f}-{hi:.2f}  B {lo_b:.2f}-{hi_b:.2f}",
            f"Phase: {strategy.phase(now)}   News day: {strategy.is_news_day(now.date())} "
            f"(mode {config.NEWS_DAY_MODE})   Level source: {config.BREAKOUT_LEVEL_SOURCE}",
            f"Day state: trades {day.trades_today} realized {fmt_money(day.realized_pnl)} " + " ".join(
                f"| {k}: trades {s.trades_today} losses-in-a-row {s.consecutive_losses} P/L {fmt_money(s.realized_pnl)}"
                for k, s in day.strategies.items()),
            "=" * 72,
        ]
        for line in lines:
            log.info(line)
        self.journal.event("START", now, account=acct, pnl=pnl, positions=positions, orders=orders, tier=t)

    def report_end(self, now: datetime) -> None:
        acct = self.broker.get_account()
        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        legs = spxw_legs(positions)
        s = self.risk.state
        lines = ["=" * 72, f"END-OF-DAY REPORT  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                 f"Trades closed: {s.trades_today}   Realized P&L: {fmt_money(s.realized_pnl)}   " + "  ".join(
                     f"{k}: {st.trades_today} trades {fmt_money(st.realized_pnl)}" for k, st in s.strategies.items()),
                 f"Equity start {fmt_money(s.start_equity)} -> end {fmt_money(acct['equity'])} "
                 f"({acct['equity'] - s.start_equity:+,.2f})"]
        table = format_day_table(s.closed_trades, s.realized_pnl)
        self.journal.card(table, now)
        mismatches = []
        if legs:
            mismatches.append(f"broker still holds {config.OPTION_ROOT} legs: {[(l['symbol'], l['qty']) for l in legs]}")
        for strat, p in self.pm.positions.items():
            mismatches.append(f"local state still has a {strat} position: {p.to_dict()}")
        if orders:
            mismatches.append(f"open orders remain: {[o['id'] for o in orders]}")
        lines.append("Reconciliation: " + ("OK - flat, no orders" if not mismatches else "MISMATCH"))
        lines.extend(f"   !! {m}" for m in mismatches)
        lines.append("=" * 72)
        for line in lines:
            (log.critical if mismatches else log.info)(line)
        log.info("\n%s", table)
        self.journal.event("END", now, account=acct, day=s.to_dict(), mismatches=mismatches)

    # ----------------------------------------------------------------- state

    def save_state(self) -> None:
        self.journal.save_state({
            "date": self.today.isoformat(),
            "day": self.risk.state.to_dict(),
            "positions": {strat: p.to_dict() for strat, p in self.pm.positions.items()},
            "acted": sorted(self.acted),
            "levels": {"overnight": self.overnight, "basis": self.basis, "orb": self.orb},
        })

    def on_interrupt(self) -> None:
        if self.risk is not None:
            self.save_state()
        if not self.pm.positions:
            log.warning("Interrupted by user. No open position.")
            return
        if config.CLOSE_ON_INTERRUPT and self.risk is not None:
            log.warning("Interrupted by user with %d open position(s): closing them now.", len(self.pm.positions))
            try:
                self.record_fills(self.pm.force_close(now_et(), self.spread_prices(), reason="INTERRUPT_CLOSE"))
            except BrokerError as e:
                log.error("Close on interrupt failed: %s", e)
            self.save_state()
            if not self.pm.positions:
                log.warning("All positions closed; flat.")
                return
        log.warning("Nothing (more) was closed automatically.")
        for strat, p in self.pm.positions.items():
            log.warning("OPEN POSITION [%s]: %s sell %s%s / buy %s%s x%d, entry credit %.2f, last %.2f, exp %s",
                        strat, p.direction, p.short_strike, p.right, p.long_strike, p.right, p.remaining,
                        p.entry_credit, p.current_price, p.expiration)
        log.warning("To close: restart `python scheduler.py` (it re-adopts and manages the spreads), "
                    "or run `python alpaca-reset.py` to liquidate ALL positions, "
                    "or close the SPXW legs in the Alpaca dashboard.")


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    """Route SIGTERM (and Windows Ctrl+Break) through the same close-on-interrupt path as Ctrl+C."""
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_interrupt)


def main() -> None:
    parser = argparse.ArgumentParser(description="0DTE SPX credit-spread bot")
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()
    setup_logging()
    install_signal_handlers()
    bot = Bot(dry_run=args.dry_run or config.DRY_RUN, once=args.once)
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.on_interrupt()
    except BrokerError as exc:
        log.error("BROKER ERROR: %s", exc)
        if "401" in str(exc) or "unauthorized" in str(exc).lower():
            log.error("The broker rejected the API keys. Open the .env file in this folder and check the "
                      "key/secret values (no quotes, no spaces). If you rotated keys, paste the new ones.")
        else:
            log.error("Check the .env file in this folder and your internet connection, then start again.")
        sys.exit(1)


if __name__ == "__main__":
    main()

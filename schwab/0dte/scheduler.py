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
from typing import TYPE_CHECKING

import pandas as pd

import clock_check
import config
import instance_lock
import market_data
import strategy
from broker import Broker, BrokerError
from journal import Journal, format_close_card, format_day_table, format_entry_card
from position_manager import OpenSpread, PositionManager, occ_expiration, occ_root, option_legs
from risk_manager import DayState, RiskManager, contracts_for, tier
from strategy import Phase

if TYPE_CHECKING:   # stock_orb (and through it orb_scanner + yfinance) is imported lazily, by F personas only
    from stock_orb import StockOrbTrader

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
ES_CHASE_MIN = 5      # how long after an SPX candle a live-ES frame that is still behind it is looked at again, every tick
TERMINAL = {"filled", "canceled", "rejected", "expired", "replaced", "dry_run"}

log = logging.getLogger("0dte")


def now_et() -> datetime:
    return datetime.now(config.ET)


def _keep_or_important(record: logging.LogRecord) -> bool:
    """2026-09-25: only entry data, exit data, and a best-price-hit line need to be recorded at all
    right now -- per-tick position status, candle debug chatter, signal/skip-reason noise, etc. are
    not needed at TICK_SECONDS=1's volume. A record passes if it's tagged keep=True (entry/exit cards,
    BEST PRICE, start/end-of-day reports, interrupt/shutdown messages) or is a genuine failure
    (ERROR/CRITICAL) that should never be silently dropped."""
    return record.levelno >= logging.ERROR or getattr(record, "keep", False)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, config.ET).timetuple()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.addFilter(_keep_or_important)
    file_handler = logging.FileHandler(LOG_DIR / f"0dte_{config.TRADER_NAME.lower()}.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(_keep_or_important)
    for handler in (console, file_handler):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def fmt_money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


# 2026-10-05: SIGNAL_SKIPPED journal events -- one row for every (signal, strategy) pair that did NOT become a
# trade, with a stable reason_code, so a later strategy ranking can see what the caps/cooldowns/gates turned away
# and not only the trades that happened. Substring rules first (the summary text carries per-signal detail),
# otherwise the reason's first word (DAY_GATE, MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT, B_NO_STRIKE_IN_RANGE, ...).
_SKIP_CODES = (
    ("position open", "POSITION_OPEN"),
    ("setup disabled for", "SETUP_DISABLED"),
    ("entry disabled for", "ENTRY_KIND_DISABLED"),
    ("sizing 0 contracts", "SIZING_ZERO"),
    ("not filled", "NOT_FILLED"),
    ("broker error", "BROKER_ERROR"),
    ("MANUAL_CLOSE cooldown", "MANUAL_CLOSE_COOLDOWN"),
)


def skip_code(reason: str) -> str:
    text = str(reason).strip()
    for needle, code in _SKIP_CODES:
        if needle in text:
            return code
    words = text.replace(":", " ").replace("(", " ").split()
    return words[0].upper() if words else "UNKNOWN"


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
        # overnight_levels_for_signal() can resolve to either ES or SPX-terms levels, and can switch
        # mid-day (live_es_available() is re-evaluated fresh every call) -- tracking which symbol an
        # existing on_setup/es_setup was built against lets try_on_break_entry/try_es_break_entry detect
        # a source flip and rebuild fresh instead of feeding candles from the other instrument's price
        # scale into a state machine calibrated for the old one.
        self.on_setup_symbol: str | None = None
        self.es_setup_symbol: str | None = None
        self.last_on_candle: datetime | None = None
        self.last_candle: datetime | None = None
        self.last_orb_candle: datetime | None = None
        self.pending_momentum: dict[str, PendingMomentum] = {}
        self.last_entry_blocks: dict[str, str] = {}   # strategy -> why entry_possible() refused it (for SIGNAL_SKIPPED)
        self.last_continuation_check: dict[str, datetime] = {}
        self.today: date = now_et().date()
        self.es_setup: strategy.TwoCandleBreak | None = None   # Strategy C, ES-sourced overnight levels
        self.last_es_candle: datetime | None = None
        self.last_de_candle: datetime | None = None             # Strategies D/E, MA/Bollinger reversion
        self.news_blackout_was_active: bool = False
        self._saved_spxw: set[tuple] = set()       # SPXW contracts today's saved state says THIS bot opened
        self._hold_alert_at: datetime | None = None
        self._eod_retry_at: datetime | None = None
        self._data_stale = False                      # an open position has no fresh price (see _check_marks)
        self._stale_alert_at: dict[str, datetime] = {}
        self._entry_wait_end: datetime | None = None  # when the tick loop was last released from working an entry order
        self._es_chase_until: datetime | None = None  # live ES was behind the SPX candle being evaluated: look again until then
        self._token_halt: str | None = None           # Schwab sim: the refresh token is expiring / dead
        self._token_checked_at: datetime | None = None
        self._token_warned_at: datetime | None = None
        self._last_good_spot: float | None = None
        self._tick_exc_logged: datetime | None = None
        self._f_last_priced: dict[str, float] = {}              # Strategy F chain-mark throttle, per ticker
        self.f: StockOrbTrader | None = None
        if "F" in config.STRATEGIES:
            try:
                # Imported here, inside the guard: a broken stock_orb / orb_scanner / yfinance install can
                # only cost this persona its F, never abort startup for the A-E personas that never import it.
                from stock_orb import StockOrbTrader
                self.f = StockOrbTrader(self, chain_broker_factory=lambda: Broker(config, dry_run=True, log=log.debug))
            except Exception as e:
                log.error("Strategy F disabled for today: %s", e)

    def f_owns_expiration(self, expiration: date | str | None) -> bool:
        """A stock option leg is this persona's (an F position, tracked or not) only when it expires inside
        F's window: 0 to F_MAX_DTE days out. Anything longer-dated in the same account belongs to someone
        else -- the 45DTE bot falling back to the same keys, or a manual position -- and is left alone."""
        if expiration is None:
            return False
        exp = expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)[:10])
        return 0 <= (exp - self.today).days <= config.F_MAX_DTE

    def bot_legs(self, positions: list[dict]) -> list[dict]:
        """Option legs at the broker that this persona adopts, closes and reconciles: SPXW, every root it
        already tracks, and -- whenever F is configured, whether or not StockOrbTrader built today --
        stock legs inside F's DTE window. Keyed on the persona's config, not on self.f, so a day F fails
        to construct still re-adopts, marks and force-closes the stock spreads it left at the broker."""
        # 2026-10-08: tracked stock legs are matched by contract, not by root alone -- "root in tracked" also swept up a
        # 45DTE spread on the same ticker (52 DTE) and force-closed it at the hard stop
        tracked_legs = {(p.root, p.right, p.expiration, float(k)) for p in self.pm.positions.values()
                        if p.root != config.OPTION_ROOT for k in (p.short_strike, p.long_strike)}
        f_on = "F" in config.STRATEGIES

        def mine(leg: dict) -> bool:
            root = occ_root(leg["symbol"])
            if root == config.OPTION_ROOT:
                # this bot only ever trades SPXW that expires the day it opens it; anything else is the owner's
                if leg.get("expiration") != self.today:
                    return False
                # and on a shared live account even a same-day SPXW leg is only the bot's if the bot opened it
                # (tracked now, or in today's saved state) -- unless adopting untracked legs is switched on
                return config.ADOPT_UNTRACKED_SPXW or \
                    (leg["right"], float(leg["strike"]), leg["expiration"]) in self._known_spxw()
            if (root, leg["right"], leg.get("expiration"), float(leg["strike"])) in tracked_legs:
                return True
            return f_on and self.f_owns_expiration(leg.get("expiration"))

        return [leg for leg in option_legs(positions) if mine(leg)]

    def _known_spxw(self) -> set[tuple]:
        """(right, strike, expiration) of every SPXW contract this bot opened: tracked now or in today's saved state."""
        known = set(self._saved_spxw)
        for p in self.pm.positions.values():
            if p.root == config.OPTION_ROOT:
                known |= {(p.right, float(p.short_strike), p.expiration), (p.right, float(p.long_strike), p.expiration)}
        return known

    def foreign_legs(self, positions: list[dict]) -> list[dict]:
        """Option legs in the account that bot_legs() leaves alone."""
        owned = self.bot_legs(positions)
        return [leg for leg in option_legs(positions) if leg not in owned]

    # ------------------------------------------------------------ lifecycle

    def _account_preflight(self, acct: dict) -> None:
        """Log the account's state and refuse to go on when a LIVE account cannot trade (Broker.preflight_account raises
        AccountNotTradable). On paper every problem is only a warning. Brokers without the check (Schwab) skip it."""
        check = getattr(self.broker, "preflight_account", None)
        if check is None:
            return
        result = check(acct)
        log.info("Account check: %s", result["summary"], extra={"keep": True})
        for text in result["warnings"] + result["problems"]:
            log.warning("Account check: %s", text, extra={"keep": True})

    def start(self) -> None:
        now = now_et()
        acct = self.broker.get_account()
        self._account_preflight(acct)
        saved = self.journal.load_state()
        saved_today = saved if saved and saved.get("date") == self.today.isoformat() else None
        self._saved_spxw = {(d["right"], float(d[k]), date.fromisoformat(d["expiration"]))
                            for d in ((saved_today or {}).get("positions") or {}).values()
                            if d and d.get("root", config.OPTION_ROOT) == config.OPTION_ROOT
                            for k in ("short_strike", "long_strike")}
        if saved_today:
            self.risk = RiskManager(acct["equity"], DayState.from_dict(saved_today["day"]))
            self.acted = set(saved_today.get("acted", []))
            log.info("Restored state for %s: %s", self.today, saved_today["day"])
            if self.f is not None:
                self.f.restore(saved_today.get("f"), now)
            self._restore_breakout_state(saved_today)
        else:
            self.risk = RiskManager(acct["equity"])
        cap = acct.get("capital_cap")      # 2026-10-15 (Schwab go-live): the day's virtual start-of-day equity IS the capital cap, whatever a restored state saw
        if cap:
            self.risk.state.start_equity = cap
        positions = self.broker.get_positions()
        legs = self.bot_legs(positions)
        foreign = self.foreign_legs(positions)
        if foreign:
            log.warning("Ignoring %d option leg(s) in the account that are not this persona's: %s", len(foreign),
                        [(l["symbol"], l["qty"]) for l in foreign], extra={"keep": True})
        if legs:
            self.pm.adopt_from_broker(legs, now, self.broker.get_spot(config.UNDERLYING),
                                      saved_today.get("positions") if saved_today else None)
        self._reconcile_saved_positions(saved_today, positions, now)
        self.report_start(now, acct, positions)
        self.broker.record_daily_equity()
        self.save_state()
        self.run(now)

    def _reconcile_saved_positions(self, saved_today: dict | None, positions: list[dict], now: datetime) -> None:
        """A position saved before a restart that is no longer at the broker was closed while the bot was down
        (by hand): book it as EXTERNAL_CLOSE so the day's trades, P/L and the manual-close cool-down are right
        instead of the position silently vanishing. Needs two reads, both showing it gone."""
        saved = (saved_today or {}).get("positions") or {}
        if not saved or not self.pm._sync_active():
            return
        tracked = {(p.root, p.right, p.expiration, p.short_strike) for p in self.pm.positions.values()}
        missing = []
        for key, d in saved.items():
            if not d:
                continue
            p = OpenSpread.from_dict(d)
            if p.remaining > 0 and (p.root, p.right, p.expiration, p.short_strike) not in tracked:
                missing.append((key, p))
        if not missing:
            return
        time.sleep(config.EXTERNAL_PREFLIGHT_RECHECK_SEC)
        try:
            again = self.broker.get_positions()
        except BrokerError:
            return
        rows: list[dict] = []
        for key, p in missing:
            if self.pm._held_qty(p, positions) == (0, 0) and self.pm._held_qty(p, again) == (0, 0):
                self.pm.positions[key] = p
                rows += self.pm._book_external(p, p.remaining, now, "EXTERNAL_CLOSE")
        if rows:
            self.record_fills(rows)

    def _restore_breakout_state(self, saved_today: dict) -> None:
        """Restore the overnight/opening-range levels, the breakout state machines they drive
        (orb_setup/on_setup/es_setup), and the last-processed-candle pointers so a restart continues
        exactly where the previous run left off instead of replaying the whole day from scratch
        through brand-new (WAITING) state machines. See the 2026-10-01 note in save_state().

        Every reconstruction below is best-effort: a malformed or unexpectedly-shaped saved value (an
        old schema, a hand-edited file, a future field rename) is logged and skipped rather than raised
        -- falling back to that one field's normal __init__ default (fresh/unset), exactly the same
        degraded-but-running outcome a persona had before this method existed, never a crash that takes
        the whole process down on restart."""
        levels = saved_today.get("levels") or {}
        try:
            if levels.get("overnight"):
                self.overnight = strategy.Levels.from_dict(levels["overnight"])
            self.basis = levels.get("basis")
            if levels.get("orb"):
                self.orb = strategy.Levels.from_dict(levels["orb"])
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Could not restore saved levels (%s: %s) -- will be refetched fresh.",
                       type(e).__name__, e, extra={"keep": True})
        breakouts = saved_today.get("breakouts") or {}
        self.news_blackout_was_active = bool(breakouts.get("news_blackout_was_active", False))
        for attr, symbol_attr, cls, key in (
            ("orb_setup", None, strategy.OrbSetup, "orb_setup"),
            ("on_setup", "on_setup_symbol", strategy.OrbSetup, "on_setup"),
            ("es_setup", "es_setup_symbol", strategy.TwoCandleBreak, "es_setup"),
        ):
            raw = breakouts.get(key)
            if not raw:
                continue
            try:
                setattr(self, attr, cls.from_dict(raw))
                if symbol_attr is not None:
                    setattr(self, symbol_attr, breakouts.get(symbol_attr))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("Could not restore %s from saved state (%s: %s) -- starting it fresh instead.",
                           key, type(e).__name__, e, extra={"keep": True})
        for attr in ("last_orb_candle", "last_on_candle", "last_es_candle", "last_de_candle", "last_candle"):
            v = breakouts.get(attr)
            if not v:
                continue
            try:
                setattr(self, attr, datetime.fromisoformat(v))
            except (TypeError, ValueError) as e:
                log.warning("Could not restore %s (%r): %s", attr, v, e, extra={"keep": True})
        try:
            self.last_continuation_check = {k: datetime.fromisoformat(v)
                                            for k, v in (breakouts.get("last_continuation_check") or {}).items()}
        except (TypeError, ValueError) as e:
            log.warning("Could not restore last_continuation_check: %s", e, extra={"keep": True})
            self.last_continuation_check = {}
        if breakouts:
            log.info("Restored breakout state: ORB=%s ON_BREAK=%s ES_BREAK=%s",
                     self.orb_setup.state if self.orb_setup else None,
                     self.on_setup.state if self.on_setup else None,
                     self.es_setup.state if self.es_setup else None)

    def run(self, now: datetime) -> None:
        if not market_data.is_trading_day(now) and not self.pm.positions:
            # keep=True: the console only shows records tagged keep (or errors), and a bot that prints its start report and then exits
            # without a word looks exactly like one that failed to start (a Saturday launch on 2026-10-10 did)
            log.info("Weekend (%s): the market is closed, nothing to do -- exiting. Start the bot on a trading day.", now.strftime("%A"),
                     extra={"keep": True})
            return
        ph = strategy.phase(now)
        if ph == Phase.CLOSED:
            if not self.pm.positions:
                log.info("Started after the hard stop (%s ET) with no open position: nothing to do today -- exiting.",
                         config.FORCE_CLOSE_TIME.strftime("%H:%M"), extra={"keep": True})
                return
            log.warning("Started after FORCE_CLOSE_TIME with open positions: closing them now.")
            if self.end_of_day(now_et()) is False:     # MANUAL_HOLD: stay up until it is lifted or the session ends
                self.loop()
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
            if self.f is not None:
                self.f.step(now)   # pre-open: only the F_PREWARM_TIME history warm-up can run here
            if now >= strategy.at_time(now, config.MARKET_OPEN):
                return
            time.sleep(config.TICK_SECONDS)

    def entry_halt_reason(self) -> str | None:
        """Why no strategy may open a position right now (None = no data/auth reason)."""
        if self._data_stale:
            return f"DATA_STALE (an open position has had no fresh price for {config.MARK_STALE_ALERT_SEC}s+: entries halted)"
        return self._token_halt

    def _check_marks(self, now: datetime) -> None:
        """Staleness watchdog: an open position that has not been priced for MARK_STALE_ALERT_SEC raises a CRITICAL alert and
        halts new entries; after MARK_STALE_FLATTEN_SEC it is closed at its last known price. Runs after every tick, including
        ticks that raised, so a data outage can no longer leave stops and the hard stop switched off."""
        if not self.pm.positions or strategy.phase(now) not in (Phase.OVERNIGHT_ONLY, Phase.ORB_ONLY, Phase.NO_NEW_ENTRIES):
            self._data_stale = False
            return
        for key in list(self.pm.positions):
            p = self.pm.positions.get(key)
            if p is None:
                continue
            is_stock = p.strategy in config.STOCK_STRATEGIES
            alert = config.F_MARK_STALE_ALERT_SEC if is_stock else config.MARK_STALE_ALERT_SEC
            flatten = config.F_MARK_STALE_FLATTEN_SEC if is_stock else config.MARK_STALE_FLATTEN_SEC
            age = self._mark_age(p, now)
            if age < alert:
                continue
            last = self._stale_alert_at.get(key)
            if last is None or (now - last).total_seconds() >= 60:
                self._stale_alert_at[key] = now
                log.critical("[%s] STALE DATA: no fresh price for %.0fs (alert %ds, flatten %ds)%s", key, age, alert, flatten,
                             "" if config.STALE_MARK_FLATTEN_ENABLED else " [flatten disabled]", extra={"keep": True})
                self.journal.event("STALE_MARK", now, strategy=p.strategy, key=key, age_sec=round(age, 1),
                                   alert_sec=alert, flatten_sec=flatten)
            if config.STALE_MARK_FLATTEN_ENABLED and age >= flatten:
                log.critical("[%s] STALE DATA for %.0fs: closing at the last known price (%.2f)", key, age, p.current_price,
                             extra={"keep": True})
                try:
                    self.record_fills(self.pm.close_one(key, now, "STALE_DATA_FLATTEN"))
                except BrokerError as e:
                    log.error("[%s] stale-data flatten failed: %s", key, e)
        self._data_stale = any(self._mark_age(p, now) >= (config.F_MARK_STALE_ALERT_SEC if p.strategy in config.STOCK_STRATEGIES
                                                         else config.MARK_STALE_ALERT_SEC)
                               for p in self.pm.positions.values())

    def _mark_age(self, p: OpenSpread, now: datetime) -> float:
        """Seconds since `p` was last priced -- but the loop is blocked while an entry order is worked (up to ENTRY_TIMEOUT_SEC,
        longer than MARK_STALE_FLATTEN_SEC), during which no position can be priced, so that time is not a data outage: the clock
        counts from the later of the last price and the moment the loop was released. The watchdog used to flatten a healthy
        sibling position the instant a slow entry returned; now a position still unpriced MARK_STALE_FLATTEN_SEC after the loop is
        released is flattened as before."""
        age = self.pm.mark_age(p, now)
        end = self._entry_wait_end
        if end is not None and end <= now:
            age = min(age, (now - end).total_seconds())
        return age

    def _token_check(self, now: datetime) -> None:
        """Schwab sim only (the broker exposes token_status): warn as the 7-day refresh token ages, refuse new entries when it
        would expire before the hard stop, and say so loudly so a human re-runs schwab_login.py."""
        status_fn = getattr(self.broker, "token_status", None)
        if status_fn is None:
            return
        if self._token_checked_at is not None and (now - self._token_checked_at).total_seconds() < config.TOKEN_CHECK_SEC:
            return
        self._token_checked_at = now
        try:
            st = status_fn(now)
        except Exception as e:                      # a broken health check must never take the bot down
            log.error("token check failed: %s", e)
            return
        state = st["state"]
        self._token_halt = None
        if state in ("EXPIRING", "DEAD", "MISSING"):
            self._token_halt = f"TOKEN_{state} ({st['message']}): new entries halted"
        if state != "OK" and (self._token_warned_at is None or (now - self._token_warned_at).total_seconds() >= 900):
            self._token_warned_at = now
            level = log.warning if state == "WARN" else log.critical
            level("SCHWAB TOKEN %s: %s -- re-login: %s", state, st["message"], st.get("relogin", "python schwab_login.py --force"),
                  extra={"keep": True})
            self.journal.event("TOKEN_STATUS", now, **{k: v for k, v in st.items() if k in ("state", "age_days", "expires_at", "message")})

    def loop(self) -> None:
        while not self.done:
            now = now_et()
            try:
                self.tick(now)
            except BrokerError as e:
                log.error("Broker error during tick: %s", e)
                self.journal.event("BROKER_ERROR", now, error=str(e))
            except Exception as e:      # noqa: BLE001 -- 2026-10-08: nothing but Ctrl+C may end the loop with positions open
                if self._tick_exc_logged is None or (now - self._tick_exc_logged).total_seconds() >= config.TICK_EXCEPTION_LOG_SEC:
                    self._tick_exc_logged = now
                    log.critical("TICK EXCEPTION %s: %s (the bot keeps running)", type(e).__name__, e, exc_info=True,
                                 extra={"keep": True})
                    self.journal.event("TICK_EXCEPTION", now, type=type(e).__name__, error=repr(e)[:300])
            try:
                self._token_check(now)
                self._check_marks(now_et())
            except Exception as e:      # noqa: BLE001 -- the watchdog itself must not be able to kill the loop either
                log.error("watchdog error: %s: %s", type(e).__name__, e)
            if self.once:
                log.info("--once: single tick complete.")
                return
            time.sleep(config.TICK_SECONDS)

    def tick(self, now: datetime) -> None:
        ph = strategy.phase(now)
        if ph == Phase.CLOSED:
            if self.end_of_day(now) is not False:
                self.done = True
            return
        blackout_now = self.news_blackout_active(now)
        if self.news_blackout_was_active and not blackout_now:
            log.info("NEWS_BLACKOUT cleared: resetting breakout state machines for a fresh post-news read.")
            for setup_obj in (self.orb_setup, self.on_setup, self.es_setup):
                if setup_obj is not None:
                    setup_obj._reset()
            if self.f is not None:
                self.f.reset_setups()
        self.news_blackout_was_active = blackout_now
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
            if self.f is not None:
                self.f.refresh_candles(now)     # F's ticker frames stay current in EVERY phase, disabled or not
            self.manage(now, candles)
        if self.pending_momentum:
            self.check_pending_momentum(now, ph)
        if ph in strategy.ENTRY_PHASES:
            # 2026-10-08: with live ES the breakout setups are fed ES candles, which can arrive a few seconds after the SPX candle
            # that triggers this evaluation (or the /ES request can fail once): while the ES frame is still behind, look again on
            # the next ticks (_es_chase_until) rather than letting that candle go by and be replayed as a stale signal.
            evaluate = new_candle or (self._es_chase_until is not None and now < self._es_chase_until)
            self._es_chase_until = None          # set again below by whichever setup still finds the ES frame behind
            if evaluate and ph in strategy.OVERNIGHT_PHASES:
                self.try_on_break_entry(now, candles)   # OVERNIGHT retired 2026-09-24; ON_BREAK covers both A and B
            if evaluate and (ph in strategy.OVERNIGHT_PHASES
                             or strategy.minutes_since_open(now) < config.C_ENTRY_END_MIN):
                self.try_es_break_entry(now, candles)   # Strategy C (config.C_ENTRY_END_MIN widens its window)
            if orb_signal is not None:
                self.try_orb_entry(now, *orb_signal)
            self.check_continuation_reentry(now, ph)
            self.check_mean_reversion_entry(now, ph)    # Strategies D/E
            if self.f is not None:
                self.f.step(now)                        # Strategy F (guarded; after manage())
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
        # orb_setup is checked separately from orb itself (not nested in the fetch above) so a restart
        # that restores orb_setup from saved state never gets it silently overwritten with a fresh
        # WAITING one just because self.orb was also freshly fetched or restored this same call.
        if self.orb is not None and self.orb_setup is None:
            self.orb_setup = strategy.OrbSetup(self.orb.high, self.orb.low)

    @staticmethod
    def log_levels(label: str, levels: strategy.Levels | None) -> None:
        if levels is not None:
            log.info("%s high %.2f low %.2f (established %s)", label, levels.high, levels.low,
                     levels.established_at.strftime("%H:%M"))

    def overnight_levels_for_signal(self) -> tuple[strategy.Levels, str] | None:
        if self.overnight is None:
            return None
        source = config.BREAKOUT_LEVEL_SOURCE
        # 2026-09-25: once a live ES source is configured (market_data.live_es_available -- Schwab
        # cross-auth in this folder's .env), the whole reason for the ES_TO_SPX proxy (yfinance's
        # ES=F being ~10 min delayed) no longer applies -- confirm directly on ES's own candles
        # instead, which is what actually captures the ES-leads-SPX lead-time edge. An explicit
        # BREAKOUT_LEVEL_SOURCE="ES" override still forces raw-ES confirmation even without a live
        # source (e.g. for testing), and this adaptive switch only kicks in for the default "ES_TO_SPX".
        if source == "ES_TO_SPX" and market_data.live_es_available():
            source = "ES"
        if source == "ES":
            return self.overnight, config.ES_SYMBOL
        if self.basis is None:
            return None
        return self.overnight.shifted(self.basis), config.SPX_SYMBOL

    # --------------------------------------------------------------- signals

    def _levels_and_candles(self, now: datetime, spx_candles: pd.DataFrame):
        """(levels, symbol, candles) for the overnight-level setups, or None when no level is usable yet. On live ES the candles are
        ES's own: if that request has JUST failed (live ES is then switched off for ES_LIVE_RETRY_SEC) the same call carries on with
        the SPX candles and the basis-shifted levels instead of evaluating nothing; and while the ES frame is merely behind the SPX
        candle being evaluated (or the request failed and no SPX fallback exists yet) the evaluation is repeated on the next ticks."""
        found = self.overnight_levels_for_signal()
        if found is None:
            if config.BREAKOUT_LEVEL_SOURCE == "ES_TO_SPX" and market_data.live_es_backoff_active():
                self._chase_es(spx_candles)
            return None
        levels, symbol = found
        if symbol == config.SPX_SYMBOL:
            return levels, symbol, spx_candles
        candles = market_data.get_candles(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK_MIN, now)
        if candles.empty:
            switched = self.overnight_levels_for_signal()
            if switched is not None and switched[1] != symbol:       # the live-ES request failed a moment ago
                return switched[0], switched[1], spx_candles
        if config.BREAKOUT_LEVEL_SOURCE == "ES_TO_SPX" and not spx_candles.empty \
                and (candles.empty or candles.index[-1] < spx_candles.index[-1]):
            self._chase_es(spx_candles)
        return levels, symbol, candles

    def _chase_es(self, spx_candles: pd.DataFrame) -> None:
        if not spx_candles.empty:
            until = spx_candles.index[-1].to_pydatetime() + timedelta(minutes=ES_CHASE_MIN)
            self._es_chase_until = until if self._es_chase_until is None else max(self._es_chase_until, until)

    def entry_possible(self, equity: float, setup: str | None = None,
                       strategies: tuple[str, ...] | None = None) -> bool:
        self.last_entry_blocks = {}
        if self.pm.manual_hold():
            log.info("No new entries: MANUAL_HOLD file present")
            self.last_entry_blocks = {s: "MANUAL_HOLD file present" for s in (strategies or strategy.spx_strategies())}
            return False
        reasons = []
        blocks: dict[str, str] = {}
        exempt_open = self.pm.open_pnl(config.DAILY_LOSS_LIMIT_EXEMPT)
        for strat in (strategies or strategy.spx_strategies()):
            if strat in self.pm.positions:
                reasons.append(f"{strat}: position open")
                blocks[strat] = "position open"
                continue
            allowed, reason = self.risk.trading_allowed(strat, equity, now_et(), setup,
                                                        exempt_open_pnl=exempt_open, open_spx=self.pm.open_spx_count(),
                                                        open_total=self.pm.open_total_count(),
                                                        close_blocked=self.pm.any_close_blocked(),
                                                        halt=self.entry_halt_reason())
            if allowed:
                return True
            reasons.append(reason)
            blocks[strat] = reason
        self.last_entry_blocks = blocks
        log.info("No new entries: %s", "; ".join(reasons))
        return False

    def signal_skipped(self, now: datetime, strat: str, setup: str, direction: str, kind: str | None, reason: str,
                       source: str = "SIGNAL", **context) -> None:
        """Journal one SIGNAL_SKIPPED row: this signal reached `strat` and did not become a trade. `reason` is the
        raw text (a "skip: " / "B: " prefix is dropped); reason_code is the stable category (see skip_code)."""
        text = str(reason)
        for prefix in ("skip: ", f"{strat}: "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        self.journal.event("SIGNAL_SKIPPED", now, strategy=strat, setup=setup, direction=direction, entry_kind=kind,
                           source=source, reason_code=skip_code(text), reason=text, **context)

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
        inputs = self._levels_and_candles(now, spx_candles)
        if inputs is None:
            return
        levels, symbol, candles = inputs
        if self.on_setup is None or self.on_setup_symbol != symbol:
            if self.on_setup is not None:
                log.warning("ON_BREAK level source switched %s -> %s: rebuilding on_setup fresh.",
                           self.on_setup_symbol, symbol, extra={"keep": True})
            self.on_setup = strategy.OrbSetup(levels.high, levels.low)
            self.on_setup_symbol = symbol
            self.last_on_candle = None
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
                               candles=[self.last_on_candle], high=levels.high, low=levels.low,
                               level_symbol=symbol, basis=self.basis)
            self.dispatch_entry(now, "ON_BREAK", fired, kind, self._spx_terms(self.on_setup.break_close, symbol))

    def try_es_break_entry(self, now: datetime, spx_candles: pd.DataFrame) -> None:
        """Strategy C: the same overnight level as ON_BREAK, through strategy.TwoCandleBreak instead
        of OrbSetup -- a deliberately simpler close-only break+confirm (no wick/pullback path)."""
        if "C" not in config.STRATEGIES or "C" in self.pm.positions:
            return
        if not strategy.setup_allowed("ES_BREAK", now.date()):
            return
        inputs = self._levels_and_candles(now, spx_candles)
        if inputs is None:
            return
        levels, symbol, candles = inputs
        if self.es_setup is None or self.es_setup_symbol != symbol:
            if self.es_setup is not None:
                log.warning("ES_BREAK level source switched %s -> %s: rebuilding es_setup fresh.",
                           self.es_setup_symbol, symbol, extra={"keep": True})
            self.es_setup = strategy.TwoCandleBreak(levels.high, levels.low)
            self.es_setup_symbol = symbol
            self.last_es_candle = None
        candles = candles[candles.index >= strategy.at_time(now, config.MARKET_OPEN)]
        if self.last_es_candle is not None:
            candles = candles[candles.index > self.last_es_candle]
        for t, candle in candles.iterrows():
            fired = self.es_setup.update(candle)
            self.last_es_candle = t.to_pydatetime()
            if fired is None:
                continue
            if t != candles.index[-1]:
                log.info("Stale ES_BREAK %s signal on replayed candle %s ignored.", fired, t.strftime("%H:%M"))
                continue
            key = f"ES_BREAK:{self.last_es_candle.isoformat()}"
            if key in self.acted:
                continue
            self.acted.add(key)
            log.info("SIGNAL ES_BREAK %s on %s candle %s vs overnight %.2f/%.2f", fired,
                     config.CANDLE_INTERVAL, t.strftime("%H:%M"), levels.high, levels.low)
            self.journal.event("SIGNAL", now, setup="ES_BREAK", direction=fired,
                               candles=[self.last_es_candle], high=levels.high, low=levels.low,
                               level_symbol=symbol, basis=self.basis)
            self.enter(now, "ES_BREAK", fired, strategies=("C",))

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

    def _spx_terms(self, price: float | None, symbol: str | None) -> float | None:
        """`price` (a level or a break close built on `symbol`'s candles) in SPX points. Only a live-ES setup is in ES terms (about
        55 points above SPX): converting it needs the SPX-ES basis, which is None until Yahoo's delayed ES has the session's first
        bar (about 09:40) -- then None is returned and the caller must not compare the price with an SPX quote."""
        if price is None or symbol != config.ES_SYMBOL:
            return price
        return None if self.basis is None else price + self.basis

    def dispatch_entry(self, now: datetime, setup: str, direction: str, kind: str | None,
                       break_close: float | None) -> None:
        """Route a fired signal to enter() now, except a MOMENTUM signal is split per strategy: any
        strategy with MOMENTUM_CONFIRM_ENABLED waits for 1-minute follow-through (see PendingMomentum)
        while the rest enter immediately, exactly as before."""
        self.last_continuation_check[setup] = now   # paces from the signal itself, not just from a later check
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

        confirm = tuple(s for s in strategy.spx_strategies() if wants_confirmation(s))
        immediate = tuple(s for s in strategy.spx_strategies() if s not in confirm)
        if confirm and break_close is None:      # a live-ES break level whose SPX equivalent is not known yet
            for s in confirm:
                self.signal_skipped(now, s, setup, direction, kind,
                                    "MOMENTUM_BASIS_UNKNOWN the break level is in ES terms and the SPX-ES basis is not known yet")
            confirm = ()
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
                    for s in p.strategies:
                        self.signal_skipped(now, s, setup, p.direction, "MOMENTUM",
                                            f"MOMENTUM_REVERSED beat {beat:+.2f} < margin {config.MOMENTUM_CONFIRM_MARGIN:g}")
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
                        for s in p.strategies:
                            self.signal_skipped(now, s, setup, p.direction, "MOMENTUM",
                                                "MOMENTUM_ENTRY_WINDOW_CLOSED confirmed after the entry window")
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
                for s in p.strategies:
                    self.signal_skipped(now, s, setup, p.direction, "MOMENTUM",
                                        f"MOMENTUM_TIMEOUT no confirmation within {config.MOMENTUM_CONFIRM_TIMEOUT_MIN} min")
                del self.pending_momentum[setup]

    def check_continuation_reentry(self, now: datetime, ph: Phase) -> None:
        """For a strategy with REENTRY_ON_CONTINUATION_ENABLED (B, by default): once a setup's own
        OrbSetup has gone DONE without resetting (the original breakout thesis is still intact) and
        that strategy is flat, try it again every REENTRY_PAUSE_MIN minutes for as long as spot keeps
        beating the break level by MOMENTUM_CONFIRM_MARGIN in the trade direction. A stalled or
        reversed move (beat below the margin) skips this round without resetting the pause timer."""
        for setup, setup_obj in (("ORB", self.orb_setup), ("ON_BREAK", self.on_setup)):
            if setup_obj is None or setup_obj.state != strategy.OrbSetup.DONE:
                continue
            eligible = tuple(s for s in strategy.spx_strategies()
                             if strategy.exit_setting(s, "REENTRY_ON_CONTINUATION_ENABLED", False)
                             and setup in config.SETUPS_BY_STRATEGY.get(s, (setup,))
                             and s not in self.pm.positions)
            if not eligible:
                continue
            last = self.last_continuation_check.get(setup)
            if last is not None and now - last < timedelta(minutes=config.REENTRY_PAUSE_MIN):
                continue
            self.last_continuation_check[setup] = now
            # 2026-10-08: an ON_BREAK setup built on live ES keeps its break close in ES points (~55 above SPX); it is compared
            # with the SPX spot here, so it is converted first -- uncorrected, a bearish break "continued" at any price and a
            # bullish one never did. No basis yet: skip this round (the pause timer already ran).
            break_close = self._spx_terms(setup_obj.break_close, self.on_setup_symbol if setup == "ON_BREAK" else None)
            if break_close is None:
                log.debug("[%s] continuation check skipped: break level is in ES terms and the SPX-ES basis is not known yet", setup)
                continue
            spot = self.broker.get_spot(config.UNDERLYING)
            sign = 1.0 if setup_obj.direction == strategy.BULLISH else -1.0
            beat = sign * (spot - break_close)
            if beat < config.MOMENTUM_CONFIRM_MARGIN:
                log.debug("[%s] continuation check: spot %.2f vs break %.2f (%+.2f) -- move has stalled, no re-entry",
                          setup, spot, break_close, beat)
                continue
            log.info("[%s] CONTINUATION re-entry for %s (%s): spot %.2f beats %.2f by %+.2f",
                     setup, setup_obj.direction, "/".join(eligible), spot, break_close, beat)
            self.journal.event("CONTINUATION_REENTRY", now, setup=setup, direction=setup_obj.direction,
                               break_close=break_close, spot=spot, strategies=list(eligible))
            self.enter(now, setup, setup_obj.direction, "MOMENTUM", strategies=eligible, source="CONTINUATION")

    def check_mean_reversion_entry(self, now: datetime, ph: Phase) -> None:
        """Strategies D (trend-gated MA/Bollinger reversion) and E (D + a 5-min two-candle confirm):
        not setup-driven like ORB/ON_BREAK/ES_BREAK -- checked fresh on every new D_E_CANDLE_INTERVAL
        candle rather than off a fired breakout signal, since a reversion trade's whole premise is the
        ABSENCE of a breakout."""
        eligible = tuple(s for s in ("D", "E") if s in config.STRATEGIES and s not in self.pm.positions)
        if not eligible:
            return
        candles = market_data.get_candles(config.SPX_SYMBOL, config.D_E_CANDLE_INTERVAL,
                                          config.D_E_LOOKBACK_MIN, now)
        if candles.empty:
            return
        if self.last_de_candle is not None and candles.index[-1] <= self.last_de_candle:
            return
        self.last_de_candle = candles.index[-1].to_pydatetime()
        if len(candles) < config.D_E_MIN_CANDLES:
            return
        closes = candles["close"]
        ema9 = strategy.ema(closes, config.D_EMA_FAST_SPAN)
        ema30 = strategy.ema(closes, config.D_EMA_SLOW_SPAN)
        _, upper, lower = strategy.bollinger(closes, config.D_BB_PERIOD, config.D_BB_STD, basis=ema30)
        spot, ma = float(closes.iloc[-1]), float(ema30.iloc[-1])
        pct_b = strategy.percent_b(spot, float(upper.iloc[-1]), float(lower.iloc[-1]))
        trend = strategy.ema_trend(float(ema9.iloc[-1]), ma, config.D_EMA_CHOP_THRESHOLD_PTS)
        direction_d = strategy.mean_reversion_signal(spot, ma, pct_b, trend, config.D_BAND_TOUCH_PCT_B)
        direction_e = direction_d if config.E_USE_TREND_GATE else strategy.mean_reversion_signal(
            spot, ma, pct_b, trend, config.D_BAND_TOUCH_PCT_B, use_trend_gate=False)
        if direction_d is None and direction_e is None:
            return
        # E's 2-candle confirmation is same-session-only: "consecutive" has to mean consecutive
        # trading activity, not the prior day's last candle glued to today's first one (the EMA/
        # Bollinger read above is deliberately the opposite -- a rolling multi-day window).
        today_candles = candles[candles.index >= strategy.at_time(now, config.MARKET_OPEN)]
        for strat in eligible:
            direction = direction_e if strat == "E" else direction_d
            if direction is None:
                continue
            if strat == "E" and not strategy.two_candle_confirm(today_candles, direction, config.E_CONFIRM_CANDLES):
                continue
            setup = "MA_BB_CHOP" if strat == "E" else "MA_BB"
            if not strategy.setup_allowed(setup, now.date()):
                continue
            key = f"{setup}:{self.last_de_candle.isoformat()}"
            if key in self.acted:
                continue
            self.acted.add(key)
            log.info("SIGNAL %s %s trend=%s pct_b=%s ma=%.2f spot=%.2f", setup, direction, trend,
                     f"{pct_b:.2f}" if pct_b is not None else "n/a", ma, spot)
            self.journal.event("SIGNAL", now, setup=setup, direction=direction, trend=trend,
                               pct_b=pct_b, ma=ma, spot=spot)
            self.enter(now, setup, direction, strategies=(strat,))

    def news_blackout_active(self, now: datetime) -> bool:
        """Bot-wide: no new entries (any strategy) within NEWS_BLACKOUT_BEFORE/AFTER_MIN of a time
        listed for today in config.NEWS_EVENTS. Does not pause the underlying setup state machines --
        a signal that fires during the blackout is simply not traded, same as any other rejected entry."""
        events = config.NEWS_EVENTS.get(now.date().isoformat(), [])
        return bool(events) and strategy.in_news_blackout(
            now, events, config.NEWS_BLACKOUT_BEFORE_MIN, config.NEWS_BLACKOUT_AFTER_MIN)

    def enter(self, now: datetime, setup: str, direction: str, kind: str | None = None,
             strategies: tuple[str, ...] | None = None, source: str = "SIGNAL") -> None:
        every = strategies or strategy.spx_strategies()
        if self.news_blackout_active(now):
            log.info("No new entries: NEWS_BLACKOUT active at %s", now.strftime("%H:%M:%S"))
            for strat in every:
                self.signal_skipped(now, strat, setup, direction, kind, "NEWS_BLACKOUT active", source)
            return
        equity = self.broker.get_account()["equity"]
        if not self.entry_possible(equity, setup, strategies):
            for strat in every:
                self.signal_skipped(now, strat, setup, direction, kind,
                                    self.last_entry_blocks.get(strat, "ENTRY_BLOCKED"), source)
            return
        if self.today not in self.broker.get_expirations(config.UNDERLYING, 0, 1):
            log.warning("No SPXW expiration for today %s: no trade.", self.today)
            for strat in every:
                self.signal_skipped(now, strat, setup, direction, kind, "NO_EXPIRATION no SPXW expiration today", source)
            return
        spot = self.broker.get_spot(config.UNDERLYING)
        self._last_good_spot = spot
        width = strategy.width_for_tier(tier(equity))
        right = strategy.direction_to_spread(direction)
        em = self.expected_move(spot)
        day_er, day_er_n = None, 0
        if any(strategy.exit_setting(s, "DAY_GATE_ENABLED", False) for s in (strategies or strategy.spx_strategies())):
            session_candles = market_data.get_candles(config.SPX_SYMBOL, "2m", config.CANDLE_LOOKBACK_MIN, now)
            session_candles = session_candles[session_candles.index >= strategy.at_time(now, config.MARKET_OPEN)]
            day_er, day_er_n = strategy.efficiency_ratio(session_candles)
        short_a, long_a = strategy.select_strikes(spot, right, width)
        lo, hi = min(short_a, long_a), max(short_a, long_a)
        # A may walk up to A_MAX_STRIKE_WALK strikes toward spot.
        lo, hi = lo - 5 * config.A_MAX_STRIKE_WALK, hi + 5 * config.A_MAX_STRIKE_WALK
        if em is not None:
            # One chain wide enough for A and for B's whole strike walk (spot +/- 2 EM + width).
            lo, hi = min(lo, spot - 2 * em - width), max(hi, spot + 2 * em + width)
        chain = self.broker.get_option_chain(config.UNDERLYING, self.today, right, lo - 1, hi + 1, spot=spot)
        strategies = strategies or strategy.spx_strategies()
        summaries: dict[str, str] = {}
        open_risk = self.pm.total_open_risk()
        for strat in strategies:
            try:
                summaries[strat], added = self.enter_leg(strat, now, setup, direction, equity, spot, width,
                                                         right, chain, em, open_risk, kind, day_er, day_er_n)
            except BrokerError as e:
                log.error("[%s] broker error during entry: %s", strat, e)
                self.journal.event("BROKER_ERROR", now, strategy=strat, error=str(e))
                summaries[strat], added = f"broker error {e}", 0.0
            if added <= 0:    # skipped, rejected, sized to zero, unfilled or errored: no trade came of this signal
                self.signal_skipped(now, strat, setup, direction, kind, summaries[strat], source, spot=spot, em=em,
                                    day_er=day_er, width=width, attempted=summaries[strat].startswith("not filled"))
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
                  width: int, right: str, chain: list[dict], em: float | None, open_risk: float,
                  kind: str | None = None, day_er: float | None = None, day_er_n: int = 0) -> tuple[str, float]:
        """Place one strategy's spread. Returns (operator summary, risk dollars added)."""
        if strat in self.pm.positions:
            return "skip: position open", 0.0
        if setup not in config.SETUPS_BY_STRATEGY.get(strat, (setup,)):
            return f"skip: {setup} setup disabled for {strat}", 0.0
        # Any kind-tagged setup (ORB or ON_BREAK -- OVERNIGHT never sets a kind) is gated the same way.
        if kind is not None and kind not in config.ORB_ENTRY_KINDS_BY_STRATEGY.get(strat, (kind,)):
            return f"skip: {setup} {kind} entry disabled for {strat}", 0.0
        if strategy.exit_setting(strat, "DAY_GATE_ENABLED", False):
            min_candles = strategy.exit_setting(strat, "DAY_GATE_MIN_CANDLES", 5)
            if day_er_n < min_candles:
                return f"skip: DAY_GATE not enough session data yet ({day_er_n} candle(s))", 0.0
            min_er = strategy.exit_setting(strat, "DAY_GATE_MIN_ER", 0.15)
            if day_er is None or day_er < min_er:
                return f"skip: DAY_GATE ER={0.0 if day_er is None else day_er:.2f} < {min_er:.2f} (choppy)", 0.0
        allowed, reason = self.risk.trading_allowed(strat, equity, now, setup,
                                                    exempt_open_pnl=self.pm.open_pnl(config.DAILY_LOSS_LIMIT_EXEMPT),
                                                    open_spx=self.pm.open_spx_count(), open_total=self.pm.open_total_count(),
                                                    close_blocked=self.pm.any_close_blocked(),
                                                    halt=self.entry_halt_reason())
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
        # 2026-09-25: B, C, D, E all share select_strikes_b's OTM-at-expected-move logic, so two of
        # them can land on the IDENTICAL contract on the same day -- Alpaca tracks positions by
        # contract symbol, not by our per-strategy label, so two strategies both trying to manage
        # "their" slice of what the broker sees as one combined position breaks its position_intent
        # inference on close ("inferred: buy_to_open, specified: buy_to_close"), leaving the position
        # stuck open after CLOSE_MAX_RETRIES. Refuse the second entry outright rather than risk that.
        # 2026-10-08: any SHARED strike counts, not only the identical pair (see PositionManager.find_collision)
        collision = self.pm.find_collision(config.OPTION_ROOT, self.today, right, short_strike, long_strike)
        if collision:
            reason = self.pm.collision_reason(collision, right, short_strike, long_strike)
            log.info("[%s] ENTRY REJECTED %s %s: %s", strat, direction, right, reason)
            self.journal.event("ENTRY_REJECTED", now, strategy=strat, setup=setup, direction=direction,
                               right=right, short=short_strike, long=long_strike, reason=reason, quote=quote)
            return f"skip: {reason}{detail}", 0.0
        qty = contracts_for(equity, width, quote.mid, open_risk, strategy.is_news_day(self.today),
                           half_size=(strat in ("D", "E")))
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
        log.info("\n%s", card, extra={"keep": True})
        self.journal.card(card, now)
        return f"sell {short_strike:g}{right} buy {long_strike:g}{right} x{filled_qty} @ {credit:.2f}{detail}", spread.open_risk

    def work_entry(self, order: dict, start_limit: float, floor_limit: float, step: float | None = None,
                   wait_sec: float | None = None, timeout_sec: float | None = None) -> dict:
        """Work a resting credit order down toward floor_limit (see _work_entry); remembers when the loop was released."""
        try:
            return self._work_entry(order, start_limit, floor_limit, step, wait_sec, timeout_sec)
        finally:
            self._entry_wait_end = now_et()

    def _work_entry(self, order: dict, start_limit: float, floor_limit: float, step: float | None = None,
                    wait_sec: float | None = None, timeout_sec: float | None = None) -> dict:
        """Work a resting credit order down toward floor_limit. The SPX strategies use the config
        defaults; F passes its own width-scaled step and a shorter wait/timeout."""
        if order.get("status") == "dry_run":
            return order
        step = config.ENTRY_PRICE_STEP if step is None else step
        wait_sec = config.ENTRY_STEP_SEC if wait_sec is None else wait_sec
        limit = start_limit
        deadline = time.monotonic() + (config.ENTRY_TIMEOUT_SEC if timeout_sec is None else timeout_sec)
        while True:
            order = self.broker.wait_for_fill(order["id"], wait_sec)
            if order["status"] in TERMINAL:
                return order
            next_limit = round(limit - step, 2)
            if time.monotonic() >= deadline or next_limit < floor_limit - 1e-9:
                break
            log.info("Entry not filled at %.2f, stepping to %.2f", limit, next_limit)
            order = self.broker.replace_order_price(order["id"], next_limit)
            limit = next_limit
        log.info("Entry timeout: cancelling order %s", order["id"])
        try:
            self.broker.cancel_order(order["id"])
        except BrokerError as e:      # 2026-10-15: a cancel can fail because the order FILLED a moment ago; that is never 'not filled'
            log.warning("Entry cancel of %s failed (%s): re-reading the order in case it filled", order["id"], e)
        final = self.broker.get_order(order["id"]) or order
        if final.get("status") not in TERMINAL:                 # pending-cancel and the like: let it settle so a late fill is seen
            final = self.broker.wait_for_fill(order["id"], 5.0, 1.0)
        return final

    # ---------------------------------------------------------------- manage

    def spread_prices(self, force: bool = False) -> dict[str, float]:
        """Current mid per open position key; positions sharing root/expiration/right share one chain call.
        A stock (F) root is marked at most every F_PRICE_EVERY_SEC -- `force` bypasses that for a close --
        and a failed stock chain is journaled and skipped so it never blocks the SPX marks."""
        groups: dict[tuple, list[tuple[str, OpenSpread]]] = {}
        for key, p in self.pm.positions.items():
            groups.setdefault((p.root, p.expiration, p.right), []).append((key, p))
        prices: dict[str, float] = {}
        mono = time.monotonic()
        for (root, expiration, right), group in groups.items():
            is_stock = root != config.OPTION_ROOT
            if is_stock:
                if not force and mono - self._f_last_priced.get(root, 0.0) < config.F_PRICE_EVERY_SEC:
                    continue
                self._f_last_priced[root] = mono   # stamped before the call: a failing stock chain is throttled too
            strikes = [s for _, p in group for s in (p.short_strike, p.long_strike)]
            try:
                chain = self.broker.get_option_chain(root if is_stock else config.UNDERLYING, expiration, right,
                                                     min(strikes) - 1, max(strikes) + 1)
            except Exception as e:
                if not is_stock:
                    raise   # the SPX chain keeps today's semantics: a BrokerError reaches loop(), anything else propagates
                log.error("[%s] chain unavailable for marks (%s): %s", group[0][0], type(e).__name__, e)
                self.journal.event("BROKER_ERROR", now_et(), strategy=group[0][1].strategy, ticker=root,
                                   error=str(e), type=type(e).__name__)
                continue
            for key, p in group:
                quote = strategy.spread_quote(chain, p.short_strike, p.long_strike)
                if quote is None:
                    log.warning("[%s] No chain quote for %s %s/%s; skipping manage this tick.",
                                key, p.right, p.short_strike, p.long_strike)
                else:
                    prices[key] = quote.mid
        return prices

    def manage(self, now: datetime, candles: pd.DataFrame) -> None:
        # 2026-10-02: reconcile with what the broker actually holds first, so a spread closed (or a
        # contract cut) by hand in the broker UI is booked and dropped here instead of becoming a ghost
        # the exit rules keep trying to close. Throttled and confirmed inside the position manager.
        external = self.pm.sync_external(now)
        if external:
            self.record_fills(external)
            if not self.pm.positions:
                return
        prices = self.spread_prices()
        for key, p in self.pm.positions.items():
            if key in prices:
                log.info("[%s] Position %s%s/%s x%d entry %.2f now %.2f best %.2f%s", key, p.short_strike,
                         p.right, p.long_strike, p.remaining, p.entry_credit, prices[key], p.best_price,
                         f" RUNNER best {p.runner_best:.2f}" if p.runner else "")
        candles_by_key = self.f.candles_by_key() if self.f is not None else None
        self.record_fills(self.pm.on_tick(now, prices, candles, candles_by_key))

    def record_fills(self, fills: list[dict]) -> None:
        for row in fills:
            self.journal.trade(row)
            strat = row["strategy"]
            s = self.risk.state
            trade_no = s.for_strategy(strat).trades_today + 1
            if row["position_closed"]:
                self.risk.record_trade(strat, row["position_pnl"], row)
                remaining = 0
                if row.get("external") and row.get("setup") in ("ORB", "ON_BREAK"):
                    # closed by hand: let the continuation re-entry check fire again right when the manual-close
                    # cool-down ends (not up to REENTRY_PAUSE_MIN later)
                    self.last_continuation_check[row["setup"]] = (
                        row["exit_time"] + timedelta(seconds=config.MANUAL_CLOSE_COOLDOWN_SEC)
                        - timedelta(minutes=config.REENTRY_PAUSE_MIN))
                day_pnl = s.pnl_by_strategy()
                if self.f is not None and strat in config.STOCK_STRATEGIES:
                    self.f.on_exit(row["root"], row["exit_time"])
            else:
                still_open = self.pm.positions.get(row["key"])
                remaining = still_open.remaining if still_open is not None else 0
                day_pnl = s.pnl_by_strategy()
                day_pnl[strat] = round(day_pnl.get(strat, 0.0) + row["position_pnl"], 2)
            card = format_close_card(row, trade_no, day_pnl, s.trades_today, remaining)
            log.info("\n%s", card, extra={"keep": True})
            self.journal.card(card, now_et())
        if fills:
            self.save_state()

    # ------------------------------------------------------------ end of day

    def order_is_mine(self, order: dict, stock_roots: set[str] | None = None) -> bool:
        """An open order on contracts this bot trades: SPXW expiring today, or a stock option inside F's DTE window (on a root
        in `stock_roots`; None = any root while F is configured). Orders on anything else (the 45DTE bot's resting GTC closes,
        the owner's own orders) are not this bot's. An order whose legs cannot be read is not mine."""
        legs = order.get("legs") or []
        if not legs:
            return False
        for leg in legs:
            root, exp = occ_root(leg["symbol"]), occ_expiration(leg["symbol"])
            if root == config.OPTION_ROOT:
                ok = exp == self.today
            else:
                roots_ok = ("F" in config.STRATEGIES) if stock_roots is None else root in stock_roots
                ok = roots_ok and exp is not None and self.f_owns_expiration(exp)
            if not ok:
                return False
        return True

    def cancel_own_orders(self) -> int:
        """Cancel the open orders on contracts this bot trades (SPXW expiring today, and F's stock options inside
        its DTE window on roots it holds) -- not every order in the account: a Schwab account is shared with the
        45DTE bot's resting GTC closes and with whatever the owner has working by hand in the broker UI. An
        order whose legs we cannot read is left alone."""
        stock_roots = {p.root for p in self.pm.positions.values()}
        cancelled = 0
        for order in self.broker.get_open_orders():
            if not self.order_is_mine(order, stock_roots):
                continue
            try:
                self.broker.cancel_order(order["id"])
                cancelled += 1
            except BrokerError as e:
                log.error("Could not cancel order %s: %s", order["id"], e)
        return cancelled

    def _spot_or_last(self) -> float:
        """SPX spot for adoption at the hard stop: live if available, else the last good one (a data outage must not stop
        the flatten)."""
        try:
            self._last_good_spot = self.broker.get_spot(config.UNDERLYING)
        except Exception as e:      # noqa: BLE001
            log.error("spot unavailable at the hard stop (%s: %s); using the last good one", type(e).__name__, e)
        return self._last_good_spot or 0.0

    def _prices_or_none(self) -> dict:
        """Chain prices for the hard-stop close, or {} when the data feed is down: force_close then closes at each position's
        last known price instead of aborting."""
        try:
            return self.spread_prices(force=True)
        except Exception as e:      # noqa: BLE001
            log.error("chain prices unavailable at the hard stop (%s: %s); closing at the last known prices",
                      type(e).__name__, e, extra={"keep": True})
            return {}

    def end_of_day(self, now: datetime) -> bool:
        """The hard stop (FORCE_CLOSE_TIME): cancel orders, close everything, report, and the bot is done -- a
        MANUAL_HOLD file does not delay it unless config.MANUAL_HOLD_DELAYS_FORCE_CLOSE. In that older mode it returns
        False (nothing done) while the file is present and positions are open before the session ends: the bot stays
        up and closes them the moment the file goes."""
        if self._eod_retry_at is not None and now < self._eod_retry_at:
            return False        # a close is still failing: the bot stays up, and waits out the backoff between passes
        hold = self.pm.manual_hold()
        if hold and config.MANUAL_HOLD_DELAYS_FORCE_CLOSE and self.pm.positions and now.time() < config.MARKET_CLOSE:
            if self._hold_alert_at is None or (now - self._hold_alert_at).total_seconds() >= 60:
                self._hold_alert_at = now
                log.critical("MANUAL_HOLD file present at the %s force-close: NOT closing %d position(s). The bot stays "
                             "up and closes them as soon as the file is deleted (until %s).",
                             config.FORCE_CLOSE_TIME.strftime("%H:%M"), len(self.pm.positions),
                             config.MARKET_CLOSE.strftime("%H:%M"), extra={"keep": True})
            return False
        override = hold and not config.MANUAL_HOLD_DELAYS_FORCE_CLOSE
        if override and self.pm.positions:
            log.critical("MANUAL_HOLD file present at the %s hard stop: closing %d position(s) anyway "
                         "(MANUAL_HOLD_DELAYS_FORCE_CLOSE is off).", config.FORCE_CLOSE_TIME.strftime("%H:%M"),
                         len(self.pm.positions), extra={"keep": True})
        if self.f is not None:
            self.f.shutdown()
        log.warning("FORCE CLOSE at %s: cancelling orders and closing everything.", now.strftime("%H:%M:%S"))
        cancelled = self.cancel_own_orders()
        log.info("Cancelled %d open orders.", cancelled)
        for attempt in range(1, config.CLOSE_MAX_RETRIES + 1):
            legs = self.bot_legs(self.broker.get_positions())
            if legs:
                self.pm.adopt_from_broker(legs, now, self._spot_or_last())
            if self.pm.positions:
                self.record_fills(self.pm.force_close(now, self._prices_or_none(), override_hold=override))
            legs = self.bot_legs(self.broker.get_positions())
            if not legs and not self.pm.positions:
                break
            log.critical("CLOSE_FAILED: option legs still at broker after attempt %d: %s",
                         attempt, [(l["symbol"], l["qty"]) for l in legs])
            time.sleep(config.CLOSE_RETRY_SEC)
        stuck = self.bot_legs(self.broker.get_positions()) or self.pm.positions
        if stuck and now.time() < config.MARKET_CLOSE:
            # 2026-10-08: exiting here left the spread open to expiry (max loss). Stay up and try again until it is flat
            # or the session ends; every pass is journaled and CRITICAL-logged.
            self._eod_retry_at = now + timedelta(seconds=config.EOD_CLOSE_RETRY_SEC)
            log.critical("HARD STOP: %d position(s) still open after %d passes -- MANUAL ACTION REQUIRED. The bot stays up "
                         "and retries every %ds until flat or %s.", len(self.pm.positions), config.CLOSE_MAX_RETRIES,
                         config.EOD_CLOSE_RETRY_SEC, config.MARKET_CLOSE.strftime("%H:%M"), extra={"keep": True})
            self.journal.event("EOD_CLOSE_STILL_OPEN", now, positions=sorted(self.pm.positions),
                               retry_at=self._eod_retry_at)
            self.save_state()
            return False
        self._eod_retry_at = None
        self.report_end(now_et())
        self.save_state()
        return True

    # --------------------------------------------------------------- reports

    def _token_report_lines(self, now: datetime) -> list[str]:
        status_fn = getattr(self.broker, "token_status", None)
        if status_fn is None:
            return []
        try:
            st = status_fn(now)
        except Exception as e:      # noqa: BLE001
            return [f"Schwab token: status unavailable ({e})"]
        return [f"Schwab token: {st['state']} -- {st['message']}"]

    def _broker_report_lines(self) -> list[str]:
        """Extra start-of-day lines the broker wants shown (the Schwab broker: account, capital cap, contract cap, live or dry-run)."""
        try:
            return list(getattr(self.broker, "account_report_lines", lambda: [])())
        except Exception as e:      # noqa: BLE001 -- a report line must never stop the bot from starting
            return [f"Broker notes unavailable ({type(e).__name__})"]

    @staticmethod
    def _live_es_report_line() -> str:
        try:
            usable, reason = market_data.live_es_status()
        except Exception as e:      # noqa: BLE001 -- a report line must never stop the bot from starting
            return f"Live ES (Schwab /ES): status unavailable ({type(e).__name__})"
        if usable:
            return f"Live ES (Schwab /ES): ON -- {reason}; breakouts are confirmed on ES candles"
        return (f"Live ES (Schwab /ES): OFF -- {reason}; breakouts are confirmed on SPX candles against ES levels shifted by "
                f"the SPX-ES basis (Yahoo ES=F, ~10 min delayed)")

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
            *self._broker_report_lines(),
            *self._token_report_lines(now),
            f"P/L  YTD {fmt_money(pnl['ytd'])}  MTD {fmt_money(pnl['mtd'])}  Today {fmt_money(pnl['today'])}",
            f"Open positions: {len(positions)}" + "".join(
                f"\n   {p['symbol']} qty {p['qty']} avg {p['avg_price']}" for p in positions),
            f"Open orders: {len(orders)}" + "".join(
                f"\n   {o['id']} {o['status']} {o['symbol']} qty {o['qty']} @ {o['limit_price']}" for o in orders),
            f"Tier {t}  width {width}  credit range A {lo:.2f}-{hi:.2f}  B {lo_b:.2f}-{hi_b:.2f}",
            *(self.f.report_lines() if self.f is not None else []),
            f"Phase: {strategy.phase(now)}   News day: {strategy.is_news_day(now.date())} "
            f"(mode {config.NEWS_DAY_MODE})   Level source: {config.BREAKOUT_LEVEL_SOURCE}",
            self._live_es_report_line(),
            f"Day state: trades {day.trades_today} realized {fmt_money(day.realized_pnl)} " + " ".join(
                f"| {k}: trades {s.trades_today} losses-in-a-row {s.consecutive_losses} P/L {fmt_money(s.realized_pnl)}"
                for k, s in day.strategies.items()),
            "=" * 72,
        ]
        for line in lines:
            log.info(line, extra={"keep": True})
        self.journal.event("START", now, account=acct, pnl=pnl, positions=positions, orders=orders, tier=t)

    def report_end(self, now: datetime) -> None:
        acct = self.broker.get_account()
        positions = self.broker.get_positions()
        orders = [o for o in self.broker.get_open_orders() if self.order_is_mine(o)]   # not the 45DTE bot's GTC closes
        legs = self.bot_legs(positions)
        foreign = self.foreign_legs(positions)
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
            mismatches.append(f"broker still holds option legs: {[(l['symbol'], l['qty']) for l in legs]}")
        for strat, p in self.pm.positions.items():
            mismatches.append(f"local state still has a {strat} position: {p.to_dict()}")
        if orders:
            mismatches.append(f"open orders remain: {[o['id'] for o in orders]}")
        lines.append("Reconciliation: " + ("OK - flat, no orders" if not mismatches else "MISMATCH"))
        lines.extend(f"   !! {m}" for m in mismatches)
        if foreign:
            lines.append(f"Ignored option legs not owned by this persona: {[(l['symbol'], l['qty']) for l in foreign]}")
        lines.append("=" * 72)
        for line in lines:
            (log.critical if mismatches else log.info)(line, extra={"keep": True})
        log.info("\n%s", table, extra={"keep": True})
        self.journal.event("END", now, account=acct, day=s.to_dict(), mismatches=mismatches)

    # ----------------------------------------------------------------- state

    def save_state(self) -> None:
        state = {
            "date": self.today.isoformat(),
            "day": self.risk.state.to_dict(),
            "positions": {key: p.to_dict() for key, p in self.pm.positions.items()},
            "acted": sorted(self.acted),
            "levels": {"overnight": self.overnight, "basis": self.basis, "orb": self.orb},
            # 2026-10-01: the breakout state machines themselves (plus the last-processed-candle
            # pointers that keep a restart from re-replaying the whole day through them) were never
            # persisted here -- only "levels" above was, and even that was never restored in start().
            # A restart wiped orb_setup/on_setup/es_setup back to fresh WAITING state, silently killing
            # that setup's entries (including CONTINUATION_REENTRY) for the rest of the day unless a
            # brand-new breakout happened to form after the restart. Confirmed live on 2026-10-01: all 9
            # personas restarted at the same 4 timestamps that morning, and only one of them kept firing
            # continuation re-entries afterward -- the other 8 went silent on that setup for the rest of
            # the day. Persisting and restoring the real state machine (not just its source levels) and
            # the last-processed-candle pointers turns a restart into a true continuation instead of a
            # from-scratch replay.
            "breakouts": {
                "orb_setup": self.orb_setup.to_dict() if self.orb_setup is not None else None,
                "on_setup": self.on_setup.to_dict() if self.on_setup is not None else None,
                "es_setup": self.es_setup.to_dict() if self.es_setup is not None else None,
                # which overnight-level source (ES vs SPX-terms) on_setup/es_setup were each built
                # against, so a restore can detect overnight_levels_for_signal() having since resolved
                # to a different source and rebuild fresh rather than feed it the wrong price scale.
                "on_setup_symbol": self.on_setup_symbol, "es_setup_symbol": self.es_setup_symbol,
                "last_orb_candle": self.last_orb_candle, "last_on_candle": self.last_on_candle,
                "last_es_candle": self.last_es_candle, "last_de_candle": self.last_de_candle,
                "last_candle": self.last_candle,
                "last_continuation_check": dict(self.last_continuation_check),
                "news_blackout_was_active": self.news_blackout_was_active,
            },
        }
        if self.f is not None:
            state["f"] = self.f.to_state()
        self.journal.save_state(state)

    def on_interrupt(self) -> None:
        keep = {"extra": {"keep": True}}
        if self.f is not None:
            self.f.shutdown()
        if self.risk is not None:
            self.save_state()
        if not self.pm.positions:
            log.warning("Interrupted by user. No open position.", **keep)
            return
        if config.CLOSE_ON_INTERRUPT and self.risk is not None:
            log.warning("Interrupted by user with %d open position(s): closing them now.",
                       len(self.pm.positions), **keep)
            try:
                self.record_fills(self.pm.force_close(now_et(), self.spread_prices(force=True), reason="INTERRUPT_CLOSE"))
            except BrokerError as e:
                log.error("Close on interrupt failed: %s", e)
            self.save_state()
            if not self.pm.positions:
                log.warning("All positions closed; flat.", **keep)
                return
        log.warning("Nothing (more) was closed automatically.", **keep)
        for key, p in self.pm.positions.items():
            log.warning("OPEN POSITION [%s]: %s sell %s%s / buy %s%s x%d, entry credit %.2f, last %.2f, exp %s",
                        key, p.direction, p.short_strike, p.right, p.long_strike, p.right, p.remaining,
                        p.entry_credit, p.current_price, p.expiration, **keep)
        log.warning("To close: restart `python scheduler.py` (it re-adopts and manages the spreads), "
                    "or run `python alpaca-reset.py` to liquidate ALL positions, "
                    "or close the SPXW / stock option legs in the Alpaca dashboard.", **keep)


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
    if getattr(config, "CLOCK_CHECK_ENABLED", True):
        try:
            verdict = clock_check.check(max_skew=float(getattr(config, "CLOCK_MAX_SKEW_SEC", 120)))
        except Exception as exc:        # noqa: BLE001 -- a bug in a safety CHECK must never keep the bot from starting
            verdict = {"ok": None, "skew": None, "sources": 0, "detail": f"the clock check itself failed ({type(exc).__name__}: {exc})"}
        if verdict["ok"] is False:
            log.critical("THIS PC'S CLOCK IS WRONG: %s. The PC says %s but it is about %s. The bot schedules the market open, the entry window, "
                         "the hard stop and the Schwab login age from this clock, so it will NOT start. Fix: Settings > Time & language > "
                         "Date & time > turn on 'Set time automatically' and the Eastern time zone, click 'Sync now' (admin PowerShell: "
                         "w32tm /resync), then start the bot again. (CLOCK_CHECK_ENABLED = False in config.py turns this check off.)",
                         verdict["detail"], now_et().strftime("%a %Y-%m-%d %H:%M %Z"),
                         datetime.fromtimestamp(time.time() - verdict["skew"], config.ET).strftime("%a %Y-%m-%d %H:%M %Z"),
                         extra={"keep": True})
            sys.exit(3)
        if verdict["ok"] is None:
            log.warning("Clock check skipped: %s. Make sure this PC's date and time are right.", verdict["detail"], extra={"keep": True})
        else:
            log.info("Clock check: %s (limit %ds): fine.", verdict["detail"], int(getattr(config, "CLOCK_MAX_SKEW_SEC", 120)),
                     extra={"keep": True})
    if getattr(config, "SINGLE_INSTANCE_LOCK", True):
        ok, holder = instance_lock.acquire(LOG_DIR, f"0dte_{config.TRADER_NAME.lower()}")
        if not ok:
            log.critical("%s is already running (%s): refusing to start a second copy against the same account. Close the other "
                         "window first (or set SINGLE_INSTANCE_LOCK = False in config.py).", config.TRADER_NAME, holder,
                         extra={"keep": True})
            sys.exit(2)
        if holder:
            log.warning("Single-instance lock NOT enforced: %s", holder, extra={"keep": True})
    bot = Bot(dry_run=args.dry_run or config.DRY_RUN, once=args.once)
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.on_interrupt()
    except BrokerError as exc:
        log.error("BROKER ERROR: %s", exc)
        if getattr(exc, "reauth_command", None):
            log.error("The Schwab token is missing, expired or rejected. Re-login with:  %s", exc.reauth_command)
        elif "401" in str(exc) or "unauthorized" in str(exc).lower():
            log.error("The broker rejected the API keys. Open the .env file in this folder and check the "
                      "key/secret values (no quotes, no spaces). If you rotated keys, paste the new ones.")
        else:
            log.error("Check the .env file in this folder and your internet connection, then start again.")
        sys.exit(1)


if __name__ == "__main__":
    main()

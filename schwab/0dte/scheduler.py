"""0DTE SPX credit-spread bot main loop.

    python scheduler.py [--dry-run] [--once]

All clock logic is US/Eastern wall time; phases derive from minutes since 09:30
so a late start lands in the right phase immediately.
"""

import argparse
import logging
import sys
import time
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
        self.last_candle: datetime | None = None
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
        self.pm.adopt_from_broker(positions, now, saved_today.get("position") if saved_today else None)
        self.report_start(now, acct, positions)
        self.broker.record_daily_equity()
        self.save_state()
        self.run(now)

    def run(self, now: datetime) -> None:
        if not market_data.is_trading_day(now) and self.pm.position is None:
            log.info("Weekend (%s): nothing to do, exiting.", now.strftime("%A"))
            return
        ph = strategy.phase(now)
        if ph == Phase.CLOSED:
            if self.pm.position is None:
                log.info("Started after FORCE_CLOSE_TIME %s with no open position: exiting.",
                         config.FORCE_CLOSE_TIME)
                return
            log.warning("Started after FORCE_CLOSE_TIME with an open position: closing it now.")
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
        if self.pm.position is not None:
            self.manage(now, candles)
        if new_candle and self.pm.position is None and ph in strategy.ENTRY_PHASES:
            self.try_entry(now, ph, candles)
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

    @staticmethod
    def log_levels(label: str, levels: strategy.Levels | None) -> None:
        if levels is not None:
            log.info("%s high %.2f low %.2f (established %s)", label, levels.high, levels.low,
                     levels.established_at.strftime("%H:%M"))

    def breakout_candidates(self, ph: Phase, now: datetime) -> list[tuple[str, strategy.Levels, str]]:
        out = []
        if ph in strategy.OVERNIGHT_PHASES and self.overnight is not None:
            if config.BREAKOUT_LEVEL_SOURCE == "ES":
                out.append(("OVERNIGHT", self.overnight, config.ES_SYMBOL))
            elif self.basis is not None:
                out.append(("OVERNIGHT", self.overnight.shifted(self.basis), config.SPX_SYMBOL))
        if ph in strategy.ORB_PHASES and self.orb is not None:
            out.append(("ORB", self.orb, config.SPX_SYMBOL))
        return [c for c in out if strategy.setup_allowed(c[0], now.date())]

    # ----------------------------------------------------------------- entry

    def try_entry(self, now: datetime, ph: Phase, spx_candles: pd.DataFrame) -> None:
        equity = self.broker.get_account()["equity"]
        allowed, reason = self.risk.trading_allowed(equity)
        if not allowed:
            log.info("No new entries: %s", reason)
            return
        for setup, levels, symbol in self.breakout_candidates(ph, now):
            candles = spx_candles if symbol == config.SPX_SYMBOL else \
                market_data.get_candles(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK_MIN, now)
            bo = strategy.detect_breakout(candles, levels.high, levels.low, levels.established_at)
            if bo is None:
                continue
            key = f"{setup}:{bo.candle_times[1].isoformat()}"
            if key in self.acted:
                continue
            self.acted.add(key)
            log.info("SIGNAL %s %s on candles %s/%s vs %.2f/%.2f", setup, bo.direction,
                     bo.candle_times[0].strftime("%H:%M"), bo.candle_times[1].strftime("%H:%M"),
                     levels.high, levels.low)
            self.journal.event("SIGNAL", now, setup=setup, direction=bo.direction,
                               candles=list(bo.candle_times), high=levels.high, low=levels.low)
            self.enter(now, setup, bo.direction, equity)
            return

    def enter(self, now: datetime, setup: str, direction: str, equity: float) -> None:
        if self.today not in self.broker.get_expirations(config.UNDERLYING, 0, 1):
            log.warning("No SPXW expiration for today %s: no trade.", self.today)
            return
        spot = self.broker.get_spot(config.UNDERLYING)
        t = tier(equity)
        width = strategy.width_for_tier(t)
        right = strategy.direction_to_spread(direction)
        short_strike, long_strike = strategy.select_strikes(spot, right, width)
        chain = self.broker.get_option_chain(config.UNDERLYING, self.today, right,
                                             min(short_strike, long_strike) - 1,
                                             max(short_strike, long_strike) + 1, spot=spot)
        quote, reject = strategy.entry_credit(chain, short_strike, long_strike, width)
        if reject:
            log.info("ENTRY REJECTED %s %s %s/%s: %s", direction, right, short_strike, long_strike, reject)
            self.journal.event("ENTRY_REJECTED", now, setup=setup, direction=direction, right=right,
                               short=short_strike, long=long_strike, reason=reject, quote=quote)
            return
        qty = contracts_for(equity, width, quote.mid, 0.0, strategy.is_news_day(self.today))
        if qty == 0:
            log.info("ENTRY REJECTED: sizing returned 0 contracts (equity %.2f width %d credit %.2f)",
                     equity, width, quote.mid)
            return
        lo, _ = strategy.credit_range(width)
        log.warning("ENTRY %s %s spot %.2f tier %d: sell %s%s buy %s%s x%d @ %.2f (bid-side %.2f, floor %.2f)",
                    setup, direction, spot, t, short_strike, right, long_strike, right, qty, quote.mid,
                    quote.bid_side, lo)
        order = self.broker.place_credit_spread(
            config.UNDERLYING, self.today, right, short_strike, long_strike, qty, quote.mid,
            time_in_force="day", root=config.OPTION_ROOT,
            client_tag=f"0dte-{config.TRADER_NAME.lower()}-{setup.lower()}")
        order = self.work_entry(order, quote.mid, lo)
        filled_qty = qty if order.get("status") == "dry_run" else int(order.get("filled_qty") or 0)
        if filled_qty == 0:
            log.info("ENTRY NOT FILLED (%s): order %s", order.get("status"), order.get("id"))
            self.journal.event("ENTRY_UNFILLED", now, order_id=order.get("id"), status=order.get("status"))
            return
        credit = float(order.get("filled_avg_price") or order.get("limit_price") or quote.mid)
        self.pm.open(OpenSpread(
            direction=direction, setup=setup, right=right, root=config.OPTION_ROOT,
            expiration=self.today, short_strike=short_strike, long_strike=long_strike, width=width,
            qty=filled_qty, entry_credit=credit, entry_time=now_et(), current_price=credit,
            best_price=credit))
        card = format_entry_card(self.pm.position, self.risk.state.trades_today + 1)
        log.info("\n%s", card)
        self.journal.card(card, now)
        self.save_state()

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

    def spread_mid(self, p: OpenSpread) -> float | None:
        chain = self.broker.get_option_chain(config.UNDERLYING, p.expiration, p.right,
                                             min(p.short_strike, p.long_strike) - 1,
                                             max(p.short_strike, p.long_strike) + 1)
        quote = strategy.spread_quote(chain, p.short_strike, p.long_strike)
        return None if quote is None else quote.mid

    def manage(self, now: datetime, candles: pd.DataFrame) -> None:
        p = self.pm.position
        mid = self.spread_mid(p)
        if mid is None:
            log.warning("No chain quote for %s %s/%s; skipping manage this tick.", p.right, p.short_strike, p.long_strike)
            return
        log.info("Position %s%s/%s x%d entry %.2f now %.2f best %.2f%s", p.short_strike, p.right,
                 p.long_strike, p.remaining, p.entry_credit, mid, p.best_price,
                 f" RUNNER best {p.runner_best:.2f}" if p.runner else "")
        self.record_fills(self.pm.on_tick(now, mid, candles))

    def record_fills(self, fills: list[dict]) -> None:
        for row in fills:
            self.journal.trade(row)
            s = self.risk.state
            trade_no = s.trades_today + 1
            if row["position_closed"]:
                self.risk.record_trade(row["position_pnl"], row)
                remaining = 0
                day_pnl = s.realized_pnl
            else:
                remaining = self.pm.position.remaining if self.pm.position else 0
                day_pnl = s.realized_pnl + row["position_pnl"]
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
            if self.pm.position is None:
                self.pm.adopt_from_broker(self.broker.get_positions(), now)
            if self.pm.position is not None:
                mid = self.spread_mid(self.pm.position)
                self.record_fills(self.pm.force_close(now, mid))
            legs = spxw_legs(self.broker.get_positions())
            if not legs and self.pm.position is None:
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
        orders = self.broker.get_open_orders()
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
            f"Tier {t}  width {width}  credit range {lo:.2f}-{hi:.2f}",
            f"Phase: {strategy.phase(now)}   News day: {strategy.is_news_day(now.date())} "
            f"(mode {config.NEWS_DAY_MODE})   Level source: {config.BREAKOUT_LEVEL_SOURCE}",
            f"Day state: {self.risk.state.to_dict() | {'closed_trades': len(self.risk.state.closed_trades)}}",
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
                 f"Trades closed: {s.trades_today}   Realized P&L: {fmt_money(s.realized_pnl)}",
                 f"Equity start {fmt_money(s.start_equity)} -> end {fmt_money(acct['equity'])} "
                 f"({acct['equity'] - s.start_equity:+,.2f})"]
        table = format_day_table(s.closed_trades, s.realized_pnl)
        self.journal.card(table, now)
        mismatches = []
        if legs:
            mismatches.append(f"broker still holds {config.OPTION_ROOT} legs: {[(l['symbol'], l['qty']) for l in legs]}")
        if self.pm.position is not None:
            mismatches.append(f"local state still has a position: {self.pm.position.to_dict()}")
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
            "position": self.pm.position.to_dict() if self.pm.position else None,
            "acted": sorted(self.acted),
            "levels": {"overnight": self.overnight, "basis": self.basis, "orb": self.orb},
        })

    def on_interrupt(self) -> None:
        if self.risk is not None:
            self.save_state()
        p = self.pm.position
        log.warning("Interrupted by user. Nothing was closed automatically.")
        if p is None:
            log.warning("No open position.")
            return
        log.warning("OPEN POSITION: %s sell %s%s / buy %s%s x%d, entry credit %.2f, last %.2f, exp %s",
                    p.direction, p.short_strike, p.right, p.long_strike, p.right, p.remaining,
                    p.entry_credit, p.current_price, p.expiration)
        log.warning("To close: restart `python scheduler.py` (it re-adopts and manages the spread), "
                    "or run `python alpaca-reset.py` to liquidate ALL positions, "
                    "or close the SPXW legs in the Alpaca dashboard.")


def main() -> None:
    parser = argparse.ArgumentParser(description="0DTE SPX credit-spread bot")
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()
    setup_logging()
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

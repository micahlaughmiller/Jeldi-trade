"""ET wall-clock scheduler: scan cadence, maintenance cycle, reports, EOD reconcile + summary."""

from __future__ import annotations

import time as time_module
from datetime import date, datetime, time, timedelta
from types import ModuleType
from typing import Any, Callable
from zoneinfo import ZoneInfo

import config_45dte
import market_data_handler
from broker import BrokerError
from order_manager import OrderManager, parse_hhmm
from signal_generator import build_trade

ET = ZoneInfo("US/Eastern")


def is_trading_day(day: date, config: ModuleType = config_45dte) -> bool:
    return day.weekday() < 5 and day not in config.MARKET_HOLIDAYS


def scan_slots(config: ModuleType = config_45dte) -> list[time]:
    slots: list[time] = []
    for start, end, step in config.SCAN_SCHEDULE:
        cursor = datetime.combine(date(2000, 1, 1), parse_hhmm(start))
        stop = datetime.combine(date(2000, 1, 1), parse_hhmm(end))
        while cursor < stop:
            slots.append(cursor.time())
            cursor += timedelta(minutes=step)
    return sorted(set(slots))


def latest_due_slot(now: time, config: ModuleType = config_45dte) -> time | None:
    """Most recent scan slot at or before `now`; None before the first slot or after the close."""
    if now >= parse_hhmm(config.MARKET_CLOSE):
        return None
    due = [s for s in scan_slots(config) if s <= now]
    return max(due) if due else None


class Scheduler:
    def __init__(self, broker: Any, orders: OrderManager, log: Any, config: ModuleType = config_45dte,
                 once: bool = False, now_fn: Callable[[], datetime] | None = None,
                 sleep_fn: Callable[[float], None] = time_module.sleep) -> None:
        self.broker = broker
        self.orders = orders
        self.risk = orders.risk
        self.log = log
        self.config = config
        self.once = once
        self.now_fn = now_fn or (lambda: datetime.now(ET))
        self.sleep_fn = sleep_fn
        self.last_scan_slot: tuple[date, time] | None = None
        self.last_maintenance: datetime | None = None
        self.last_report: datetime | None = None
        self.last_idle_log: datetime | None = None
        self.eod_done_for: date | None = None
        self.equity: float | None = None
        self.orders.on_position_change = self.print_report

    # ---------------------------------------------------------------- reports

    def refresh_account(self) -> dict[str, Any]:
        account = self.broker.get_account()
        self.equity = float(account["equity"])
        return account

    def start_of_day_report(self) -> None:
        account = self.refresh_account()
        self.orders.roll_day(self.equity)
        pnl = self.broker.get_pnl_summary()
        adopt = self.orders.adopt()
        for position in self.orders.positions:
            self.orders.refresh_price(position)
        self.orders.save()
        open_orders = self.broker.get_open_orders()

        def money(v: float | None) -> str:
            return "n/a" if v is None else f"{v:+,.2f}"

        print("\n" + "#" * 96)
        print(f"START OF DAY  {self.now_fn().strftime('%Y-%m-%d %H:%M:%S %Z')}  broker={self.broker.name} "
              f"{'PAPER' if self.broker.is_paper else 'LIVE'}{'  DRY-RUN' if getattr(self.broker, 'dry_run', False) else ''}")
        print("#" * 96)
        print(f"equity ${self.equity:,.2f} | cash ${account['cash']:,.2f} | options BP ${account['options_buying_power']:,.2f}")
        print(f"P&L  YTD {money(pnl.get('ytd'))} | MTD {money(pnl.get('mtd'))} | today {money(pnl.get('today'))}")
        print(f"\nOPEN ORDERS ({len(open_orders)})")
        for o in open_orders:
            print(f"  {o['symbol']:<22}{o['order_class']:<7}{str(o.get('side')):<6}x{o['qty']:<4}"
                  f"limit {o.get('limit_price')}  {o['time_in_force']}  {o['status']}  id={o['id']}")
        print(self.orders.position_report(self.equity))
        self.log.start_of_day(equity=self.equity, cash=account["cash"], options_bp=account["options_buying_power"],
                              pnl=pnl, open_orders=len(open_orders), **adopt,
                              breaker=self.risk.breaker, dry_run=getattr(self.broker, "dry_run", False))
        try:
            self.broker.record_daily_equity()
        except BrokerError as exc:
            self.log.log_event("EQUITY_JOURNAL_ERROR", str(exc))

    def print_report(self) -> None:
        print(self.orders.position_report(self.equity))
        self.last_report = self.now_fn()
        if self.equity is not None:
            alert, drop = self.risk.daily_loss_alert(self.orders.state["day"].get("start_equity"), self.equity)
            if alert:
                self.log.log_event("DAILY_LOSS_ALERT", f"equity down {drop:.1%} from start of day", drop_pct=drop)

    # ------------------------------------------------------------------- scan

    def run_scan(self) -> None:
        result = market_data_handler.scan(log=self.log, config=self.config)
        for row in result.signals:
            self.log.signal(row)
        if not result.signals:
            return
        self.refresh_account()
        now = self.now_fn()
        if now.time() >= parse_hhmm(self.config.CANCEL_UNFILLED_AT):
            self.log.log_event("ENTRY_WINDOW_CLOSED", f"{len(result.signals)} signal(s) ignored after {self.config.CANCEL_UNFILLED_AT}")
            return
        for row in result.signals:
            self.try_enter(row, now.date())

    def try_enter(self, row: dict[str, Any], today: date) -> None:
        symbol = row["broker_symbol"]
        busy = {p["broker_symbol"] for p in self.orders.positions} | {e["broker_symbol"] for e in self.orders.working_entries}
        if symbol in busy or symbol in self.orders.blocked_symbols or self.risk.breaker_tripped:
            self.log.risk_check(symbol, False, 0, "pre-check: busy, blocked or breaker tripped")
            return
        try:
            spec = build_trade(self.broker, row, today, self.config)
        except BrokerError as exc:
            self.log.log_event("SIGNAL_ERROR", f"{symbol}: {exc}", symbol=symbol)
            return
        self.log.trade_spec(spec)
        if not spec["accepted"]:
            return
        decision = self.risk.check_new_entry(spec, self.equity, self.orders.positions, self.orders.working_entries,
                                             self.orders.entries_today, self.orders.blocked_symbols)
        self.log.risk_check(symbol, decision.allowed, decision.qty, decision.reason, **decision.details)
        if decision.allowed:
            self.orders.submit_entry(spec, decision.qty)

    # ------------------------------------------------------------ maintenance

    def run_maintenance(self) -> None:
        self.orders.poll_entries()
        self.orders.reduce_prices()
        self.orders.maintain()
        self.last_maintenance = self.now_fn()

    def run_end_of_day(self) -> None:
        self.orders.cancel_unfilled_entries("end-of-day sweep") if self.orders.working_entries else None
        self.orders.poll_entries()
        self.refresh_account()
        for position in self.orders.positions:
            self.orders.refresh_price(position)
        self.orders.save()
        reconcile = self.orders.reconcile()
        day = self.orders.state["day"]
        summary = {
            "equity_start": day.get("start_equity"), "equity_end": self.equity,
            "realized_pl_today": self.orders.realized_today_total(), "unrealized_pl": self.orders.unrealized_total(),
            "open_positions": [
                {k: p.get(k) for k in ("id", "symbol", "right", "expiration", "short_strike", "long_strike", "qty",
                                       "entry_credit", "current_price", "unrealized_pl", "close_order_id", "close_status")}
                for p in self.orders.positions
            ],
            "closed_today": self.orders.closed_today(), "entries_today": self.orders.entries_today,
            "portfolio_risk": self.orders.open_risk_dollars(), "breaker": self.risk.breaker, "reconcile": reconcile,
        }
        self.log.write_daily_summary(summary)
        self.print_report()
        self.eod_done_for = self.now_fn().date()

    # ------------------------------------------------------------------- loop

    def tick(self) -> None:
        cfg = self.config
        now = self.now_fn()
        today, clock = now.date(), now.time()
        if not is_trading_day(today, cfg):
            self._idle(now, "market holiday/weekend")
            return
        market_open, market_close, eod = parse_hhmm(cfg.MARKET_OPEN), parse_hhmm(cfg.MARKET_CLOSE), parse_hhmm(cfg.EOD_TIME)
        if market_open <= clock < market_close:
            self.orders.roll_day(self.equity or 0.0)
            slot = latest_due_slot(clock, cfg)
            if slot is not None and self.last_scan_slot != (today, slot):
                self.last_scan_slot = (today, slot)
                self.run_scan()
            if self.last_maintenance is None or (now - self.last_maintenance).total_seconds() >= cfg.MAINTENANCE_INTERVAL_SEC:
                self.run_maintenance()
            if clock >= parse_hhmm(cfg.CANCEL_UNFILLED_AT) and not self.orders.state["day"].get("unfilled_canceled"):
                n = self.orders.cancel_unfilled_entries()
                self.log.log_event("CANCEL_UNFILLED", f"canceled {n} unfilled entr{'y' if n == 1 else 'ies'} at {cfg.CANCEL_UNFILLED_AT}")
            if self.last_report is None or (now - self.last_report).total_seconds() >= cfg.REPORT_INTERVAL_MIN * 60:
                self.refresh_account()
                self.print_report()
        elif clock >= eod and self.eod_done_for != today:
            self.run_end_of_day()
        else:
            self._idle(now, "outside session")

    def _idle(self, now: datetime, why: str) -> None:
        if self.last_idle_log is None or (now - self.last_idle_log).total_seconds() >= self.config.IDLE_LOG_INTERVAL_SEC:
            self.last_idle_log = now
            self.log.log_event("IDLE", f"{why}; next check in {self.config.IDLE_LOG_INTERVAL_SEC // 60} min")

    def run(self) -> None:
        self.start_of_day_report()
        if self.once and (not is_trading_day(self.now_fn().date(), self.config)
                          or self.now_fn().time() >= parse_hhmm(self.config.EOD_TIME)):
            self.log.log_event("ONCE_EXIT", "--once outside a session: report printed, exiting")
            return
        while True:
            try:
                self.tick()
            except BrokerError as exc:
                self.log.log_event("BROKER_ERROR", str(exc))
            if self.once and self.eod_done_for == self.now_fn().date():
                self.log.log_event("ONCE_EXIT", "--once: end-of-day complete, exiting")
                return
            self.sleep_fn(self.config.LOOP_SLEEP_SEC)

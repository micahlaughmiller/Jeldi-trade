from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import config_45dte
import market_data_handler as mdh
import scheduler_45dte as sch
from fake_broker import FakeBroker
from logger_system import TradingLogger
from order_manager import OrderManager

ET = ZoneInfo("US/Eastern")


def test_trading_day_rules():
    assert sch.is_trading_day(date(2026, 9, 17))
    assert not sch.is_trading_day(date(2026, 9, 19))
    assert not sch.is_trading_day(date(2026, 9, 7))
    assert not sch.is_trading_day(date(2026, 11, 26))


def test_scan_slots_follow_schedule():
    slots = sch.scan_slots()
    assert slots[0] == time(9, 30) and slots[-1] == time(15, 55)
    assert time(11, 25) in slots and time(11, 30) in slots and time(11, 35) not in slots
    assert time(12, 0) in slots and time(14, 30) in slots and time(14, 35) not in slots
    assert time(15, 0) in slots and time(15, 5) in slots
    assert len(slots) == 24 + 7 + 12


def test_latest_due_slot():
    assert sch.latest_due_slot(time(9, 29)) is None
    assert sch.latest_due_slot(time(9, 30)) == time(9, 30)
    assert sch.latest_due_slot(time(10, 7)) == time(10, 5)
    assert sch.latest_due_slot(time(12, 59)) == time(12, 30)
    assert sch.latest_due_slot(time(15, 58)) == time(15, 55)
    assert sch.latest_due_slot(time(16, 0)) is None


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def make(tmp_path, start: datetime, monkeypatch):
    clock = Clock(start)
    broker = FakeBroker(now=start)
    log = TradingLogger(tmp_path, now_fn=clock, echo=False)
    om = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp_path / "state.json")
    scans = []
    monkeypatch.setattr(mdh, "scan", lambda log=None, config=None: scans.append(clock.now) or mdh.ScanResult())
    scheduler = sch.Scheduler(broker, om, log, config_45dte, now_fn=clock, sleep_fn=lambda s: None)
    return clock, broker, om, scheduler, scans


def test_late_start_scans_immediately_then_follows_cadence(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()
    scheduler.tick()
    assert scans == [clock.now]
    clock.now += timedelta(minutes=1)
    scheduler.tick()
    assert len(scans) == 1
    clock.now = clock.now.replace(minute=10, second=0)
    scheduler.tick()
    assert len(scans) == 2
    clock.now = clock.now.replace(hour=12, minute=15)
    scheduler.tick()
    assert len(scans) == 3
    clock.now = clock.now.replace(hour=12, minute=29)
    scheduler.tick()
    assert len(scans) == 3
    clock.now = clock.now.replace(hour=12, minute=30)
    scheduler.tick()
    assert len(scans) == 4


def test_maintenance_interval_and_eod(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 13, 0, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()
    scheduler.tick()
    first = scheduler.last_maintenance
    clock.now += timedelta(seconds=30)
    scheduler.tick()
    assert scheduler.last_maintenance == first
    clock.now += timedelta(seconds=31)
    scheduler.tick()
    assert scheduler.last_maintenance == clock.now
    clock.now = clock.now.replace(hour=16, minute=5)
    scheduler.tick()
    assert scheduler.eod_done_for == date(2026, 9, 17)
    assert (tmp_path / "summary_2026-09-17.json").exists()
    assert (tmp_path / "reconcile_2026-09-17.json").exists()


def test_no_scan_on_weekend(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 19, 10, 0, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()
    scheduler.tick()
    assert scans == []


def test_unfilled_entries_canceled_at_1555(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 15, 50, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()
    om.submit_entry({"symbol": "XYZ", "broker_symbol": "XYZ", "right": "P", "expiration": date(2026, 11, 6),
                     "short_strike": 95.0, "long_strike": 90.0, "credit": 1.60, "max_loss": 3.40, "strong": False}, 1)
    scheduler.tick()
    assert len(om.working_entries) == 1
    clock.now = clock.now.replace(minute=55)
    scheduler.tick()
    assert om.working_entries == []

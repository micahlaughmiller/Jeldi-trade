import json
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
                     "short_strike": 95.0, "long_strike": 90.0, "credit": 1.60, "max_loss": 3.40, "strong": False,
                     "width": 5.0, "min_credit": 1.50}, 1)
    scheduler.tick()
    assert len(om.working_entries) == 1
    clock.now = clock.now.replace(minute=55)
    scheduler.tick()
    assert om.working_entries == []


# ------------------------------------------------------------------ manual-close cool-down

TRADE = {
    "symbol": "XYZ", "broker_symbol": "XYZ", "right": "P", "expiration": date(2026, 11, 6), "short_strike": 95.0,
    "long_strike": 90.0, "credit": 1.60, "bid_side": 1.40, "max_loss": 3.40, "width": 5.0, "strong": False,
    "min_credit": 1.50, "dte": 50, "short_delta": -0.30, "dte_out_of_range": False, "accepted": True, "reason": "",
}


def fake_build_trade(broker, row, today=None, config=None):
    return {**TRADE, "symbol": row["symbol"], "broker_symbol": row["broker_symbol"]}


def signal_row(symbol="XYZ"):
    return {"symbol": symbol, "broker_symbol": symbol, "signal": "oversold", "strong": False, "rsi14": 25.0,
            "rsi28": 28.0, "close": 100.0}


def closed_by_hand(tmp_path, monkeypatch):
    """A scheduler whose only position was closed by hand in the broker's platform and has just been booked."""
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 0, tzinfo=ET), monkeypatch)
    for key, value in dict(EXTERNAL_SYNC_ENABLED=True, EXTERNAL_SYNC_CONFIRM_CYCLES=2, EXTERNAL_SYNC_MIN_AGE_SEC=120,
                           MANUAL_CLOSE_COOLDOWN_SEC=120).items():
        monkeypatch.setattr(config_45dte, key, value)
    monkeypatch.setattr(sch, "build_trade", fake_build_trade)
    scheduler.refresh_account()
    om.roll_day(scheduler.equity)
    oid = om.submit_entry(dict(TRADE), 2)
    broker.fill_order(oid, 1.60)
    om.poll_entries()
    assert len(om.positions) == 1
    broker.positions = []
    for seconds in (130, 60):   # past the min age -> first absent read, then the confirming one
        clock.now += timedelta(seconds=seconds)
        om.maintain()
    assert om.positions == [] and om.state["closed"][-1]["close_reason"] == "closed outside the bot (manual)"
    return clock, broker, om, scheduler


def entry_submissions(broker):
    return [c for c in broker.calls if c[0] == "place_credit_spread"]


def risk_checks(tmp_path, needle="manual-close cooldown"):
    lines = (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == "RISK_CHECK" and needle in e["message"]]


def test_manual_close_cooldown_blocks_entries_for_two_minutes_then_resumes(tmp_path, monkeypatch):
    clock, broker, om, scheduler = closed_by_hand(tmp_path, monkeypatch)
    booked = clock.now
    assert om.blocked_symbols == set() and om.entries_paused()      # nothing blocked: only paused
    scheduler.try_enter(signal_row(), booked.date())
    assert om.working_entries == [] and len(entry_submissions(broker)) == 1     # only the original entry
    (event,) = risk_checks(tmp_path)
    assert event["allowed"] is False and event["message"].endswith(f"until {booked + timedelta(seconds=120):%H:%M:%S}")
    clock.now = booked + timedelta(seconds=119)
    scheduler.try_enter(signal_row(), clock.now.date())
    assert om.working_entries == [] and len(entry_submissions(broker)) == 1
    clock.now = booked + timedelta(seconds=120)                     # at 120 s the cool-down is over
    scheduler.try_enter(signal_row(), clock.now.date())
    assert [e["broker_symbol"] for e in om.working_entries] == ["XYZ"] and len(entry_submissions(broker)) == 2


def test_manual_close_cooldown_is_reported_once_per_scan(tmp_path, monkeypatch):
    clock, broker, om, scheduler = closed_by_hand(tmp_path, monkeypatch)
    rows = [signal_row("XYZ"), signal_row("ABC")]
    monkeypatch.setattr(mdh, "scan", lambda log=None, config=None: mdh.ScanResult(results=rows))
    scheduler.run_scan()
    assert len(risk_checks(tmp_path)) == 1 and om.working_entries == []     # two signals, one line, no entries
    clock.now += timedelta(seconds=30)
    scheduler.run_scan()
    assert len(risk_checks(tmp_path)) == 2                                  # the next scan reports it again
    clock.now += timedelta(seconds=100)                                     # >= 120 s after the booking
    scheduler.run_scan()
    assert len(risk_checks(tmp_path)) == 2 and sorted(e["broker_symbol"] for e in om.working_entries) == ["ABC", "XYZ"]


def test_no_cooldown_without_a_manual_close(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 0, tzinfo=ET), monkeypatch)
    monkeypatch.setattr(sch, "build_trade", fake_build_trade)
    scheduler.refresh_account()
    om.roll_day(scheduler.equity)
    scheduler.try_enter(signal_row(), clock.now.date())
    assert [e["broker_symbol"] for e in om.working_entries] == ["XYZ"] and not risk_checks(tmp_path)


def test_the_account_check_runs_at_startup_and_a_refusal_propagates(tmp_path, monkeypatch):
    import pytest
    from broker import BrokerError
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()                                         # a broker without the check (the fake): fine
    monkeypatch.setattr(broker, "preflight_account", lambda a: {"summary": "PAPER account x: ACTIVE", "problems": ["p"],
                                                                "warnings": ["w"]}, raising=False)
    scheduler.start_of_day_report()
    events = [json.loads(l) for l in (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()]
    check = [e for e in events if e["type"] == "ACCOUNT_CHECK"]
    assert len(check) == 1 and check[0]["problems"] == ["p"] and "PAPER account x" in check[0]["message"]

    class Refused(BrokerError):
        pass

    def refuse(account):
        raise Refused("refusing to trade: blocked")
    monkeypatch.setattr(broker, "preflight_account", refuse, raising=False)
    with pytest.raises(BrokerError, match="refusing to trade"):
        scheduler.start_of_day_report()


# ------------------------------------------------- data-failure safety (2026-10-08)

def _hold_a_position(clock, broker, om):
    from test_order_manager import spec
    oid = om.submit_entry(spec(1.60), 1)
    broker.fill_order(oid, 1.60)
    om.poll_entries()
    return om.positions[0]


def test_a_tick_that_raises_anything_does_not_end_the_loop(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    calls = []

    def boom():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("not a BrokerError")
        scheduler.eod_done_for = clock.now.date()
    monkeypatch.setattr(scheduler, "tick", boom)
    scheduler.once = True
    scheduler.run()
    assert len(calls) == 3
    events = [json.loads(l) for l in (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events].count("TICK_EXCEPTION") == 1                 # logged once per TICK_EXCEPTION_LOG_SEC


def test_an_unpriced_position_raises_a_stale_alert_and_halts_entries(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    scheduler.start_of_day_report()
    pos = _hold_a_position(clock, broker, om)
    pos["price_at"] = (clock.now - timedelta(seconds=300)).isoformat()
    scheduler._check_marks(clock.now)
    assert scheduler._data_stale is False and scheduler.entry_halt_reason() is None
    pos["price_at"] = (clock.now - timedelta(seconds=700)).isoformat()
    scheduler._check_marks(clock.now)
    assert scheduler._data_stale is True and "data stale" in scheduler.entry_halt_reason()
    events = [json.loads(l) for l in (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()]
    assert any(e["type"] == "STALE_MARK" for e in events)
    scheduler.try_enter({"broker_symbol": "XYZ"}, clock.now.date())
    events = [json.loads(l) for l in (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()]
    assert any("data stale" in str(e) and e["type"] == "RISK_CHECK" for e in events)
    pos["price_at"] = clock.now.isoformat()                                          # prices flow again
    scheduler._check_marks(clock.now)
    assert scheduler.entry_halt_reason() is None


def test_the_stale_check_stays_quiet_outside_the_session(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    pos = _hold_a_position(clock, broker, om)
    pos["price_at"] = (clock.now - timedelta(hours=18)).isoformat()                  # yesterday's close
    scheduler._check_marks(datetime(2026, 9, 17, 8, 0, tzinfo=ET))
    assert scheduler._data_stale is False


def test_the_token_check_halts_entries_when_expiring_and_clears_when_fixed(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    state = {"state": "DEAD", "message": "token is 8.0 days old", "relogin": "cd 45DTE && python schwab_login.py --force",
             "age_days": 8.0, "expires_at": "2026-09-16T10:00:00-04:00"}
    monkeypatch.setattr(broker, "token_status", lambda now: dict(state), raising=False)
    scheduler.start_of_day_report()                                                  # the start-up check runs unthrottled
    assert "TOKEN_DEAD" in scheduler.entry_halt_reason()
    events = [json.loads(l) for l in (tmp_path / "session_2026-09-17.jsonl").read_text().splitlines()]
    assert any(e["type"] == "TOKEN_STATUS" and "re-login" in e["message"] for e in events)
    state.update(state="OK", message="fresh")
    scheduler._token_check(clock.now + timedelta(seconds=10))                        # throttled: not re-read yet
    assert scheduler._token_halt is not None
    scheduler._token_check(clock.now + timedelta(seconds=90))
    assert scheduler.entry_halt_reason() is None


def test_a_broken_token_check_never_raises_and_brokers_without_one_are_skipped(tmp_path, monkeypatch):
    clock, broker, om, scheduler, scans = make(tmp_path, datetime(2026, 9, 17, 10, 7, tzinfo=ET), monkeypatch)
    scheduler._token_check(clock.now)                                                # the fake has no token_status
    monkeypatch.setattr(broker, "token_status", lambda now: 1 / 0, raising=False)
    scheduler._token_check(clock.now + timedelta(minutes=5))
    assert scheduler.entry_halt_reason() is None

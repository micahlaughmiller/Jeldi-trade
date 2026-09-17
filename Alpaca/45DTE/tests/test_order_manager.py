import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config_45dte
from fake_broker import FakeBroker, make_quote, occ_symbol
from logger_system import TradingLogger
from order_manager import OrderManager, spread_id

ET = ZoneInfo("US/Eastern")
EXP = date(2026, 11, 6)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def spec(credit=1.60, strong=False, symbol="XYZ"):
    return {
        "symbol": symbol, "broker_symbol": symbol, "right": "P", "expiration": EXP, "short_strike": 95.0,
        "long_strike": 90.0, "credit": credit, "max_loss": round(5 - credit, 2), "strong": strong, "dte": 50,
        "short_delta": -0.30, "dte_out_of_range": False,
    }


def set_chain(broker: FakeBroker, short_mid: float, long_mid: float, symbol="XYZ"):
    broker.chains[(symbol, EXP, "P")] = [
        make_quote(symbol, EXP, "P", 90.0, long_mid - 0.05, long_mid + 0.05, -0.2),
        make_quote(symbol, EXP, "P", 95.0, short_mid - 0.05, short_mid + 0.05, -0.3),
    ]


@pytest.fixture
def env(tmp_path):
    clock = Clock(datetime(2026, 9, 17, 10, 0, tzinfo=ET))
    broker = FakeBroker(now=clock.now)
    log = TradingLogger(tmp_path, now_fn=clock, echo=False)
    om = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp_path / "state.json")
    om.roll_day(25_000.0)
    return clock, broker, log, om, tmp_path


def entry_orders(broker):
    return [o for o in broker.orders.values() if o["kind"] == "entry"]


def close_orders(broker):
    return [o for o in broker.orders.values() if o["kind"] == "close"]


class TestEntryToClose:
    def test_entry_fill_places_gtc_close_at_half_credit(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 3)
        call = next(c for c in broker.calls if c[0] == "place_credit_spread")
        assert call[1] == ("XYZ", EXP, "P", 95.0, 90.0, 3)
        assert call[2] == {"limit_credit": 1.60, "time_in_force": "day"}
        assert om.working_entries[0]["order_id"] == oid and om.entries_today == 1

        om.poll_entries()
        assert om.positions == [] and len(om.working_entries) == 1

        broker.fill_order(oid, 1.62)
        om.poll_entries()
        assert om.working_entries == []
        pos = om.positions[0]
        assert pos["qty"] == 3 and pos["entry_credit"] == 1.62 and pos["max_loss"] == 3.38
        close = broker.orders[pos["close_order_id"]]
        assert close["kind"] == "close" and close["time_in_force"] == "gtc"
        assert close["limit_price"] == 0.80 and close["qty"] == 3
        assert pos["close_limit"] == 0.80

        state = json.loads((tmp / "state.json").read_text())
        assert pos["id"] in state["positions"]
        assert (tmp / "trades_2026-09-17.csv").exists()

    def test_partial_fill_places_close_for_filled_qty_then_replaces_on_completion(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 4)
        broker.fill_order(oid, 1.60, qty=1)
        om.poll_entries()
        assert len(om.working_entries) == 1 and om.positions[0]["qty"] == 1
        first_close = om.positions[0]["close_order_id"]
        assert broker.orders[first_close]["qty"] == 1
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        assert om.working_entries == [] and om.positions[0]["qty"] == 4
        second_close = om.positions[0]["close_order_id"]
        assert second_close != first_close and broker.orders[second_close]["qty"] == 4
        assert broker.orders[first_close]["status"] == "canceled"

    def test_close_fill_records_realized_pnl_and_removes_position(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        pos = om.positions[0]
        set_chain(broker, 2.00, 1.20)
        broker.fill_order(pos["close_order_id"], 0.80)
        om.maintain()
        assert om.positions == []
        closed = om.closed_today()[0]
        assert closed["realized_pl"] == pytest.approx((1.60 - 0.80) * 100 * 2)
        assert closed["close_reason"] == "profit target"
        assert om.realized_today_total() == pytest.approx(160.0)

    def test_state_reloads_from_disk(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        om2 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        assert om2.positions[0]["id"] == om.positions[0]["id"]
        assert om2.entries_today == 1


class TestPriceReduction:
    def test_reduces_every_hour_to_floor_then_cancels(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.56), 1)
        clock.advance(minutes=30)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.56
        limits = []
        for _ in range(3):
            clock.advance(minutes=31)
            om.reduce_prices()
            clock.advance(minutes=30)
            limits.append(om.working_entries[0]["limit_credit"])
        assert limits == [1.54, 1.52, 1.50]
        assert om.working_entries[0]["order_id"] != oid
        assert broker.orders[oid]["status"] == "replaced"
        replaces = [c for c in broker.calls if c[0] == "replace_order_price"]
        assert [c[1][1] for c in replaces] == [1.54, 1.52, 1.50]
        last_id = om.working_entries[0]["order_id"]
        clock.advance(minutes=31)
        om.reduce_prices()
        assert om.working_entries == []
        assert broker.orders[last_id]["status"] == "canceled"

    def test_strong_signal_uses_lower_floor(self, env):
        clock, broker, log, om, tmp = env
        om.submit_entry(spec(1.42, strong=True), 1)
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.40
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries == []

    def test_cancel_unfilled_entries(self, env):
        clock, broker, log, om, tmp = env
        a = om.submit_entry(spec(1.60), 1)
        b = om.submit_entry(spec(1.60, symbol="ABC"), 1)
        assert om.cancel_unfilled_entries() == 2
        assert om.working_entries == []
        assert broker.orders[a]["status"] == "canceled" and broker.orders[b]["status"] == "canceled"
        assert om.state["day"]["unfilled_canceled"] is True


class TestMaintenance:
    def _open_position(self, env, credit=1.60, qty=2):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(credit), qty)
        broker.fill_order(oid, credit)
        om.poll_entries()
        return om.positions[0]

    def test_recreates_canceled_close_order(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 2.50, 1.20)
        old = pos["close_order_id"]
        broker.orders[old]["status"] = "canceled"
        om.maintain()
        assert pos["close_order_id"] != old
        assert broker.orders[pos["close_order_id"]]["status"] == "accepted"
        assert broker.orders[pos["close_order_id"]]["limit_price"] == 0.80

    def test_recreates_missing_close_order(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 2.50, 1.20)
        del broker.orders[pos["close_order_id"]]
        om.maintain()
        assert pos["close_order_id"] in broker.orders

    def test_refreshes_price_and_unrealized(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env, credit=1.60, qty=2)
        set_chain(broker, 2.50, 1.20)
        om.maintain()
        assert pos["current_price"] == 1.30
        assert pos["unrealized_pl"] == pytest.approx((1.60 - 1.30) * 100 * 2)
        assert om.unrealized_total() == pytest.approx(60.0)

    def test_dte_exit_closes_at_market_and_records(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env, credit=1.60, qty=2)
        set_chain(broker, 1.00, 0.60)
        close_id = pos["close_order_id"]
        clock.now = datetime.combine(EXP - timedelta(days=7), datetime.min.time(), tzinfo=ET).replace(hour=10)
        om.maintain()
        assert om.positions == []
        assert broker.orders[close_id]["status"] == "canceled"
        assert any(c[0] == "close_spread_at_market" and c[1][:6] == ("XYZ", EXP, "P", 95.0, 90.0, 2) for c in broker.calls)
        assert broker.positions == []
        closed = om.state["closed"][-1]
        assert closed["exit_debit"] == 0.40 and closed["realized_pl"] == pytest.approx(240.0)
        assert "DTE 7" in closed["close_reason"]

    def test_no_exit_at_dte_8(self, env):
        clock, broker, log, om, tmp = env
        self._open_position(env)
        set_chain(broker, 1.00, 0.60)
        clock.now = datetime.combine(EXP - timedelta(days=8), datetime.min.time(), tzinfo=ET).replace(hour=10)
        om.maintain()
        assert len(om.positions) == 1

    def test_max_loss_hit_exits_and_feeds_breaker(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env, credit=1.50, qty=1)
        set_chain(broker, 6.00, 1.30)
        om.maintain()
        assert om.positions == []
        assert om.risk.max_loss_hits == 1
        closed = om.state["closed"][-1]
        assert closed["exit_debit"] == 4.70 and "max loss" in closed["close_reason"]

    def test_breaker_trips_after_three_max_loss_exits(self, env):
        clock, broker, log, om, tmp = env
        for sym in ("AAA", "BBB", "CCC"):
            oid = om.submit_entry(spec(1.50, symbol=sym), 1)
            broker.fill_order(oid, 1.50)
            om.poll_entries()
            set_chain(broker, 6.00, 1.30, symbol=sym)
        om.maintain()
        assert om.positions == [] and om.risk.breaker_tripped
        state = json.loads((tmp / "state.json").read_text())
        assert state["breaker"]["tripped"] is True and len(state["breaker"]["hits"]) == 3

    def test_exit_error_keeps_position_for_retry(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.00, 0.60)
        broker.fail_on.add("close_spread_at_market")
        clock.now = datetime.combine(EXP - timedelta(days=3), datetime.min.time(), tzinfo=ET).replace(hour=10)
        om.maintain()
        assert len(om.positions) == 1 and om.positions[0]["close_order_id"] is None


class TestAdoptAndReconcile:
    def test_adopts_paired_legs_and_matches_close_order(self, env):
        clock, broker, log, om, tmp = env
        broker.add_position("ABC", EXP, "P", 95.0, -2, 3.10, current_price=2.50)
        broker.add_position("ABC", EXP, "P", 90.0, 2, 1.50, current_price=1.20)
        existing_close = broker.place_close_spread("ABC", EXP, "P", 95.0, 90.0, 2, limit_debit=0.80)
        broker.add_position("LONE", EXP, "C", 120.0, -1, 1.00)
        broker.calls.clear()
        summary = om.adopt()
        assert summary["adopted"] == 1 and summary["unpaired"] == ["LONE"]
        pos = om.positions[0]
        assert pos["id"] == spread_id("ABC", EXP, "P", 95.0, 90.0)
        assert pos["qty"] == 2 and pos["entry_credit"] == 1.60 and pos["source"] == "adopted"
        assert pos["close_order_id"] == existing_close["id"]
        assert pos["current_price"] == 1.30 and pos["unrealized_pl"] == pytest.approx(60.0)
        assert om.blocked_symbols == {"LONE"}
        om.maintain()
        assert len(close_orders(broker)) == 1

    def test_adopt_creates_close_when_none_exists(self, env):
        clock, broker, log, om, tmp = env
        broker.add_position("ABC", EXP, "P", 95.0, -1, 3.10)
        broker.add_position("ABC", EXP, "P", 90.0, 1, 1.50)
        set_chain(broker, 2.0, 1.0, symbol="ABC")
        om.adopt()
        assert om.positions[0]["close_order_id"] is None
        om.maintain()
        assert om.positions[0]["close_order_id"] is not None
        assert broker.orders[om.positions[0]["close_order_id"]]["limit_price"] == 0.80

    def test_adopt_flags_mismatched_qty_as_unpaired(self, env):
        clock, broker, log, om, tmp = env
        broker.add_position("ABC", EXP, "P", 95.0, -2, 3.10)
        broker.add_position("ABC", EXP, "P", 90.0, 1, 1.50)
        om.adopt()
        assert om.positions == [] and om.blocked_symbols == {"ABC"}

    def test_adopt_flags_wrong_direction_as_unpaired(self, env):
        clock, broker, log, om, tmp = env
        broker.add_position("ABC", EXP, "P", 90.0, -1, 1.50)
        broker.add_position("ABC", EXP, "P", 95.0, 1, 3.10)
        om.adopt()
        assert om.blocked_symbols == {"ABC"}

    def test_adopt_resolves_position_that_closed_while_offline(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        broker.fill_order(om.positions[0]["close_order_id"], 0.80)
        om.adopt()
        assert om.positions == [] and om.state["closed"][-1]["realized_pl"] == pytest.approx(80.0)

    def test_adopt_picks_up_untracked_universe_entry_but_leaves_foreign_orders(self, env):
        clock, broker, log, om, tmp = env
        order = broker.place_credit_spread("AAPL", EXP, "P", 95.0, 90.0, 2, limit_credit=1.55, time_in_force="day")
        foreign = broker.place_credit_spread("SPXW", EXP, "P", 6525.0, 6500.0, 1, limit_credit=5.00, time_in_force="day")
        om.adopt()
        assert [e["order_id"] for e in om.working_entries] == [order["id"]]
        entry = om.working_entries[0]
        assert entry["broker_symbol"] == "AAPL"
        assert entry["short_strike"] == 95.0 and entry["long_strike"] == 90.0 and entry["limit_credit"] == 1.55
        om.cancel_unfilled_entries()
        assert broker.orders[foreign["id"]]["status"] == "accepted"
        events = [json.loads(l) for l in (tmp / "session_2026-09-17.jsonl").read_text().splitlines()]
        assert any(e["type"] == "UNKNOWN_OPEN_ORDER" and e["order_id"] == foreign["id"] for e in events)

    def test_reconcile_clean_and_mismatched(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        report = om.reconcile()
        assert report["ok"] and report["mismatches"] == []
        assert (tmp / "reconcile_2026-09-17.json").exists()

        broker.positions = [p for p in broker.positions if p["strike"] != 90.0]
        broker.add_position("GHOST", EXP, "C", 50.0, -1, 1.0)
        pos = om.positions[0]
        broker.orders[pos["close_order_id"]]["status"] = "canceled"
        report = om.reconcile()
        kinds = sorted((m["kind"], m.get("symbol") or m.get("order_id")) for m in report["mismatches"])
        assert ("position", occ_symbol("XYZ", EXP, "P", 90.0)) in kinds
        assert ("position", occ_symbol("GHOST", EXP, "C", 50.0)) in kinds
        assert ("order", pos["close_order_id"]) in kinds
        assert not report["ok"]
        events = [json.loads(l) for l in (tmp / "session_2026-09-17.jsonl").read_text().splitlines()]
        assert any(e["type"] == "RECONCILE_MISMATCH" for e in events)


def test_position_report_renders(env):
    clock, broker, log, om, tmp = env
    oid = om.submit_entry(spec(1.60), 2)
    broker.fill_order(oid, 1.60)
    om.poll_entries()
    set_chain(broker, 2.0, 1.0)
    om.maintain()
    text = om.position_report(25_000.0)
    assert "XYZ" in text and "95/90" in text and "portfolio risk $680" in text

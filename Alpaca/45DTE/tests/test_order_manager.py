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

ORIGINAL_EXIT_RULES = dict(EXIT_DTE_ENABLED=True, MAX_LOSS_EXIT=True, PROFIT_FLOOR_ENABLED=True,
                           PROFIT_FLOOR_MODE="fixed", PROFIT_FLOOR_ARM_PCT_OF_TARGET=0.85,
                           PROFIT_FLOOR_PCT_OF_MAX=0.30, PROFIT_FLOOR_TRAIL_TICK=0.05,
                           PROFIT_FLOOR_MIN_PROFIT=0.05)
TRAIL_EXIT_RULES = dict(EXIT_DTE_ENABLED=False, MAX_LOSS_EXIT=False, PROFIT_FLOOR_ENABLED=True,
                        PROFIT_FLOOR_MODE="trail", PROFIT_FLOOR_ARM_PCT_OF_TARGET=0.40,
                        PROFIT_FLOOR_TRAIL_TICK=0.05, PROFIT_FLOOR_MIN_PROFIT=0.05)


@pytest.fixture(autouse=True)
def _original_exit_rules(monkeypatch):
    """Pin the original exit rules so the tests below mean the same thing in every broker folder (the
    Schwab folder's own config overrides several of them); the trail-mode tests opt in explicitly."""
    for key, value in ORIGINAL_EXIT_RULES.items():
        monkeypatch.setattr(config_45dte, key, value)


@pytest.fixture
def trail_rules(monkeypatch):
    for key, value in TRAIL_EXIT_RULES.items():
        monkeypatch.setattr(config_45dte, key, value)


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
        "short_delta": -0.30, "dte_out_of_range": False, "width": 5.0,
        "min_credit": 1.40 if strong else 1.50,
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
    def test_reduces_every_hour_to_floor_then_holds(self, env):
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
        for _ in range(3):
            clock.advance(minutes=61)
            om.reduce_prices()
        assert om.working_entries[0]["order_id"] == last_id
        assert om.working_entries[0]["limit_credit"] == 1.50
        assert broker.orders[last_id]["status"] != "canceled"
        assert len([c for c in broker.calls if c[0] == "replace_order_price"]) == 3

    def test_entry_at_the_floor_is_never_reduced_or_canceled(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.50), 1)
        for _ in range(4):
            clock.advance(minutes=61)
            om.reduce_prices()
        assert om.working_entries[0]["order_id"] == oid
        assert om.working_entries[0]["limit_credit"] == 1.50
        assert broker.orders[oid]["status"] != "canceled"
        assert not [c for c in broker.calls if c[0] in ("replace_order_price", "cancel_order")]

    def test_step_that_would_cross_the_floor_stops_at_it(self, env):
        clock, broker, log, om, tmp = env
        om.submit_entry(spec(1.51), 1)
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.50
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.50 and om.working_entries

    def test_strong_signal_uses_lower_floor(self, env):
        clock, broker, log, om, tmp = env
        om.submit_entry(spec(1.42, strong=True), 1)
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.40
        clock.advance(minutes=61)
        om.reduce_prices()
        assert om.working_entries[0]["limit_credit"] == 1.40 and om.working_entries

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


class TestProfitFloor:
    """entry_credit=1.60, PROFIT_TARGET_PCT=0.50, PROFIT_FLOOR_ARM_PCT_OF_TARGET=0.85,
    PROFIT_FLOOR_PCT_OF_MAX=0.30 -> arm price 0.90 (42.5% of max), floor price 1.10 (30% of max)."""

    def _open_position(self, env, credit=1.60, qty=2):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(credit), qty)
        broker.fill_order(oid, credit)
        om.poll_entries()
        return om.positions[0]

    def test_arms_once_price_reaches_arm_level_but_does_not_exit_yet(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 0.60, 0.30)   # price 0.30, well past the 0.90 arm level, short of target
        om.maintain()
        assert pos["profit_floor_armed"] is True
        assert len(om.positions) == 1   # not exited -- armed, not yet given back to the floor

    def test_not_armed_stays_open_through_a_move_that_never_reached_the_arm_level(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.80, 0.30)   # price 1.50 -- never got close to the 0.90 arm level
        om.maintain()
        assert not pos.get("profit_floor_armed")
        set_chain(broker, 2.00, 0.20)   # widens further to 1.80 (a real loser) -- floor must not fire unarmed
        om.maintain()
        assert len(om.positions) == 1 and not pos.get("profit_floor_armed")

    def test_exits_at_floor_after_arming_and_giving_back(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        close_id = pos["close_order_id"]
        set_chain(broker, 0.60, 0.30)    # price 0.30: arms (past the 0.90 arm level)
        om.maintain()
        assert pos["profit_floor_armed"] is True
        set_chain(broker, 1.50, 0.30)    # reverses to price 1.20 -- past the 1.10 floor
        om.maintain()
        assert om.positions == []
        assert broker.orders[close_id]["status"] == "canceled"
        closed = om.state["closed"][-1]
        assert closed["exit_debit"] == 1.20 and "profit floor" in closed["close_reason"]
        # this is the user's own real scenario: reached close to target, reversed hard -- the floor
        # exit at 1.20 (a real, if partial, profit) beats letting it round-trip through entry (1.60)
        # to an outright loss.
        assert closed["realized_pl"] == pytest.approx((1.60 - 1.20) * 100 * 2)

    def test_armed_position_still_exits_on_dte_if_floor_never_re_triggers(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 0.60, 0.30)
        om.maintain()
        assert pos["profit_floor_armed"] is True
        set_chain(broker, 0.50, 0.20)    # stays favorable, never gives back to the floor
        clock.now = datetime.combine(EXP - timedelta(days=7), datetime.min.time(), tzinfo=ET).replace(hour=10)
        om.maintain()
        assert om.positions == []
        assert "DTE 7" in om.state["closed"][-1]["close_reason"]

    def test_disabled_via_config_never_arms_or_exits(self, env, monkeypatch):
        clock, broker, log, om, tmp = env
        monkeypatch.setattr(config_45dte, "PROFIT_FLOOR_ENABLED", False)
        pos = self._open_position(env)
        set_chain(broker, 0.60, 0.30)    # price 0.30 -- would have armed if enabled
        om.maintain()
        assert not pos.get("profit_floor_armed") and len(om.positions) == 1


class TestTrailFloor:
    """Schwab's rules: no DTE exit, no max-loss exit, floor arms 40% of the way to the 50% target.
    entry_credit=1.60 -> target 0.80, arm level 1.30 (20% of max profit); then the first 0.05 give-back
    from the best price exits at market -- but only if that still books a profit."""

    @pytest.fixture(autouse=True)
    def _trail(self, trail_rules):
        pass

    def _open_position(self, env, credit=1.60, qty=2):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(credit), qty)
        broker.fill_order(oid, credit)
        om.poll_entries()
        return om.positions[0]

    def test_arms_at_forty_percent_of_target_and_not_before(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.55, 0.20)   # price 1.35 -- short of the 1.30 arm level
        om.maintain()
        assert not pos.get("profit_floor_armed")
        set_chain(broker, 1.50, 0.20)   # price 1.30 -- exactly the arm level
        om.maintain()
        assert pos["profit_floor_armed"] is True and pos["profit_floor_best"] == 1.30
        assert len(om.positions) == 1

    def test_first_nickel_giveback_exits_for_a_profit(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        close_id = pos["close_order_id"]
        set_chain(broker, 1.50, 0.30)   # price 1.20: arms, nothing to give back yet
        om.maintain()
        assert pos["profit_floor_armed"] is True and len(om.positions) == 1
        set_chain(broker, 1.55, 0.30)   # price 1.25: +0.05 off the 1.20 best
        om.maintain()
        assert om.positions == []
        assert broker.orders[close_id]["status"] == "canceled"
        closed = om.state["closed"][-1]
        assert closed["exit_debit"] == 1.25 and "profit floor trail" in closed["close_reason"]
        assert closed["realized_pl"] == pytest.approx((1.60 - 1.25) * 100 * 2)

    def test_trail_follows_new_lows_and_ignores_smaller_wobbles(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        for short_mid, expect_best in ((1.50, 1.20), (1.40, 1.10), (1.43, 1.10), (1.44, 1.10)):
            set_chain(broker, short_mid, 0.30)
            om.maintain()
            assert pos["profit_floor_best"] == expect_best and len(om.positions) == 1   # +0.03/+0.04: held
        set_chain(broker, 1.45, 0.30)   # price 1.15: exactly 0.05 off the 1.10 best
        om.maintain()
        assert om.positions == [] and om.state["closed"][-1]["exit_debit"] == 1.15

    def test_unarmed_position_never_exits_on_a_giveback(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        for short_mid in (1.55, 1.60, 1.70, 1.52):   # prices 1.35/1.40/1.50/1.32: all above the 1.30 arm level
            set_chain(broker, short_mid, 0.20)
            om.maintain()
        assert len(om.positions) == 1 and not pos.get("profit_floor_armed")

    def test_reversal_through_entry_disarms_instead_of_selling_a_loss_then_rearms(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.50, 0.30)   # price 1.20: arms
        om.maintain()
        set_chain(broker, 2.00, 0.30)   # price 1.70 -- through entry (1.60) before the exit could fire
        om.maintain()
        assert len(om.positions) == 1 and pos["profit_floor_armed"] is False
        assert "profit_floor_best" not in pos
        set_chain(broker, 1.55, 0.30)   # back to 1.25: re-arms
        om.maintain()
        assert pos["profit_floor_armed"] is True and pos["profit_floor_best"] == 1.25
        set_chain(broker, 1.60, 0.30)   # 1.30: +0.05 off best, still a profit
        om.maintain()
        assert om.positions == [] and om.state["closed"][-1]["exit_debit"] == 1.30

    def test_giveback_to_exactly_entry_is_not_a_profit_so_it_rides(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.50, 0.30)
        om.maintain()
        set_chain(broker, 1.90, 0.30)   # price 1.60 == entry credit: zero P/L
        om.maintain()
        assert len(om.positions) == 1 and pos["profit_floor_armed"] is False

    def test_no_dte_exit_and_no_max_loss_exit_but_the_breaker_still_counts(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env, credit=1.50, qty=1)
        clock.now = datetime.combine(EXP - timedelta(days=3), datetime.min.time(), tzinfo=ET).replace(hour=10)
        set_chain(broker, 6.00, 1.30)   # price 4.70 -- past the 90%-of-max-loss mark
        om.maintain()
        assert len(om.positions) == 1 and pos["max_loss_hit"] is True
        assert om.risk.max_loss_hits == 1
        assert not any(c[0] == "close_spread_at_market" for c in broker.calls)

    def test_giveback_that_leaves_under_the_minimum_profit_rides_instead_of_selling(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.50, 0.30)   # 1.20: arms
        om.maintain()
        set_chain(broker, 1.87, 0.30)   # 1.57: still 0.03 of mid profit, but a market fill could land at a loss
        om.maintain()
        assert len(om.positions) == 1 and pos["profit_floor_armed"] is False
        assert not any(c[0] == "close_spread_at_market" for c in broker.calls)

    def test_exit_error_while_armed_keeps_it_armed_and_retries_next_poll(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.50, 0.30)   # 1.20: arms
        om.maintain()
        set_chain(broker, 1.55, 0.30)   # 1.25: trips
        broker.fail_on.add("close_spread_at_market")
        om.maintain()
        assert len(om.positions) == 1 and pos["profit_floor_armed"] is True and pos["close_order_id"] is None
        broker.fail_on.discard("close_spread_at_market")
        om.maintain()
        assert om.positions == [] and om.state["closed"][-1]["exit_debit"] == 1.25

    def test_armed_state_survives_a_restart_and_a_missing_best_reseeds_from_the_current_price(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 1.50, 0.30)   # 1.20: arms, best 1.20
        om.maintain()
        om2 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        reloaded = om2.positions[0]
        assert reloaded["profit_floor_armed"] is True and reloaded["profit_floor_best"] == 1.20
        # an armed position with no recorded best (armed under "fixed" mode, then switched) reseeds
        del reloaded["profit_floor_best"]
        set_chain(broker, 1.45, 0.30)   # 1.15
        om2.maintain()
        assert reloaded["profit_floor_best"] == 1.15 and len(om2.positions) == 1

    def test_trail_mode_with_the_max_loss_stop_re_enabled_still_stops_out_after_a_disarm(self, env, monkeypatch):
        clock, broker, log, om, tmp = env
        monkeypatch.setattr(config_45dte, "MAX_LOSS_EXIT", True)
        pos = self._open_position(env, credit=1.50, qty=1)
        set_chain(broker, 1.40, 0.30)    # 1.10: arms
        om.maintain()
        set_chain(broker, 2.00, 0.30)    # 1.70: through entry -> disarms (no exit)
        om.maintain()
        assert len(om.positions) == 1 and pos["profit_floor_armed"] is False
        set_chain(broker, 6.00, 1.30)    # 4.70: max loss -> the re-enabled stop fires
        om.maintain()
        assert om.positions == [] and "max loss" in om.state["closed"][-1]["close_reason"]

    def test_gtc_profit_target_order_still_rests_at_half_the_credit(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        assert broker.orders[pos["close_order_id"]]["limit_price"] == 0.80


class TestExpiredPositions:
    """With the DTE exit off a position can reach expiration; the broker settles it, not the bot."""

    @pytest.fixture(autouse=True)
    def _trail(self, trail_rules):
        pass

    def _expired(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        clock.now = datetime.combine(EXP + timedelta(days=1), datetime.min.time(), tzinfo=ET).replace(hour=10)
        return om.positions[0]

    def test_settled_legs_book_the_record_as_expired_without_placing_any_order(self, env):
        clock, broker, log, om, tmp = env
        pos = self._expired(env)
        pos["current_price"], pos["unrealized_pl"] = 3.10, -300.0   # the last mark before expiry
        broker.positions = []   # the broker settled both legs
        calls_before = len(broker.calls)
        om.maintain()
        assert om.positions == []
        closed = om.state["closed"][-1]
        assert "expired" in closed["close_reason"] and closed["exit_debit"] is None and closed["realized_pl"] is None
        assert closed["last_price_before_expiry"] == 3.10 and closed["est_realized_pl"] == -300.0
        assert log.counts["EXPIRED_SHARES"] == 0
        new_calls = [c[0] for c in broker.calls[calls_before:]]
        assert "place_close_spread" not in new_calls and "close_spread_at_market" not in new_calls

    def test_legs_still_at_the_broker_leave_the_position_untouched(self, env):
        clock, broker, log, om, tmp = env
        pos = self._expired(env)
        om.maintain()
        om.maintain()
        assert len(om.positions) == 1 and log.counts["EXPIRED_STILL_OPEN"] == 1   # once per day, not per poll
        first_day = pos["expired_alerted_on"]
        clock.advance(days=1)
        om.maintain()
        assert pos["expired_alerted_on"] != first_day and log.counts["EXPIRED_STILL_OPEN"] == 1   # again, next day
        assert not any(c[0] == "close_spread_at_market" for c in broker.calls)

    def test_shares_left_by_assignment_are_flagged_loudly(self, env):
        clock, broker, log, om, tmp = env
        self._expired(env)
        broker.positions = [{"symbol": "XYZ", "underlying": "XYZ", "qty": 200, "asset_class": "stock",
                             "expiration": None, "strike": None, "right": None}]
        om.maintain()
        assert om.positions == [] and log.counts["EXPIRED_SHARES"] == 1

    def test_a_broker_error_while_checking_does_not_book_it(self, env):
        clock, broker, log, om, tmp = env
        self._expired(env)
        broker.fail_on.add("get_positions")
        om.maintain()
        assert len(om.positions) == 1


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

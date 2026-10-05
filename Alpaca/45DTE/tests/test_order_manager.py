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
PNL_BASIS_RULES = dict(EXIT_PNL_BASIS="fill", EXIT_PREV_TICK_MAX_AGE_SEC=300)   # the tick basis opts in below
MANUAL_OVERRIDE_RULES = dict(EXTERNAL_EXIT_RECHECK_SEC=0, EXTERNAL_SYNC_ENABLED=True, EXTERNAL_SYNC_CONFIRM_CYCLES=2,
                             EXTERNAL_SYNC_MIN_AGE_SEC=120, MANUAL_CLOSE_COOLDOWN_SEC=120)
TRAIL_EXIT_RULES = dict(EXIT_DTE_ENABLED=False, MAX_LOSS_EXIT=False, PROFIT_FLOOR_ENABLED=True,
                        PROFIT_FLOOR_MODE="trail", PROFIT_FLOOR_ARM_PCT_OF_TARGET=0.40,
                        PROFIT_FLOOR_TRAIL_TICK=0.05, PROFIT_FLOOR_MIN_PROFIT=0.05)


@pytest.fixture(autouse=True)
def _original_exit_rules(monkeypatch):
    """Pin the original exit rules (and the manual-override sync settings) so the tests below mean the same
    thing in every broker folder (the Schwab folder's own config overrides several of the exit rules); the
    trail-mode tests opt in explicitly."""
    for key, value in {**ORIGINAL_EXIT_RULES, **MANUAL_OVERRIDE_RULES, **PNL_BASIS_RULES}.items():
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

    def test_recreates_rejected_close_order(self, env):
        # ('canceled' / 'replaced' are owner actions in thinkorswim and are NOT re-created: see TestOwnerCloseOrderChanges)
        clock, broker, log, om, tmp = env
        pos = self._open_position(env)
        set_chain(broker, 2.50, 1.20)
        old = pos["close_order_id"]
        broker.orders[old]["status"] = "rejected"
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

    def test_no_dte_exit_and_no_max_loss_exit_and_a_mark_does_not_feed_the_breaker(self, env):
        clock, broker, log, om, tmp = env
        pos = self._open_position(env, credit=1.50, qty=1)
        clock.now = datetime.combine(EXP - timedelta(days=3), datetime.min.time(), tzinfo=ET).replace(hour=10)
        set_chain(broker, 6.00, 1.30)   # price 4.70 -- past the 90%-of-max-loss mark
        om.maintain()
        assert len(om.positions) == 1 and pos["max_loss_hit"] is True      # flagged for the report ...
        assert om.risk.max_loss_hits == 0                                  # ... but only a REALIZED loss counts
        assert not any(c[0] == "close_spread_at_market" for c in broker.calls)

    def test_a_spread_that_dips_to_max_loss_and_recovers_never_moves_the_breaker(self, env):
        clock, broker, log, om, tmp = env
        for sym in ("AAA", "BBB", "CCC"):                                  # three would have tripped the old mark-based count
            oid = om.submit_entry(spec(1.50, symbol=sym), 1)
            broker.fill_order(oid, 1.50)
            om.poll_entries()
            set_chain(broker, 6.00, 1.30, symbol=sym)                      # 4.70: at the max-loss line
        om.maintain()
        assert len(om.positions) == 3 and om.risk.max_loss_hits == 0 and not om.risk.breaker_tripped
        for sym in ("AAA", "BBB", "CCC"):
            set_chain(broker, 2.00, 1.30, symbol=sym)                      # 0.70: recovered (a winner past the target)
        om.maintain()
        assert om.risk.max_loss_hits == 0 and not om.risk.breaker_tripped

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

    def test_expiry_at_the_max_loss_line_is_a_realized_hit_and_expiry_above_it_is_not(self, env):
        clock, broker, log, om, tmp = env
        pos = self._expired(env)
        pos["current_price"], pos["unrealized_pl"] = 4.70, -620.0           # last mark before expiry: past 4.66 (90% of max loss)
        broker.positions = []
        om.maintain()
        assert om.positions == [] and om.risk.max_loss_hits == 1

    def test_expiry_with_a_last_mark_short_of_the_line_does_not_count(self, env):
        clock, broker, log, om, tmp = env
        pos = self._expired(env)
        pos["current_price"], pos["unrealized_pl"] = 2.50, -180.0
        broker.positions = []
        om.maintain()
        assert om.positions == [] and om.risk.max_loss_hits == 0

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


# =============================================================================================
# Manual overrides: the owner closes / cancels / edits things by hand in thinkorswim and the bot
# must follow the broker's reality instead of fighting it. `broker.positions` (a plain list) and
# `broker.orders` are edited directly below to simulate what happens in TOS.
# =============================================================================================


def events(tmp, kind=None):
    out = []
    for path in sorted(tmp.glob("session_*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in out if kind is None or e["type"] == kind]


def open_position(env, credit=1.60, qty=2, aged=True, symbol="XYZ"):
    """Entry filled and tracked. `aged` moves the clock past EXTERNAL_SYNC_MIN_AGE_SEC so the sync looks at it."""
    clock, broker, log, om, tmp = env
    oid = om.submit_entry(spec(credit, symbol=symbol), qty)
    broker.fill_order(oid, credit)
    om.poll_entries()
    if aged:
        clock.advance(seconds=config_45dte.EXTERNAL_SYNC_MIN_AGE_SEC + 10)
    return next(p for p in om.positions if p["broker_symbol"] == symbol)


def drop_leg(broker, strike):
    broker.positions = [p for p in broker.positions if p["strike"] != strike]


def set_held(broker, strike, qty):
    next(p for p in broker.positions if p["strike"] == strike)["qty"] = qty


def cycles(clock, om, n=1, seconds=60):
    for _ in range(n):
        clock.advance(seconds=seconds)
        om.maintain()


def calls(broker, name, since=0):
    return [c for c in broker.calls[since:] if c[0] == name]


def dte_7(clock):
    clock.now = datetime.combine(EXP - timedelta(days=7), datetime.min.time(), tzinfo=ET).replace(hour=10)


class TestExternalSync:
    def test_a_single_absent_cycle_does_nothing(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        close_id = pos["close_order_id"]
        saved = [dict(p) for p in broker.positions]
        broker.positions = []                    # closed by hand in TOS... seen once (a lagging read looks the same)
        n = len(broker.calls)
        cycles(clock, om)
        assert len(om.positions) == 1 and pos["ext_obs"]["n"] == 1
        assert broker.orders[close_id]["status"] == "accepted"
        assert not calls(broker, "cancel_order", n) and not calls(broker, "place_close_spread", n)
        broker.positions = saved                 # the next read shows the legs again: the counter resets
        cycles(clock, om)
        assert "ext_obs" not in pos and len(om.positions) == 1
        broker.positions = []
        cycles(clock, om)                        # absent once more, but the count restarted from zero
        assert len(om.positions) == 1 and pos["ext_obs"]["n"] == 1 and log.counts["EXTERNAL_CLOSE"] == 0

    def test_both_legs_gone_for_the_confirm_cycles_books_a_manual_close(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        set_chain(broker, 2.50, 1.20)            # mark 1.30
        cycles(clock, om)
        assert pos["current_price"] == 1.30
        close_id = pos["close_order_id"]
        broker.positions = []
        n = len(broker.calls)
        cycles(clock, om)
        assert len(om.positions) == 1            # first absent read: only an observation
        cycles(clock, om)
        assert om.positions == []
        closed = om.state["closed"][-1]
        assert closed["close_reason"] == "closed outside the bot (manual)"
        assert closed["exit_debit"] is None and closed["realized_pl"] is None
        assert closed["last_price_before_exit"] == 1.30
        assert closed["est_realized_pl"] == pytest.approx((1.60 - 1.30) * 100 * 2)
        assert broker.orders[close_id]["status"] == "canceled"
        assert log.counts["EXTERNAL_CLOSE"] == 1 and log.counts["CLOSE_ERROR"] == 0 and om.risk.max_loss_hits == 0
        assert not calls(broker, "close_spread_at_market", n) and not calls(broker, "place_close_spread", n)
        assert json.loads((tmp / "state.json").read_text())["positions"] == {}
        cycles(clock, om)                        # nothing left to look at: no ghost, no errors
        assert log.counts["CLOSE_ERROR"] == 0 and om.blocked_symbols == set()

    def test_a_ghost_cannot_feed_the_breaker_through_a_max_loss_mark(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, credit=1.50, qty=1)
        broker.positions = []
        set_chain(broker, 6.00, 1.30)            # the quote now says max loss (4.70) for a spread that no longer exists
        cycles(clock, om, 3)
        assert om.positions == [] and om.risk.max_loss_hits == 0 and not om.risk.breaker_tripped
        assert om.state["closed"][-1]["exit_debit"] is None
        assert not calls(broker, "close_spread_at_market")

    def test_a_fresh_position_is_not_checked_until_the_min_age(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, aged=False)
        broker.positions = []
        n = len(broker.calls)
        cycles(clock, om, 3, seconds=30)         # 30 / 60 / 90 seconds old: too young to judge
        assert len(om.positions) == 1 and not calls(broker, "get_positions", n)
        cycles(clock, om, 1, seconds=40)         # 130 s: first observation
        assert len(om.positions) == 1 and pos["ext_obs"]["n"] == 1
        cycles(clock, om)
        assert om.positions == []

    def test_a_fresh_fill_restarts_the_min_age(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 4)
        broker.fill_order(oid, 1.60, qty=2)
        om.poll_entries()
        clock.advance(seconds=600)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        pos = om.positions[0]
        assert pos["qty"] == 4
        set_held(broker, 95.0, -2)               # the positions API still shows only the first two
        set_held(broker, 90.0, 2)
        cycles(clock, om, 3, seconds=30)         # < 120 s since the second fill: not judged
        assert pos["qty"] == 4 and log.counts["EXTERNAL_PARTIAL_CLOSE"] == 0
        set_held(broker, 95.0, -4)               # and the API catches up
        set_held(broker, 90.0, 4)
        cycles(clock, om, 4)
        assert pos["qty"] == 4 and log.counts["EXTERNAL_PARTIAL_CLOSE"] == 0

    def test_a_failed_positions_read_is_logged_and_never_acted_on(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.positions = []
        broker.fail_on.add("get_positions")
        cycles(clock, om, 3)
        assert len(om.positions) == 1 and log.counts["POLL_ERROR"] >= 3 and "ext_obs" not in pos
        broker.fail_on.discard("get_positions")
        cycles(clock, om)                        # first good read: an observation, nothing more
        assert len(om.positions) == 1
        cycles(clock, om)
        assert om.positions == []

    def test_the_observation_counter_survives_a_restart(self, env):
        clock, broker, log, om, tmp = env
        open_position(env)
        broker.positions = []
        cycles(clock, om)
        om2 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        assert om2.positions[0]["ext_obs"]["n"] == 1
        clock.advance(seconds=60)
        om2.maintain()
        assert om2.positions == [] and om2.state["closed"][-1]["close_reason"] == "closed outside the bot (manual)"

    def test_disabled_sync_never_reads_positions_or_acts(self, env, monkeypatch):
        clock, broker, log, om, tmp = env
        monkeypatch.setattr(config_45dte, "EXTERNAL_SYNC_ENABLED", False)
        pos = open_position(env)
        broker.positions = []
        n = len(broker.calls)
        cycles(clock, om, 4)
        assert len(om.positions) == 1 and not calls(broker, "get_positions", n) and "ext_obs" not in pos

    def test_dry_run_brokers_are_never_synced(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.dry_run = True                    # a dry-run broker holds no real legs, so absence means nothing
        broker.positions = []
        n = len(broker.calls)
        cycles(clock, om, 4)
        assert len(om.positions) == 1 and not calls(broker, "get_positions", n)

    def test_the_bots_own_gtc_fill_is_never_booked_as_a_manual_close(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.fill_order(pos["close_order_id"], 0.80)   # our profit target filled: legs gone
        om.sync_external()
        om.sync_external()                       # confirmed absent -- but the close order explains it
        assert om.positions == [] and om.state["closed"][-1]["close_reason"] == "profit target"
        assert log.counts["EXTERNAL_CLOSE"] == 0 and om.state["manual_cooldown_until"] is None

    def test_manual_partial_close_shrinks_and_replaces_the_gtc_at_the_new_size(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=4)
        old = pos["close_order_id"]
        set_held(broker, 95.0, -2)               # the owner closed 2 of the 4 contracts
        set_held(broker, 90.0, 2)
        cycles(clock, om)
        assert pos["qty"] == 4                   # one observation: not yet
        cycles(clock, om)
        assert pos["qty"] == 2 and pos["external_closed_qty"] == 2 and len(om.positions) == 1
        assert broker.orders[old]["status"] == "canceled"
        new = broker.orders[pos["close_order_id"]]
        assert new["id"] != old and new["qty"] == 2 and new["limit_price"] == 0.80 and new["status"] == "accepted"
        assert log.counts["EXTERNAL_PARTIAL_CLOSE"] == 1 and pos["max_loss"] == 3.40
        assert om.open_risk_dollars() == pytest.approx(2 * 3.40 * 100)
        n = len(broker.calls)
        cycles(clock, om, 3)                     # stable afterwards
        assert pos["close_order_id"] == new["id"] and not calls(broker, "place_close_spread", n)
        assert log.counts["EXTERNAL_PARTIAL_CLOSE"] == 1
        assert om.state["manual_cooldown_until"] is None   # a partial close is not a full close

    def test_a_later_fill_of_the_same_entry_does_not_resurrect_manually_closed_contracts(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 4)
        broker.fill_order(oid, 1.60, qty=2)
        om.poll_entries()
        pos = om.positions[0]
        clock.advance(seconds=200)
        set_held(broker, 95.0, -1)               # the owner closes one of the two filled contracts
        set_held(broker, 90.0, 1)
        cycles(clock, om, 2)
        assert pos["qty"] == 1
        broker.fill_order(oid, 1.60)             # the other two fill later: held is 1 + 2
        om.poll_entries()
        assert pos["qty"] == 3

    def test_the_bots_own_gtc_part_filling_is_not_taken_for_a_manual_close(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        close_id = pos["close_order_id"]
        broker.fill_order(close_id, 0.80, qty=1)        # our GTC part-fills: 1 of 2 contracts is out
        cycles(clock, om, 3)
        assert pos["qty"] == 2 and log.counts["EXTERNAL_PARTIAL_CLOSE"] == 0
        assert pos["close_order_id"] == close_id and broker.orders[close_id]["status"] == "partially_filled"

    def test_a_naked_leg_freezes_all_order_activity_until_the_legs_are_consistent(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, credit=1.50, qty=1)
        old = pos["close_order_id"]
        drop_leg(broker, 90.0)                   # the owner closed the long by hand: only the short is left
        cycles(clock, om)
        assert not pos.get("manual_attention")   # one observation is not enough
        cycles(clock, om)
        assert pos["manual_attention"] is True
        alert = events(tmp, "NAKED_LEG")
        assert len(alert) == 1 and alert[0]["kind"] == "naked" and alert[0]["short_held"] == 1 and alert[0]["long_held"] == 0
        assert "MANUAL ATTENTION" in om.position_report(25_000.0)

        n = len(broker.calls)
        broker.orders[old]["status"] = "rejected"        # normally re-created within a cycle
        set_chain(broker, 6.00, 1.30)                    # and this mark says max loss
        cycles(clock, om, 3)
        for name in ("place_close_spread", "close_spread_at_market", "cancel_order", "replace_order_price"):
            assert not calls(broker, name, n), name
        assert pos["max_loss_hit"] is False and om.risk.max_loss_hits == 0 and len(om.positions) == 1
        assert len(events(tmp, "NAKED_LEG")) == 1        # same day: no re-alert
        clock.advance(days=1)
        cycles(clock, om)
        assert len(events(tmp, "NAKED_LEG")) == 2        # re-alerts once the next day

        broker.add_position("XYZ", EXP, "P", 90.0, 1, 0.50)   # the owner puts the long back
        cycles(clock, om)
        assert pos["manual_attention"] is True           # consistent once: still frozen
        cycles(clock, om)
        assert len(events(tmp, "MANUAL_ATTENTION_CLEARED")) == 1
        # managed normally again (original rules: the rejected GTC is re-created, then the max-loss stop fires)
        assert om.positions == [] and "max loss" in om.state["closed"][-1]["close_reason"]
        assert calls(broker, "close_spread_at_market", n)

    def test_a_frozen_spread_whose_other_leg_then_goes_is_booked_as_closed(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=1)
        drop_leg(broker, 90.0)
        cycles(clock, om, 2)
        assert pos["manual_attention"] is True
        drop_leg(broker, 95.0)
        cycles(clock, om)
        assert len(om.positions) == 1
        cycles(clock, om)
        assert om.positions == [] and om.state["closed"][-1]["close_reason"] == "closed outside the bot (manual)"
        assert not events(tmp, "MANUAL_ATTENTION_CLEARED")

    def test_a_naked_short_leg_alone_is_just_as_frozen(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=1)
        drop_leg(broker, 95.0)                   # the short was bought back by hand, the long is left
        cycles(clock, om, 2)
        assert pos["manual_attention"] is True and events(tmp, "NAKED_LEG")[0]["short_held"] == 0

    def test_unbalanced_legs_are_frozen_not_shrunk_into_an_unmanaged_naked_contract(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        set_held(broker, 90.0, 1)                # one of the two long contracts is gone; the short is still 2
        cycles(clock, om, 2)
        assert pos["manual_attention"] is True and pos["qty"] == 2
        assert events(tmp, "NAKED_LEG")[0]["kind"] == "unbalanced" and log.counts["EXTERNAL_PARTIAL_CLOSE"] == 0

    def test_extra_owner_contracts_on_the_same_strikes_are_left_alone(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        set_held(broker, 95.0, -5)
        set_held(broker, 90.0, 5)
        n = len(broker.calls)
        cycles(clock, om, 4)
        assert pos["qty"] == 2 and len(om.positions) == 1 and not pos.get("manual_attention")
        assert len(events(tmp, "EXTERNAL_EXTRA_CONTRACTS")) == 1
        for name in ("place_close_spread", "cancel_order", "close_spread_at_market"):
            assert not calls(broker, name, n), name

    def test_extra_owner_contracts_do_not_block_an_exit_and_are_not_closed(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        set_held(broker, 95.0, -5)
        set_held(broker, 90.0, 5)
        set_chain(broker, 1.00, 0.60)
        dte_7(clock)
        om.maintain()
        assert om.positions == []
        closes = calls(broker, "close_spread_at_market")
        assert len(closes) == 1 and closes[0][1][5] == 2             # only our 2 contracts
        assert {p["strike"]: p["qty"] for p in broker.positions} == {95.0: -3, 90.0: 3}
        assert log.counts["EXIT_VERIFY_FAILED"] == 0 and "DTE 7" in om.state["closed"][-1]["close_reason"]

    def test_exit_with_both_legs_already_gone_books_a_manual_close_and_sends_nothing(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        close_id = pos["close_order_id"]
        pos["current_price"], pos["unrealized_pl"] = 1.30, 60.0
        broker.positions = []
        n = len(broker.calls)
        assert om.exit_at_market(pos, "test exit") is True
        assert not calls(broker, "close_spread_at_market", n) and not calls(broker, "place_close_spread", n)
        closed = om.state["closed"][-1]
        assert closed["close_reason"] == "closed outside the bot (manual)" and closed["exit_debit"] is None
        assert closed["last_price_before_exit"] == 1.30 and closed["est_realized_pl"] == 60.0
        assert broker.orders[close_id]["status"] == "canceled"
        assert om.positions == [] and om.risk.max_loss_hits == 0
        assert log.counts["EXIT_SKIPPED"] == 1 and log.counts["EXTERNAL_CLOSE"] == 1

    def test_the_exit_preflight_catches_it_through_maintain_when_the_sync_is_off(self, env, monkeypatch):
        clock, broker, log, om, tmp = env
        monkeypatch.setattr(config_45dte, "EXTERNAL_SYNC_ENABLED", False)
        pos = open_position(env)
        set_chain(broker, 1.00, 0.60)
        broker.positions = []
        dte_7(clock)
        om.maintain()
        assert om.positions == [] and not calls(broker, "close_spread_at_market")
        assert om.state["closed"][-1]["close_reason"] == "closed outside the bot (manual)"

    def test_exit_with_one_leg_gone_freezes_and_sends_nothing(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        drop_leg(broker, 90.0)
        n = len(broker.calls)
        assert om.exit_at_market(pos, "test exit") is False
        assert pos["manual_attention"] is True and len(om.positions) == 1 and log.counts["NAKED_LEG"] == 1
        assert om.exit_at_market(pos, "again") is False    # frozen: refuses outright
        for name in ("close_spread_at_market", "place_close_spread", "cancel_order"):
            assert not calls(broker, name, n), name

    def test_exit_does_not_trust_a_missing_read_on_a_brand_new_position(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, aged=False)
        broker.positions = []
        n = len(broker.calls)
        assert om.exit_at_market(pos, "test exit") is False
        assert len(om.positions) == 1 and log.counts["EXIT_DEFERRED"] == 1
        assert not calls(broker, "close_spread_at_market", n) and not calls(broker, "cancel_order", n)

    def test_exit_closes_only_the_contracts_still_held(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=4)
        set_held(broker, 95.0, -2)               # the owner already closed 2 of the 4
        set_held(broker, 90.0, 2)
        set_chain(broker, 1.00, 0.60)            # price 0.40
        assert om.exit_at_market(pos, "test exit") is True
        assert calls(broker, "close_spread_at_market")[0][1][5] == 2
        closed = om.state["closed"][-1]
        assert closed["qty"] == 2 and closed["realized_pl"] == pytest.approx((1.60 - 0.40) * 100 * 2)
        assert broker.positions == []

    def test_exit_is_not_booked_when_the_held_count_does_not_drop(self, env, monkeypatch):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2)
        monkeypatch.setattr(broker, "close_spread_at_market",
                            lambda *a, **k: {"id": "X1", "status": "filled", "filled_avg_price": 1.0})
        assert om.exit_at_market(pos, "test exit") is False
        assert len(om.positions) == 1 and log.counts["EXIT_VERIFY_FAILED"] == 1 and pos["close_order_id"] is None

    def test_exit_does_not_send_when_the_preflight_read_fails(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        close_id = pos["close_order_id"]
        broker.fail_on.add("get_positions")
        assert om.exit_at_market(pos, "test exit") is False
        assert not calls(broker, "close_spread_at_market") and broker.orders[close_id]["status"] == "accepted"
        assert len(om.positions) == 1

    def test_dotted_tickers_the_broker_spells_differently_are_not_mistaken_for_closed(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, symbol="BRK.B")
        for p in broker.positions:               # the broker reports the OCC root without the dot
            p["symbol"] = p["symbol"].replace("BRK.B", "BRKB")
            p["underlying"] = "BRKB"
        cycles(clock, om, 4)
        assert len(om.positions) == 1 and log.counts["EXTERNAL_CLOSE"] == 0 and "ext_obs" not in pos


class TestOwnerCloseOrderChanges:
    def test_owner_cancel_sets_manual_hold_and_the_gtc_is_never_recreated(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        old = pos["close_order_id"]
        broker.cancel_order(old)                 # the owner cancels it in thinkorswim
        n = len(broker.calls)
        cycles(clock, om, 4, seconds=10)
        assert pos["manual_hold"] is True and pos["close_order_id"] is None
        assert len(events(tmp, "CLOSE_ORDER_CANCELED_BY_OWNER")) == 1
        assert not calls(broker, "place_close_spread", n) and len(close_orders(broker)) == 1
        assert len(om.positions) == 1 and "canceled by owner" in om.position_report(25_000.0)

    def test_the_profit_floor_trail_keeps_running_while_the_gtc_is_held_off(self, env, trail_rules):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.cancel_order(pos["close_order_id"])
        set_chain(broker, 1.50, 0.30)            # 1.20: arms
        om.maintain()
        assert pos["manual_hold"] is True and pos["profit_floor_armed"] is True
        set_chain(broker, 1.55, 0.30)            # 1.25: first nickel back -> exit
        om.maintain()
        assert om.positions == [] and "profit floor trail" in om.state["closed"][-1]["close_reason"]

    def test_an_owner_edit_replaced_plus_a_new_open_order_is_adopted(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        old = pos["close_order_id"]
        edited = broker.replace_order_price(old, 0.95)   # what an edit in TOS looks like: old 'replaced', new id
        n = len(broker.calls)
        cycles(clock, om)
        assert broker.orders[old]["status"] == "replaced"
        assert pos["close_order_id"] == edited["id"] and pos["close_limit"] == 0.95 and not pos.get("manual_hold")
        assert log.counts["CLOSE_ORDER_ADOPTED"] == 1 and not calls(broker, "place_close_spread", n)
        cycles(clock, om, 2)
        assert pos["close_order_id"] == edited["id"] and len(close_orders(broker)) == 2   # nothing new created
        broker.fill_order(edited["id"], 0.95)    # and the owner's order is the one that is tracked to its fill
        cycles(clock, om)
        assert om.positions == [] and om.state["closed"][-1]["exit_debit"] == 0.95

    def test_replaced_with_no_replacement_open_means_hold(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        old = pos["close_order_id"]
        broker.orders[old]["status"] = "replaced"
        n = len(broker.calls)
        cycles(clock, om, 2)
        assert pos["manual_hold"] is True and not calls(broker, "place_close_spread", n)

    @pytest.mark.parametrize("status", ["rejected", "expired", "unknown"])
    def test_a_rejected_expired_or_unknown_gtc_is_still_recreated(self, env, status):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        old = pos["close_order_id"]
        broker.orders[old]["status"] = status
        cycles(clock, om)
        assert pos["close_order_id"] != old and broker.orders[pos["close_order_id"]]["status"] == "accepted"
        assert not pos.get("manual_hold") and log.counts["CLOSE_ORDER_LOST"] == 1

    def test_a_failed_open_orders_read_decides_nothing(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        old = pos["close_order_id"]
        broker.cancel_order(old)
        broker.fail_on.add("get_open_orders")
        n = len(broker.calls)
        cycles(clock, om)
        assert not pos.get("manual_hold") and pos["close_order_id"] == old and not calls(broker, "place_close_spread", n)
        assert log.counts["POLL_ERROR"] >= 1
        broker.fail_on.discard("get_open_orders")
        cycles(clock, om)
        assert pos["manual_hold"] is True

    def test_a_new_fill_resets_the_hold_and_places_the_close_for_the_new_size(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 4)
        broker.fill_order(oid, 1.60, qty=1)
        om.poll_entries()
        pos = om.positions[0]
        broker.cancel_order(pos["close_order_id"])
        om.maintain()
        assert pos["manual_hold"] is True
        broker.fill_order(oid, 1.60)             # the rest of the entry fills
        om.poll_entries()
        assert "manual_hold" not in pos and pos["qty"] == 4 and log.counts["MANUAL_HOLD_CLEARED"] == 1
        new = broker.orders[pos["close_order_id"]]
        assert new["qty"] == 4 and new["status"] == "accepted"

    def test_a_manual_partial_close_does_not_put_back_a_gtc_the_owner_removed(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=4)
        broker.cancel_order(pos["close_order_id"])
        cycles(clock, om)
        assert pos["manual_hold"] is True
        set_held(broker, 95.0, -2)
        set_held(broker, 90.0, 2)
        n = len(broker.calls)
        cycles(clock, om, 2)
        assert pos["qty"] == 2 and pos["manual_hold"] is True and not calls(broker, "place_close_spread", n)

    def test_manual_hold_survives_a_restart(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.cancel_order(pos["close_order_id"])
        cycles(clock, om)
        om2 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        assert om2.positions[0]["manual_hold"] is True
        n = len(broker.calls)
        clock.advance(seconds=60)
        om2.maintain()
        assert not calls(broker, "place_close_spread", n)

    def test_a_failed_replacement_after_our_own_cancel_is_not_mistaken_for_an_owner_cancel(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60, qty=1)
        om.poll_entries()
        pos = om.positions[0]
        broker.fail_on.add("place_close_spread")
        broker.fill_order(oid, 1.60)             # the position grows: we cancel the old close, the new one fails
        om.poll_entries()
        assert pos["close_order_id"] is None and log.counts["CLOSE_ERROR"] == 1
        broker.fail_on.discard("place_close_spread")
        om.maintain()
        assert not pos.get("manual_hold")
        assert broker.orders[pos["close_order_id"]]["qty"] == 2 and broker.orders[pos["close_order_id"]]["status"] == "accepted"


class TestAdoptWithManualOverrides:
    def test_a_vanished_position_cancels_its_leftover_close_order(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, symbol="AAPL")
        old = pos["close_order_id"]
        broker.positions = []                    # the legs are gone (closed by hand while the bot was off)
        om.adopt()
        assert om.positions == [] and om.state["closed"][-1]["close_reason"] == "vanished at broker"
        assert broker.orders[old]["status"] == "canceled" and om.working_entries == []

    def test_a_vanished_positions_already_dead_close_order_is_not_cancelled_again(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, symbol="AAPL")
        broker.cancel_order(pos["close_order_id"])
        broker.positions = []
        n = len(broker.calls)
        om.adopt()
        assert om.positions == [] and not calls(broker, "cancel_order", n)

    def test_a_closing_order_is_never_adopted_as_an_entry(self, env):
        clock, broker, log, om, tmp = env
        put_close = broker.place_close_spread("AAPL", EXP, "P", 95.0, 90.0, 2, limit_debit=0.80)    # buy 95P, sell 90P
        call_close = broker.place_close_spread("AAPL", EXP, "C", 100.0, 105.0, 2, limit_debit=0.80)  # buy 100C, sell 105C
        call_entry = broker.place_credit_spread("AAPL", EXP, "C", 100.0, 105.0, 1, limit_credit=1.55, time_in_force="day")
        om.adopt()
        assert [e["order_id"] for e in om.working_entries] == [call_entry["id"]]   # the real credit spread is still adopted
        assert {put_close["id"], call_close["id"]} <= {e["order_id"] for e in events(tmp, "UNKNOWN_OPEN_ORDER")}

    def test_a_tracked_spread_next_to_an_owner_leg_is_not_written_off_as_vanished(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=2, aged=False)
        close_id = pos["close_order_id"]
        broker.add_position("XYZ", EXP, "P", 85.0, -1, 0.40)   # the owner's own leg: same underlying/expiry/right
        summary = om.adopt()
        assert len(om.positions) == 1 and om.state["closed"] == []
        assert om.blocked_symbols == {"XYZ"} and summary["unpaired"] == ["XYZ"]
        assert broker.orders[close_id]["status"] == "accepted"

    def test_a_tracked_spread_that_lost_a_leg_while_offline_stays_tracked_and_is_flagged(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=1)
        drop_leg(broker, 90.0)
        om.adopt()
        assert len(om.positions) == 1 and om.state["closed"] == [] and om.blocked_symbols == {"XYZ"}
        cycles(clock, om, 2)
        assert pos["manual_attention"] is True


class TestManualCloseCooldown:
    def test_starts_on_an_external_close_and_ends_at_the_configured_time(self, env):
        clock, broker, log, om, tmp = env
        open_position(env)
        assert om.state["manual_cooldown_until"] is None and not om.entries_paused()
        broker.positions = []
        cycles(clock, om, 2)
        booked = clock.now
        assert om.state["closed"][-1]["close_reason"] == "closed outside the bot (manual)"
        assert datetime.fromisoformat(om.state["manual_cooldown_until"]) == booked + timedelta(seconds=120)
        assert om.entries_paused() and om.entries_paused(booked + timedelta(seconds=119))
        assert not om.entries_paused(booked + timedelta(seconds=120)) and not om.entries_paused(booked + timedelta(seconds=300))
        assert om.blocked_symbols == set() and om.positions == []   # nothing left behind to block the symbol

    def test_the_exit_preflight_booking_starts_it_too(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env)
        broker.positions = []
        assert om.exit_at_market(pos, "test exit") is True
        assert om.entries_paused()

    def test_survives_a_restart_and_old_state_files_load(self, env):
        clock, broker, log, om, tmp = env
        open_position(env)
        broker.positions = []
        cycles(clock, om, 2)
        om2 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        assert om2.entries_paused()
        state = json.loads((tmp / "state.json").read_text())
        state.pop("manual_cooldown_until")
        (tmp / "state.json").write_text(json.dumps(state))
        om3 = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp / "state.json")
        assert not om3.entries_paused() and om3.state["manual_cooldown_until"] is None

    def test_not_triggered_by_the_bots_own_exits(self, env):
        clock, broker, log, om, tmp = env
        open_position(env)
        set_chain(broker, 1.00, 0.60)
        dte_7(clock)
        om.maintain()                            # DTE exit at market
        assert om.positions == [] and om.state["manual_cooldown_until"] is None
        pos = open_position(env, symbol="ABC")
        broker.fill_order(pos["close_order_id"], 0.80)   # profit target fills
        om.maintain()
        assert om.positions == [] and om.state["manual_cooldown_until"] is None

    def test_not_triggered_by_partial_closes_naked_legs_or_vanished_bookings(self, env):
        clock, broker, log, om, tmp = env
        pos = open_position(env, qty=4)
        set_held(broker, 95.0, -2)
        set_held(broker, 90.0, 2)
        cycles(clock, om, 2)
        assert pos["qty"] == 2 and om.state["manual_cooldown_until"] is None
        drop_leg(broker, 90.0)
        cycles(clock, om, 2)
        assert pos["manual_attention"] is True and om.state["manual_cooldown_until"] is None
        broker.positions = []
        om.adopt()                               # startup reconciliation: "vanished at broker"
        assert om.positions == [] and om.state["manual_cooldown_until"] is None


def test_position_report_renders(env):
    clock, broker, log, om, tmp = env
    oid = om.submit_entry(spec(1.60), 2)
    broker.fill_order(oid, 1.60)
    om.poll_entries()
    set_chain(broker, 2.0, 1.0)
    om.maintain()
    text = om.position_report(25_000.0)
    assert "XYZ" in text and "95/90" in text and "portfolio risk $680" in text



class TestExitPreflightNeedsTwoAgreeingReads:
    """An empty/stale positions response at the moment an exit fires must not make the bot drop a live position."""

    class Flaky(FakeBroker):
        script: list = []

        def get_positions(self):
            if self.script:
                return self.script.pop(0)
            return super().get_positions()

    def _armed_position(self, tmp_path):
        clock = Clock(datetime(2026, 9, 17, 10, 0, tzinfo=ET))
        broker = self.Flaky(now=clock.now)
        broker.script = []
        log = TradingLogger(tmp_path, now_fn=clock, echo=False)
        om = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp_path / "state.json")
        om.roll_day(25_000.0)
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        clock.advance(hours=1)                                   # old enough to be past the minimum age
        return clock, broker, om

    def test_one_empty_read_does_not_book_the_position_as_closed(self, tmp_path):
        clock, broker, om = self._armed_position(tmp_path)
        pos = om.positions[0]
        broker.script = [[]]                                     # the first read is empty, the next is the truth
        assert om.exit_at_market(pos, "test exit") is False      # deferred, nothing booked, nothing sent
        assert len(om.positions) == 1 and not any(c[0] == "close_spread_at_market" for c in broker.calls)
        assert om.exit_at_market(pos, "test exit") is True       # next cycle the truth is read and it closes normally
        assert om.positions == [] and "closed outside" not in om.state["closed"][-1]["close_reason"]

    def test_two_empty_reads_do_book_it_as_closed_by_hand(self, tmp_path):
        clock, broker, om = self._armed_position(tmp_path)
        pos = om.positions[0]
        broker.script = [[], []]
        assert om.exit_at_market(pos, "test exit") is True
        assert om.positions == [] and "manual" in om.state["closed"][-1]["close_reason"]
        assert not any(c[0] == "close_spread_at_market" for c in broker.calls)



class TestOfficialExitPrice:
    """EXIT_PNL_BASIS "prev_tick": the official P/L is the mark one tick before the trigger tick (as in the 0DTE
    bots); the broker's real fill is kept beside it."""

    @pytest.fixture(autouse=True)
    def _tick_basis(self, monkeypatch):
        monkeypatch.setattr(config_45dte, "EXIT_PNL_BASIS", "prev_tick")

    @staticmethod
    def _fill_at(broker, price):
        """Make every market close fill at `price` (a paper fill far from the marks)."""
        orig = broker.close_spread_at_market

        def worse(*args, **kwargs):
            order = orig(*args, **kwargs)
            order["filled_avg_price"] = price
            return order
        broker.close_spread_at_market = worse

    def _armed_trail_position(self, env, trail_rules_applied=True):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 2)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        set_chain(broker, 1.50, 0.30)         # 1.20 -> armed at the best
        om.maintain()
        return om.positions[0]

    def test_a_trail_exit_books_at_the_prior_tick_not_the_adverse_fill(self, env, trail_rules):
        clock, broker, log, om, tmp = env
        self._armed_trail_position(env)
        self._fill_at(broker, 1.95)           # the paper fill lands 0.70 away from the mark
        clock.advance(seconds=60)
        set_chain(broker, 1.55, 0.30)         # 1.25: the first nickel against -> trail exit fires
        om.maintain()
        assert om.positions == []
        rec = om.state["closed"][-1]
        assert rec["exit_debit"] == 1.20 and rec["exit_basis"] == "prev_tick"
        assert rec["realized_pl"] == pytest.approx((1.60 - 1.20) * 100 * 2)          # +80 official
        assert rec["fill_debit"] == 1.95 and rec["realized_pl_actual_fill"] == pytest.approx((1.60 - 1.95) * 100 * 2)
        assert rec["trigger_price"] == 1.25 and rec["prev_tick_price"] == 1.20 and rec["exit_slippage"] == 0.70
        assert om.realized_today_total() == pytest.approx(80.0)

    def test_the_close_event_and_trades_csv_carry_both_figures(self, env, trail_rules):
        clock, broker, log, om, tmp = env
        self._armed_trail_position(env)
        self._fill_at(broker, 1.95)
        clock.advance(seconds=60)
        set_chain(broker, 1.55, 0.30)
        om.maintain()
        events = [json.loads(line) for line in (tmp / "session_2026-09-17.jsonl").read_text().splitlines()]
        closed = next(e for e in events if e["type"] == "POSITION_CLOSED")
        assert closed["realized_pl"] == 80.0 and closed["realized_pl_actual_fill"] == -70.0
        assert "actual fill 1.95 would have booked -70.00" in closed["message"]
        import csv
        rows = list(csv.DictReader((tmp / "trades_2026-09-17.csv").open()))
        close = next(r for r in rows if r["action"] == "CLOSE")
        assert float(close["realized_pl"]) == 80.0 and float(close["realized_pl_actual_fill"]) == -70.0
        assert float(close["fill_debit"]) == 1.95 and float(close["prev_tick_price"]) == 1.20
        assert "P&L +80.00 [fill 1.95 = -70.00]" in om.position_report(25_000.0)

    def test_no_prior_tick_falls_back_to_the_trigger_mark(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        pos = om.positions[0]
        set_chain(broker, 1.50, 0.30)
        om.refresh_price(pos)                 # the very first tick: nothing before it
        self._fill_at(broker, 1.90)
        assert om.exit_at_market(pos, "test") is True
        rec = om.state["closed"][-1]
        assert rec["exit_debit"] == 1.20 and rec["exit_basis"] == "trigger_mark" and rec["prev_tick_price"] is None
        assert rec["realized_pl_actual_fill"] == pytest.approx((1.60 - 1.90) * 100)

    def test_a_stale_prior_tick_is_not_used(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        pos = om.positions[0]
        set_chain(broker, 1.50, 0.30)
        om.refresh_price(pos)                                  # 1.20, yesterday's last tick
        clock.advance(hours=17)
        set_chain(broker, 1.40, 0.30)
        om.refresh_price(pos)                                  # 1.10, first tick of the day
        assert pos["prev_price"] == 1.20
        assert om.exit_at_market(pos, "test") is True
        rec = om.state["closed"][-1]
        assert rec["prev_tick_price"] is None and rec["exit_debit"] == 1.10 and rec["exit_basis"] == "trigger_mark"

    def test_a_stale_trigger_mark_falls_back_to_the_fill(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        pos = om.positions[0]
        set_chain(broker, 1.50, 0.30)
        om.refresh_price(pos)
        om.refresh_price(pos)
        clock.advance(hours=1)                                 # no refresh since: the marks say nothing about now
        self._fill_at(broker, 1.90)
        assert om.exit_at_market(pos, "test") is True
        rec = om.state["closed"][-1]
        assert rec["exit_debit"] == 1.90 and rec["exit_basis"] == "fill" and rec["trigger_price"] is None

    def test_the_fill_basis_keeps_the_brokers_fill_as_official(self, env, trail_rules, monkeypatch):
        monkeypatch.setattr(config_45dte, "EXIT_PNL_BASIS", "fill")
        clock, broker, log, om, tmp = env
        self._armed_trail_position(env)
        self._fill_at(broker, 1.95)
        clock.advance(seconds=60)
        set_chain(broker, 1.55, 0.30)
        om.maintain()
        rec = om.state["closed"][-1]
        assert rec["exit_debit"] == 1.95 and rec["realized_pl"] == pytest.approx((1.60 - 1.95) * 100 * 2)
        assert rec["exit_basis"] == "fill" and rec["realized_pl_actual_fill"] == rec["realized_pl"]

    def test_a_resting_profit_target_fill_is_never_booked_worse_than_its_limit(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        om.maintain()
        pos = om.positions[0]
        limit = pos["close_limit"]
        broker.fill_order(pos["close_order_id"], limit + 0.60)    # a paper fill the limit order could never give
        om.maintain()
        rec = om.state["closed"][-1]
        assert rec["close_reason"] == "profit target" and rec["exit_basis"] == "limit"
        assert rec["exit_debit"] == limit and rec["fill_debit"] == pytest.approx(limit + 0.60)
        assert rec["realized_pl"] == pytest.approx((1.60 - limit) * 100)

    def test_a_resting_profit_target_fill_better_than_its_limit_keeps_the_better_price(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.60), 1)
        broker.fill_order(oid, 1.60)
        om.poll_entries()
        om.maintain()
        pos = om.positions[0]
        broker.fill_order(pos["close_order_id"], pos["close_limit"] - 0.10)
        om.maintain()
        rec = om.state["closed"][-1]
        assert rec["exit_debit"] == pytest.approx(pos["close_limit"] - 0.10) and rec["exit_basis"] == "fill"

    def test_a_max_loss_stop_still_feeds_the_breaker_on_the_tick_basis(self, env):
        clock, broker, log, om, tmp = env
        oid = om.submit_entry(spec(1.50), 1)
        broker.fill_order(oid, 1.50)
        om.poll_entries()
        set_chain(broker, 5.30, 1.30)         # 4.00: short of the 4.65 line
        om.maintain()
        assert om.risk.max_loss_hits == 0
        clock.advance(seconds=60)
        set_chain(broker, 6.00, 1.30)         # 4.70: the stop fires; the prior tick (4.00) is the official price
        om.maintain()
        rec = om.state["closed"][-1]
        assert om.positions == [] and rec["exit_debit"] == 4.00 and rec["trigger_price"] == 4.70
        assert om.risk.max_loss_hits == 1     # the decision mark was at the line: a realized max-loss stop

    def test_a_profit_exit_with_a_max_loss_sized_paper_fill_does_not_feed_the_breaker(self, env, trail_rules):
        clock, broker, log, om, tmp = env
        self._armed_trail_position(env)
        self._fill_at(broker, 4.90)           # a wild paper fill, far past the max-loss line
        clock.advance(seconds=60)
        set_chain(broker, 1.55, 0.30)
        om.maintain()
        rec = om.state["closed"][-1]
        assert rec["realized_pl"] > 0 > rec["realized_pl_actual_fill"]
        assert om.risk.max_loss_hits == 0

    def test_an_old_trades_csv_header_is_kept_for_the_rest_of_the_day(self, env, trail_rules):
        clock, broker, log, om, tmp = env
        old_header = "timestamp,action,spread_id,symbol,right,expiration,short_strike,long_strike,qty,entry_credit,exit_debit,realized_pl,reason,order_id"
        (tmp / "trades_2026-09-17.csv").write_text(old_header + "\n", encoding="utf-8")
        self._armed_trail_position(env)
        clock.advance(seconds=60)
        set_chain(broker, 1.55, 0.30)
        om.maintain()
        import csv
        rows = list((tmp / "trades_2026-09-17.csv").open(newline=""))
        parsed = list(csv.reader(rows))
        assert ",".join(parsed[0]) == old_header and len(parsed) > 1 and all(len(r) == 14 for r in parsed)

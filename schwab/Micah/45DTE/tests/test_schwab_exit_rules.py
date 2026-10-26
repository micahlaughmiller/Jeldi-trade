"""Schwab-only: the exit rules as Schwab's own (un-pinned) config_45dte.py actually sets them.

test_order_manager.py pins the rules so it means the same thing in both broker folders; this file does the
opposite and runs against the real Schwab config, so editing a value there (or reverting it to the Alpaca
defaults) fails here. The only exits are the GTC close at 50% of max profit and the 5-cent trail that arms
40% of the way to that target; an under-water position rides to the target or to expiration."""

from datetime import datetime, timedelta

import pytest

import config_45dte
from fake_broker import FakeBroker
from logger_system import TradingLogger
from order_manager import OrderManager
from test_order_manager import ET, EXP, Clock, set_chain, spec


@pytest.fixture
def env(tmp_path):
    clock = Clock(datetime(2026, 9, 17, 10, 0, tzinfo=ET))
    broker = FakeBroker(now=clock.now)
    log = TradingLogger(tmp_path, now_fn=clock, echo=False)
    om = OrderManager(broker, log, config_45dte, now_fn=clock, state_path=tmp_path / "state.json")
    om.roll_day(25_000.0)
    return clock, broker, om


def _open(env, credit=1.60, qty=2):
    clock, broker, om = env
    oid = om.submit_entry(spec(credit), qty)
    broker.fill_order(oid, credit)
    om.poll_entries()
    return om.positions[0]


def test_schwab_config_values():
    assert config_45dte.PROFIT_TARGET_PCT == 0.50
    assert config_45dte.EXIT_DTE_ENABLED is False and config_45dte.MAX_LOSS_EXIT is False
    assert config_45dte.PROFIT_FLOOR_ENABLED is True and config_45dte.PROFIT_FLOOR_MODE == "trail"
    assert config_45dte.PROFIT_FLOOR_ARM_PCT_OF_TARGET == 0.40 and config_45dte.PROFIT_FLOOR_TRAIL_TICK == 0.05
    assert config_45dte.EXIT_PNL_BASIS == "prev_tick"      # official P/L = the tick before the close, like the 0DTE bots


def test_arm_level_is_forty_percent_of_the_way_to_the_target(env):
    clock, broker, om = env
    assert om._profit_target(1.60) == 0.80
    assert om._profit_floor_arm_price(1.60) == 1.30      # 20% of max profit
    assert om._profit_floor_arm_price(0.80) == 0.65


def test_nickel_giveback_after_arming_banks_a_profit(env):
    clock, broker, om = env
    pos = _open(env)
    set_chain(broker, 1.50, 0.30)    # 1.20 -> armed
    om.maintain()
    set_chain(broker, 1.55, 0.30)    # 1.25 -> first nickel against
    om.maintain()
    assert om.positions == []
    rec = om.state["closed"][-1]
    assert rec["exit_basis"] == "prev_tick" and rec["exit_debit"] == 1.20           # the tick before the 1.25 trigger
    assert rec["realized_pl"] == pytest.approx((1.60 - 1.20) * 100 * 2)             # official: +80
    assert rec["realized_pl_actual_fill"] == pytest.approx((1.60 - 1.25) * 100 * 2)  # the (fake) broker filled at 1.25


def test_a_loser_rides_with_no_stop_and_no_dte_exit(env):
    clock, broker, om = env
    pos = _open(env, credit=1.50, qty=1)
    clock.now = datetime.combine(EXP - timedelta(days=2), datetime.min.time(), tzinfo=ET).replace(hour=10)
    set_chain(broker, 6.00, 1.30)    # 4.70: past the 90%-of-max-loss mark, 2 DTE
    om.maintain()
    assert len(om.positions) == 1 and pos["max_loss_hit"] is True and om.risk.max_loss_hits == 0   # a mark is not a loss
    assert not any(c[0] == "close_spread_at_market" for c in broker.calls)

"""Resting exits (2026-11-06): right after an entry fills, the bot places an OCO at the broker -- profit-target limit +
stop-limit -- and the script's own rules cancel it before closing early. FakeBroker plays Schwab."""

import pytest

import config
from conftest import et
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager
from test_position_manager import TODAY, events, open_b_put_spread, open_put_spread, rising, tick


@pytest.fixture(autouse=True)
def _resting_on(monkeypatch):
    monkeypatch.setattr(config, "RESTING_EXITS", True, raising=False)
    monkeypatch.setattr(config, "RESTING_EXIT_POLL_SEC", 0, raising=False)
    monkeypatch.setattr(config, "RESTING_EXIT_CANCEL_WAIT_SEC", 0, raising=False)
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    monkeypatch.setattr(config, "QUOTE_JUMP_SANITY_PCT", None)
    monkeypatch.setattr(config, "TICK_SECONDS", 15)


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(equity=10_000, today=TODAY)
    b.supports_resting_exits = True
    b.set_chain("P", {7505.0: (4.9, 5.1), 7510.0: (7.9, 8.1), 7450.0: (2.4, 2.6), 7445.0: (1.4, 1.6)})
    return b


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path, "TEST")


@pytest.fixture
def pm(broker, journal) -> PositionManager:
    return PositionManager(broker, journal)


def test_an_entry_places_an_oco_at_the_broker(pm, broker, journal):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)            # B: target 0.70, stop 1.50 on a $5 width
    assert len(broker.oco_calls) == 1
    call = broker.oco_calls[0]
    assert (call["right"], call["short"], call["long"], call["qty"], call["tif"]) == ("P", 7450.0, 7445.0, 1, "day")
    assert (call["target"], call["stop"], call["limit"]) == (0.70, 1.50, 1.65)     # limit = stop + 3% of the width
    assert p.oco_id and p.oco_target_id and p.oco_stop_id and p.oco_attempts == 1
    assert (p.oco_target_price, p.oco_stop_price, p.oco_limit_price) == (0.70, 1.50, 1.65)
    placed = events(journal, "RESTING_EXIT_PLACED")
    assert len(placed) == 1 and placed[0]["oco_id"] == p.oco_id and placed[0]["target"] == 0.70


def test_without_broker_support_or_the_setting_no_oco_is_placed(pm, broker, monkeypatch):
    broker.supports_resting_exits = False
    p = open_b_put_spread(pm, broker, qty=1)
    assert broker.oco_calls == [] and p.oco_id is None
    broker.supports_resting_exits = True
    monkeypatch.setattr(config, "RESTING_EXITS", False)
    pm2 = PositionManager(broker, pm.journal)
    open_put_spread(pm2, broker, qty=1)
    assert broker.oco_calls == []


def test_a_filled_target_is_booked_at_its_fill_as_oco_target(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    assert tick(pm, et(10, 2), 0.90, rising(), strat="B") == []          # a tick, no exit
    broker.fill_oco(p.oco_id, "target", 0.70)
    rows = pm.check_resting_exits(et(10, 3))
    assert len(rows) == 1
    row = rows[0]
    assert row["exit_reason"] == "OCO_TARGET" and row["exit_price"] == 0.70 and row["qty"] == 1
    assert row["pnl"] == pytest.approx(30.0) and row["pnl_actual_fill"] == pytest.approx(30.0)
    assert row["position_closed"] is True and row["oco_id"]
    assert "B" not in pm.positions and broker.close_calls == []         # no close of our own was sent
    assert events(journal, "EXIT")[0]["exit_reason"] == "OCO_TARGET"


def test_a_filled_stop_is_booked_as_oco_stop(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.fill_oco(p.oco_id, "stop", 1.55)
    row = pm.check_resting_exits(et(10, 3))[0]
    assert row["exit_reason"] == "OCO_STOP" and row["exit_price"] == 1.55 and row["pnl"] == pytest.approx(-55.0)


def test_the_poll_is_throttled(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "RESTING_EXIT_POLL_SEC", 3)
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)               # placed at 10:00:00, counts as a read
    broker.fill_oco(p.oco_id, "target", 0.70)
    assert pm.check_resting_exits(et(10, 0, 1)) == []                   # 1 s later: not read again yet
    assert len(pm.check_resting_exits(et(10, 0, 4))) == 1


def test_the_scripts_own_close_cancels_the_oco_first(pm, broker, journal):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    oco = p.oco_id
    broker.spread_close_price = 1.52
    rows = tick(pm, et(10, 2), 1.50, rising(), strat="B")              # B's stop: the script closes
    assert len(rows) == 1 and rows[0]["exit_reason"] == "STOP_LOSS" and rows[0]["exit_price"] == 1.52
    assert broker.orders[oco]["status"] == "canceled"
    assert all(c["status"] == "canceled" for c in broker.orders[oco]["children"])
    assert len(broker.close_calls) == 1 and "B" not in pm.positions
    assert events(journal, "RESTING_EXIT_CANCELLED")[0]["reason"] == "STOP_LOSS"


def test_a_fill_during_the_cancel_is_booked_once_and_no_close_is_sent(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    oco = p.oco_id

    def cancel_fills_the_stop(order_id):
        broker.fill_oco(oco, "stop", 1.55)                              # Schwab filled the stop a moment before the cancel arrived
        return False
    monkeypatch.setattr(broker, "cancel_order", cancel_fills_the_stop)
    rows = tick(pm, et(10, 2), 1.50, rising(), strat="B")
    assert len(rows) == 1 and rows[0]["exit_reason"] == "OCO_STOP" and rows[0]["exit_price"] == 1.55
    assert broker.close_calls == [] and "B" not in pm.positions


def test_a_cancel_failure_sends_no_close_and_keeps_the_oco(pm, broker, journal, monkeypatch):
    from broker import BrokerError
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)

    def cancel_fails(order_id):
        raise BrokerError("simulated cancel failure")
    monkeypatch.setattr(broker, "cancel_order", cancel_fails)
    assert tick(pm, et(10, 2), 1.50, rising(), strat="B") == []
    assert broker.close_calls == [] and pm.positions["B"].oco_id == p.oco_id
    assert events(journal, "RESTING_EXIT_CANCEL_FAILED")[0]["reason"] == "STOP_LOSS"


def test_an_oco_still_working_after_the_cancel_holds_the_close(pm, broker, monkeypatch):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    monkeypatch.setattr(broker, "cancel_order", lambda order_id: True)   # acknowledged, but the OCO stays "accepted"
    assert tick(pm, et(10, 2), 1.50, rising(), strat="B") == []
    assert broker.close_calls == [] and pm.positions["B"].oco_id == p.oco_id


def test_a_dead_oco_is_replaced_once_then_the_exits_are_script_managed(pm, broker, journal):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    first = p.oco_id
    broker.cancel_order(first)                                          # died at the broker without a fill
    assert pm.check_resting_exits(et(10, 1)) == []
    assert p.oco_id and p.oco_id != first and p.oco_attempts == 2 and len(broker.oco_calls) == 2
    broker.cancel_order(p.oco_id)
    assert pm.check_resting_exits(et(10, 2)) == []
    assert p.oco_id is None and len(broker.oco_calls) == 2
    lost = events(journal, "RESTING_EXIT_LOST")
    assert [e["replaced"] for e in lost] == [True, False]
    broker.spread_close_price = 1.52
    rows = tick(pm, et(10, 3), 1.50, rising(), strat="B")              # the script still protects the position
    assert rows[0]["exit_reason"] == "STOP_LOSS" and len(broker.close_calls) == 1


def test_an_oco_the_broker_rejects_leaves_the_exits_script_managed(pm, broker, journal):
    broker.oco_status, broker.oco_reason = "rejected", "Stop orders are not supported for this product"
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    assert p.oco_id is None and p.oco_attempts == 1
    failed = events(journal, "RESTING_EXIT_FAILED")
    assert failed[0]["status"] == "rejected" and "not supported" in failed[0]["error"]


def test_a_broker_error_at_placement_leaves_the_exits_script_managed(pm, broker, journal):
    broker.oco_failures = 1
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    assert p.oco_id is None and "simulated OCO failure" in events(journal, "RESTING_EXIT_FAILED")[0]["error"]


def test_a_spread_gone_from_the_broker_whose_oco_filled_is_booked_at_the_fill_not_as_a_hand_close(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.fill_oco(p.oco_id, "target", 0.70)                          # the spread is gone from the account
    rows = pm._book_external(p, 1, et(10, 5), "EXTERNAL_CLOSE")
    assert rows[0]["exit_reason"] == "OCO_TARGET" and rows[0]["exit_price"] == 0.70 and rows[0]["pnl"] == pytest.approx(30.0)
    assert "B" not in pm.positions


def test_the_oco_ids_survive_a_state_round_trip(pm, broker):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    back = OpenSpread.from_dict(p.to_dict())
    assert (back.oco_id, back.oco_target_id, back.oco_stop_id, back.oco_attempts) == (p.oco_id, p.oco_target_id, p.oco_stop_id, 1)
    assert (back.oco_target_price, back.oco_stop_price, back.oco_limit_price) == (0.70, 1.50, 1.65)


def test_the_parent_state_is_rebuilt_from_the_children_when_the_broker_hides_the_parent(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.fill_oco(p.oco_id, "target", 0.70)
    real_get = broker.get_order
    monkeypatch.setattr(broker, "get_order", lambda oid: None if oid == p.oco_id else real_get(oid))
    row = pm.check_resting_exits(et(10, 3))[0]
    assert row["exit_reason"] == "OCO_TARGET" and row["exit_price"] == 0.70


def test_force_close_releases_the_oco_before_closing(pm, broker):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    oco = p.oco_id
    rows = pm.force_close(et(15, 30), {"B": 1.10})
    assert rows[0]["exit_reason"] == "FORCE_CLOSE" and broker.orders[oco]["status"] == "canceled"
    assert len(broker.close_calls) == 1


def test_target_only_mode_rests_the_target_and_the_script_keeps_the_stop(pm, broker, journal, monkeypatch):
    """Schwab refused the OCO form: the broker rests the profit target alone. A target fill is booked at its fill; a stop is
    the script's, and it cancels the resting target first."""
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    broker.oco_mode = "target"
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    assert p.oco_id and p.oco_target_id == p.oco_id and p.oco_stop_id is None
    assert events(journal, "RESTING_EXIT_PLACED")[0]["mode"] == "target"
    broker.fill_oco(p.oco_id, "target", 0.70)
    row = pm.check_resting_exits(et(10, 3))[0]
    assert row["exit_reason"] == "OCO_TARGET" and row["exit_price"] == 0.70 and row["pnl"] == pytest.approx(30.0)
    p2 = open_put_spread(pm, broker, qty=1)                                 # A: stop 3.60
    resting = p2.oco_id
    assert tick(pm, et(10, 4), 3.60, rising()) == []                        # 1st confirming print: held
    broker.spread_close_price = 3.65
    rows = tick(pm, et(10, 6), 3.61, rising())                              # 2nd: the script closes
    assert rows[0]["exit_reason"] == "STOP_LOSS" and rows[0]["exit_price"] == 3.65
    assert broker.orders[resting]["status"] == "canceled" and len(broker.close_calls) == 1


def test_a_refused_cancel_of_an_already_dead_order_lets_the_close_go_ahead(pm, broker, journal, monkeypatch):
    from broker import BrokerError
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.cancel_order(p.oco_id)                                       # died at Schwab (the hard stop cancelled it, say)

    def cancel_refused(order_id):
        raise BrokerError("Schwab cancel_order failed: HTTP 400 order is not cancellable")
    monkeypatch.setattr(broker, "cancel_order", cancel_refused)
    broker.spread_close_price = 1.52
    rows = tick(pm, et(10, 2), 1.50, rising(), strat="B")
    assert rows[0]["exit_reason"] == "STOP_LOSS" and len(broker.close_calls) == 1
    assert events(journal, "RESTING_EXIT_CANCELLED")[0]["cancel_error"].startswith("Schwab cancel_order failed")


def test_a_refused_cancel_of_a_live_order_still_holds_the_close(pm, broker, journal, monkeypatch):
    from broker import BrokerError
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)

    def cancel_refused(order_id):
        raise BrokerError("simulated cancel failure")
    monkeypatch.setattr(broker, "cancel_order", cancel_refused)
    assert tick(pm, et(10, 2), 1.50, rising(), strat="B") == []
    assert broker.close_calls == [] and pm.positions["B"].oco_id == p.oco_id
    assert events(journal, "RESTING_EXIT_CANCEL_FAILED")[0]["state"] == "accepted"


def test_f_stock_spreads_get_no_resting_exit_unless_asked(pm, broker, monkeypatch):
    from strategy import BULLISH
    spread = OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root="AAPL", expiration=TODAY,
                        short_strike=150.0, long_strike=145.0, width=5, qty=1, entry_credit=0.50, entry_time=et(10, 0),
                        current_price=0.50, best_price=0.50, profit_target=0.30, stop_loss=0.50)
    pm.open(spread)
    assert broker.oco_calls == [] and spread.oco_id is None
    monkeypatch.setattr(config, "RESTING_EXITS_FOR_STOCKS", True, raising=False)
    spread2 = OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root="MSFT", expiration=TODAY,
                         short_strike=400.0, long_strike=395.0, width=5, qty=1, entry_credit=0.50, entry_time=et(10, 0),
                         current_price=0.50, best_price=0.50, profit_target=0.30, stop_loss=0.50)
    pm.open(spread2)
    assert len(broker.oco_calls) == 1 and spread2.oco_id


def test_a_forced_read_ignores_the_throttle(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "RESTING_EXIT_POLL_SEC", 300)
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.fill_oco(p.oco_id, "target", 0.70)
    assert pm.check_resting_exits(et(10, 0, 1)) == []
    assert len(pm.check_resting_exits(et(10, 0, 1), force=True)) == 1


def test_resting_order_ids_cover_parent_and_children(pm, broker):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    assert pm.resting_order_ids() == {p.oco_id, p.oco_target_id, p.oco_stop_id}


def test_a_cancel_the_bot_asked_for_that_confirms_late_is_not_replaced(pm, broker, journal, monkeypatch):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    first = p.oco_id
    monkeypatch.setattr(broker, "cancel_order", lambda order_id: True)       # accepted, but Schwab still shows the OCO live
    assert tick(pm, et(10, 2), 1.50, rising(), strat="B") == []              # the close waits
    assert p.oco_id == first and p.oco_cancel_requested is True
    broker.orders[first]["status"] = "canceled"                               # the cancel confirms a moment later
    for c in broker.orders[first]["children"]:
        c["status"] = "canceled"
    assert pm.check_resting_exits(et(10, 2, 5)) == []
    assert p.oco_id is None and len(broker.oco_calls) == 1                   # no "lost" re-placement
    assert events(journal, "RESTING_EXIT_LOST") == []
    broker.spread_close_price = 1.52
    rows = tick(pm, et(10, 2, 6), 1.50, rising(), strat="B")                 # the next tick closes
    assert rows[0]["exit_reason"] == "STOP_LOSS" and len(broker.close_calls) == 1


def test_a_partial_oco_fill_keeps_the_remainder_resting(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "EXIT_PNL_BASIS", "fill")
    p = open_b_put_spread(pm, broker, qty=2, credit=1.00)
    oco = p.oco_id
    broker.fill_oco(oco, "target", 0.70, qty=1)
    rows = pm.check_resting_exits(et(10, 3))
    assert len(rows) == 1 and rows[0]["qty"] == 1 and rows[0]["position_closed"] is False
    assert p.remaining == 1 and p.oco_id == oco                              # the remainder still rests at the broker
    broker.orders[oco]["status"] = "accepted"                                 # Schwab keeps the OCO working for the remainder
    for c in broker.orders[oco]["children"]:
        c["status"], c["filled_qty"] = ("filled", 1) if c["exit_role"] == "target" else ("accepted", 0)
    assert pm.check_resting_exits(et(10, 4)) == []                           # the booked fill is not booked twice
    broker.spread_close_price = 1.52
    rows = tick(pm, et(10, 5), 1.50, rising(), strat="B")                    # the script stop cancels the remainder first
    assert rows[0]["exit_reason"] == "STOP_LOSS" and broker.orders[oco]["status"] == "canceled" and len(broker.close_calls) == 1


def test_an_oco_fill_carries_schwabs_fill_time(pm, broker):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.fill_oco(p.oco_id, "target", 0.70)
    for c in broker.orders[p.oco_id]["children"]:
        if c["exit_role"] == "target":
            c["filled_at"] = et(10, 6)
    row = pm.check_resting_exits(et(13, 0))[0]                                # noticed much later (a restart, say)
    assert row["exit_time"] == et(10, 6)


def test_a_gone_spread_whose_resting_order_cannot_be_read_waits_before_being_booked_as_a_hand_close(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "RESTING_EXIT_UNRESOLVED_GRACE_SEC", 120, raising=False)
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    from broker import BrokerError

    def unreadable(order_id):
        raise BrokerError("Schwab get_order failed: HTTP 503")
    monkeypatch.setattr(broker, "get_order", unreadable)
    assert pm._book_external(p, 1, et(10, 5), "EXTERNAL_CLOSE") == []        # not yet
    assert "B" in pm.positions
    rows = pm._book_external(p, 1, et(10, 7, 30), "EXTERNAL_CLOSE")           # 150 s later: booked at the mark, flagged
    assert rows[0]["exit_reason"] == "EXTERNAL_CLOSE" and rows[0]["external"] is True
    assert events(journal, "RESTING_EXIT_UNRESOLVED")[0]["oco_id"]


def test_a_gone_spread_whose_resting_order_is_dead_is_a_hand_close_at_once(pm, broker):
    p = open_b_put_spread(pm, broker, qty=1, credit=1.00)
    broker.cancel_order(p.oco_id)                                             # the owner cancelled the OCO in the app, then closed by hand
    rows = pm._book_external(p, 1, et(10, 5), "EXTERNAL_CLOSE")
    assert rows[0]["exit_reason"] == "EXTERNAL_CLOSE" and "B" not in pm.positions


def test_a_force_close_waits_longer_for_the_cancel(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "RESTING_EXIT_CANCEL_WAIT_SEC", 0, raising=False)
    monkeypatch.setattr(config, "RESTING_EXIT_CANCEL_WAIT_FORCE_SEC", 0, raising=False)
    waits: list[bool] = []
    real = pm._release_resting

    def spy(p, now, reason, force=False):
        waits.append(force)
        return real(p, now, reason, force=force)
    monkeypatch.setattr(pm, "_release_resting", spy)
    open_b_put_spread(pm, broker, qty=1, credit=1.00)
    pm.force_close(et(15, 30), {"B": 1.10})
    assert waits == [True]

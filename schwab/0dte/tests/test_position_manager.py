import json
from datetime import date

import pytest

import config
from conftest import et, make_candles
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager, spxw_legs
from strategy import BEARISH, BULLISH

TODAY = date(2026, 9, 17)


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(equity=10_000, today=TODAY)
    b.set_chain("P", {7505.0: (4.9, 5.1), 7510.0: (7.9, 8.1)})
    return b


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path, "TEST")


@pytest.fixture
def pm(broker, journal) -> PositionManager:
    return PositionManager(broker, journal)


def open_put_spread(pm: PositionManager, broker: FakeBroker, qty: int, credit: float = 3.00) -> OpenSpread:
    broker.add_spread_position("P", 7510.0, 7505.0, qty, credit + 5.0, 5.0)
    spread = OpenSpread(direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                        short_strike=7510.0, long_strike=7505.0, width=5, qty=qty, entry_credit=credit,
                        entry_time=et(10, 0), current_price=credit, best_price=credit)
    pm.open(spread)
    return spread


def rising(n: int = 3):
    return make_candles(et(9, 54), [(100 + 2 * i, 103 + 2 * i, 99 + 2 * i, 102 + 2 * i) for i in range(n)])


def events(journal: Journal, kind: str) -> list[dict]:
    path = journal.dir / f"events_{TODAY.isoformat()}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def test_stop_hit_closes_all(pm, broker, journal):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 3.35
    assert pm.on_tick(et(10, 2), 3.29, rising()) == []
    fills = pm.on_tick(et(10, 4), 3.30, rising())
    assert len(fills) == 1 and fills[0]["exit_reason"] == "STOP_LOSS"
    assert fills[0]["qty"] == 2 and fills[0]["pnl"] == pytest.approx(-70.0)
    assert fills[0]["position_closed"] is True
    assert pm.position is None
    assert spxw_legs(broker.get_positions()) == []
    assert broker.close_calls == [{"right": "P", "short": 7510.0, "long": 7505.0, "qty": 2}]


def test_target_with_one_contract_full_close(pm, broker):
    open_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 2.70
    fills = pm.on_tick(et(10, 2), 2.70, rising())
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 1
    assert fills[0]["pnl"] == pytest.approx(30.0)
    assert pm.position is None and broker.get_positions() == []


def test_target_two_contracts_without_momentum_full_close(pm, broker):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    stalling = make_candles(et(9, 54), [(100, 103, 99, 102), (102, 105, 101, 104), (104, 105, 103, 104.5)])
    fills = pm.on_tick(et(10, 2), 2.70, stalling)
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 2
    assert pm.position is None


def test_target_with_momentum_enters_runner_then_trails_out(pm, broker, journal):
    open_put_spread(pm, broker, qty=3)
    broker.spread_close_price = 2.70
    fills = pm.on_tick(et(10, 2), 2.70, rising())
    assert fills[0]["exit_reason"] == "TARGET_HALF" and fills[0]["qty"] == 1
    assert fills[0]["position_closed"] is False
    p = pm.position
    assert p is not None and p.runner is True and p.remaining == 2
    assert p.runner_best == 2.70 and p.momentum_at_target == pytest.approx(2.0)
    assert len(events(journal, "RUNNER_START")) == 1

    assert pm.on_tick(et(10, 4), 2.00, rising()) == []
    assert p.runner_best == 2.00
    assert pm.on_tick(et(10, 6), 2.45, rising()) == []
    broker.spread_close_price = 2.50
    fills = pm.on_tick(et(10, 8), 2.50, rising())
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["qty"] == 2
    assert fills[0]["runner"] == "y" and fills[0]["pnl"] == pytest.approx(100.0)
    assert fills[0]["position_pnl"] == pytest.approx(130.0)
    assert pm.position is None and broker.get_positions() == []


def test_runner_stop_at_target_level(pm, broker):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    pm.on_tick(et(10, 2), 2.70, rising())
    assert pm.position.runner is True
    broker.spread_close_price = 2.72
    fills = pm.on_tick(et(10, 4), 2.71, rising())
    assert fills[0]["exit_reason"] == "RUNNER_STOP"
    assert fills[0]["pnl"] == pytest.approx(28.0)
    assert pm.position is None


def test_runner_momentum_slowdown_exits(pm, broker):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    pm.on_tick(et(10, 2), 2.70, rising())
    assert pm.position.momentum_at_target == pytest.approx(2.0)
    slower = make_candles(et(9, 56), [(100, 103, 99, 101.5), (101.5, 104, 101, 103), (103, 105, 102, 104.5)])
    broker.spread_close_price = 2.60
    fills = pm.on_tick(et(10, 4), 2.60, slower)
    assert fills[0]["exit_reason"] == "RUNNER_MOMENTUM_SLOWED" and fills[0]["qty"] == 1
    assert pm.position is None


def test_runner_disabled_closes_all(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_ENABLED", False)
    open_put_spread(pm, broker, qty=4)
    broker.spread_close_price = 2.70
    fills = pm.on_tick(et(10, 2), 2.70, rising())
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 4


def test_close_retries_after_broker_error(pm, broker, journal):
    open_put_spread(pm, broker, qty=1)
    broker.close_failures = 1
    broker.spread_close_price = 3.35
    fills = pm.on_tick(et(10, 2), 3.31, rising())
    assert len(fills) == 1 and pm.position is None
    assert len(broker.close_calls) == 2
    assert len(events(journal, "CLOSE_FAILED")) == 1


def test_close_gives_up_after_max_retries(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    open_put_spread(pm, broker, qty=1)
    broker.close_failures = 5
    fills = pm.on_tick(et(10, 2), 3.31, rising())
    assert fills == [] and pm.position is not None
    assert len(broker.close_calls) == 2
    assert len(events(journal, "CLOSE_FAILED")) == 2


def test_force_close(pm, broker):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.95
    fills = pm.force_close(et(12, 30), 2.95)
    assert fills[0]["exit_reason"] == "FORCE_CLOSE" and fills[0]["qty"] == 2
    assert fills[0]["pnl"] == pytest.approx(10.0)
    assert pm.position is None


def test_adopt_from_broker_put_spread(pm, broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 2, 8.10, 5.05)
    spread = pm.adopt_from_broker(broker.get_positions(), et(10, 15))
    assert spread is pm.position
    assert spread.direction == BULLISH and spread.right == "P" and spread.root == "SPXW"
    assert (spread.short_strike, spread.long_strike, spread.width) == (7510.0, 7505.0, 5)
    assert spread.qty == 2 and spread.entry_credit == pytest.approx(3.05)
    assert spread.expiration == TODAY and spread.setup == "ADOPTED"


def test_adopt_from_broker_call_spread_and_saved_state(pm, broker):
    broker.set_chain("C", {7490.0: (9.9, 10.1), 7495.0: (6.9, 7.1)})
    broker.add_spread_position("C", 7490.0, 7495.0, 1, 10.00, 7.00)
    saved = OpenSpread(direction=BEARISH, setup="OVERNIGHT", right="C", root="SPXW", expiration=TODAY,
                       short_strike=7490.0, long_strike=7495.0, width=5, qty=2, entry_credit=3.00,
                       entry_time=et(9, 50), current_price=2.7, best_price=2.6, runner=True,
                       runner_best=2.6, momentum_at_target=1.5, closed_qty=1, realized_pnl=30.0).to_dict()
    spread = pm.adopt_from_broker(broker.get_positions(), et(10, 15), saved)
    assert spread.setup == "OVERNIGHT" and spread.runner is True
    assert spread.remaining == 1 and spread.closed_qty == 1 and spread.qty == 2
    assert spread.current_price == pytest.approx(3.0)


def test_adopt_ignores_non_spxw_and_empty(pm, broker):
    assert pm.adopt_from_broker([], et(10, 0)) is None
    stock = {"symbol": "AAPL", "asset_class": "stock", "qty": 10, "avg_price": 100.0}
    assert pm.adopt_from_broker([stock], et(10, 0)) is None
    assert pm.position is None


def test_state_roundtrip_and_trades_csv(pm, broker, journal):
    spread = open_put_spread(pm, broker, qty=1)
    assert OpenSpread.from_dict(spread.to_dict()) == spread
    broker.spread_close_price = 2.70
    fills = pm.on_tick(et(10, 2), 2.70, rising())
    journal.trade(fills[0])
    text = journal.trades_path.read_text().splitlines()
    assert text[0].startswith("date,entry_time,exit_time,direction")
    assert "PROFIT_TARGET" in text[1] and ",n" in text[1]


def falling_last():
    return make_candles(et(9, 54), [(100, 103, 99, 102), (102, 105, 101, 104), (104, 105, 101, 102)])


def test_profit_lock_not_armed_below_arm_level(pm, broker):
    open_put_spread(pm, broker, qty=1)
    assert pm.on_tick(et(10, 2), 2.90, rising()) == []      # +0.10 profit: below PROFIT_LOCK_ARM
    assert pm.on_tick(et(10, 4), 3.00, rising()) == []      # gave it all back, but never armed
    assert pm.position is not None


def test_profit_lock_giveback_exits_once_armed(pm, broker):
    open_put_spread(pm, broker, qty=2)
    assert pm.on_tick(et(10, 2), 2.80, rising()) == []      # +0.20 arms the rule, best = 2.80
    assert pm.on_tick(et(10, 4), 2.89, rising()) == []      # gave back 0.09 < 0.10
    broker.spread_close_price = 2.90
    fills = pm.on_tick(et(10, 6), 2.90, rising())           # gave back 0.10 -> lock it in
    assert len(fills) == 1 and fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK"
    assert fills[0]["qty"] == 2 and fills[0]["pnl"] == pytest.approx(20.0)
    assert pm.position is None


def test_profit_lock_momentum_flip_exits_once_armed(pm, broker):
    open_put_spread(pm, broker, qty=1)
    assert pm.on_tick(et(10, 2), 2.80, rising()) == []
    broker.spread_close_price = 2.82
    fills = pm.on_tick(et(10, 4), 2.82, falling_last())     # still +0.18 but last candle closed red
    assert len(fills) == 1 and fills[0]["exit_reason"] == "PROFIT_LOCK_MOMENTUM"
    assert fills[0]["pnl"] == pytest.approx(18.0)


def test_profit_lock_momentum_flip_ignored_before_arming(pm, broker):
    open_put_spread(pm, broker, qty=1)
    assert pm.on_tick(et(10, 2), 2.95, falling_last()) == []


def test_profit_lock_disabled(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "PROFIT_LOCK_ENABLED", False)
    open_put_spread(pm, broker, qty=1)
    assert pm.on_tick(et(10, 2), 2.80, rising()) == []
    assert pm.on_tick(et(10, 4), 2.95, falling_last()) == []
    assert pm.position is not None

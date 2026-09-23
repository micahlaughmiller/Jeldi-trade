import json
from datetime import date

import pytest

import config
from conftest import et, make_candles
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager, classify_by_moneyness, spxw_legs
from strategy import BEARISH, BULLISH

TODAY = date(2026, 9, 17)
SPOT = 7500.0


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(equity=10_000, today=TODAY)
    b.set_chain("P", {7505.0: (4.9, 5.1), 7510.0: (7.9, 8.1), 7450.0: (2.4, 2.6), 7445.0: (1.4, 1.6)})
    return b


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path, "TEST")


@pytest.fixture
def pm(broker, journal) -> PositionManager:
    return PositionManager(broker, journal)


def open_put_spread(pm: PositionManager, broker: FakeBroker, qty: int, credit: float = 3.00) -> OpenSpread:
    broker.add_spread_position("P", 7510.0, 7505.0, qty, credit + 5.0, 5.0)
    spread = OpenSpread(strategy="A", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                        short_strike=7510.0, long_strike=7505.0, width=5, qty=qty, entry_credit=credit,
                        entry_time=et(10, 0), current_price=credit, best_price=credit,
                        profit_target=config.PROFIT_TARGET, stop_loss=config.STOP_LOSS)
    pm.open(spread)
    return spread


def open_b_put_spread(pm: PositionManager, broker: FakeBroker, qty: int, credit: float = 1.00) -> OpenSpread:
    broker.add_spread_position("P", 7450.0, 7445.0, qty, credit + 1.5, 1.5)
    spread = OpenSpread(strategy="B", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                        short_strike=7450.0, long_strike=7445.0, width=5, qty=qty, entry_credit=credit,
                        entry_time=et(10, 0), current_price=credit, best_price=credit,
                        profit_target=config.B_PROFIT_TARGET, stop_loss=config.B_STOP_LOSS)
    pm.open(spread)
    return spread


def rising(n: int = 3):
    return make_candles(et(9, 54), [(100 + 2 * i, 103 + 2 * i, 99 + 2 * i, 102 + 2 * i) for i in range(n)])


def events(journal: Journal, kind: str) -> list[dict]:
    path = journal.dir / f"events_{journal.slug}_{TODAY.isoformat()}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def tick(pm: PositionManager, when, price: float, candles, strat: str = "A") -> list[dict]:
    return pm.on_tick(when, {strat: price}, candles)


def test_stop_hit_closes_all(pm, broker, journal):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 3.60
    assert tick(pm, et(10, 2), 3.54, rising()) == []
    fills = tick(pm, et(10, 4), 3.55, rising())
    assert len(fills) == 1 and fills[0]["exit_reason"] == "STOP_LOSS"
    assert fills[0]["qty"] == 2 and fills[0]["pnl"] == pytest.approx(-120.0)
    assert fills[0]["position_closed"] is True and fills[0]["strategy"] == "A"
    assert pm.positions == {}
    assert spxw_legs(broker.get_positions()) == []
    assert broker.close_calls == [{"right": "P", "short": 7510.0, "long": 7505.0, "qty": 2}]
    assert events(journal, "ENTRY")[0]["strategy"] == "A"
    assert events(journal, "EXIT")[0]["strategy"] == "A"


def test_b_target_with_one_contract_full_close(pm, broker):
    # B books half at target, but one contract cannot be split -> full close at the target
    open_b_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 0.70
    fills = tick(pm, et(10, 2), 0.70, rising(), strat="B")
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 1
    assert fills[0]["pnl"] == pytest.approx(30.0)
    assert pm.positions == {} and broker.get_positions() == []


def test_a_runs_whole_position_at_target(pm, broker, journal):
    # A books nothing at the target: the whole position becomes the runner (stop at target, trail 0.50)
    open_put_spread(pm, broker, qty=2)
    assert tick(pm, et(10, 2), 2.70, rising()) == []
    p = pm.positions["A"]
    assert p.runner is True and p.remaining == 2 and p.closed_qty == 0
    assert p.runner_best == 2.70 and p.momentum_at_target == pytest.approx(2.0)
    assert len(events(journal, "RUNNER_START")) == 1 and broker.close_calls == []
    assert tick(pm, et(10, 4), 2.00, rising()) == []
    broker.spread_close_price = 2.50
    fills = tick(pm, et(10, 6), 2.50, rising())
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["qty"] == 2
    assert fills[0]["runner"] == "y" and fills[0]["pnl"] == pytest.approx(100.0)
    assert fills[0]["position_closed"] is True and pm.positions == {}


def test_a_single_contract_runs_too(pm, broker):
    # with nothing to book, RUNNER_MIN_CONTRACTS does not apply to A
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 2.70, rising()) == []
    assert pm.positions["A"].runner is True and pm.positions["A"].remaining == 1


def test_a_runs_at_target_even_without_momentum(pm, broker):
    # A's RUNNER_MOMENTUM_GATE is off: the target always starts the runner
    open_put_spread(pm, broker, qty=2)
    flat = make_candles(et(9, 54), [(100, 101, 99, 100.2), (100.2, 101, 99.5, 100.1), (100.1, 100.5, 99.8, 100.0)])
    assert tick(pm, et(10, 2), 2.70, flat) == []
    assert pm.positions["A"].runner is True and pm.positions["A"].remaining == 2


def test_a_runner_ignores_momentum_slowdown(pm, broker):
    open_put_spread(pm, broker, qty=2)
    tick(pm, et(10, 2), 2.70, rising())
    slower = make_candles(et(9, 56), [(100, 103, 99, 101.5), (101.5, 104, 101, 103), (103, 105, 102, 104.5)])
    assert tick(pm, et(10, 4), 2.60, slower) == []
    assert pm.positions["A"].runner is True


def test_a_profit_lock_arms_at_030_and_gives_back_035(pm, broker):
    open_put_spread(pm, broker, qty=2)
    assert tick(pm, et(10, 2), 2.80, rising()) == []      # +0.20: below A's 0.30 arm
    assert tick(pm, et(10, 4), 3.00, rising()) == []      # all given back, never armed, no exit
    assert tick(pm, et(10, 6), 2.72, rising()) == []      # +0.28: still below the arm
    pm.positions["A"].profit_target = 1.00                  # keep the runner out of the way for this test
    assert tick(pm, et(10, 8), 2.65, rising()) == []      # +0.35 arms, best 2.65
    assert tick(pm, et(10, 10), 2.99, rising()) == []     # gave back 0.34 < 0.35
    broker.spread_close_price = 3.00
    fills = tick(pm, et(10, 12), 3.00, rising())          # gave back 0.35 -> lock
    assert len(fills) == 1 and fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK" and pm.positions == {}


def test_a_profit_lock_ignores_candle_against(pm, broker):
    open_put_spread(pm, broker, qty=1)
    pm.positions["A"].profit_target = 1.00
    assert tick(pm, et(10, 2), 2.60, rising()) == []      # +0.40 armed
    assert tick(pm, et(10, 4), 2.62, falling_last()) == []   # red candle: ignored for A
    assert "A" in pm.positions


def test_b_keeps_shared_lock_gate_and_slowdown(pm, broker):
    from strategy import exit_setting
    assert (exit_setting("B", "PROFIT_LOCK_ARM"), exit_setting("B", "PROFIT_LOCK_GIVEBACK")) == (0.15, 0.10)
    assert exit_setting("B", "PROFIT_LOCK_ON_MOMENTUM_FLIP") is True
    assert exit_setting("B", "RUNNER_MOMENTUM_GATE", True) is True and exit_setting("B", "RUNNER_SLOWDOWN_EXIT", True) is True
    assert (exit_setting("A", "PROFIT_LOCK_ARM"), exit_setting("A", "PROFIT_LOCK_GIVEBACK")) == (0.30, 0.35)
    open_b_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 0.80, rising(), strat="B") == []           # +0.20 arms B's lock
    broker.spread_close_price = 0.90
    fills = tick(pm, et(10, 4), 0.90, rising(), strat="B")                # 0.10 giveback -> B locks
    assert fills[0]["strategy"] == "B" and fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK"


def test_exit_row_records_trigger_and_slippage(pm, broker):
    spread = open_put_spread(pm, broker, qty=1)
    spread.entry_mid = 3.05
    broker.spread_close_price = 3.60
    fills = tick(pm, et(10, 2), 3.56, rising())
    row = fills[0]
    assert row["trigger_price"] == 3.56 and row["exit_price"] == 3.60
    assert row["exit_slippage"] == pytest.approx(0.04)
    assert row["entry_mid"] == 3.05 and row["entry_slippage"] == pytest.approx(0.05)


def test_target_two_contracts_without_momentum_full_close(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    stalling = make_candles(et(9, 54), [(100, 103, 99, 102), (102, 105, 101, 104), (104, 105, 103, 104.5)])
    fills = tick(pm, et(10, 2), 2.70, stalling)
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 2
    assert pm.positions == {}


def test_target_with_momentum_enters_runner_then_trails_out(pm, broker, journal, monkeypatch):
    # half-off runner mechanics (B's default); A now runs the whole position, see test_a_runs_whole_position
    monkeypatch.setattr(config, "RUNNER_CLOSE_FRACTION_BY_STRATEGY", {"A": 0.5, "B": 0.5})
    open_put_spread(pm, broker, qty=3)
    broker.spread_close_price = 2.70
    fills = tick(pm, et(10, 2), 2.70, rising())
    assert fills[0]["exit_reason"] == "TARGET_HALF" and fills[0]["qty"] == 1
    assert fills[0]["position_closed"] is False
    p = pm.positions["A"]
    assert p.runner is True and p.remaining == 2
    assert p.runner_best == 2.70 and p.momentum_at_target == pytest.approx(2.0)
    runner_events = events(journal, "RUNNER_START")
    assert len(runner_events) == 1 and runner_events[0]["strategy"] == "A"

    assert tick(pm, et(10, 4), 2.00, rising()) == []
    assert p.runner_best == 2.00
    assert tick(pm, et(10, 6), 2.45, rising()) == []
    broker.spread_close_price = 2.50
    fills = tick(pm, et(10, 8), 2.50, rising())
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["qty"] == 2
    assert fills[0]["runner"] == "y" and fills[0]["pnl"] == pytest.approx(100.0)
    assert fills[0]["position_pnl"] == pytest.approx(130.0)
    assert pm.positions == {} and broker.get_positions() == []


def test_runner_stop_at_target_level(pm, broker, monkeypatch):
    # half-off runner mechanics (B's default); A now runs the whole position, see test_a_runs_whole_position
    monkeypatch.setattr(config, "RUNNER_CLOSE_FRACTION_BY_STRATEGY", {"A": 0.5, "B": 0.5})
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    tick(pm, et(10, 2), 2.70, rising())
    assert pm.positions["A"].runner is True
    broker.spread_close_price = 2.72
    fills = tick(pm, et(10, 4), 2.71, rising())
    assert fills[0]["exit_reason"] == "RUNNER_STOP"
    assert fills[0]["pnl"] == pytest.approx(28.0)
    assert pm.positions == {}


def test_runner_momentum_slowdown_exits(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    # half-off runner mechanics (B's default); A now runs the whole position, see test_a_runs_whole_position
    monkeypatch.setattr(config, "RUNNER_CLOSE_FRACTION_BY_STRATEGY", {"A": 0.5, "B": 0.5})
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.70
    tick(pm, et(10, 2), 2.70, rising())
    assert pm.positions["A"].momentum_at_target == pytest.approx(2.0)
    slower = make_candles(et(9, 56), [(100, 103, 99, 101.5), (101.5, 104, 101, 103), (103, 105, 102, 104.5)])
    broker.spread_close_price = 2.60
    fills = tick(pm, et(10, 4), 2.60, slower)
    assert fills[0]["exit_reason"] == "RUNNER_MOMENTUM_SLOWED" and fills[0]["qty"] == 1
    assert pm.positions == {}


def test_runner_disabled_closes_all(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_ENABLED", False)
    open_put_spread(pm, broker, qty=4)
    broker.spread_close_price = 2.70
    fills = tick(pm, et(10, 2), 2.70, rising())
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 4


def test_close_retries_after_broker_error(pm, broker, journal):
    open_put_spread(pm, broker, qty=1)
    broker.close_failures = 1
    broker.spread_close_price = 3.60
    fills = tick(pm, et(10, 2), 3.56, rising())
    assert len(fills) == 1 and pm.positions == {}
    assert len(broker.close_calls) == 2
    failed = events(journal, "CLOSE_FAILED")
    assert len(failed) == 1 and failed[0]["strategy"] == "A"


def test_close_gives_up_after_max_retries(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    open_put_spread(pm, broker, qty=1)
    broker.close_failures = 5
    fills = tick(pm, et(10, 2), 3.56, rising())
    assert fills == [] and "A" in pm.positions
    assert len(broker.close_calls) == 2
    assert len(events(journal, "CLOSE_FAILED")) == 2


def test_force_close(pm, broker):
    open_put_spread(pm, broker, qty=2)
    broker.spread_close_price = 2.95
    fills = pm.force_close(et(15, 30), {"A": 2.95})
    assert fills[0]["exit_reason"] == "FORCE_CLOSE" and fills[0]["qty"] == 2
    assert fills[0]["pnl"] == pytest.approx(10.0)
    assert pm.positions == {}


# -------------------------------------------------------- two strategies at once

def test_a_stops_out_while_b_keeps_running(pm, broker):
    open_put_spread(pm, broker, qty=2)          # A: entry 3.00, stop 3.55
    open_b_put_spread(pm, broker, qty=1)        # B: entry 1.00, stop 1.50
    assert set(pm.positions) == {"A", "B"}
    assert pm.total_open_risk() == pytest.approx(2 * 200.0 + 400.0)
    broker.spread_close_price = 3.60
    fills = pm.on_tick(et(10, 4), {"A": 3.56, "B": 1.20}, rising())
    assert [f["strategy"] for f in fills] == ["A"] and fills[0]["exit_reason"] == "STOP_LOSS"
    assert set(pm.positions) == {"B"}
    b = pm.positions["B"]
    assert b.current_price == 1.20 and b.remaining == 1
    assert broker.close_calls == [{"right": "P", "short": 7510.0, "long": 7505.0, "qty": 2}]


def test_b_hits_its_own_wider_stop_a_unaffected(pm, broker):
    open_put_spread(pm, broker, qty=1)
    open_b_put_spread(pm, broker, qty=1)
    # B stops at +0.50 (1.50) while A, at +0.10, is nowhere near its 0.55 stop
    assert pm.on_tick(et(10, 2), {"A": 3.10, "B": 1.45}, rising()) == []
    broker.spread_close_price = 1.55
    fills = pm.on_tick(et(10, 4), {"A": 3.10, "B": 1.50}, rising())
    assert [f["strategy"] for f in fills] == ["B"] and fills[0]["exit_reason"] == "STOP_LOSS"
    assert fills[0]["pnl"] == pytest.approx(-55.0)
    assert set(pm.positions) == {"A"} and pm.positions["A"].current_price == 3.10


def test_b_target_uses_its_own_profit_target(pm, broker):
    open_b_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 0.70
    fills = pm.on_tick(et(10, 2), {"B": 0.70}, rising())
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["pnl"] == pytest.approx(30.0)


def test_missing_price_skips_that_position_only(pm, broker):
    open_put_spread(pm, broker, qty=1)
    open_b_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 3.60
    fills = pm.on_tick(et(10, 2), {"A": 3.56}, rising())
    assert [f["strategy"] for f in fills] == ["A"]
    assert pm.positions["B"].current_price == 1.00


def test_force_close_closes_both(pm, broker):
    open_put_spread(pm, broker, qty=1)
    open_b_put_spread(pm, broker, qty=1)
    fills = pm.force_close(et(15, 30), {"A": 2.95, "B": None})
    assert sorted(f["strategy"] for f in fills) == ["A", "B"]
    assert pm.positions == {} and spxw_legs(broker.get_positions()) == []


# ------------------------------------------------------------------- adoption

def test_classify_by_moneyness():
    assert classify_by_moneyness("P", 7510.0, SPOT) == "A"
    assert classify_by_moneyness("P", 7450.0, SPOT) == "B"
    assert classify_by_moneyness("C", 7490.0, SPOT) == "A"
    assert classify_by_moneyness("C", 7550.0, SPOT) == "B"


def test_adopt_from_broker_put_spread(pm, broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 2, 8.10, 5.05)
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT)
    assert len(adopted) == 1 and adopted[0] is pm.positions["A"]
    spread = adopted[0]
    assert spread.direction == BULLISH and spread.right == "P" and spread.root == "SPXW"
    assert (spread.short_strike, spread.long_strike, spread.width) == (7510.0, 7505.0, 5)
    assert spread.qty == 2 and spread.entry_credit == pytest.approx(3.05)
    assert spread.expiration == TODAY and spread.setup == "ADOPTED"
    assert (spread.profit_target, spread.stop_loss) == (config.PROFIT_TARGET, config.STOP_LOSS)


def test_adopt_classifies_itm_as_a_and_otm_as_b(pm, broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 2, 8.10, 5.05)
    broker.add_spread_position("P", 7450.0, 7445.0, 1, 2.50, 1.50)
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT)
    assert {s.strategy for s in adopted} == {"A", "B"}
    a, b = pm.positions["A"], pm.positions["B"]
    assert (a.short_strike, a.long_strike, a.qty) == (7510.0, 7505.0, 2)
    assert (b.short_strike, b.long_strike, b.qty) == (7450.0, 7445.0, 1)
    assert b.entry_credit == pytest.approx(1.00)
    assert (b.profit_target, b.stop_loss) == (config.B_PROFIT_TARGET, config.B_STOP_LOSS)


def test_adopt_otm_call_spread_is_b(pm, broker):
    broker.set_chain("C", {7550.0: (2.4, 2.6), 7560.0: (0.9, 1.1)})
    broker.add_spread_position("C", 7550.0, 7560.0, 1, 2.50, 1.00)
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT)
    assert adopted[0].strategy == "B" and adopted[0].direction == BEARISH


def test_adopt_from_broker_call_spread_and_saved_state(pm, broker):
    broker.set_chain("C", {7490.0: (9.9, 10.1), 7495.0: (6.9, 7.1)})
    broker.add_spread_position("C", 7490.0, 7495.0, 1, 10.00, 7.00)
    saved = OpenSpread(strategy="A", direction=BEARISH, setup="OVERNIGHT", right="C", root="SPXW", expiration=TODAY,
                       short_strike=7490.0, long_strike=7495.0, width=5, qty=2, entry_credit=3.00,
                       entry_time=et(9, 50), current_price=2.7, best_price=2.6, profit_target=0.30, stop_loss=0.30,
                       runner=True, runner_best=2.6, momentum_at_target=1.5, closed_qty=1, realized_pnl=30.0)
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT, {"A": saved.to_dict()})
    spread = adopted[0]
    assert spread.setup == "OVERNIGHT" and spread.runner is True
    assert spread.remaining == 1 and spread.closed_qty == 1 and spread.qty == 2
    assert spread.current_price == pytest.approx(3.0)


def test_adopt_saved_state_overrides_moneyness(pm, broker):
    # a spread that is now ITM was opened as B; saved state wins over the moneyness guess
    broker.add_spread_position("P", 7510.0, 7505.0, 1, 6.50, 5.00)
    saved = OpenSpread(strategy="B", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                       short_strike=7510.0, long_strike=7505.0, width=5, qty=1, entry_credit=1.50,
                       entry_time=et(10, 5), current_price=1.5, best_price=1.5, profit_target=0.30, stop_loss=0.50)
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT, {"B": saved.to_dict()})
    assert adopted[0].strategy == "B" and "B" in pm.positions and "A" not in pm.positions


def test_adopt_skips_positions_already_tracked(pm, broker):
    open_put_spread(pm, broker, qty=2)
    assert pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT) == []
    assert set(pm.positions) == {"A"}


def test_adopt_ignores_non_spxw_and_empty(pm, broker):
    assert pm.adopt_from_broker([], et(10, 0), SPOT) == []
    stock = {"symbol": "AAPL", "asset_class": "stock", "qty": 10, "avg_price": 100.0}
    assert pm.adopt_from_broker([stock], et(10, 0), SPOT) == []
    assert pm.positions == {}


def test_state_roundtrip_and_trades_csv(pm, broker, journal, monkeypatch):
    # half-off runner mechanics (B's default); A now runs the whole position, see test_a_runs_whole_position
    monkeypatch.setattr(config, "RUNNER_CLOSE_FRACTION_BY_STRATEGY", {"A": 0.5, "B": 0.5})
    spread = open_put_spread(pm, broker, qty=1)
    assert OpenSpread.from_dict(spread.to_dict()) == spread
    broker.spread_close_price = 2.70
    fills = tick(pm, et(10, 2), 2.70, rising())
    journal.trade(fills[0])
    text = journal.trades_path.read_text().splitlines()
    assert text[0].startswith("date,strategy,entry_time,exit_time,direction")
    assert text[0].endswith("runner,trigger_price,exit_slippage,entry_mid,entry_slippage")
    assert text[1].startswith("2026-09-17,A,")
    assert "PROFIT_TARGET" in text[1] and ",n," in text[1]


def falling_last():
    return make_candles(et(9, 54), [(100, 103, 99, 102), (102, 105, 101, 104), (104, 105, 101, 102)])


def test_profit_lock_not_armed_below_arm_level(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 2.90, rising()) == []      # +0.10 profit: below PROFIT_LOCK_ARM
    assert tick(pm, et(10, 4), 3.00, rising()) == []      # gave it all back, but never armed
    assert "A" in pm.positions


def test_profit_lock_giveback_exits_once_armed(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    open_put_spread(pm, broker, qty=2)
    assert tick(pm, et(10, 2), 2.80, rising()) == []      # +0.20 arms the rule, best = 2.80
    assert tick(pm, et(10, 4), 2.89, rising()) == []      # gave back 0.09 < 0.10
    broker.spread_close_price = 2.90
    fills = tick(pm, et(10, 6), 2.90, rising())           # gave back 0.10 -> lock it in
    assert len(fills) == 1 and fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK"
    assert fills[0]["qty"] == 2 and fills[0]["pnl"] == pytest.approx(20.0)
    assert pm.positions == {}


def test_profit_lock_momentum_flip_exits_once_armed(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 2.80, rising()) == []
    broker.spread_close_price = 2.82
    fills = tick(pm, et(10, 4), 2.82, falling_last())     # still +0.18 but last candle closed red
    assert len(fills) == 1 and fills[0]["exit_reason"] == "PROFIT_LOCK_MOMENTUM"
    assert fills[0]["pnl"] == pytest.approx(18.0)


def test_profit_lock_momentum_flip_ignored_before_arming(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 2.95, falling_last()) == []


def test_profit_lock_disabled(pm, broker, monkeypatch):
    # shared exit defaults (what B runs); A's own tuning is covered by the test_a_* tests
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {}, "B": {}})
    monkeypatch.setattr(config, "PROFIT_LOCK_ENABLED", False)
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 2.80, rising()) == []
    assert tick(pm, et(10, 4), 2.95, falling_last()) == []
    assert "A" in pm.positions


# ------------------------------------------------------------ profit floor and stale timer (A only)

def floor_tuning(monkeypatch, **extra):
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {**config.A_BASE_TUNING, **extra}, "B": {}})


def test_profit_floor_arms_and_exits_at_floor(pm, broker, journal, monkeypatch):
    floor_tuning(monkeypatch, PROFIT_FLOOR_BY_WIDTH={5: (0.10, 0.05)})
    open_put_spread(pm, broker, qty=2)                       # $5-wide, credit 3.00
    assert tick(pm, et(10, 2), 2.92, rising()) == []         # +0.08: not armed
    assert pm.positions["A"].floor_price is None
    assert tick(pm, et(10, 4), 2.90, rising()) == []         # +0.10 arms -> floor at 2.95
    assert pm.positions["A"].floor_price == 2.95
    assert events(journal, "FLOOR_SET")[0]["profit_floor"] == 0.05
    assert tick(pm, et(10, 6), 2.94, rising()) == []         # above the floor in profit terms
    broker.spread_close_price = 2.96
    fills = tick(pm, et(10, 8), 2.95, rising())              # back to the floor -> out with +0.05
    assert fills[0]["exit_reason"] == "PROFIT_FLOOR" and fills[0]["qty"] == 2 and pm.positions == {}


def test_profit_floor_keeps_runner_going(pm, broker, monkeypatch):
    floor_tuning(monkeypatch, PROFIT_FLOOR_BY_WIDTH={5: (0.10, 0.05)})
    open_put_spread(pm, broker, qty=2)
    assert tick(pm, et(10, 2), 2.70, rising()) == []         # target -> runner, floor also set (2.95)
    p = pm.positions["A"]
    assert p.runner is True and p.floor_price == 2.95
    assert tick(pm, et(10, 4), 2.40, rising()) == []         # runs on
    broker.spread_close_price = 2.71
    fills = tick(pm, et(10, 6), 2.70, rising())              # runner stop at the target level fires first
    assert fills[0]["exit_reason"] == "RUNNER_STOP"


def test_profit_floor_width_10_uses_its_own_step(pm, broker, monkeypatch):
    floor_tuning(monkeypatch, PROFIT_FLOOR_BY_WIDTH={10: (0.20, 0.15), 5: (0.10, 0.05)})
    broker.set_chain("P", {7515.0: (10.9, 11.1), 7505.0: (4.9, 5.1)})
    broker.add_spread_position("P", 7515.0, 7505.0, 1, 11.0, 5.0)
    pm.open(OpenSpread(strategy="A", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                       short_strike=7515.0, long_strike=7505.0, width=10, qty=1, entry_credit=6.00,
                       entry_time=et(10, 0), current_price=6.0, best_price=6.0, profit_target=0.30, stop_loss=0.55))
    assert tick(pm, et(10, 2), 5.85, rising()) == []         # +0.15: below the $10 arm of 0.20
    assert pm.positions["A"].floor_price is None
    assert tick(pm, et(10, 4), 5.80, rising()) == []         # +0.20 arms -> floor at 5.85
    assert pm.positions["A"].floor_price == 5.85


def test_stale_timer_sets_breakeven_floor_only_when_in_profit(pm, broker, journal, monkeypatch):
    floor_tuning(monkeypatch, STALE_TIMER_MIN=5)
    open_put_spread(pm, broker, qty=1)                       # entered 10:00
    assert tick(pm, et(10, 4), 2.90, rising()) == []         # 4 min: too early
    assert pm.positions["A"].floor_price is None
    assert tick(pm, et(10, 5), 3.05, rising()) == []         # 5 min but under water: no floor
    assert pm.positions["A"].floor_price is None
    assert tick(pm, et(10, 6), 2.90, rising()) == []         # 6 min, +0.10 -> floor at +0.05 (2.95), stays in
    assert pm.positions["A"].floor_price == 2.95
    assert events(journal, "FLOOR_SET")[0]["why"] == "STALE_TIMER 5m"
    broker.spread_close_price = 2.96
    fills = tick(pm, et(10, 8), 2.95, rising())
    assert fills[0]["exit_reason"] == "PROFIT_FLOOR" and fills[0]["pnl"] == pytest.approx(4.0)


def test_stale_timer_with_tiny_profit_exits_at_once(pm, broker, monkeypatch):
    # in profit but under the +0.05 floor when the timer fires: the floor is already breached -> take what is there
    floor_tuning(monkeypatch, STALE_TIMER_MIN=5)
    open_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 2.98
    fills = tick(pm, et(10, 6), 2.97, rising())
    assert fills[0]["exit_reason"] == "PROFIT_FLOOR" and fills[0]["pnl"] == pytest.approx(2.0)


def test_stale_timer_ignored_once_runner_started(pm, broker, monkeypatch):
    floor_tuning(monkeypatch, STALE_TIMER_MIN=5)
    open_put_spread(pm, broker, qty=1)
    tick(pm, et(10, 2), 2.70, rising())                      # runner
    assert tick(pm, et(10, 9), 2.60, rising()) == []
    assert pm.positions["A"].floor_price is None


def test_floor_never_set_for_b_by_default(pm, broker):
    open_b_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 9), 0.85, rising(), strat="B") == []
    assert pm.positions["B"].floor_price is None
    assert config.PROFIT_FLOOR_BY_WIDTH == {} and config.STALE_TIMER_MIN is None


def test_floor_survives_state_roundtrip(pm, broker, monkeypatch):
    floor_tuning(monkeypatch, PROFIT_FLOOR_BY_WIDTH={5: (0.10, 0.05)})
    spread = open_put_spread(pm, broker, qty=1)
    tick(pm, et(10, 2), 2.90, rising())
    assert OpenSpread.from_dict(spread.to_dict()).floor_price == 2.95


# ------------------------------------------------------------ deep-runner trail tightening (A only)

def test_a_runner_trail_tightens_past_2_deep_profit(pm, broker):
    open_put_spread(pm, broker, qty=2, credit=6.00)   # $5-wide fixture chain caps width at 5 but credit/target math is independent
    assert tick(pm, et(10, 2), 5.70, rising()) == []              # target -> runner starts, best 5.70
    p = pm.positions["A"]
    assert p.runner is True and p.runner_best == 5.70
    assert tick(pm, et(10, 4), 4.50, rising()) == []               # best now 4.50: profit 1.50, still below the $2 deep threshold
    assert p.runner_best == 4.50
    broker.spread_close_price = 4.90
    assert tick(pm, et(10, 6), 4.79, rising()) == []               # +0.29 giveback: below the still-wide 0.50 trail
    assert tick(pm, et(10, 8), 3.95, rising()) == []               # best 3.95: profit 2.05, past the $2 deep threshold now
    broker.spread_close_price = 4.30
    fills = tick(pm, et(10, 10), 4.26, rising())                   # +0.31 giveback from 3.95: trips the tightened 0.30 trail
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and pm.positions == {}


def test_a_runner_trail_stays_050_before_deep_profit(pm, broker):
    open_put_spread(pm, broker, qty=2, credit=6.00)
    assert tick(pm, et(10, 2), 5.70, rising()) == []
    p = pm.positions["A"]
    assert tick(pm, et(10, 4), 5.00, rising()) == []               # profit 1.00: below the $2 threshold, trail is 0.50
    assert tick(pm, et(10, 6), 5.29, rising()) == []               # +0.29 giveback: not enough for the wide trail
    broker.spread_close_price = 5.51
    fills = tick(pm, et(10, 8), 5.50, rising())                    # +0.50 giveback trips the untightened trail
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL"


def test_b_runner_trail_never_tightens(pm, broker):
    open_b_put_spread(pm, broker, qty=4, credit=3.00)
    fills = tick(pm, et(10, 2), 2.70, rising(), strat="B")         # B books half at target (fraction 0.5)
    assert fills and fills[0]["exit_reason"] == "TARGET_HALF" and fills[0]["qty"] == 2
    p = pm.positions["B"]
    assert p.runner is True and p.remaining == 2
    assert tick(pm, et(10, 4), 0.80, rising(), strat="B") == []    # best 0.80: profit 3.00-0.80=2.20, past $2, but B never tightens
    broker.spread_close_price = 1.31
    assert tick(pm, et(10, 6), 1.29, rising(), strat="B") == []    # +0.49 giveback: still under B's flat 0.50 trail
    fills = tick(pm, et(10, 8), 1.30, rising(), strat="B")         # +0.50 giveback trips it
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["strategy"] == "B"

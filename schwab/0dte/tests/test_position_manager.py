import json
from datetime import date

import pytest

import config
from conftest import et, make_candles
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager, classify_by_moneyness, occ_root, option_legs, spxw_legs
from strategy import BEARISH, BULLISH

TODAY = date(2026, 9, 17)
SPOT = 7500.0


@pytest.fixture(autouse=True)
def _stop_confirm_at_two_ticks(monkeypatch):
    # These tests call pm.on_tick() directly (never scheduler's sleep loop), so TICK_SECONDS only
    # matters here as the divisor STOP_CONFIRM_SECONDS is scaled by. A's STOP_CONFIRM_SECONDS=30 at
    # the real TICK_SECONDS=1 needs 30 ticks to confirm; these tests were written around exactly 2
    # confirming ticks, so pin TICK_SECONDS=15 here to get that back (2 * 15 = 30, matching A's real
    # setting) rather than rewriting every test to loop 30 times.
    monkeypatch.setattr(config, "TICK_SECONDS", 15)


@pytest.fixture(autouse=True)
def _legacy_exit_flow(monkeypatch):
    # 2026-09-30: every strategy defaults to config.STAGED_EXIT_ENABLED=True now (test_staged_exit.py
    # covers that path). This whole file predates the staged system and exercises the flat
    # target/stop/lock/runner sequence _tick_position still falls back to when it's off -- that
    # sequence is unchanged code, just no longer the default, so these tests stay valid against it.
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_quote_sanity_gate(monkeypatch):
    # 2026-09-30: every strategy also defaults to config.QUOTE_JUMP_SANITY_PCT != None now
    # (test_quote_sanity.py covers that path directly). This file's tests deliberately jump straight
    # to specific price points across ticks to exercise the runner/trail/lock mechanics in isolation --
    # not simulating a real gradual tick-by-tick sequence -- so the sanity gate would otherwise hold
    # many of them as "implausible," which has nothing to do with what these tests are checking.
    monkeypatch.setattr(config, "QUOTE_JUMP_SANITY_PCT", None)


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
    assert tick(pm, et(10, 2), 3.50, rising()) == []          # below the 0.60 stop
    assert tick(pm, et(10, 4), 3.60, rising()) == []          # 1st confirming print at the stop: held
    assert pm.positions["A"].stop_streak == 1
    broker.spread_close_price = 3.65
    fills = tick(pm, et(10, 6), 3.62, rising())               # 2nd confirming print: closes
    assert len(fills) == 1 and fills[0]["exit_reason"] == "STOP_LOSS"
    assert fills[0]["qty"] == 2 and fills[0]["pnl"] == pytest.approx(-130.0)
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


def test_a_profit_lock_arms_at_015_and_gives_back_005(pm, broker):
    # 2026-09-29: retuned tighter (arm +0.15, giveback 0.05) after a full week at 0% win rate where
    # the looser 0.30/0.35 let gains that had gotten close to target give ALL the way back to a loss.
    open_put_spread(pm, broker, qty=2)
    assert tick(pm, et(10, 2), 2.90, rising()) == []      # +0.10: below A's 0.15 arm
    assert tick(pm, et(10, 4), 2.95, rising()) == []      # worse print, still unarmed, no exit
    assert tick(pm, et(10, 6), 2.80, rising()) == []      # +0.20 arms, best 2.80
    assert tick(pm, et(10, 8), 2.83, rising()) == []      # gave back 0.03 < 0.05
    broker.spread_close_price = 2.85
    fills = tick(pm, et(10, 10), 2.85, rising())          # gave back 0.05 -> lock
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
    assert (exit_setting("A", "PROFIT_LOCK_ARM"), exit_setting("A", "PROFIT_LOCK_GIVEBACK")) == (0.15, 0.05)
    open_b_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 0.80, rising(), strat="B") == []           # +0.20 arms B's lock
    broker.spread_close_price = 0.90
    fills = tick(pm, et(10, 4), 0.90, rising(), strat="B")                # 0.10 giveback -> B locks
    assert fills[0]["strategy"] == "B" and fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK"


def test_exit_row_records_trigger_and_slippage(pm, broker):
    spread = open_put_spread(pm, broker, qty=1)
    spread.entry_mid = 3.05
    assert tick(pm, et(10, 2), 3.60, rising()) == []       # 1st confirming print at the stop: held
    broker.spread_close_price = 3.65
    fills = tick(pm, et(10, 4), 3.61, rising())            # 2nd confirming print: closes
    row = fills[0]
    assert row["trigger_price"] == 3.61 and row["exit_price"] == 3.65
    assert row["exit_slippage"] == pytest.approx(0.04)
    # 2026-10-01: a perfect, zero-slippage fill exactly at the trigger price would have been -61.00
    # (entry 3.00, trigger 3.61) vs the real -65.00 (entry 3.00, actual fill 3.65) -- isolates
    # execution-quality slippage from the decision itself, to help tell a paper-sim fill artifact
    # apart from a real script problem.
    assert row["pnl_at_trigger"] == pytest.approx(-61.00)
    assert row["pnl"] == pytest.approx(-65.00)
    assert row["entry_mid"] == 3.05 and row["entry_slippage"] == pytest.approx(0.05)
    # 2026-09-30: prev-tick diagnostic fields (a real live anomaly -- a reported price jumped an
    # implausible amount in a single tick right before an exit). prev_tick_price is current_price as
    # of the 10:02 tick (3.60, the tick immediately before the closing one), NOT the entry credit and
    # NOT the exit fill -- isolating the decision-price jump (trigger vs. prior tick) from fill
    # slippage (fill vs. this tick's trigger, already covered by exit_slippage above).
    assert row["prev_tick_price"] == pytest.approx(3.60)
    assert row["prev_tick_jump"] == pytest.approx(0.01)                 # 3.61 trigger - 3.60 prior tick
    assert row["prev_tick_pnl"] == pytest.approx((3.00 - 3.60) * 100)    # entry 3.00, 1 contract


def test_exit_row_prev_tick_compares_against_entry_on_the_very_first_tick(pm, broker):
    # No REAL prior tick exists yet -- current_price is initialized to entry_credit at construction,
    # so the first tick's "prior" naturally reads as the entry credit itself (a real, if less
    # interesting, baseline), not None. B has no STOP_CONFIRM_SECONDS override, so it closes on the
    # very first qualifying tick.
    open_b_put_spread(pm, broker, qty=1, credit=1.00)
    fills = tick(pm, et(10, 2), 1.50, rising(), strat="B")   # >= B's 0.50 stop, closes immediately
    row = fills[0]
    assert row["exit_reason"] == "STOP_LOSS"
    assert row["prev_tick_price"] == pytest.approx(1.00)
    assert row["prev_tick_jump"] == pytest.approx(0.50)
    assert row["prev_tick_pnl"] == pytest.approx(0.00)


def test_exit_row_prev_tick_fields_none_on_a_force_close_with_no_prior_tick(pm, broker):
    # force_close() never runs _tick_position (it closes directly), so a position closed before its
    # first real tick has no prev_price at all -- None, not a misleading fabricated comparison.
    open_b_put_spread(pm, broker, qty=1, credit=1.00)
    fills = pm.force_close(et(15, 30), {"B": 1.20})
    row = fills[0]
    assert row["prev_tick_price"] is None
    assert row["prev_tick_pnl"] is None
    assert row["prev_tick_jump"] is None


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

    # kept above the $1.00 deep-profit line (runner_best 2.10 -> profit 0.90) so the flat $0.50 trail applies
    assert tick(pm, et(10, 4), 2.10, rising()) == []
    assert p.runner_best == 2.10
    assert tick(pm, et(10, 6), 2.55, rising()) == []
    broker.spread_close_price = 2.60
    fills = tick(pm, et(10, 8), 2.60, rising())
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["qty"] == 2
    assert fills[0]["runner"] == "y" and fills[0]["pnl"] == pytest.approx(80.0)
    assert fills[0]["position_pnl"] == pytest.approx(110.0)
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
    assert tick(pm, et(10, 2), 3.60, rising()) == []       # 1st confirming print: held
    broker.close_failures = 1
    broker.spread_close_price = 3.65
    fills = tick(pm, et(10, 4), 3.61, rising())            # 2nd confirming print: closes (after 1 retry)
    assert len(fills) == 1 and pm.positions == {}
    assert len(broker.close_calls) == 2
    failed = events(journal, "CLOSE_FAILED")
    assert len(failed) == 1 and failed[0]["strategy"] == "A"


def test_close_gives_up_after_max_retries(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    open_put_spread(pm, broker, qty=1)
    assert tick(pm, et(10, 2), 3.60, rising()) == []       # 1st confirming print: held
    broker.close_failures = 5
    fills = tick(pm, et(10, 4), 3.61, rising())            # 2nd confirming print: attempts close, exhausts retries
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
    open_put_spread(pm, broker, qty=2)          # A: entry 3.00, stop 3.60 (2 confirming ticks)
    open_b_put_spread(pm, broker, qty=1)        # B: entry 1.00, stop 1.50
    assert set(pm.positions) == {"A", "B"}
    assert pm.total_open_risk() == pytest.approx(2 * 200.0 + 400.0)
    assert pm.on_tick(et(10, 2), {"A": 3.60, "B": 1.20}, rising()) == []   # A's 1st confirming print
    broker.spread_close_price = 3.65
    fills = pm.on_tick(et(10, 4), {"A": 3.61, "B": 1.20}, rising())        # A's 2nd confirming print
    assert [f["strategy"] for f in fills] == ["A"] and fills[0]["exit_reason"] == "STOP_LOSS"
    assert set(pm.positions) == {"B"}
    b = pm.positions["B"]
    assert b.current_price == 1.20 and b.remaining == 1
    assert broker.close_calls == [{"right": "P", "short": 7510.0, "long": 7505.0, "qty": 2}]


def test_b_hits_its_own_wider_stop_a_unaffected(pm, broker):
    open_put_spread(pm, broker, qty=1)
    open_b_put_spread(pm, broker, qty=1)
    # B stops at +0.50 (1.50) while A, at +0.10, is nowhere near its 0.60 stop
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
    assert pm.on_tick(et(10, 2), {"A": 3.60}, rising()) == []   # A's 1st confirming print
    broker.spread_close_price = 3.65
    fills = pm.on_tick(et(10, 4), {"A": 3.61}, rising())        # A's 2nd confirming print: closes
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
    assert text[0].endswith("runner,trigger_price,exit_slippage,pnl_at_trigger,entry_mid,entry_slippage,"
                           "prev_tick_price,prev_tick_pnl,prev_tick_jump,ticker,expiration,mfe,mae,scanner_score")
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
                       entry_time=et(10, 0), current_price=6.0, best_price=6.0, profit_target=0.30, stop_loss=0.60))
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


# ------------------------------------------------------------ stop confirmation (A: 2 ticks; B: 1, unchanged)

def test_a_stop_needs_two_confirming_ticks(pm, broker, journal):
    open_put_spread(pm, broker, qty=2, credit=6.00)          # A stop at 6.60 (config.STOP_LOSS = 0.60)
    assert tick(pm, et(10, 2), 6.60, rising()) == []         # 1st print at the stop: held, not confirmed
    assert pm.positions["A"].stop_streak == 1
    broker.spread_close_price = 6.65
    fills = tick(pm, et(10, 4), 6.62, rising())              # 2nd consecutive print: confirmed, closes
    assert fills[0]["exit_reason"] == "STOP_LOSS" and pm.positions == {}
    assert events(journal, "EXIT")[0]["qty"] == 2


def test_a_stop_streak_resets_if_price_recovers(pm, broker):
    open_put_spread(pm, broker, qty=2, credit=6.00)
    assert tick(pm, et(10, 2), 6.60, rising()) == []         # 1st print at the stop
    assert pm.positions["A"].stop_streak == 1
    assert tick(pm, et(10, 4), 6.40, rising()) == []         # pulls back below the stop: streak resets
    assert pm.positions["A"].stop_streak == 0
    assert tick(pm, et(10, 6), 6.60, rising()) == []         # back at the stop: this is a fresh 1st print
    assert pm.positions["A"].stop_streak == 1
    assert "A" in pm.positions


def test_b_stop_is_still_immediate(pm, broker):
    open_b_put_spread(pm, broker, qty=2, credit=1.00)        # B stop at 1.50
    broker.spread_close_price = 1.52
    fills = tick(pm, et(10, 2), 1.50, rising(), strat="B")   # single print: B has no confirm-ticks override
    assert fills[0]["exit_reason"] == "STOP_LOSS" and fills[0]["strategy"] == "B"


# ------------------------------------------------------------ tighter deep-runner trail (threshold lowered to $1.00)

def test_a_runner_trail_tightens_past_1_deep_profit(pm, broker):
    open_put_spread(pm, broker, qty=2, credit=6.00)
    assert tick(pm, et(10, 2), 5.70, rising()) == []          # target -> runner, best 5.70
    assert tick(pm, et(10, 4), 4.90, rising()) == []          # best 4.90: profit 1.10, past the new $1.00 threshold
    p = pm.positions["A"]
    assert p.runner_best == 4.90
    broker.spread_close_price = 5.21
    fills = tick(pm, et(10, 6), 5.20, rising())               # +0.30 giveback trips the now-active tight trail
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and pm.positions == {}


# ------------------------------------------------------------ Strategy F: stock roots, per-position exits

EXP2 = date(2026, 9, 19)


def f_spread(root: str = "TSLA", qty: int = 1, credit: float = 0.18, width: float = 1.0, **kw) -> OpenSpread:
    from strategy import f_exit_params
    ex = f_exit_params(width)
    return OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root=root, expiration=EXP2,
                      short_strike=100.0, long_strike=100.0 - width, width=width, qty=qty, entry_credit=credit,
                      entry_time=et(9, 55), current_price=credit, best_price=credit, profit_target=ex["profit_target"],
                      stop_loss=ex["stop_loss"], overrides=ex["overrides"], entry_mid=credit, **kw)


def open_f(pm: PositionManager, broker: FakeBroker, root: str = "TSLA", qty: int = 1, width: float = 1.0,
           credit: float = 0.18) -> OpenSpread:
    broker.set_chain("P", {100.0: (0.45, 0.55), 100.0 - width: (0.28, 0.36)}, underlying=root)
    broker.add_spread_position("P", 100.0, 100.0 - width, qty, 0.50, 0.32, EXP2, root=root)
    spread = f_spread(root, qty, credit, width)
    pm.open(spread)
    return spread


def test_open_spread_key_ticker_underlying_mfe_mae():
    a = OpenSpread(strategy="A", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                   short_strike=7510.0, long_strike=7505.0, width=5, qty=1, entry_credit=3.0, entry_time=et(10, 0),
                   current_price=3.0, best_price=2.8, profit_target=0.3, stop_loss=0.6, worst_price=3.4)
    assert (a.key, a.ticker, a.underlying, a.mfe, a.mae) == ("A", None, "SPX", 0.2, 0.4)
    f = f_spread()
    assert (f.key, f.ticker, f.underlying, f.mfe, f.mae) == ("F:TSLA", "TSLA", "TSLA", 0.0, 0.0)
    f.current_price = 0.28
    assert f.unrealized_pnl == pytest.approx(-10.0) and a.unrealized_pnl == 0.0


def test_option_legs_filters_by_root_and_spxw_legs_is_the_spxw_subset(broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 1, 8.1, 5.05)
    broker.add_spread_position("P", 100.0, 99.0, 1, 0.5, 0.32, EXP2, root="TSLA")
    positions = broker.get_positions() + [{"symbol": "AAPL", "asset_class": "stock", "qty": 10, "avg_price": 100.0}]
    assert {occ_root(l["symbol"]) for l in option_legs(positions)} == {"SPXW", "TSLA"}
    assert {occ_root(l["symbol"]) for l in option_legs(positions, {"TSLA"})} == {"TSLA"}
    assert option_legs(positions, {"SPXW"}) == spxw_legs(positions) and len(spxw_legs(positions)) == 2
    assert option_legs(positions, set()) == []


def test_from_dict_of_a_pre_f_state_and_roundtrip_with_overrides_and_scan():
    old = {"strategy": "B", "direction": "BULLISH", "setup": "ORB", "right": "P", "root": "SPXW",
           "expiration": "2026-09-17", "short_strike": 7450.0, "long_strike": 7445.0, "width": 5, "qty": 1,
           "entry_credit": 1.0, "entry_time": et(10, 0).isoformat(), "current_price": 1.0, "best_price": 1.0,
           "profit_target": 0.3, "stop_loss": 0.5}
    restored = OpenSpread.from_dict(old)
    assert restored.key == "B" and restored.overrides is None and restored.scan is None and restored.worst_price is None
    f = f_spread(width=2.5, scan={"score": 71.5, "confirm": {"vwap": 99.1}}, entry_bid=0.12, entry_ask=0.24)
    assert OpenSpread.from_dict(f.to_dict()) == f and f.to_dict()["overrides"]["TRAIL_AMOUNT"] == 0.15


def test_f_knobs_use_width_scaled_overrides_a_keeps_exit_setting(pm, broker):
    f = open_f(pm, broker, credit=0.20)                                       # $1-wide
    assert (f.target_price, f.stop_price) == (0.14, 0.30)
    assert (pm._knob(f, "PROFIT_LOCK_ARM"), pm._knob(f, "PROFIT_LOCK_GIVEBACK"), pm._knob(f, "TRAIL_AMOUNT")) == (0.05, 0.05, 0.06)
    assert pm._knob(f, "STOP_CONFIRM_TICKS", 1) == 1 and pm._knob(f, "RUNNER_MOMENTUM_GATE", True) is True
    a = open_put_spread(pm, broker, qty=1)
    assert (pm._knob(a, "PROFIT_LOCK_ARM"), pm._knob(a, "PROFIT_LOCK_GIVEBACK"), pm._knob(a, "TRAIL_AMOUNT")) == (0.15, 0.05, 0.50)
    a.runner_best = a.entry_credit
    assert pm._runner_trail(f) == 0.06 and pm._runner_trail(a) == config.TRAIL_AMOUNT
    # arms at +0.05, tolerates a 0.04 giveback, locks at a 0.05 giveback
    assert pm.on_tick(et(10, 2), {"F:TSLA": 0.15}, None) == []
    assert pm.on_tick(et(10, 3), {"F:TSLA": 0.19}, None) == []
    broker.spread_close_price = 0.20
    fills = pm.on_tick(et(10, 4), {"F:TSLA": 0.20}, None)
    assert fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK" and fills[0]["key"] == "F:TSLA" and "F:TSLA" not in pm.positions
    assert "A" in pm.positions


def test_f_runner_trails_by_six_percent_of_width(pm, broker):
    f = open_f(pm, broker, qty=2, width=5.0)                                  # $5-wide, credit 0.18 -> target -0.12
    f.entry_credit, f.best_price, f.current_price, f.entry_mid = 0.90, 0.90, 0.90, 0.90
    rising_ = make_candles(et(9, 40), [(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(3)], minutes=5)
    broker.spread_close_price = 0.60
    fills = pm.on_tick(et(10, 2), {"F:TSLA": 0.60}, None, {"F:TSLA": rising_})
    assert fills[0]["exit_reason"] == "TARGET_HALF" and fills[0]["qty"] == 1 and f.runner is True
    assert pm.on_tick(et(10, 4), {"F:TSLA": 0.20}, None, {"F:TSLA": rising_}) == []
    assert pm.on_tick(et(10, 5), {"F:TSLA": 0.49}, None, {"F:TSLA": rising_}) == []   # +0.29 < the 0.30 trail
    broker.spread_close_price = 0.52
    fills = pm.on_tick(et(10, 6), {"F:TSLA": 0.50}, None, {"F:TSLA": rising_})
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and fills[0]["runner"] == "y" and pm.positions == {}


def test_exact_tie_moves_arm_the_lock_fire_the_giveback_and_trip_the_trail(pm, broker):
    # 0.15 - 0.10 = 0.04999... and 0.10 + 0.05 = 0.15000...02 in binary: compared at 2 dp, a 5-cent move
    # arms a 0.05 lock and a 5-cent giveback closes, exactly as on any other credit
    f = open_f(pm, broker, credit=0.15)                                       # $1-wide: arm 0.05, giveback 0.05
    assert pm.on_tick(et(10, 2), {"F:TSLA": 0.10}, None) == []               # armed on the exact tie
    broker.spread_close_price = 0.15
    fills = pm.on_tick(et(10, 3), {"F:TSLA": 0.15}, None)
    assert fills[0]["exit_reason"] == "PROFIT_LOCK_GIVEBACK" and pm.positions == {}
    # runner trail: best 0.27 + 0.30 = 0.5700000000000001 must still trip on a 0.57 print
    f = open_f(pm, broker, qty=2, width=5.0)
    f.entry_credit, f.best_price, f.current_price, f.entry_mid = 0.90, 0.90, 0.90, 0.90
    rising_ = make_candles(et(9, 40), [(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(3)], minutes=5)
    broker.spread_close_price = 0.60
    assert pm.on_tick(et(10, 4), {"F:TSLA": 0.60}, None, {"F:TSLA": rising_})[0]["exit_reason"] == "TARGET_HALF"
    assert pm.on_tick(et(10, 5), {"F:TSLA": 0.27}, None, {"F:TSLA": rising_}) == []
    broker.spread_close_price = 0.57
    fills = pm.on_tick(et(10, 6), {"F:TSLA": 0.57}, None, {"F:TSLA": rising_})
    assert fills[0]["exit_reason"] == "RUNNER_TRAIL" and pm.positions == {}


def test_open_pnl_includes_what_partial_fills_already_booked(pm, broker):
    f = open_f(pm, broker, qty=4, width=5.0)
    f.entry_credit, f.best_price, f.current_price, f.entry_mid = 0.90, 0.90, 0.90, 0.90
    rising_ = make_candles(et(9, 40), [(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(3)], minutes=5)
    broker.spread_close_price = 0.60
    fills = pm.on_tick(et(10, 2), {"F:TSLA": 0.60}, None, {"F:TSLA": rising_})
    assert (fills[0]["exit_reason"], fills[0]["qty"], fills[0]["position_closed"]) == ("TARGET_HALF", 2, False)
    assert f.realized_pnl == pytest.approx(60.0) and f.remaining == 2
    f.current_price = 0.90                                                    # runner marks flat at entry
    assert pm.unrealized_pnl(("F",)) == 0.0 and pm.open_pnl(("F",)) == pytest.approx(60.0)
    f.current_price = 0.70
    assert pm.unrealized_pnl(("F",)) == pytest.approx(40.0) and pm.open_pnl(("F",)) == pytest.approx(100.0)
    assert pm.open_pnl(("A", "B")) == 0.0


def test_worst_price_and_exit_row_fields(pm, broker):
    f = open_f(pm, broker)
    assert pm.on_tick(et(10, 2), {"F:TSLA": 0.15}, None) == []
    assert pm.on_tick(et(10, 3), {"F:TSLA": 0.24}, None) == []
    assert (f.best_price, f.worst_price, f.mfe, f.mae) == (0.15, 0.24, pytest.approx(0.03), pytest.approx(0.06))
    broker.spread_close_price = 0.30
    row = pm.on_tick(et(10, 4), {"F:TSLA": 0.28}, None)[0]
    assert (row["key"], row["ticker"], row["expiration"], row["exit_reason"]) == ("F:TSLA", "TSLA", "2026-09-19", "STOP_LOSS")
    assert (row["mfe"], row["mae"], row["scanner_score"], row["direction_match"]) == (pytest.approx(0.03), pytest.approx(0.10), None, None)
    a = open_put_spread(pm, broker, qty=1)
    broker.spread_close_price = 3.65
    tick(pm, et(10, 5), 3.60, rising())
    row = tick(pm, et(10, 6), 3.61, rising())[0]
    assert (row["key"], row["ticker"], row["expiration"], row["mae"], row["scanner_score"]) == ("A", None, "2026-09-17", pytest.approx(0.61), None)


def test_on_tick_routes_ticker_candles_to_f_and_never_the_spx_frame(pm, broker):
    f = open_f(pm, broker, qty=2)
    open_put_spread(pm, broker, qty=2)
    spx = rising()
    broker.spread_close_price = 0.12
    fills = pm.on_tick(et(10, 2), {"F:TSLA": 0.12, "A": 2.70}, spx, {"F:TSLA": None})
    assert [fl["exit_reason"] for fl in fills] == ["PROFIT_TARGET"] and fills[0]["qty"] == 2   # no frame: no runner
    assert pm.positions["A"].runner is True and pm.positions["A"].momentum_at_target == pytest.approx(2.0)   # A got the SPX frame
    f2 = open_f(pm, broker, "AMZN", qty=2)
    fills = pm.on_tick(et(10, 4), {"F:AMZN": 0.12}, spx, {"F:AMZN": spx})
    assert fills[0]["exit_reason"] == "TARGET_HALF" and f2.runner is True                 # its own frame: runner


def test_adopt_stock_pair_as_f_matching_saved_state_by_root(pm, broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 2, 8.10, 5.05)
    broker.set_chain("P", {100.0: (0.45, 0.55), 99.0: (0.28, 0.36)}, underlying="TSLA")
    broker.add_spread_position("P", 100.0, 99.0, 3, 0.50, 0.32, EXP2, root="TSLA")
    broker.set_chain("P", {100.0: (0.45, 0.55), 97.5: (0.15, 0.25)}, underlying="AMZN")
    broker.add_spread_position("P", 100.0, 97.5, 1, 0.55, 0.20, EXP2, root="AMZN")
    saved = f_spread("TSLA", qty=3, scan={"score": 66.0})
    saved.best_price = 0.11
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT, {"F:TSLA": saved.to_dict()})
    assert {s.key for s in adopted} == {"A", "F:TSLA", "F:AMZN"} and set(pm.positions) == {"A", "F:TSLA", "F:AMZN"}
    t = pm.positions["F:TSLA"]
    assert t.setup == "STOCK_ORB" and t.best_price == 0.11 and t.scan == {"score": 66.0} and t.qty == 3
    assert t.current_price == pytest.approx(0.18)
    a = pm.positions["F:AMZN"]
    assert (a.strategy, a.setup, a.root, a.width, a.direction, a.entry_credit) == ("F", "ADOPTED", "AMZN", 2.5, BULLISH, 0.35)
    assert (a.profit_target, a.stop_loss) == (0.15, 0.25) and a.overrides["TRAIL_AMOUNT"] == 0.15
    assert pm.positions["A"].strategy == "A" and pm.has_root("AMZN") and not pm.has_root("META")
    assert pm.adopt_from_broker(broker.get_positions(), et(10, 16), SPOT) == []            # already tracked
    assert [e["strategy"] for e in events(pm.journal, "ADOPTED")].count("F") == 2


def test_adopt_roots_filter_keeps_stock_legs_out_for_spx_only_personas(pm, broker):
    broker.add_spread_position("P", 7510.0, 7505.0, 2, 8.10, 5.05)
    broker.add_spread_position("P", 100.0, 99.0, 1, 0.50, 0.32, EXP2, root="TSLA")
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), SPOT, roots={"SPXW"})
    assert [s.key for s in adopted] == ["A"] and set(pm.positions) == {"A"}
    # a third SPXW pair never lands on F: the free-slot list is the SPX strategies only
    broker.add_spread_position("P", 7450.0, 7445.0, 1, 2.50, 1.50)
    broker.set_chain("C", {7550.0: (2.4, 2.6), 7560.0: (0.9, 1.1)})
    broker.add_spread_position("C", 7550.0, 7560.0, 1, 2.50, 1.00)
    pm.adopt_from_broker(broker.get_positions(), et(10, 16), SPOT, roots={"SPXW"})
    assert set(pm.positions) == {"A", "B"}


def test_duplicate_key_is_tracked_not_raised(pm, broker, journal):
    first = open_f(pm, broker)
    second = f_spread()
    assert pm.open(second) == "F:TSLA#2"
    assert set(pm.positions) == {"F:TSLA", "F:TSLA#2"} and pm.key_of(second) == "F:TSLA#2" and pm.key_of(first) == "F:TSLA"
    dup = events(journal, "DUPLICATE_POSITION_KEY")
    assert len(dup) == 1 and dup[0]["tracked_as"] == "F:TSLA#2" and pm.open_count("F") == 2
    broker.spread_close_price = 0.30
    row = pm.on_tick(et(10, 4), {"F:TSLA#2": 0.28}, None)[0]
    assert row["key"] == "F:TSLA#2" and set(pm.positions) == {"F:TSLA"}


def test_remaining_at_broker_filters_by_root(pm, broker):
    broker.add_spread_position("P", 100.0, 99.0, 2, 8.10, 5.05)              # SPXW legs on the same strikes
    f = open_f(pm, broker, qty=1)
    assert pm._remaining_at_broker(f) == 1
    broker._reduce_spread("TSLA", EXP2, "P", 100.0, 99.0, 1)
    assert pm._remaining_at_broker(f) == 0                                    # the SPXW legs do not count

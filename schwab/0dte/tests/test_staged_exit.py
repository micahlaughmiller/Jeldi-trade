"""2026-09-29/30 staged exit system (PositionManager._staged_tick): every strategy (A-F all use the
same signals, per the user's own instruction) replaces the flat target/stop/lock/runner sequence
with stage0 raw stop -> stage1 arm+trail+momentum-reversal -> stage2 hard floor -> stage3 debounced
runner trail. config.STAGED_EXIT_ENABLED defaults True, so these tests run against the real default
(no monkeypatching needed) -- test_position_manager.py/test_scheduler_f.py explicitly turn it back
off to keep exercising the legacy flow it falls back to.
"""
from datetime import date

import pytest

import config
from conftest import et, make_candles
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager
from strategy import BEARISH, BULLISH
from strategy import exit_levels as strategy_exit_levels

TODAY = date(2026, 9, 17)


def events(journal, kind):
    import json
    rows = [json.loads(line) for path in sorted(journal.dir.glob("events_*.jsonl")) for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(equity=10_000, today=TODAY)


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path, "TEST")


@pytest.fixture
def pm(broker, journal) -> PositionManager:
    return PositionManager(broker, journal)


def open_spread(pm, broker, strategy="B", qty=2, credit=2.00, width=5.0, right="P") -> OpenSpread:
    short, long = (7450.0, 7445.0) if right == "P" else (7550.0, 7555.0)
    broker.add_spread_position(right, short, long, qty, credit + width, width)
    direction = BULLISH if right == "P" else BEARISH
    target, stop = strategy_exit_levels(strategy)
    spread = OpenSpread(strategy=strategy, direction=direction, setup="ORB", right=right, root="SPXW",
                        expiration=TODAY, short_strike=short, long_strike=long, width=width, qty=qty,
                        entry_credit=credit, entry_time=et(10, 0), current_price=credit, best_price=credit,
                        profit_target=target, stop_loss=stop)
    pm.open(spread)
    return spread


def open_f_spread(pm, broker, root="TSLA", qty=2, credit=0.80, width=3.0) -> OpenSpread:
    broker.add_spread_position("P", 417.5, 412.5, qty, credit + width, width, root=root)
    spread = OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root=root,
                        expiration=TODAY, short_strike=417.5, long_strike=412.5, width=width, qty=qty,
                        entry_credit=credit, entry_time=et(9, 55), current_price=credit, best_price=credit,
                        profit_target=config.F_PROFIT_TARGET, stop_loss=config.F_STOP_LOSS)
    pm.open(spread)
    return spread


def flat(n: int = 3):
    """Directionless candles: no momentum either way (small, alternating bodies)."""
    return make_candles(et(9, 54), [(100, 100.5, 99.5, 100.2) for _ in range(n)])


def rising(n: int = 3):
    return make_candles(et(9, 54), [(100 + 2 * i, 103 + 2 * i, 99 + 2 * i, 102 + 2 * i) for i in range(n)])


def red_candle_against_bullish():
    # A single completed candle that closes below its open -- candle_against(BULLISH) reads True.
    return make_candles(et(9, 54), [(100, 100.5, 97, 97.5)])


def tick(pm, when, price, candles, key="B"):
    return pm.on_tick(when, {key: price}, candles)


# ---------------------------------------------------------------------------------- stage 0: stop

def test_stage0_stop_closes_immediately_at_default_confirm(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    # B has no STOP_CONFIRM_SECONDS override -> confirm resolves to 1 tick regardless of TICK_SECONDS.
    fills = tick(pm, et(10, 5), 2.40, flat())   # entry 2.00 + STAGE0_STOP_PCT(0.08)*5 = 2.40
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_stage0_stop_holds_for_a_until_confirmed(pm, broker, monkeypatch):
    # A's STOP_CONFIRM_SECONDS=30; pin TICK_SECONDS=15 so that's exactly 2 confirming ticks.
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    open_spread(pm, broker, strategy="A", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 2.40, flat(), key="A") == []           # 1st confirming print, holds
    fills = tick(pm, et(10, 6), 2.41, flat(), key="A")
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_stage0_stop_streak_resets_if_price_recovers(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    open_spread(pm, broker, strategy="A", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 2.40, flat(), key="A") == []
    assert tick(pm, et(10, 6), 2.10, flat(), key="A") == []           # recovers below stop -> resets
    assert tick(pm, et(10, 7), 2.40, flat(), key="A") == []           # only the 1st confirming print again


# ---------------------------------------------------------------------------------- stage 1: arm

def test_stage1_arms_at_20_cents_but_does_not_close_immediately(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    # profit 0.20 (STAGE1_ARM_PCT*5) -- best_price just became 1.80, the 0.15 trail (1.95) is untouched.
    assert tick(pm, et(10, 5), 1.80, flat()) == []


def test_stage1_trail_exits_on_15_cent_giveback_from_best(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 1.80, flat()) == []                    # arms at +0.20
    fills = tick(pm, et(10, 6), 1.95, flat())                          # gives back 0.15 from 1.80
    assert fills and fills[0]["exit_reason"] == "STAGE1_TRAIL"


def test_stage1_momentum_reversal_exits_before_the_trail_gives_back(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 1.80, flat()) == []                    # arms at +0.20
    # Price barely moves (nowhere near the 0.15 trail) but the last candle closed against the trade.
    fills = tick(pm, et(10, 6), 1.81, red_candle_against_bullish())
    assert fills and fills[0]["exit_reason"] == "STAGE1_MOMENTUM_REVERSAL"


def test_stage1_bb_edge_proximity_also_counts_as_reversal_risk(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 1.80, flat()) == []                    # arms at +0.20
    # 20 flat-ish candles so the Bollinger band is tight, then a sharp new high -- pct_b pinned >= 0.95
    # against a BULLISH position (price stretched to the upper band = exhaustion risk), no red candle.
    bars = [(100, 100.3, 99.7, 100.0) for _ in range(19)] + [(100.0, 104.0, 100.0, 104.0)]
    candles = make_candles(et(9, 30), bars)
    fills = tick(pm, et(10, 6), 1.81, candles)
    assert fills and fills[0]["exit_reason"] == "STAGE1_MOMENTUM_REVERSAL"


# ---------------------------------------------------------------------------------- stage 2: floor

def test_stage2_floor_exits_unconditionally_even_with_reversing_momentum(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 1.70, flat()) == []                    # +0.30 target -> stage2 arms
    # Floor at +0.25 (1.75) -- "no matter what," so even a candle closed against the trade doesn't
    # change the reason; it's the unconditional floor, not a momentum check.
    fills = tick(pm, et(10, 6), 1.75, red_candle_against_bullish())
    assert fills and fills[0]["exit_reason"] == "STAGE2_FLOOR"


def test_stage2_does_not_arm_before_the_30_cent_target(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    assert tick(pm, et(10, 5), 1.80, flat()) == []                    # stage1 only
    # 1.75 would be the stage-2 floor price, but stage 2 has never armed (target not yet hit) --
    # stage-1's trail (best 1.80 + 0.15 = 1.95) isn't touched either, so this stays open.
    assert tick(pm, et(10, 6), 1.75, flat()) == []


# ---------------------------------------------------------------------------- stage 3: debounce

def test_stage3_trail_does_not_ratchet_to_an_unheld_new_best(pm, broker):
    open_spread(pm, broker, strategy="B", credit=4.00, width=10.0)
    assert tick(pm, et(10, 0, 0), 3.40, flat()) == []                  # +0.60 target -> arms at 3.40
    assert tick(pm, et(10, 0, 5), 3.00, flat()) == []                  # new best, unconfirmed
    # Unratcheted trail sits at 3.40+0.30=3.70 (safe); a bug that ratchets immediately would sit at
    # 3.00+0.30=3.30 and wrongly close here. Floor is 3.50, also clear of this probe.
    assert tick(pm, et(10, 0, 20), 3.40, rising()) == []                # only 15s since the 3.00 best


def test_stage3_trail_ratchets_after_30s_held_with_momentum_then_exits(pm, broker):
    open_spread(pm, broker, strategy="B", credit=4.00, width=10.0)
    assert tick(pm, et(10, 0, 0), 3.40, flat()) == []                   # +0.60 target -> arms at 3.40
    assert tick(pm, et(10, 0, 5), 3.00, flat()) == []                   # new best, unconfirmed
    # 35s after the 3.00 best was set, still holding (no deeper low since) with momentum continuing:
    # ratchets the anchor to 3.00, new trail 3.00+0.30=3.30, and 3.40 already clears it.
    fills = tick(pm, et(10, 0, 40), 3.40, rising())
    assert fills and fills[0]["exit_reason"] == "STAGE3_TRAIL"


# ------------------------------------------------------------------------------- shared behavior

def test_a_and_b_share_identical_staged_thresholds(pm, broker):
    # 2026-09-30: "A should be tuned the same, I use the same signals for A" -- no per-strategy
    # override of any STAGE*_PCT constant, so A and B (same width/credit) hit every stage at the
    # exact same spread price.
    open_spread(pm, broker, strategy="A", qty=2, credit=2.00, width=5.0)
    open_spread(pm, broker, strategy="B", qty=2, credit=2.00, width=5.0, right="C")   # own contracts (no STRIKE_COLLISION)
    fills = pm.on_tick(et(10, 5), {"A": 1.70, "B": 1.70}, flat())       # both cross the 0.30 target
    assert fills == []
    fills = pm.on_tick(et(10, 6), {"A": 1.75, "B": 1.75}, flat())       # both hit the 0.25 floor
    reasons = {f["strategy"]: f["exit_reason"] for f in fills}
    assert reasons == {"A": "STAGE2_FLOOR", "B": "STAGE2_FLOOR"}


def test_f_scales_staged_thresholds_by_its_own_width(pm, broker, pre_f_stop_fix):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)
    # STAGE0_STOP_PCT(0.08) * width(3) = 0.24 -> stop at 0.80+0.24=1.04.
    assert p.width == 3.0
    fills = tick(pm, et(9, 56), 1.04, flat(), key="F:TSLA")
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_staged_exit_disabled_falls_back_to_legacy_flat_stop(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)
    # Legacy B_STOP_LOSS=0.50 (flat, not width-scaled) -> stop at 2.50, NOT the staged 2.40.
    assert tick(pm, et(10, 5), 2.40, flat()) == []
    fills = tick(pm, et(10, 6), 2.50, flat())
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


# ------------------------------------------------------- F stop hold and time confirmation (2026-10-07)

def test_f_stop_timing_defaults():
    import strategy
    assert [strategy.exit_setting("F", k) for k in ("STOP_MIN_HOLD_SEC", "STOP_HOLD_EMERGENCY_MULT", "STOP_CONFIRM_TIME_SEC")] \
        == [60, 3.0, 30]
    assert [strategy.exit_setting("B", k) for k in ("STOP_MIN_HOLD_SEC", "STOP_CONFIRM_TIME_SEC")] == [0, 0]


def test_f_stop_is_ignored_in_the_first_minute(pm, broker):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)             # stop 1.04, emergency 0.80 + 3 x 0.24 = 1.52, entry 9:55
    assert tick(pm, et(9, 55, 20), 1.10, flat(), key="F:TSLA") == []   # past the stop, inside the hold
    assert tick(pm, et(9, 55, 40), 1.20, flat(), key="F:TSLA") == []
    assert p.stop_first_at is None and "F:TSLA" in pm.positions


def test_f_emergency_stop_still_fires_inside_the_hold(pm, broker):
    open_f_spread(pm, broker, credit=0.80, width=3.0)
    fills = tick(pm, et(9, 55, 30), 1.52, flat(), key="F:TSLA")
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_f_stop_needs_thirty_seconds_above_the_stop_after_the_hold(pm, broker):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)
    assert tick(pm, et(10, 0, 0), 1.10, flat(), key="F:TSLA") == [] and p.stop_first_at == et(10, 0, 0)
    assert tick(pm, et(10, 0, 15), 1.12, flat(), key="F:TSLA") == []
    fills = tick(pm, et(10, 0, 30), 1.10, flat(), key="F:TSLA")
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_f_stop_confirmation_restarts_if_the_price_recovers(pm, broker):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)
    assert tick(pm, et(10, 0, 0), 1.10, flat(), key="F:TSLA") == []
    assert tick(pm, et(10, 0, 15), 0.95, flat(), key="F:TSLA") == [] and p.stop_first_at is None
    assert tick(pm, et(10, 0, 30), 1.10, flat(), key="F:TSLA") == [] and p.stop_first_at == et(10, 0, 30)
    assert tick(pm, et(10, 0, 50), 1.10, flat(), key="F:TSLA") == []                    # only 20 s this time
    assert tick(pm, et(10, 1, 0), 1.10, flat(), key="F:TSLA")[0]["exit_reason"] == "STOP_LOSS"


def test_other_strategies_stop_exactly_as_before(pm, broker):
    open_spread(pm, broker, strategy="B", credit=2.00, width=5.0)      # entered 10:00
    fills = tick(pm, et(10, 0, 10), 2.40, flat())                      # 10 s after entry, at the stop: still immediate
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_stop_first_at_survives_a_state_round_trip(pm, broker):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)
    p.stop_first_at = et(10, 0, 5)
    assert OpenSpread.from_dict(p.to_dict()).stop_first_at == et(10, 0, 5)
    p.stop_first_at = None
    assert OpenSpread.from_dict(p.to_dict()).stop_first_at is None


def test_f_emergency_stop_fires_at_once_after_the_hold_too(pm, broker):
    p = open_f_spread(pm, broker, credit=0.80, width=3.0)             # stop 1.04, emergency 1.52
    fills = tick(pm, et(10, 0, 0), 1.52, flat(), key="F:TSLA")        # well past the 60 s hold: no 30 s wait for a collapse
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS" and p.stop_first_at is None


def test_a_failed_verification_read_does_not_lose_the_fills_of_the_other_positions(pm, broker, monkeypatch):
    from broker import BrokerError
    open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    open_spread(pm, broker, strategy="C", qty=1, credit=2.00, width=5.0, right="C")
    real = pm._remaining_at_broker
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] == 2:                                            # the read after the SECOND close order fails (a 429)
            raise BrokerError("429 too many requests")
        return real(p)
    monkeypatch.setattr(pm, "_remaining_at_broker", flaky)
    fills = pm.force_close(et(13, 0), {"B": 2.0, "C": 2.0}, override_hold=True)
    assert sorted(f["strategy"] for f in fills) == ["B", "C"] and pm.positions == {}
    assert not any(f.get("external") for f in fills)                   # booked at their fills, not as hand-closes


def test_force_close_continues_past_a_broker_error_in_one_position(pm, broker, monkeypatch):
    from broker import BrokerError
    open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    open_spread(pm, broker, strategy="C", qty=1, credit=2.00, width=5.0, right="C")
    real_close = pm._close

    def boom(p, *a, **k):
        if p.strategy == "B":
            raise BrokerError("boom")
        return real_close(p, *a, **k)
    monkeypatch.setattr(pm, "_close", boom)
    fills = pm.force_close(et(13, 0), {"B": 2.0, "C": 2.0}, override_hold=True)
    assert [f["strategy"] for f in fills] == ["C"] and set(pm.positions) == {"B"}


# ------------------------------------------------- a close that fails on every attempt is latched (2026-10-08)

def test_a_failed_close_pass_is_latched_with_a_backoff_and_halts_entries(pm, broker, journal, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    p = open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    broker.close_failures = 99
    assert tick(pm, et(10, 5, 0), 2.40, flat()) == []                   # stop: the close fails on both attempts
    assert (p.close_fail_count, p.close_blocked_until) == (1, et(10, 5, 30)) and pm.any_close_blocked() is True
    assert len(broker.close_calls) == 2
    assert tick(pm, et(10, 5, 10), 2.45, flat()) == [] and len(broker.close_calls) == 2        # latched: nothing is sent
    assert [e["fail_count"] for e in events(journal, "CLOSE_BLOCKED")] == [1]
    assert tick(pm, et(10, 5, 31), 2.45, flat()) == [] and len(broker.close_calls) == 4        # backoff over: one more pass
    assert (p.close_fail_count, p.close_blocked_until) == (2, et(10, 6, 31))                    # 60 s this time
    assert [e["fail_count"] for e in events(journal, "CLOSE_BLOCKED")] == [1, 2]


def test_the_backoff_grows_and_then_repeats_its_last_step(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    p = open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    broker.close_failures = 99
    now, waits = et(10, 5, 0), []
    for _ in range(6):
        tick(pm, now, 2.40, flat())
        waits.append(int((p.close_blocked_until - now).total_seconds()))
        now = p.close_blocked_until
    assert waits == [30, 60, 120, 300, 300, 300]


def test_a_later_successful_close_clears_the_latch(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    p = open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    broker.close_failures = 1
    assert tick(pm, et(10, 5, 0), 2.40, flat()) == [] and p.close_fail_count == 1
    fills = tick(pm, et(10, 5, 31), 2.40, flat())
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS" and pm.positions == {} and pm.any_close_blocked() is False


def test_the_hard_stop_and_force_close_ignore_the_latch(pm, broker, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    broker.close_failures = 1
    tick(pm, et(10, 5, 0), 2.40, flat())
    assert pm.any_close_blocked() is True
    rows = pm.force_close(et(10, 5, 5), {"B": 2.4}, override_hold=True)         # 25 s into the backoff, but this is the last resort
    assert [r["exit_reason"] for r in rows] == ["FORCE_CLOSE"] and pm.positions == {}


def test_the_latch_survives_a_state_round_trip(pm, broker):
    p = open_spread(pm, broker, strategy="B", qty=1, credit=2.00, width=5.0)
    p.close_fail_count, p.close_blocked_until = 2, et(10, 7)
    again = OpenSpread.from_dict(p.to_dict())
    assert (again.close_fail_count, again.close_blocked_until) == (2, et(10, 7))
    p.close_blocked_until = None
    assert OpenSpread.from_dict(p.to_dict()).close_blocked_until is None

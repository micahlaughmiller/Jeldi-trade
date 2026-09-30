"""2026-09-30 quote-jump sanity gate (PositionManager._sanitize_quote): the actual fix for a live
anomaly where a single bad/stale option-chain print reached an exit decision directly (ASTRA reported
5.21 one tick, exited ~6.10 the very next -- too large a move for that one real second). See
config.py's quote-jump-sanity section for the full rationale. Applies to every strategy uniformly,
ahead of both the staged and legacy exit paths (it sits at the very top of _tick_position).
"""
import json
from datetime import date

import pytest

import config
from conftest import et, make_candles
from fake_broker import FakeBroker
from journal import Journal
from position_manager import OpenSpread, PositionManager
from strategy import BULLISH

TODAY = date(2026, 9, 17)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(equity=10_000, today=TODAY)


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path, "TEST")


@pytest.fixture
def pm(broker, journal) -> PositionManager:
    return PositionManager(broker, journal)


def open_spread(pm, broker, strategy="B", qty=2, credit=2.00, width=5.0) -> OpenSpread:
    broker.add_spread_position("P", 7450.0, 7445.0, qty, credit + width, width)
    spread = OpenSpread(strategy=strategy, direction=BULLISH, setup="ORB", right="P", root="SPXW",
                        expiration=TODAY, short_strike=7450.0, long_strike=7445.0, width=width, qty=qty,
                        entry_credit=credit, entry_time=et(10, 0), current_price=credit, best_price=credit,
                        profit_target=config.B_PROFIT_TARGET, stop_loss=config.B_STOP_LOSS)
    pm.open(spread)
    return spread


def open_f_spread(pm, broker, root="TSLA", qty=2, credit=0.15, width=1.0) -> OpenSpread:
    broker.add_spread_position("P", 417.5, 412.5, qty, credit + width, width)
    spread = OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root=root,
                        expiration=TODAY, short_strike=417.5, long_strike=412.5, width=width, qty=qty,
                        entry_credit=credit, entry_time=et(9, 55), current_price=credit, best_price=credit,
                        profit_target=config.F_PROFIT_TARGET, stop_loss=config.F_STOP_LOSS)
    pm.open(spread)
    return spread


def flat(n: int = 3):
    return make_candles(et(9, 54), [(100, 100.5, 99.5, 100.2) for _ in range(n)])


def tick(pm, when, price, key="B"):
    return pm.on_tick(when, {key: price}, flat())


def tick_f(pm, when, price, key="F:TSLA"):
    return pm.on_tick(when, {key: price}, flat(), {key: flat()})


def events(journal: Journal, kind: str) -> list[dict]:
    path = journal.dir / f"events_{journal.slug}_{TODAY.isoformat()}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def test_first_ever_tick_passes_through_any_size_of_move(pm, broker):
    # No prior accepted price exists yet -- nothing to sanity-check against, so even a huge jump from
    # the entry credit on the very first tick is trusted immediately (this is normal: the first real
    # quote after entry can legitimately be far from the entry credit).
    open_spread(pm, broker, credit=2.00, width=5.0)
    fills = tick(pm, et(10, 0, 1), 2.60)   # would be a "STOP_LOSS" candidate too, but that's not the point here
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


def test_normal_small_move_is_never_gated(pm, broker):
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 1), 2.10)   # first tick, accepted as the baseline
    fills = tick(pm, et(10, 0, 2), 2.15)   # tiny, ordinary 5-cent move a second later
    assert fills == []
    assert p.current_price == 2.15   # accepted immediately, no pending/hold


def test_implausible_jump_is_held_not_acted_on(pm, broker, journal):
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 1), 1.70)   # first tick: accepted baseline, deep in profit
    # One second later, an implausible jump to a STOP-triggering price (would be a real exit if
    # trusted) -- bound at width 5.0 is max(0.05, 0.06*5)=0.30 per nominal tick; this is a $0.80 jump.
    fills = tick(pm, et(10, 0, 2), 2.50)
    assert fills == []                        # held: no exit decision made on this tick
    assert p.current_price == 1.70             # NOT advanced -- still the last accepted price
    held = events(journal, "QUOTE_JUMP_HELD")
    assert held and held[0]["from_price"] == 1.70 and held[0]["to_price"] == 2.50


def test_held_jump_that_does_not_repeat_is_dropped(pm, broker):
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 1), 1.70)
    tick(pm, et(10, 0, 2), 2.50)      # implausible spike, held
    # Next tick reverts back near the pre-spike level (but clear of the 1.75 stage-2 floor, so a real
    # close doesn't confound the assertion) -- the spike didn't repeat, so it's dropped and this
    # (in-bound, relative to 1.70) reading is accepted normally instead.
    fills = tick(pm, et(10, 0, 3), 1.65)
    assert fills == []
    assert p.current_price == 1.65
    assert p.pending_quote is None


def test_held_jump_that_repeats_is_confirmed_and_acted_on(pm, broker, journal):
    # A jump big enough that even the elapsed-time widening (test_wider_elapsed_gap_widens_the_
    # allowed_jump) can't explain acceptance on its own within the confirm window -- isolates the
    # "two consistent readings" confirmation path specifically, not just "waited long enough."
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 1), 1.70)
    tick(pm, et(10, 0, 2), 4.20)      # implausible spike #1, held
    assert p.current_price == 1.70   # still not advanced
    tick(pm, et(10, 0, 3), 4.21)      # spike #2, essentially the same reading -- but not yet 2s held
    assert p.current_price == 1.70   # still held (QUOTE_JUMP_CONFIRM_SECONDS=2 not yet elapsed)
    # Third consistent reading, now >= 2s since the pending value was first seen: confirmed as real.
    fills = tick(pm, et(10, 0, 4), 4.22)
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"
    confirmed = events(journal, "QUOTE_JUMP_CONFIRMED")
    assert confirmed and confirmed[0]["to_price"] == 4.22


def test_gate_can_be_disabled_globally(pm, broker, monkeypatch):
    # exit_setting() treats an explicit None in a per-strategy override dict as "not set" (falls
    # through to the module default), same as every other exit knob -- so disabling really does mean
    # the module-level default, not a per-strategy override of None.
    monkeypatch.setattr(config, "QUOTE_JUMP_SANITY_PCT", None)
    open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 1), 1.70)
    # With the gate off, the same implausible jump that test_implausible_jump_is_held_not_acted_on
    # holds is instead acted on immediately.
    fills = tick(pm, et(10, 0, 2), 2.50)
    assert fills and fills[0]["exit_reason"] == "STOP_LOSS"


# --------------------------------------------------------------------------------------------------
# 2026-09-30, post-review: an independent adversarial review of the first version of this gate (before
# it was ever pushed) found the cadence allowance was measured off real WALL-CLOCK elapsed time since
# the last accepted price -- including time spent stuck holding an unconfirmed quote. That meant the
# bound kept widening open the longer a bad reading went unconfirmed, so eventually ANY value (not
# just the one actually pending) would slip through the plain jump<=bound check on its own, without
# ever needing a second consistent reading -- silently defeating the whole point of requiring
# confirmation. The same design also made Strategy F (whose real per-ticker cadence is ~15x
# TICK_SECONDS) effectively ungated in ordinary operation: on a thin width the resulting bound covered
# nearly the entire feasible price range. The tests below cover the two fixes that replaced it: a
# FIXED, capped per-strategy cadence multiplier (not measured elapsed time), and an explicit
# QUOTE_JUMP_MAX_HOLD_SECONDS backstop so a position still can't get stuck refusing to tick forever.

def test_f_gets_a_capped_cadence_allowance_not_the_full_real_gap(pm, broker):
    # F's real per-ticker cadence is ~15x TICK_SECONDS (F_PRICE_EVERY_SEC=15) -- QUOTE_JUMP_MAX_NOMINAL_
    # TICKS caps that down to 5x, so a jump this large (50% of a $1 width) is still correctly flagged,
    # not silently passed through the way an uncapped 15x allowance would (that would cover 90% of the
    # width, i.e. almost nothing gets caught).
    p = open_f_spread(pm, broker, credit=0.15, width=1.0)
    tick_f(pm, et(9, 56, 0), 0.15)              # first tick ever, baseline
    fills = tick_f(pm, et(9, 56, 15), 0.65)     # 15s later (F's real cadence) -- a 50-cent jump
    assert fills == []
    assert p.current_price == 0.15              # NOT accepted -- held pending instead
    assert p.pending_quote == 0.65


def test_repeated_different_readings_stay_held_not_silently_accepted(pm, broker):
    # The bug this guards against: several DIFFERENT anomalous readings in a row, each never quite
    # matching the previous one, must NOT eventually slip through just because real time has passed --
    # only an explicit confirmation (two consistent readings) or the explicit max-hold backstop may
    # ever accept one. None of these ticks should ever be treated as a normal in-bound reading.
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 0), 1.70)
    for i, price in enumerate([2.20, 2.90, 3.50], start=1):
        tick(pm, et(10, 0, i), price)
        assert p.current_price == 1.70   # still stuck -- none of these was ever silently accepted


def test_max_hold_forces_through_a_persistently_different_reading(pm, broker, journal):
    # A genuinely fast, continuously-evolving REAL move that never repeats a value long enough to
    # confirm must not leave the position stuck forever -- QUOTE_JUMP_MAX_HOLD_SECONDS is the explicit,
    # logged backstop that replaces the safety property the old (broken) elapsed-time widening used to
    # provide by accident.
    p = open_spread(pm, broker, credit=2.00, width=5.0)
    tick(pm, et(10, 0, 0), 1.70)                      # accepted -- stuck_since is still None here
    for i, price in enumerate([2.20, 2.55, 2.85, 3.10], start=1):
        tick(pm, et(10, 0, i), price)                 # held -- stuck_since set at et(10,0,1), the FIRST hold
    assert p.current_price == 1.70   # still stuck (4s since stuck_since), not confirmed or forced yet
    # QUOTE_JUMP_MAX_HOLD_SECONDS=6, measured from stuck_since (et(10,0,1)) not the last accepted
    # price: at et(10,0,7), 6 seconds stuck, force through whatever the current reading is.
    tick(pm, et(10, 0, 7), 3.30)
    assert p.current_price == 3.30
    forced = events(journal, "QUOTE_JUMP_FORCED")
    assert forced and forced[0]["to_price"] == 3.30

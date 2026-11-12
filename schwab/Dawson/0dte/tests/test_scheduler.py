"""Entry flow of the Bot with a FakeBroker: one signal opens A and B independently."""

import json
from datetime import date, timedelta

import pytest

import config
import scheduler
from conftest import et
from fake_broker import FakeBroker
from risk_manager import RiskManager
from strategy import BULLISH

TODAY = date(2026, 9, 17)
SPOT = 7600.0


def put_chain() -> dict[float, tuple[float, float]]:
    # A: 7610/7605 pays 3.00. B: 7560/7555 pays 0.60 (too low) -> 7565/7560 pays 0.90 (in range).
    return {
        7610.0: (7.9, 8.1), 7605.0: (4.9, 5.1), 7600.0: (19.0, 21.0),
        7565.0: (1.9, 2.1), 7560.0: (1.0, 1.2), 7555.0: (0.4, 0.6), 7550.0: (0.2, 0.3),
    }


@pytest.fixture
def bot(tmp_path, monkeypatch) -> scheduler.Bot:
    broker = FakeBroker(equity=10_000, today=TODAY)
    broker.spot = SPOT
    broker.set_chain("P", put_chain())
    broker.set_chain("C", {7600.0: (21.0, 23.0)})   # ATM straddle: 22 + 20 = 42
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: broker)
    # These tests call pm.on_tick()/bot.tick() directly rather than the real sleep loop, and were
    # written around exactly 2 confirming ticks for A's stop -- pin TICK_SECONDS=15 so that still
    # holds (A's real STOP_CONFIRM_SECONDS=30 / 15 = 2 ticks) rather than rewriting every such test.
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    b = scheduler.Bot(dry_run=False, once=True)
    b.today = TODAY
    b.risk = RiskManager(10_000)
    return b


def events(bot: scheduler.Bot, kind: str) -> list[dict]:
    # ENTRY events are stamped with the real fill time, so read every day file the journal wrote
    rows = [json.loads(line) for path in sorted(bot.journal.dir.glob(f"events_{bot.journal.slug}_*.jsonl"))
            for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def test_a_walks_one_strike_toward_spot_when_credit_is_deep(bot):
    # spot 7605.5: base 7615/7610 pays 3.40 (above the 3.25 bias) -> A walks to 7610/7605 at 3.00.
    # ATM straddle at 7605: call 22 + put 2 = EM 24 -> B starts at 7585 and takes 7585/7580 at 0.90.
    bot.broker.spot = 7605.5
    bot.broker.set_chain("C", {7605.0: (21.0, 23.0)})
    rows = put_chain()
    rows.update({7615.0: (8.3, 8.5), 7610.0: (4.9, 5.1), 7605.0: (1.9, 2.1),
                 7585.0: (2.9, 3.1), 7580.0: (2.0, 2.2)})
    bot.broker.set_chain("P", rows)
    bot.enter(et(10, 15), "ORB", BULLISH)
    a, b = bot.pm.positions["A"], bot.pm.positions["B"]
    assert (a.short_strike, a.long_strike, a.entry_credit) == (7610.0, 7605.0, 3.00)
    assert (b.short_strike, b.long_strike, b.entry_credit) == (7585.0, 7580.0, 0.90)
    rejected = events(bot, "ENTRY_REJECTED")
    assert rejected == []


def test_signal_opens_a_then_b(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    a, b = bot.pm.positions["A"], bot.pm.positions["B"]
    assert (a.short_strike, a.long_strike, a.qty, a.entry_credit) == (7610.0, 7605.0, 2, 3.00)
    assert (a.profit_target, a.stop_loss) == (0.30, 0.60)
    assert (b.short_strike, b.long_strike, b.entry_credit) == (7565.0, 7560.0, 0.90)
    assert (b.profit_target, b.stop_loss) == (0.30, 0.50)
    # B sized against A's just-placed risk: A = 2 x $200 = $400 open; B max loss $410 -> 5% of 10k -> 1
    assert b.qty == 1
    orders = [o for o in bot.broker.orders.values() if o["status"] == "filled"]
    assert [o["symbol"].startswith("SPXW260917P07610000") for o in orders] == [True, False]
    entries = events(bot, "ENTRY")
    assert [e["strategy"] for e in entries] == ["A", "B"]
    signal_line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at"))
    assert "A: sell 7610P buy 7605P x2 @ 3.00" in signal_line
    assert "B: sell 7565P buy 7560P x1 @ 0.90 EM 42.00" in signal_line
    assert bot.journal.load_state()["positions"].keys() == {"A", "B"}


def test_a_skipped_when_it_has_a_position_b_still_trades(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.positions.pop("B")
    bot.enter(et(10, 25), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at 10:25")][0]
    assert "A: skip: position open" in line and "B: sell 7565P" in line


def test_b_rejected_when_no_strike_in_range_a_still_trades(bot, caplog):
    caplog.set_level("INFO")
    chain = put_chain()
    chain[7565.0] = (1.3, 1.5)     # 7565/7560 now pays 0.30; nothing reaches 0.65 before the $10 guard
    chain[7570.0] = (1.5, 1.7)
    chain.update({s: (1.6, 1.8) for s in (7575.0, 7580.0, 7585.0, 7590.0, 7595.0)})
    bot.broker.set_chain("P", chain)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB")][0]
    assert "B: skip: B_NO_STRIKE_IN_RANGE" in line
    assert events(bot, "ENTRY_REJECTED")[0]["strategy"] == "B"


def test_blocked_strategy_does_not_block_the_other(bot):
    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        bot.risk.record_trade("A", -50.0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"B"}


def test_no_entry_when_both_blocked(bot, caplog):
    caplog.set_level("INFO")
    bot.risk.record_trade("A", -1_000.0)     # combined daily loss limit
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert bot.pm.positions == {}
    assert any("No new entries" in r.getMessage() and "DAILY_LOSS_LIMIT" in r.getMessage() for r in caplog.records)


def test_manage_prices_both_positions_from_one_chain_and_records_per_strategy(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    calls_before = len(bot.broker.close_calls)
    prices = bot.spread_prices()
    assert prices == {"A": 3.00, "B": 0.90}
    assert bot.pm.on_tick(et(10, 20), {"A": 3.60, "B": 0.95}, None) == []   # A's 1st confirming print
    bot.broker.spread_close_price = 3.65
    bot.record_fills(bot.pm.on_tick(et(10, 30), {"A": 3.61, "B": 0.95}, None))
    assert set(bot.pm.positions) == {"B"}
    assert len(bot.broker.close_calls) == calls_before + 1
    s = bot.risk.state
    assert s.for_strategy("A").trades_today == 1 and s.for_strategy("A").consecutive_losses == 1
    assert s.for_strategy("B").trades_today == 0
    assert bot.journal.trades_path.read_text().splitlines()[1].startswith("2026-09-17,A,")


# ------------------------------------------------------------ strategy A entry policy

def test_day_gate_blocks_a_when_session_reads_choppy(bot, caplog, monkeypatch):
    from conftest import make_candles
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {**config.A_BASE_TUNING, "DAY_GATE_ENABLED": True,
                                                               "DAY_GATE_MIN_ER": 0.5, "DAY_GATE_MIN_CANDLES": 2},
                                                             "B": {}})
    choppy = [(7600, 7601, 7599, 7600.2), (7600.2, 7601, 7599, 7599.8), (7599.8, 7601, 7599, 7600.2),
              (7600.2, 7601, 7599, 7599.8), (7599.8, 7601, 7599, 7600.1)]
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 30), choppy, minutes=2))
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"B"}   # B is unaffected by A's gate
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at 10:15")][0]
    assert "A: skip: DAY_GATE ER=" in line and "(choppy)" in line and "B: sell 7565P" in line


def test_day_gate_allows_a_when_session_reads_trending(bot, monkeypatch):
    from conftest import make_candles
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {**config.A_BASE_TUNING, "DAY_GATE_ENABLED": True,
                                                               "DAY_GATE_MIN_ER": 0.5, "DAY_GATE_MIN_CANDLES": 2},
                                                             "B": {}})
    trending = [(7600, 7601, 7599.9, 7600.5), (7600.5, 7601.5, 7600.4, 7601.0),
                (7601.0, 7602.0, 7600.9, 7601.5), (7601.5, 7602.5, 7601.4, 7602.0)]
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 30), trending, minutes=2))
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}


def test_day_gate_blocks_a_before_enough_candles_exist(bot, caplog, monkeypatch):
    from conftest import make_candles
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "EXIT_TUNING_BY_STRATEGY", {"A": {**config.A_BASE_TUNING, "DAY_GATE_ENABLED": True,
                                                               "DAY_GATE_MIN_ER": 0.1, "DAY_GATE_MIN_CANDLES": 5},
                                                             "B": {}})
    trending = [(7600, 7601, 7599.9, 7600.5), (7600.5, 7601.5, 7600.4, 7601.0)]   # only 2 candles, needs 5
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 30), trending, minutes=2))
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at 10:15")][0]
    assert "A: skip: DAY_GATE not enough session data yet (2 candle(s))" in line


def test_day_gate_off_by_default_for_both_strategies(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}   # no monkeypatch: DAY_GATE_ENABLED defaults False everywhere


def test_a_takes_both_orb_entry_kinds(bot):
    bot.enter(et(10, 15), "ORB", BULLISH, kind="MOMENTUM")
    assert set(bot.pm.positions) == {"A", "B"}
    bot.pm.positions.clear()
    bot.risk.state.for_strategy("A").last_exit = None
    bot.enter(et(10, 25), "ORB", BULLISH, kind="PULLBACK")
    assert set(bot.pm.positions) == {"A", "B"}


def test_entry_kind_policy_can_restrict_a(bot, caplog, monkeypatch):
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "ORB_ENTRY_KINDS_BY_STRATEGY", {"A": ("PULLBACK",), "B": ("MOMENTUM", "PULLBACK")})
    bot.enter(et(10, 15), "ORB", BULLISH, kind="MOMENTUM")
    assert set(bot.pm.positions) == {"B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at 10:15")][0]
    assert "A: skip: ORB MOMENTUM entry disabled for A" in line and "B: sell 7565P" in line


def test_overnight_setup_is_retired_for_both_strategies(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(9, 40), "OVERNIGHT", BULLISH)
    assert bot.pm.positions == {}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL OVERNIGHT")][0]
    assert "A: skip: OVERNIGHT setup disabled for A" in line
    assert "B: skip: OVERNIGHT setup disabled for B" in line   # both now trade the overnight levels via ON_BREAK


def test_a_cooldown_blocks_reentry_after_its_exit(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.on_tick(et(10, 28), {"A": 3.60}, None)                          # A's 1st confirming print
    bot.broker.spread_close_price = 3.65
    bot.record_fills(bot.pm.on_tick(et(10, 30), {"A": 3.61}, None))       # A stopped out at 10:30
    assert set(bot.pm.positions) == {"B"}
    bot.pm.positions.pop("B")
    bot.enter(et(10, 45), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at 10:45")][0]
    assert "A: skip: A: COOLDOWN until 11:00" in line
    bot.pm.positions.pop("B")
    bot.enter(et(11, 5), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}


def test_a_cooldown_does_not_block_a_different_setup(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.on_tick(et(10, 28), {"A": 3.60}, None)                        # A's 1st confirming print
    bot.broker.spread_close_price = 3.65
    bot.record_fills(bot.pm.on_tick(et(10, 30), {"A": 3.61}, None))     # A stopped out of an ORB position at 10:30
    assert set(bot.pm.positions) == {"B"}
    bot.pm.positions.pop("B")
    bot.enter(et(10, 45), "ON_BREAK", BULLISH)                          # a DIFFERENT setup, still inside the cool-down
    assert set(bot.pm.positions) == {"A", "B"}   # A's cool-down is what's being tested here; B trades ON_BREAK too
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ON_BREAK")]
    assert line and "A: sell" in line[0] and "COOLDOWN" not in line[0]


def test_entry_records_quote_mid_for_fill_quality(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert bot.pm.positions["A"].entry_mid == 3.00 and bot.pm.positions["B"].entry_mid == 0.90


# ------------------------------------------------------------------- interrupt

def test_interrupt_closes_every_open_spread(bot, caplog):
    caplog.set_level("WARNING")
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    bot.on_interrupt()
    assert bot.pm.positions == {} and bot.broker.get_positions() == []
    exits = events(bot, "EXIT")
    assert sorted(e["strategy"] for e in exits) == ["A", "B"]
    assert {e["exit_reason"] for e in exits} == {"INTERRUPT_CLOSE"}
    assert bot.risk.state.trades_today == 2
    assert any("closing them now" in r.getMessage() for r in caplog.records)
    assert json.loads(bot.journal.state_path.read_text())["positions"] == {}


def test_interrupt_leaves_positions_when_disabled(bot, monkeypatch, caplog):
    caplog.set_level("WARNING")
    monkeypatch.setattr(config, "CLOSE_ON_INTERRUPT", False)
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.on_interrupt()
    assert set(bot.pm.positions) == {"A", "B"} and bot.broker.close_calls == []
    assert any("OPEN POSITION [A]" in r.getMessage() for r in caplog.records)


def test_signal_handlers_route_to_keyboard_interrupt():
    import signal
    scheduler.install_signal_handlers()
    assert signal.getsignal(signal.SIGTERM) is scheduler._raise_interrupt
    with pytest.raises(KeyboardInterrupt):
        scheduler._raise_interrupt(signal.SIGTERM, None)


# ------------------------------------------------------------ ON_BREAK: overnight levels through the ORB state machine

def test_on_break_momentum_entry_delays_a_enters_b_immediately(bot, caplog):
    # 2026-09-24: A has MOMENTUM_CONFIRM_ENABLED by default (B does not) -- B trades the signal right
    # away, A waits for 1-minute follow-through instead of entering on the bare break-close margin.
    from conftest import make_candles
    from strategy import Levels
    caplog.set_level("INFO")
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624),      # 09:30 break: closes above 7620
            (7624, 7630, 7621, 7628)]      # 09:32 holds and closes above the break close -> momentum
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"B"}
    assert bot.pm.positions["B"].setup == "ON_BREAK"
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "ON_BREAK"]
    assert sig and sig[0]["trigger"] == "MOMENTUM" and sig[0]["direction"] == BULLISH
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ON_BREAK BULLISH at 09:34")][0]
    assert "B: sell 7565P" in line
    assert "ON_BREAK" in bot.pending_momentum
    p = bot.pending_momentum["ON_BREAK"]
    assert (p.direction, p.strategies, p.break_close) == (BULLISH, ("A",), 7624.0)
    pend = events(bot, "MOMENTUM_PENDING")
    assert pend and pend[0]["strategies"] == ["A"] and pend[0]["break_close"] == 7624.0
    # the same candles again do not re-fire the underlying ORB state machine
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert len([r for r in events(bot, "SIGNAL") if r["setup"] == "ON_BREAK"]) == 1


def test_momentum_confirmation_succeeds_after_two_beating_1m_candles(bot, caplog, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    caplog.set_level("INFO")
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert "A" not in bot.pm.positions
    m1 = make_candles(et(9, 35), [(7628, 7629, 7627.5, 7628.6), (7628.6, 7630, 7628, 7629.2)], minutes=1)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: m1)
    bot.check_pending_momentum(et(9, 37), scheduler.Phase.OVERNIGHT_ONLY)
    assert "ON_BREAK" not in bot.pending_momentum
    assert "A" in bot.pm.positions and bot.pm.positions["A"].setup == "ON_BREAK"
    assert events(bot, "MOMENTUM_CONFIRMED")[0]["strategies"] == ["A"]
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ON_BREAK] MOMENTUM confirmed")]
    assert line


def test_momentum_confirmation_aborts_on_a_reversing_candle(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    m1 = make_candles(et(9, 34), [(7628, 7629, 7627.5, 7628.6), (7628.6, 7627, 7620, 7621.0)], minutes=1)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: m1)
    bot.check_pending_momentum(et(9, 36), scheduler.Phase.OVERNIGHT_ONLY)
    assert "ON_BREAK" not in bot.pending_momentum
    assert "A" not in bot.pm.positions
    aborted = events(bot, "MOMENTUM_ABORTED")
    assert aborted and aborted[0]["reason"] == "reversed"


def test_momentum_confirmation_times_out(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 34), [], minutes=1))
    bot.check_pending_momentum(et(9, 34) + timedelta(minutes=config.MOMENTUM_CONFIRM_TIMEOUT_MIN + 1),
                               scheduler.Phase.OVERNIGHT_ONLY)
    assert "ON_BREAK" not in bot.pending_momentum and "A" not in bot.pm.positions
    assert events(bot, "MOMENTUM_ABORTED")[0]["reason"] == "timeout"


# -------------------------------------------------------- B continuation re-entry

def _done_on_break(bot):
    """Fire the standard ON_BREAK MOMENTUM signal, leave on_setup in DONE, and simulate B having
    already closed the position it took on that first entry (flat, ready for a re-entry check)."""
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert bot.on_setup.state == bot.on_setup.DONE and bot.on_setup.break_close == 7624.0
    bot.pm.positions.pop("B", None)   # B took the first entry and has since exited


def _spy_enter(bot, monkeypatch):
    """The fake broker's option chain only covers strikes near its fixture spot; a synthetic spot
    chosen just to clear the continuation margin has no real chain around it. Spy on enter() instead
    of requiring the full strike-selection/sizing pipeline to resolve for an arbitrary test spot."""
    calls = []
    monkeypatch.setattr(bot, "enter", lambda *a, **k: calls.append((a, k)))
    return calls


def test_continuation_reentry_fires_for_b_when_spot_still_confirms(bot, caplog, monkeypatch):
    caplog.set_level("INFO")
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0   # beats 7624.0 break by 5.0, well past the 0.5 margin
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:4] == ("ON_BREAK", BULLISH, "MOMENTUM") and kwargs["strategies"] == ("B",)
    ev = events(bot, "CONTINUATION_REENTRY")
    assert ev and ev[0]["strategies"] == ["B"] and ev[0]["spot"] == 7629.0
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ON_BREAK] CONTINUATION")]
    assert line


def test_continuation_reentry_skips_when_move_has_stalled(bot, monkeypatch):
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7624.2   # only beats by 0.2, under the 0.5 margin
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == [] and events(bot, "CONTINUATION_REENTRY") == []


def test_continuation_reentry_respects_the_pause(bot, monkeypatch):
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0
    bot.check_continuation_reentry(et(9, 35), scheduler.Phase.OVERNIGHT_ONLY)   # 1 min after the signal: too soon
    assert len(calls) == 0
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)   # 5 min after the signal: fires
    assert len(calls) == 1
    bot.broker.spot = 7635.0
    bot.check_continuation_reentry(et(9, 41), scheduler.Phase.OVERNIGHT_ONLY)   # 2 min later: still paced, no re-fire
    assert len(calls) == 1
    bot.check_continuation_reentry(et(9, 44), scheduler.Phase.OVERNIGHT_ONLY)   # 5 min after THAT fire: fires again
    assert len(calls) == 2


def test_continuation_reentry_stops_once_the_setup_resets(bot, monkeypatch):
    from conftest import make_candles
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.on_setup.update(make_candles(et(9, 40), [(7624, 7625, 7615, 7618)], minutes=2).iloc[0])   # closes back inside
    assert bot.on_setup.state == bot.on_setup.WAITING
    bot.broker.spot = 7629.0
    bot.check_continuation_reentry(et(9, 44), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == [] and events(bot, "CONTINUATION_REENTRY") == []


def test_continuation_reentry_does_not_apply_to_a_by_default(bot, monkeypatch):
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls[0][1]["strategies"] == ("B",)   # A never included: MOMENTUM_CONFIRM is on for A, REENTRY is not
    ev = events(bot, "CONTINUATION_REENTRY")
    assert ev and ev[0]["strategies"] == ["B"]


def test_continuation_reentry_skipped_if_b_already_has_a_position(bot, monkeypatch):
    _done_on_break(bot)
    calls = _spy_enter(bot, monkeypatch)
    bot.pm.positions["B"] = object()   # any occupant of the "B" slot marks the strategy as not flat
    bot.broker.spot = 7629.0
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == [] and events(bot, "CONTINUATION_REENTRY") == []


def test_momentum_confirmation_skipped_if_entry_window_closed_by_the_time_it_resolves(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    m1 = make_candles(et(9, 35), [(7628, 7629, 7627.5, 7628.6), (7628.6, 7630, 7628, 7629.2)], minutes=1)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: m1)
    bot.check_pending_momentum(et(9, 37), scheduler.Phase.CLOSED)
    assert "ON_BREAK" not in bot.pending_momentum and "A" not in bot.pm.positions
    assert events(bot, "MOMENTUM_ABORTED")[0]["reason"] == "entry window closed"


def test_on_break_entry_kind_gate_applies_to_on_break_too(bot, caplog, monkeypatch):
    # Regression test for the 2026-09-24 fix: the gate used to be hardcoded to setup=="ORB" and
    # silently did not apply to ON_BREAK signals at all.
    from conftest import make_candles
    from strategy import Levels
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "ORB_ENTRY_KINDS_BY_STRATEGY", {"A": ("PULLBACK",), "B": ("MOMENTUM", "PULLBACK")})
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]   # momentum entry_kind
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ON_BREAK BULLISH at 09:34")][0]
    assert "A: skip: ON_BREAK MOMENTUM entry disabled for A" in line and "B: sell 7565P" in line


def test_on_break_pullback_entry(bot):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624),      # break
            (7624, 7625, 7619, 7621),      # wick back to the level, holds
            (7621, 7623, 7620.5, 7622)]    # green close above -> pullback entry
    bot.try_on_break_entry(et(9, 36), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"A", "B"}
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "ON_BREAK"][0]
    assert sig["trigger"] == "PULLBACK"


def test_on_break_needs_levels_and_basis(bot):
    from conftest import make_candles
    bot.overnight = None
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]))
    assert bot.pm.positions == {}
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = None                        # ES_TO_SPX source without a basis yet: no signal
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]))
    assert bot.pm.positions == {}


def test_on_break_ignores_premarket_candles_and_wick_only_break(bot):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7630, 7635, 7625, 7632),      # 09:26 pre-market candle above the level: must be ignored
            (7632, 7634, 7628, 7633),      # 09:28
            (7615, 7628, 7614, 7619),      # 09:30 wick above, closes below -> not a break
            (7619, 7621, 7615, 7618)]      # 09:32
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 26), bars))
    assert bot.pm.positions == {} and bot.on_setup.state == bot.on_setup.WAITING


# ------------------------------------------------------ adaptive ES confirmation source (Phase 5)

def test_overnight_levels_uses_spx_proxy_when_live_es_unavailable(bot, monkeypatch):
    from strategy import Levels
    monkeypatch.setattr(config, "BREAKOUT_LEVEL_SOURCE", "ES_TO_SPX")
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: False)
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 5.0
    levels, symbol = bot.overnight_levels_for_signal()
    assert symbol == config.SPX_SYMBOL and levels.high == 7625.0


def test_overnight_levels_switches_to_raw_es_when_live_available(bot, monkeypatch):
    from strategy import Levels
    monkeypatch.setattr(config, "BREAKOUT_LEVEL_SOURCE", "ES_TO_SPX")
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 5.0
    levels, symbol = bot.overnight_levels_for_signal()
    assert symbol == config.ES_SYMBOL and levels.high == 7620.0   # unshifted -- raw ES level


def test_overnight_levels_explicit_es_override_ignores_live_availability(bot, monkeypatch):
    from strategy import Levels
    monkeypatch.setattr(config, "BREAKOUT_LEVEL_SOURCE", "ES")
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: False)
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    levels, symbol = bot.overnight_levels_for_signal()
    assert symbol == config.ES_SYMBOL


# ---------------------------------------------------------------------- Strategy C: ES_BREAK

def test_es_break_fires_on_close_only_confirmation(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    # break candle wicks well below the level but still CLOSES above it -- TwoCandleBreak ignores
    # the wick entirely (unlike ON_BREAK's OrbSetup, which would read that wick as a pullback).
    bars = [(7615, 7625, 7500, 7624), (7624, 7630, 7621, 7628)]
    bot.try_es_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"C"}
    c = bot.pm.positions["C"]
    assert (c.short_strike, c.long_strike, c.entry_credit) == (7565.0, 7560.0, 0.90)
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "ES_BREAK"][0]
    assert sig["direction"] == "BULLISH"


def test_es_break_rejects_strike_collision_with_already_open_b(bot, monkeypatch, caplog):
    # Regression test for a real live failure: B and C (and D/E) all use select_strikes_b, so they
    # can land on the identical contract -- Alpaca then can't reconcile two strategies each trying to
    # manage "their" slice of one combined broker-side position, and CLOSE_FAILED with a
    # position_intent mismatch once either side tries to exit. Refuse the second entry outright.
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    from conftest import make_candles
    from strategy import Levels
    bot.enter(et(10, 15), "ORB", BULLISH)   # opens A and B; B lands on 7565/7560 (see put_chain())
    assert "B" in bot.pm.positions
    b_qty_before = bot.pm.positions["B"].qty
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]   # would pick the SAME 7565/7560 as B
    bot.try_es_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert "C" not in bot.pm.positions
    assert bot.pm.positions["B"].qty == b_qty_before   # B untouched
    rejected = [r for r in events(bot, "ENTRY_REJECTED") if r["strategy"] == "C"]
    assert rejected and "STRIKE_COLLISION" in rejected[0]["reason"] and "B" in rejected[0]["reason"]


def test_es_break_not_checked_when_c_not_in_strategies(bot):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_es_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert bot.pm.positions == {} and bot.es_setup is None


def test_es_break_resets_on_close_back_inside_no_wick_pullback_path(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624),      # break
            (7624, 7625, 7619, 7619)]      # closes back inside -> reset, no PULLBACK state to salvage it
    bot.try_es_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert bot.pm.positions == {} and bot.es_setup.state == bot.es_setup.WAITING


# ------------------------------------------------------------ Strategies D/E: MA/Bollinger reversion

def _de_candles(n=30):
    from conftest import make_candles
    bars = [(7600.0, 7601.0, 7599.0, 7600.0)] * n
    return make_candles(et(11, 0), bars, minutes=5)


def test_mean_reversion_entry_opens_d_on_bullish_signal(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"D"}
    d = bot.pm.positions["D"]
    assert (d.short_strike, d.long_strike) == (7565.0, 7560.0)
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "MA_BB"][0]
    assert sig["direction"] == "BULLISH"


def test_mean_reversion_entry_none_when_signal_is_none(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: None)
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert bot.pm.positions == {}


def _de_candles_cross_session(today_count, today_body="up"):
    """30 total 5-min candles: (30 - today_count) from "yesterday" (day 16) plus today_count from
    today (day 17, starting at the open) -- proves the EMA/Bollinger window is a rolling one that
    doesn't reset each session."""
    import pandas as pd
    from conftest import make_candles
    yesterday = make_candles(et(10, 0, day=16), [(7600.0, 7601.0, 7599.0, 7600.0)] * (30 - today_count), minutes=5)
    body = (7600.0, 7601.0, 7599.0, 7601.0) if today_body == "up" else (7601.0, 7601.5, 7599.0, 7599.0)
    today = make_candles(et(9, 30, day=17), [body] * today_count, minutes=5)
    return pd.concat([yesterday, today])


def test_mean_reversion_entry_d_fires_early_in_session_using_prior_session_candles(bot, monkeypatch):
    # Regression test: D must NOT need to wait for 30 candles to accumulate fresh each day -- only 1
    # candle today, 29 from "yesterday" filling the rest of the rolling window, and D still fires.
    monkeypatch.setattr(config, "STRATEGIES", ("D",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles_cross_session(1))
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(9, 35, day=17), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"D"}


def test_mean_reversion_entry_e_does_not_confirm_across_the_session_boundary(bot, monkeypatch):
    # E's 2-candle confirmation must be same-session-only: with just 1 candle today, there is no
    # valid "2 consecutive today candles" no matter how many prior-session candles exist -- confirmed
    # using the REAL two_candle_confirm (not mocked) on genuinely cross-day candle data.
    monkeypatch.setattr(config, "STRATEGIES", ("E",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles_cross_session(1))
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(9, 35, day=17), scheduler.Phase.ORB_ONLY)
    assert bot.pm.positions == {}


def test_mean_reversion_entry_e_confirms_with_two_real_same_session_candles(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("E",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles_cross_session(2, "up"))
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(9, 40, day=17), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"E"}


def test_mean_reversion_entry_skips_e_without_two_candle_confirm(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    monkeypatch.setattr(scheduler.strategy, "two_candle_confirm", lambda *a, **k: False)
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    # D still fires (no confirm requirement); E is skipped
    assert set(bot.pm.positions) == {"D"}


def test_mean_reversion_entry_enters_e_with_confirm_and_half_size(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("E",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    monkeypatch.setattr(scheduler.strategy, "two_candle_confirm", lambda *a, **k: True)
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"E"}
    assert bot.pm.positions["E"].qty <= config.TESTING_HALF_SIZE_MAX_CONTRACTS
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "MA_BB_CHOP"][0]
    assert sig["direction"] == "BULLISH"


def test_mean_reversion_entry_skips_when_not_enough_candles(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D",))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles(n=10))
    called = []
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: called.append(1) or "BULLISH")
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert bot.pm.positions == {} and called == []


def test_mean_reversion_entry_skipped_when_d_and_e_already_positioned(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    bot.pm.positions["D"] = object()
    bot.pm.positions["E"] = object()
    called = []
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: called.append(1) or _de_candles())
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert called == []   # short-circuited before even fetching candles


def test_d_rejects_a_fired_orb_signal_it_does_not_trade(bot, monkeypatch):
    # Regression test for a real live bug: D/E had no SETUPS_BY_STRATEGY entry, so enter_leg's
    # `.get(strat, (setup,))` fallback silently made them eligible for ANY fired breakout setup routed
    # through dispatch_entry/enter() (which fans out across every strategy.spx_strategies(), D/E
    # included) -- not just their own MA_BB/MA_BB_CHOP mean-reversion signal. A real D EXIT event was
    # found tagged setup="ORB", which should be structurally impossible by D's design. No "B" here so
    # D's select_strikes_b pick can't ALSO be rejected by the unrelated strike-collision guard --
    # this isolates the SETUPS_BY_STRATEGY gate specifically.
    monkeypatch.setattr(config, "STRATEGIES", ("A", "D"))
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert "A" in bot.pm.positions
    assert "D" not in bot.pm.positions


def test_e_rejects_a_fired_on_break_signal_it_does_not_trade(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("A", "E"))
    bot.enter(et(10, 15), "ON_BREAK", BULLISH)
    assert "A" in bot.pm.positions
    assert "E" not in bot.pm.positions


def test_d_still_enters_on_its_own_setup_alongside_a_and_b(bot, monkeypatch):
    # The fix must not touch D's OWN entry path -- check_mean_reversion_entry always calls enter_leg
    # with D's own "MA_BB" setup, which the new SETUPS_BY_STRATEGY["D"] entry allows same as before.
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "D"))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert "D" in bot.pm.positions


# --------------------------------------------------------------------------------- news blackout

def test_news_blackout_blocks_entry_and_clears_after(bot, monkeypatch):
    from datetime import time as dtime
    monkeypatch.setattr(config, "NEWS_EVENTS", {TODAY.isoformat(): [dtime(9, 30)]})
    bot.enter(et(9, 32), "ORB", BULLISH)
    assert bot.pm.positions == {}
    bot.enter(et(9, 40), "ORB", BULLISH)   # outside the +/-5 min window: allowed
    assert set(bot.pm.positions) == {"A", "B"}


def test_news_blackout_boundary_resets_breakout_state_machines(bot, monkeypatch):
    from datetime import time as dtime
    import pandas as pd
    from strategy import OrbSetup
    monkeypatch.setattr(config, "NEWS_EVENTS", {TODAY.isoformat(): [dtime(9, 30)]})
    bot.orb_setup = OrbSetup(7620.0, 7560.0)
    bot.orb_setup.state = OrbSetup.BROKEN
    bot.orb_setup.direction = BULLISH
    bot.news_blackout_was_active = True
    bot.refresh_levels = lambda now: None   # avoid network calls; not under test here
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: pd.DataFrame())
    bot.tick(et(9, 40))   # blackout window has just cleared
    assert bot.orb_setup.state == OrbSetup.WAITING


# ------------------------------------------------- restart state persistence (2026-10-01)

def test_save_state_includes_breakout_state(bot):
    from strategy import Levels, OrbSetup, TwoCandleBreak
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = -12.5
    bot.orb = Levels(7630.0, 7570.0, et(10, 0))
    bot.orb_setup = OrbSetup(7630.0, 7570.0)
    bot.orb_setup.state = OrbSetup.DONE
    bot.orb_setup.direction = BULLISH
    bot.orb_setup.break_close = 7635.0
    bot.on_setup = OrbSetup(7620.0, 7560.0)
    bot.on_setup_symbol = config.SPX_SYMBOL
    bot.es_setup = TwoCandleBreak(7620.0, 7560.0)
    bot.es_setup_symbol = config.ES_SYMBOL
    bot.last_orb_candle = et(10, 5)
    bot.last_on_candle = et(9, 40)
    bot.last_continuation_check = {"ORB": et(10, 10), "ON_BREAK": et(9, 45)}
    bot.news_blackout_was_active = True
    bot.save_state()
    saved = bot.journal.load_state()
    b = saved["breakouts"]
    assert b["orb_setup"] == {"or_high": 7630.0, "or_low": 7570.0, "state": "DONE",
                              "direction": BULLISH, "break_close": 7635.0,
                              "candles_since_break": 0, "entry_kind": None}
    assert b["on_setup"]["state"] == "WAITING" and b["es_setup"]["state"] == "WAITING"
    assert b["on_setup_symbol"] == config.SPX_SYMBOL and b["es_setup_symbol"] == config.ES_SYMBOL
    assert b["last_orb_candle"] == et(10, 5).isoformat() and b["last_on_candle"] == et(9, 40).isoformat()
    assert b["last_es_candle"] is None and b["last_de_candle"] is None
    assert b["last_continuation_check"] == {"ORB": et(10, 10).isoformat(), "ON_BREAK": et(9, 45).isoformat()}
    assert b["news_blackout_was_active"] is True
    assert saved["levels"]["overnight"]["high"] == 7620.0 and saved["levels"]["basis"] == -12.5


def test_restore_breakout_state_rebuilds_everything_from_a_saved_dict(bot):
    from strategy import Levels, OrbSetup, TwoCandleBreak
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = -12.5
    bot.orb = Levels(7630.0, 7570.0, et(10, 0))
    bot.orb_setup = OrbSetup(7630.0, 7570.0)
    bot.orb_setup.state = OrbSetup.DONE
    bot.orb_setup.direction = BULLISH
    bot.orb_setup.break_close = 7635.0
    bot.on_setup = OrbSetup(7620.0, 7560.0)
    bot.on_setup_symbol = config.SPX_SYMBOL
    bot.es_setup = TwoCandleBreak(7620.0, 7560.0)
    bot.es_setup.state = TwoCandleBreak.ARMED
    bot.es_setup.direction = BULLISH
    bot.es_setup_symbol = config.ES_SYMBOL
    bot.last_orb_candle = et(10, 5)
    bot.last_es_candle = et(9, 50)
    bot.last_continuation_check = {"ORB": et(10, 10)}
    bot.news_blackout_was_active = True
    bot.save_state()
    saved_today = bot.journal.load_state()

    fresh = scheduler.Bot.__new__(scheduler.Bot)   # a brand-new, post-"restart" instance's blank slate
    fresh.overnight = fresh.basis = fresh.orb = None
    fresh.orb_setup = fresh.on_setup = fresh.es_setup = None
    fresh.on_setup_symbol = fresh.es_setup_symbol = None
    fresh.last_orb_candle = fresh.last_on_candle = fresh.last_es_candle = fresh.last_de_candle = None
    fresh.last_candle = None
    fresh.last_continuation_check = {}
    fresh.news_blackout_was_active = False
    fresh._restore_breakout_state(saved_today)

    assert fresh.overnight == Levels(7620.0, 7560.0, et(9, 29)) and fresh.basis == -12.5
    assert fresh.orb == Levels(7630.0, 7570.0, et(10, 0))
    assert fresh.orb_setup.state == OrbSetup.DONE and fresh.orb_setup.direction == BULLISH
    assert fresh.orb_setup.break_close == 7635.0
    assert fresh.on_setup.state == OrbSetup.WAITING and fresh.on_setup_symbol == config.SPX_SYMBOL
    assert fresh.es_setup.state == TwoCandleBreak.ARMED and fresh.es_setup.direction == BULLISH
    assert fresh.es_setup_symbol == config.ES_SYMBOL
    assert fresh.last_orb_candle == et(10, 5) and fresh.last_es_candle == et(9, 50)
    assert fresh.last_on_candle is None and fresh.last_de_candle is None   # never set -> stays None
    assert fresh.last_continuation_check == {"ORB": et(10, 10)}
    assert fresh.news_blackout_was_active is True


def test_restore_breakout_state_is_a_no_op_on_a_blank_state_dict(bot):
    """An older state.json written before this fix (or a day with no breakout activity yet at all)
    has no "breakouts" key, or one full of nulls -- restoring from it must leave everything None,
    exactly like a real first start of the day, not raise."""
    bot._restore_breakout_state({"levels": {}, "breakouts": {}})
    assert bot.orb_setup is None and bot.on_setup is None and bot.es_setup is None
    assert bot.last_orb_candle is None and bot.last_continuation_check == {}
    assert bot.news_blackout_was_active is False
    bot._restore_breakout_state({})   # no "levels"/"breakouts" key at all
    assert bot.orb_setup is None and bot.last_continuation_check == {}


def test_restore_breakout_state_catches_a_malformed_sub_dict_instead_of_crashing(bot, caplog):
    """A saved breakouts.orb_setup that's present but missing a required field (an old/future schema,
    a hand-edited file) must not raise all the way out of start() and take the whole persona process
    down -- it should log a warning and fall back to a fresh (None) orb_setup, same as a missing key."""
    caplog.set_level("WARNING")
    bot._restore_breakout_state({"breakouts": {"orb_setup": {"state": "DONE"}}})   # missing or_high etc.
    assert bot.orb_setup is None
    assert any("Could not restore orb_setup" in r.getMessage() for r in caplog.records)


def test_restore_breakout_state_catches_a_malformed_levels_dict_instead_of_crashing(bot, caplog):
    caplog.set_level("WARNING")
    bot._restore_breakout_state({"levels": {"orb": {"high": 7630.0}}})   # missing low/established_at
    assert bot.orb is None
    assert any("Could not restore saved levels" in r.getMessage() for r in caplog.records)


def test_try_on_break_entry_rebuilds_on_setup_when_level_source_switches(bot, monkeypatch):
    """overnight_levels_for_signal() can resolve to a different source (ES vs SPX-terms) on a later
    call -- e.g. a restart right after a Schwab token refresh makes live ES suddenly available. A
    restored on_setup built against the OLD source must be rebuilt fresh, not fed candles from the new
    source's different price scale."""
    from conftest import make_candles
    from strategy import Levels, OrbSetup
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bot.on_setup = OrbSetup(7620.0, 7560.0)   # as if restored, built against SPX-terms levels
    bot.on_setup.state = OrbSetup.BROKEN
    bot.on_setup.break_close = 7624.0
    bot.on_setup_symbol = config.SPX_SYMBOL
    bot.last_on_candle = et(9, 50)
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)   # ES now available
    bars = [(7615, 7625, 7614, 7624)]
    bot.try_on_break_entry(et(9, 55), make_candles(et(9, 30), bars))
    assert bot.on_setup_symbol == config.ES_SYMBOL   # rebuilt against the new source
    assert bot.on_setup.state == OrbSetup.WAITING     # fresh, not the stale BROKEN state
    assert bot.last_on_candle is None                 # replay restarts from scratch under the new source


def test_try_es_break_entry_rebuilds_es_setup_when_level_source_switches(bot, monkeypatch):
    """Same fix as the ON_BREAK case above, applied to Strategy C's TwoCandleBreak."""
    from conftest import make_candles
    from strategy import Levels, TwoCandleBreak
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bot.es_setup = TwoCandleBreak(7620.0, 7560.0)   # as if restored, built against SPX-terms levels
    bot.es_setup.state = TwoCandleBreak.ARMED
    bot.es_setup_symbol = config.SPX_SYMBOL
    bot.last_es_candle = et(9, 50)
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)   # ES now available
    bars = [(7615, 7625, 7614, 7624)]
    bot.try_es_break_entry(et(9, 55), make_candles(et(9, 30), bars))
    assert bot.es_setup_symbol == config.ES_SYMBOL
    assert bot.es_setup.state == TwoCandleBreak.WAITING
    assert bot.last_es_candle is None


def test_refresh_levels_does_not_recreate_orb_setup_once_restored(bot, monkeypatch):
    """The actual 2026-10-01 bug: refresh_levels() used to create a brand-new (WAITING) orb_setup
    whenever self.orb was freshly fetched, even if a restore had already put a real (e.g. DONE)
    orb_setup in place a moment earlier in start() -- silently discarding it."""
    from strategy import Levels, OrbSetup
    monkeypatch.setattr(scheduler.market_data, "get_opening_range",
                        lambda now: Levels(7630.0, 7570.0, et(10, 0)))
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))   # already resolved -- only the ORB branch is under test
    bot.basis = 0.0
    bot.orb = None   # not yet fetched this run (as if orb wasn't in the saved state either)
    bot.orb_setup = OrbSetup(7630.0, 7570.0)
    bot.orb_setup.state = OrbSetup.DONE
    bot.orb_setup.break_close = 7640.0
    bot.refresh_levels(et(10, 2))
    assert bot.orb is not None   # the levels themselves are still (re)fetched normally
    assert bot.orb_setup.state == OrbSetup.DONE and bot.orb_setup.break_close == 7640.0   # but NOT recreated


def test_continuation_reentry_fires_after_simulated_restart(bot, monkeypatch):
    """The real 2026-10-01 symptom, reproduced directly: ON_BREAK reaches DONE, gets saved, the bot
    "restarts" (every piece of in-memory breakout state goes back to its __init__ default, exactly
    like a fresh Bot object), state is restored from the saved file, and continuation re-entry still
    fires -- before this fix, the restored run would have started from a blank WAITING on_setup and
    never fired again for the rest of the day."""
    _done_on_break(bot)
    bot.last_continuation_check["ON_BREAK"] = et(9, 34)
    bot.save_state()
    saved_today = bot.journal.load_state()

    # simulate a restart: every field start() would normally leave at its __init__ default is wiped
    bot.overnight = bot.basis = bot.orb = None
    bot.orb_setup = bot.on_setup = bot.es_setup = None
    bot.last_orb_candle = bot.last_on_candle = bot.last_es_candle = bot.last_de_candle = None
    bot.last_continuation_check = {}

    bot._restore_breakout_state(saved_today)
    assert bot.on_setup is not None and bot.on_setup.state == bot.on_setup.DONE
    assert bot.last_continuation_check.get("ON_BREAK") == et(9, 34)   # the pacing timer survived too

    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0   # beats the restored 7624.0 break by 5.0, well past the 0.5 margin
    # 2 min after the RESTORED 9:34 checkpoint, still inside REENTRY_PAUSE_MIN (5 min) -- proves this
    # fires on the restored timestamp specifically, not merely because on_setup.state == DONE: if
    # last_continuation_check had failed to restore (stayed {}), this call would fire immediately.
    bot.check_continuation_reentry(et(9, 36), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == []
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:4] == ("ON_BREAK", BULLISH, "MOMENTUM") and kwargs["strategies"] == ("B",)


# ------------------------------------------------------------ C/D/E test levers (2026-10-02)

def _gated_only(*a, use_trend_gate=True, **k):
    """Stands in for mean_reversion_signal: no setup while the trend gate is on, a bullish one with it off."""
    return None if use_trend_gate else "BULLISH"


def test_e_without_the_trend_gate_fires_where_the_gated_signal_does_not(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    monkeypatch.setattr(config, "E_USE_TREND_GATE", False)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", _gated_only)
    monkeypatch.setattr(scheduler.strategy, "two_candle_confirm", lambda *a, **k: True)
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"E"}          # D stays on its gate and sits out


def test_e_keeps_the_trend_gate_by_default(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("D", "E"))
    assert config.E_USE_TREND_GATE is True
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles())
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", _gated_only)
    monkeypatch.setattr(scheduler.strategy, "two_candle_confirm", lambda *a, **k: True)
    bot.check_mean_reversion_entry(et(12, 30), scheduler.Phase.ORB_ONLY)
    assert bot.pm.positions == {}


def test_c_runs_past_the_overnight_window_only_when_c_entry_end_min_allows(bot, monkeypatch):
    from conftest import make_candles
    calls = []
    bot.refresh_levels = lambda now: None
    monkeypatch.setattr(bot, "try_es_break_entry", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(bot, "try_on_break_entry", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.market_data, "get_candles",
                        lambda *a, **k: make_candles(et(10, 0), [(7600.0, 7601.0, 7599.0, 7600.0)], minutes=2))
    assert config.C_ENTRY_END_MIN == 30
    bot.tick(et(10, 15))                             # ORB_ONLY phase, 45 min after the open
    assert calls == []
    bot.last_candle = None
    monkeypatch.setattr(config, "C_ENTRY_END_MIN", 60)
    bot.tick(et(10, 15))
    assert calls == [1]
    bot.last_candle = None
    bot.tick(et(10, 35))                             # 65 min after the open: past even the extended window
    assert calls == [1]


# ------------------------------------------------- SIGNAL_SKIPPED journal events (2026-10-05)

def skipped(bot, strat=None):
    rows = events(bot, "SIGNAL_SKIPPED")
    return [r for r in rows if strat is None or r["strategy"] == strat]


def test_skip_code_maps_reasons_to_stable_codes():
    cases = {
        "position open": "POSITION_OPEN", "ORB setup disabled for C": "SETUP_DISABLED",
        "ORB MOMENTUM entry disabled for D": "ENTRY_KIND_DISABLED", "sizing 0 contracts EM 42.00": "SIZING_ZERO",
        "not filled (canceled) EM 42.00": "NOT_FILLED", "broker error boom": "BROKER_ERROR",
        "MAX_TRADES_PER_DAY (5)": "MAX_TRADES_PER_DAY", "MAX_CONSECUTIVE_LOSSES (3)": "MAX_CONSECUTIVE_LOSSES",
        "COOLDOWN until 10:30": "COOLDOWN", "MANUAL_CLOSE cooldown until 10:22:15": "MANUAL_CLOSE_COOLDOWN",
        "DAILY_LOSS_LIMIT realized=-1000.00": "DAILY_LOSS_LIMIT", "DAY_GATE ER=0.10 < 0.15 (choppy) EM 42.00": "DAY_GATE",
        "B_NO_STRIKE_IN_RANGE EM 42.00": "B_NO_STRIKE_IN_RANGE", "NO_EXPECTED_MOVE": "NO_EXPECTED_MOVE",
        "STRIKE_COLLISION 7565/7560P already open under A": "STRIKE_COLLISION", "": "UNKNOWN",
    }
    for text, code in cases.items():
        assert scheduler.skip_code(text) == code, text


def test_a_strategy_with_a_position_open_is_journaled_as_skipped_while_the_other_enters(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.positions.pop("B")
    bot.enter(et(10, 25), "ORB", BULLISH)
    rows = skipped(bot)
    assert [(r["strategy"], r["reason_code"]) for r in rows] == [("A", "POSITION_OPEN")]
    r = rows[0]
    assert (r["setup"], r["direction"], r["source"], r["attempted"]) == ("ORB", BULLISH, "SIGNAL", False)
    assert r["spot"] == SPOT and r["em"] == 42.0 and r["width"] > 0


def test_a_strike_rejection_is_journaled_with_the_signal_context(bot):
    chain = put_chain()
    chain[7565.0] = (1.3, 1.5)
    chain[7570.0] = (1.5, 1.7)
    chain.update({s: (1.6, 1.8) for s in (7575.0, 7580.0, 7585.0, 7590.0, 7595.0)})
    bot.broker.set_chain("P", chain)
    bot.enter(et(10, 15), "ORB", BULLISH)
    rows = skipped(bot, "B")
    assert len(rows) == 1 and rows[0]["reason_code"] == "B_NO_STRIKE_IN_RANGE" and rows[0]["spot"] == SPOT
    assert skipped(bot, "A") == []                       # A traded


def test_the_daily_trade_cap_is_journaled_per_strategy(bot):
    for _ in range(5):
        bot.risk.record_trade("B", 10.0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A"}
    rows = skipped(bot, "B")
    assert len(rows) == 1 and rows[0]["reason_code"] == "MAX_TRADES_PER_DAY"
    assert rows[0]["spot"] == SPOT and "MAX_TRADES_PER_DAY (5)" in rows[0]["reason"]


def test_every_strategy_is_journaled_when_the_whole_signal_is_blocked(bot):
    bot.risk.record_trade("A", -1_000.0)                 # combined daily loss limit
    bot.enter(et(10, 15), "ORB", BULLISH)
    rows = skipped(bot)
    assert {r["strategy"] for r in rows} == {"A", "B"} and {r["reason_code"] for r in rows} == {"DAILY_LOSS_LIMIT"}
    assert all(r["reason"].startswith("DAILY_LOSS_LIMIT") for r in rows)


def test_a_manual_hold_is_journaled(bot, monkeypatch):
    monkeypatch.setattr(bot.pm, "manual_hold", lambda: True)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert {(r["strategy"], r["reason_code"]) for r in skipped(bot)} == {("A", "MANUAL_HOLD"), ("B", "MANUAL_HOLD")}


def test_a_news_blackout_is_journaled(bot, monkeypatch):
    from datetime import time as dtime
    monkeypatch.setattr(config, "NEWS_EVENTS", {TODAY.isoformat(): [dtime(9, 30)]})
    bot.enter(et(9, 32), "ORB", BULLISH)
    assert {(r["strategy"], r["reason_code"]) for r in skipped(bot)} == {("A", "NEWS_BLACKOUT"), ("B", "NEWS_BLACKOUT")}


def test_a_missing_expiration_is_journaled(bot, monkeypatch):
    monkeypatch.setattr(bot.broker, "get_expirations", lambda *a, **k: [])
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert {r["reason_code"] for r in skipped(bot)} == {"NO_EXPIRATION"} and len(skipped(bot)) == 2


def test_a_continuation_re_entry_is_tagged_with_its_source(bot):
    bot.risk.record_trade("A", -1_000.0)
    bot.enter(et(10, 15), "ORB", BULLISH, "MOMENTUM", source="CONTINUATION")
    assert {r["source"] for r in skipped(bot)} == {"CONTINUATION"} and {r["entry_kind"] for r in skipped(bot)} == {"MOMENTUM"}


def test_an_unfilled_entry_is_journaled_as_an_attempted_skip(bot, monkeypatch):
    monkeypatch.setattr(bot, "work_entry", lambda order, *a, **k: {**order, "status": "canceled", "filled_qty": 0})
    bot.enter(et(10, 15), "ORB", BULLISH)
    rows = skipped(bot)
    assert {r["strategy"] for r in rows} == {"A", "B"} and {r["reason_code"] for r in rows} == {"NOT_FILLED"}
    assert all(r["attempted"] is True for r in rows)


def test_momentum_aborts_are_journaled_per_waiting_strategy(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    m1 = make_candles(et(9, 34), [(7628, 7629, 7627.5, 7628.6), (7628.6, 7627, 7620, 7621.0)], minutes=1)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: m1)
    bot.check_pending_momentum(et(9, 36), scheduler.Phase.OVERNIGHT_ONLY)
    rows = skipped(bot, "A")
    assert len(rows) == 1 and rows[0]["reason_code"] == "MOMENTUM_REVERSED" and rows[0]["entry_kind"] == "MOMENTUM"


def test_momentum_timeout_and_closed_window_are_journaled(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 34), [], minutes=1))
    bot.check_pending_momentum(et(9, 34) + timedelta(minutes=config.MOMENTUM_CONFIRM_TIMEOUT_MIN + 1),
                               scheduler.Phase.OVERNIGHT_ONLY)
    assert [r["reason_code"] for r in skipped(bot, "A")] == ["MOMENTUM_TIMEOUT"]
    bot.pending_momentum["ON_BREAK"] = scheduler.PendingMomentum(
        setup="ON_BREAK", direction=BULLISH, break_close=7624.0, strategies=("A",), signal_time=et(9, 34))
    m1 = make_candles(et(9, 35), [(7628, 7629, 7627.5, 7628.6), (7628.6, 7630, 7628, 7629.2)], minutes=1)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: m1)
    bot.check_pending_momentum(et(9, 37), scheduler.Phase.CLOSED)
    assert [r["reason_code"] for r in skipped(bot, "A")] == ["MOMENTUM_TIMEOUT", "MOMENTUM_ENTRY_WINDOW_CLOSED"]


# ------------------------------------------------- 12:00 ET entry cutoff and the 5-trade total cap (2026-10-07)

def test_the_total_cap_blocks_every_strategy_and_is_journaled(bot, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    for strat in ("A", "A", "A", "B", "B"):
        bot.risk.record_trade(strat, 10.0)                        # 5 trades today across A and B; neither is at its own cap
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert bot.pm.positions == {}
    rows = skipped(bot)
    assert {r["strategy"] for r in rows} == {"A", "B"} and {r["reason_code"] for r in rows} == {"TOTAL_MAX_TRADES_PER_DAY"}


def test_an_open_position_counts_toward_the_total_cap_within_one_signal(bot, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    for strat in ("A", "A", "A", "B"):
        bot.risk.record_trade(strat, 10.0)                        # 4 trades so far: exactly one more is allowed
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A"}                         # A takes the 5th ...
    assert [(r["strategy"], r["reason_code"]) for r in skipped(bot)] == [("B", "TOTAL_MAX_TRADES_PER_DAY")]   # ... B is refused


def test_a_shared_cap_blocks_its_group_through_the_scheduler_and_is_journaled(bot, monkeypatch):
    """2026-11-12: a daily cap shared by a group (Schwab: B and C, 3 a day). A and B share 3 here: A's two closes and the position A
    opens on this signal fill it, so B is refused on the same signal with the shared-cap code (the open position counts)."""
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("A", "B"): 3})
    for strat in ("A", "A"):
        bot.risk.record_trade(strat, 10.0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A"}                                              # A takes the third ...
    assert [(r["strategy"], r["reason_code"]) for r in skipped(bot)] == [("B", "SHARED_MAX_TRADES_PER_DAY")]   # ... B is refused
    assert bot.pm.open_counts() == {"A": 1}


def test_nothing_enters_from_noon_eastern_and_the_bot_keeps_managing_until_the_hard_stop(bot, monkeypatch):
    import pandas as pd
    calls = []
    for name in ("try_es_break_entry", "try_on_break_entry", "try_orb_entry", "check_continuation_reentry",
                 "check_mean_reversion_entry"):
        monkeypatch.setattr(bot, name, lambda *a, _n=name, **k: calls.append(_n))
    bot.refresh_levels = lambda now: None
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: pd.DataFrame())
    bot.tick(et(11, 59, 30))
    assert "check_continuation_reentry" in calls and "check_mean_reversion_entry" in calls    # still entering
    calls.clear()
    bot.tick(et(12, 0, 30))
    assert calls == [] and bot.done is False                       # no entry path runs; the bot is still up (positions are managed)
    bot.tick(et(13, 0, 30))
    assert bot.done is True


# ------------------------------------------------- at most N trades open at one time (2026-10-08)

def test_the_open_position_cap_stops_a_strategy_from_entering_and_is_journaled(bot, monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 1)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert len(bot.pm.positions) == 1                              # the first strategy took the only slot ...
    refused = [r for r in skipped(bot) if r["reason_code"] == "MAX_OPEN_POSITIONS"]
    assert len(refused) == 1 and refused[0]["reason"] == "MAX_OPEN_POSITIONS (1/1)"   # ... the second was turned away
    bot.enter(et(10, 25), "ORB", BULLISH)                          # a later signal while it is still open: nothing enters
    assert len(bot.pm.positions) == 1


def test_two_open_slots_let_both_strategies_in_and_a_closed_slot_frees_up(bot, monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 2)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    bot.pm.positions.pop("B")
    assert bot.entry_possible(10_000.0) is True and bot.pm.open_total_count() == 1


# ------------------------------------------------- close failures, the hard stop, shared accounts, account check (2026-10-08)

def test_entries_are_halted_while_a_close_is_latched(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.positions.pop("B")
    bot.pm.positions["A"].close_fail_count = 1                      # A's close keeps failing at the broker
    bot.enter(et(10, 25), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A"}                           # B had a free slot but nothing enters
    assert {r["reason_code"] for r in skipped(bot) if r["strategy"] == "B"} == {"CLOSE_BLOCKED"}


def test_the_hard_stop_keeps_the_bot_up_while_a_close_keeps_failing(bot, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    monkeypatch.setattr(config, "EOD_CLOSE_RETRY_SEC", 60)
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.broker.close_failures = 10_000
    assert bot.end_of_day(et(13, 0, 0)) is False                    # used to return True and exit with both spreads open
    assert set(bot.pm.positions) == {"A", "B"} and events(bot, "END") == []
    calls = len(bot.broker.close_calls)
    assert bot.end_of_day(et(13, 0, 30)) is False and len(bot.broker.close_calls) == calls     # waiting out the 60 s
    assert bot.end_of_day(et(13, 1, 5)) is False and len(bot.broker.close_calls) > calls       # next pass
    assert events(bot, "EOD_CLOSE_STILL_OPEN")
    bot.broker.close_failures = 0
    assert bot.end_of_day(et(13, 2, 10)) is True and bot.pm.positions == {}                     # flat: finished normally
    assert events(bot, "END")


def test_the_tick_after_the_hard_stop_does_not_finish_the_day_while_positions_are_stuck(bot, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.broker.close_failures = 10_000
    bot.tick(et(13, 0, 5))
    assert bot.done is False and bot.pm.positions
    bot.broker.close_failures = 0
    bot.tick(et(13, 2, 5))
    assert bot.done is True and bot.pm.positions == {}


def test_after_the_session_ends_a_stuck_close_is_reported_and_the_bot_finishes(bot, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.broker.close_failures = 10_000
    assert bot.end_of_day(et(16, 1)) is True
    assert events(bot, "END")[-1]["mismatches"]


def _leg_symbols(legs):
    return {leg["symbol"] for leg in legs}


def test_a_45dte_spread_on_a_tracked_f_ticker_is_not_this_bots_to_close(bot):
    from datetime import timedelta
    from test_position_manager import f_spread
    near, far = TODAY + timedelta(days=2), TODAY + timedelta(days=52)
    bot.pm.open(f_spread("TSLA"))                                        # F holds TSLA (expiry from f_spread's own date)
    f_exp = bot.pm.positions["F:TSLA"].expiration
    bot.broker.add_spread_position("P", 100.0, 99.0, 1, 0.5, 0.32, f_exp, root="TSLA")      # F's legs
    bot.broker.add_spread_position("P", 300.0, 295.0, 1, 4.0, 2.5, far, root="TSLA")        # the 45DTE bot's, same ticker
    mine = bot.bot_legs(bot.broker.get_positions())
    assert {leg["expiration"] for leg in mine if leg["underlying"] == "TSLA" or "TSLA" in leg["symbol"]} == {f_exp}
    foreign = bot.foreign_legs(bot.broker.get_positions())
    assert any(leg["expiration"] == far for leg in foreign) and not any(leg["expiration"] == f_exp for leg in foreign)


def test_order_is_mine_ignores_the_45dte_bots_resting_orders(bot, monkeypatch):
    from datetime import timedelta
    from broker import occ_symbol
    spx_today = {"legs": [{"symbol": occ_symbol("SPXW", TODAY, "P", 7500.0)}, {"symbol": occ_symbol("SPXW", TODAY, "P", 7495.0)}]}
    far = TODAY + timedelta(days=52)
    near = TODAY + timedelta(days=2)
    gtc_close = {"legs": [{"symbol": occ_symbol("TSLA", far, "P", 300.0)}, {"symbol": occ_symbol("TSLA", far, "P", 295.0)}]}
    f_order = {"legs": [{"symbol": occ_symbol("TSLA", near, "P", 300.0)}, {"symbol": occ_symbol("TSLA", near, "P", 295.0)}]}
    assert bot.order_is_mine(spx_today) is True and bot.order_is_mine(gtc_close) is False
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "F"))
    assert bot.order_is_mine(f_order) is True and bot.order_is_mine(f_order, {"AAPL"}) is False
    assert bot.order_is_mine(gtc_close) is False and bot.order_is_mine({"legs": []}) is False
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B"))
    assert bot.order_is_mine(f_order) is False                       # F not configured: stock orders are never ours


def test_the_end_of_day_report_does_not_count_the_45dte_bots_resting_orders(bot):
    from datetime import timedelta
    far = TODAY + timedelta(days=52)
    bot.broker.get_open_orders = lambda: [{"id": "g1", "status": "new", "symbol": "TSLA", "qty": 1, "limit_price": 0.8,
                                           "legs": [{"symbol": f"TSLA{far.strftime('%y%m%d')}P00300000"},
                                                    {"symbol": f"TSLA{far.strftime('%y%m%d')}P00295000"}]}]
    bot.report_end(et(13, 0, 5))
    assert events(bot, "END")[-1]["mismatches"] == []                # it used to log a CRITICAL "open orders remain" every day


def test_the_account_check_runs_at_startup_and_a_refusal_stops_the_bot(bot, monkeypatch, caplog):
    from broker import BrokerError

    class AccountNotTradable(BrokerError):           # stands in for the Alpaca broker's exception (Schwab has no such check)
        pass
    caplog.set_level("INFO")
    bot._account_preflight({"equity": 1000.0})                      # a broker without the check (the fake): nothing happens
    seen = []

    def fine(acct):
        seen.append(acct)
        return {"summary": "PAPER account x: status ACTIVE", "problems": ["trading is blocked"], "warnings": ["small account"]}
    monkeypatch.setattr(bot.broker, "preflight_account", fine, raising=False)
    bot._account_preflight({"equity": 1000.0})
    assert seen and any("Account check: PAPER account x" in r.getMessage() for r in caplog.records)
    assert any("trading is blocked" in r.getMessage() for r in caplog.records)          # on paper a problem is just a warning

    def refuse(acct):
        raise AccountNotTradable("refusing to trade: blocked")
    monkeypatch.setattr(bot.broker, "preflight_account", refuse, raising=False)
    with pytest.raises(AccountNotTradable):
        bot._account_preflight({"equity": 1000.0})


# ------------------------------------------------- data-failure safety: stale marks, tick exceptions, token (2026-10-08)

def test_a_tick_that_raises_anything_does_not_end_the_loop(bot, monkeypatch):
    calls = []

    def boom(now):
        calls.append(now)
        if len(calls) < 3:
            raise ValueError("a non-BrokerError, like an authlib OAuthError")
        bot.done = True
    monkeypatch.setattr(bot, "tick", boom)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
    bot.once = False
    bot.loop()
    assert len(calls) == 3 and bot.done is True
    assert [e["type"] for e in events(bot, "TICK_EXCEPTION")] == ["ValueError"]     # logged once per TICK_EXCEPTION_LOG_SEC


def _open_both(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    for p in bot.pm.positions.values():
        p.last_mark_at = et(10, 15)


def test_a_position_with_no_fresh_price_raises_an_alert_and_halts_entries(bot):
    _open_both(bot)
    bot._check_marks(et(10, 15, 10))
    assert bot._data_stale is False and bot.entry_halt_reason() is None            # 10 s old: fine
    bot._check_marks(et(10, 15, 25))
    assert bot._data_stale is True and "DATA_STALE" in bot.entry_halt_reason()
    assert {e["key"] for e in events(bot, "STALE_MARK")} == {"A", "B"}
    bot.pm.positions.pop("B")
    bot.enter(et(10, 15, 26), "ORB", BULLISH)                                        # B's slot is free, but entries are halted
    assert "B" not in bot.pm.positions
    assert {r["reason_code"] for r in skipped(bot) if r["strategy"] == "B"} == {"DATA_STALE"}
    for p in bot.pm.positions.values():
        p.last_mark_at = et(10, 15, 27)                                              # prices are flowing again
    bot._check_marks(et(10, 15, 28))
    assert bot._data_stale is False and bot.entry_halt_reason() is None


def test_a_position_stale_for_ninety_seconds_is_flattened_at_its_last_known_price(bot):
    _open_both(bot)
    bot._check_marks(et(10, 16, 15))                                                 # 75 s: alert only
    assert set(bot.pm.positions) == {"A", "B"}
    bot._check_marks(et(10, 16, 31))                                                 # 91 s: closed
    assert bot.pm.positions == {}
    reasons = {e["exit_reason"] for e in events(bot, "EXIT")}
    assert reasons == {"STALE_DATA_FLATTEN"}


def test_the_stale_flatten_respects_a_manual_hold_and_can_be_switched_off(bot, monkeypatch):
    _open_both(bot)
    monkeypatch.setattr(config, "STALE_MARK_FLATTEN_ENABLED", False)
    bot._check_marks(et(10, 20))
    assert set(bot.pm.positions) == {"A", "B"} and bot._data_stale is True            # alert and halt only
    monkeypatch.setattr(config, "STALE_MARK_FLATTEN_ENABLED", True)
    monkeypatch.setattr(bot.pm, "manual_hold", lambda: True)
    bot._check_marks(et(10, 20, 5))
    assert set(bot.pm.positions) == {"A", "B"}                                        # the owner said: no closes


def test_marks_are_stamped_when_prices_arrive_and_the_watchdog_ignores_a_fresh_position(bot):
    _open_both(bot)
    bot.pm.on_tick(et(10, 16), {"A": 3.0, "B": 0.9}, None)
    assert {p.last_mark_at for p in bot.pm.positions.values()} == {et(10, 16)}
    bot._check_marks(et(10, 16, 15))
    assert bot._data_stale is False
    never_priced = bot.pm.positions["B"]
    never_priced.last_mark_at, never_priced.last_tick_at, never_priced.entry_time = None, None, et(10, 15)
    assert bot.pm.mark_age(never_priced, et(10, 16, 30)) == 90.0                      # its entry counts as its first price


def test_the_hard_stop_flattens_even_when_every_data_call_fails(bot, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    from broker import BrokerError

    def dead(*a, **k):
        raise BrokerError("data feed down")
    monkeypatch.setattr(bot.broker, "get_spot", dead)
    monkeypatch.setattr(bot, "spread_prices", dead)
    assert bot.end_of_day(et(13, 0, 0)) is True and bot.pm.positions == {}          # closed at the last known prices


def test_the_staleness_watchdog_runs_even_when_the_tick_raised(bot, monkeypatch):
    _open_both(bot)
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 17, 5))                  # 125 s after the last price
    calls = []

    def boom(now):
        calls.append(now)
        if len(calls) == 2:
            bot.done = True
        raise RuntimeError("tick blew up")
    monkeypatch.setattr(bot, "tick", boom)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
    bot.once = False
    bot.loop()
    assert bot.pm.positions == {}                                                    # 125 s without a price: flattened anyway


def test_the_token_check_halts_entries_when_the_token_is_expiring_and_clears_when_fixed(bot, monkeypatch):
    state = {"state": "EXPIRING", "message": "token is 6.9 days old", "relogin": "cd x && python schwab_login.py --force",
             "age_days": 6.9, "expires_at": "2026-10-09T10:00:00-04:00"}
    monkeypatch.setattr(bot.broker, "token_status", lambda now: dict(state), raising=False)
    bot._token_check(et(9, 31))
    assert "TOKEN_EXPIRING" in bot.entry_halt_reason()
    assert events(bot, "TOKEN_STATUS")[-1]["state"] == "EXPIRING"
    bot.enter(et(9, 32), "ORB", BULLISH)
    assert bot.pm.positions == {} and {r["reason_code"] for r in skipped(bot)} == {"TOKEN_EXPIRING"}
    state.update(state="OK", message="token is 0.1 days old")
    bot._token_check(et(9, 31, 30))                                                  # within TOKEN_CHECK_SEC: not re-read yet
    assert bot._token_halt is not None
    bot._token_check(et(9, 32, 5))
    assert bot._token_halt is None and bot.entry_halt_reason() is None


def test_a_token_warning_does_not_halt_entries_and_a_broken_check_never_raises(bot, monkeypatch):
    monkeypatch.setattr(bot.broker, "token_status", lambda now: {"state": "WARN", "message": "5.2 days old"}, raising=False)
    bot._token_check(et(9, 31))
    assert bot.entry_halt_reason() is None
    monkeypatch.setattr(bot.broker, "token_status", lambda now: 1 / 0, raising=False)
    bot._token_checked_at = None
    bot._token_check(et(9, 40))                                                      # swallowed and logged


def test_the_start_report_shows_the_token_line_when_the_broker_has_one(bot, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(bot.broker, "token_status", lambda now: {"state": "WARN", "message": "5.2 days old"}, raising=False)
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    assert any("Schwab token: WARN -- 5.2 days old" in r.getMessage() for r in caplog.records)


# ------------------------------------------------- ASTRA-only audit (2026-10-08): live-ES break close, entry-wait watchdog, shared legs

def _es_on_break(bot, direction, break_close_es, basis=-55.0):
    from strategy import OrbSetup
    bot.basis = basis
    bot.on_setup = OrbSetup(7680.0, 7620.0)
    bot.on_setup.state = OrbSetup.DONE
    bot.on_setup.direction = direction
    bot.on_setup.break_close = break_close_es
    bot.on_setup_symbol = config.ES_SYMBOL


def test_a_live_es_break_close_is_converted_to_spx_terms_before_the_continuation_check(bot, monkeypatch):
    _es_on_break(bot, BULLISH, 7679.0)                  # ES terms; 7679 - 55 = 7624 SPX
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0                            # SPX beats 7624 by 5 -> a real continuation
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert len(calls) == 1
    assert events(bot, "CONTINUATION_REENTRY")[0]["break_close"] == 7624.0


def test_a_reversed_bearish_live_es_break_no_longer_looks_like_a_continuation(bot, monkeypatch):
    from strategy import BEARISH
    _es_on_break(bot, BEARISH, 7620.0)                  # SPX terms 7565; spot has since come back UP to 7600
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7600.0
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == []                                  # uncorrected: -(7600 - 7620) = +20 "beat" -> a re-entry INTO the reversal


def test_a_bullish_live_es_break_that_continues_is_no_longer_blocked_by_the_scale(bot, monkeypatch):
    _es_on_break(bot, BULLISH, 7679.0)
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7626.0                            # 7626 - 7679 = -53 uncorrected: "never continues"
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert len(calls) == 1


def test_a_live_es_break_without_a_basis_skips_the_continuation_check_instead_of_guessing(bot, monkeypatch):
    _es_on_break(bot, BULLISH, 7679.0)
    bot.basis = None
    calls = _spy_enter(bot, monkeypatch)
    monkeypatch.setattr(bot.broker, "get_spot", lambda *a, **k: pytest.fail("no spot call without a usable break level"))
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert calls == []


def test_an_spx_terms_setup_is_untouched_by_the_conversion(bot, monkeypatch):
    _es_on_break(bot, BULLISH, 7624.0, basis=-55.0)
    bot.on_setup_symbol = config.SPX_SYMBOL             # the proxy path: levels already shifted to SPX terms
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert len(calls) == 1 and events(bot, "CONTINUATION_REENTRY")[0]["break_close"] == 7624.0
    bot.on_setup_symbol = None                          # a state file from before the symbol was recorded
    bot.last_continuation_check.clear()
    assert bot._spx_terms(7624.0, bot.on_setup_symbol) == 7624.0


def test_the_signal_says_which_level_source_and_basis_it_used(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)
    monkeypatch.setattr(scheduler.market_data, "get_candles",
                        lambda *a, **k: make_candles(et(9, 30), [(7675, 7685, 7674, 7684), (7684, 7690, 7681, 7688)]))
    bot.overnight = Levels(7680.0, 7620.0, et(9, 29))   # ES terms
    bot.basis = -55.0
    dispatched = []
    monkeypatch.setattr(bot, "dispatch_entry", lambda *a, **k: dispatched.append(a))
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), [(7620, 7630, 7619, 7624)]))
    sig = events(bot, "SIGNAL")[0]
    assert sig["level_symbol"] == config.ES_SYMBOL and sig["basis"] == -55.0 and sig["high"] == 7680.0
    assert dispatched and dispatched[0][4] == pytest.approx(7684.0 - 55.0)       # the break close handed on is in SPX terms


def test_the_proxy_signal_records_the_spx_source(bot, monkeypatch):
    _done_on_break(bot)
    sig = events(bot, "SIGNAL")[0]
    assert sig["level_symbol"] == config.SPX_SYMBOL and sig["basis"] == 0.0


def test_a_momentum_wait_with_an_unconvertible_break_close_is_skipped_not_started(bot, monkeypatch):
    real = scheduler.strategy.exit_setting
    monkeypatch.setattr(scheduler.strategy, "exit_setting",
                        lambda s, name, default=None: True if name == "MOMENTUM_CONFIRM_ENABLED" else real(s, name, default))
    calls = _spy_enter(bot, monkeypatch)
    bot.dispatch_entry(et(9, 34), "ORB", BULLISH, "MOMENTUM", None)
    assert bot.pending_momentum == {} and calls == []
    assert {r["reason_code"] for r in skipped(bot)} == {"MOMENTUM_BASIS_UNKNOWN"}


def test_the_start_report_says_whether_live_es_is_on(bot, monkeypatch):
    monkeypatch.setattr(scheduler.market_data, "live_es_status", lambda: (False, "access token expired"))
    line = bot._live_es_report_line()
    assert "OFF" in line and "access token expired" in line and "SPX-ES basis" in line
    monkeypatch.setattr(scheduler.market_data, "live_es_status", lambda: (True, "token file holds a valid access token"))
    assert "ON" in bot._live_es_report_line()
    monkeypatch.setattr(scheduler.market_data, "live_es_status", lambda: 1 / 0)
    assert "status unavailable" in bot._live_es_report_line()


def test_a_slow_entry_wait_does_not_get_a_healthy_position_flattened(bot):
    _open_both(bot)                                      # both priced at 10:15:00
    bot._entry_wait_end = et(10, 17, 5)                  # the loop was blocked working an order until 10:17:05 (125 s)
    bot._check_marks(et(10, 17, 6))
    assert set(bot.pm.positions) == {"A", "B"} and bot._data_stale is False and events(bot, "STALE_MARK") == []
    for p in bot.pm.positions.values():
        p.last_mark_at = et(10, 17, 7)                   # the first tick after the wait priced them
    bot._check_marks(et(10, 17, 9))
    assert bot._data_stale is False






def test_work_entry_records_when_it_released_the_loop(bot, monkeypatch):
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 20, 30))
    assert bot._entry_wait_end is None
    assert bot.work_entry({"id": "x", "status": "dry_run"}, 0.9, 0.65) == {"id": "x", "status": "dry_run"}
    assert bot._entry_wait_end == et(10, 20, 30)
    monkeypatch.setattr(bot, "_work_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broker down")))
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 22))
    with pytest.raises(RuntimeError):
        bot.work_entry({"id": "x"}, 0.9, 0.65)
    assert bot._entry_wait_end == et(10, 22)             # recorded even when the wait raised


def _overlap_setup(bot, monkeypatch, short, long):
    """B open on 7565/7560P; the next strategy is offered `short`/`long` on the same side."""
    from strategy import SpreadQuote
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert "B" in bot.pm.positions and (bot.pm.positions["B"].short_strike, bot.pm.positions["B"].long_strike) == (7565.0, 7560.0)
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    monkeypatch.setattr(scheduler.strategy, "select_strikes_b",
                        lambda *a, **k: (short, long, SpreadQuote(mid=0.90, bid_side=0.85, ask_side=0.95)))
    bot.enter(et(10, 25), "ES_BREAK", BULLISH, strategies=("C",))
    return [r for r in events(bot, "ENTRY_REJECTED") if r["strategy"] == "C"]


@pytest.mark.parametrize("short,long", [(7570.0, 7565.0), (7560.0, 7555.0)], ids=["new long is the open short", "new short is the open long"])
def test_a_spread_that_shares_a_strike_with_an_open_one_is_refused(bot, monkeypatch, short, long):
    rejected = _overlap_setup(bot, monkeypatch, short, long)
    assert "C" not in bot.pm.positions
    assert rejected and "STRIKE_COLLISION" in rejected[0]["reason"] and "shares a strike" in rejected[0]["reason"] \
        and "under B" in rejected[0]["reason"]
    assert {r["reason_code"] for r in skipped(bot) if r["strategy"] == "C"} == {"STRIKE_COLLISION"}


def test_a_neighbouring_spread_with_no_shared_strike_is_still_allowed(bot, monkeypatch):
    rejected = _overlap_setup(bot, monkeypatch, 7555.0, 7550.0)
    assert rejected == [] and "C" in bot.pm.positions


def test_find_collision_ignores_other_rights_roots_and_expirations(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    b = bot.pm.positions["B"]
    f = bot.pm.find_collision
    assert f(b.root, b.expiration, "P", 7565.0, 7560.0) == "B" and f(b.root, b.expiration, "P", 7560.0, 7555.0) == "B"
    assert f(b.root, b.expiration, "C", 7565.0, 7570.0) is None
    assert f("TSLA", b.expiration, "P", 7565.0, 7560.0) is None
    assert f(b.root, b.expiration + timedelta(days=1), "P", 7565.0, 7560.0) is None
    assert bot.pm.collision_reason("B", "P", 7565.0, 7560.0) == "STRIKE_COLLISION 7565/7560P already open under B"


def test_main_refuses_to_start_a_second_copy(monkeypatch, caplog):
    import sys
    monkeypatch.setattr(sys, "argv", ["scheduler.py"])
    monkeypatch.setattr(scheduler, "setup_logging", lambda: None)
    monkeypatch.setattr(scheduler, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(scheduler.instance_lock, "acquire", lambda directory, name: (False, "pid 4242"))
    monkeypatch.setattr(scheduler, "Bot", lambda *a, **k: pytest.fail("a second copy must not even build its broker"))
    with pytest.raises(SystemExit) as exc:
        scheduler.main()
    assert exc.value.code == 2
    assert any("already running (pid 4242)" in r.getMessage() for r in caplog.records)


def test_main_takes_the_lock_named_after_the_persona(monkeypatch):
    import sys
    seen = []
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--once"])
    monkeypatch.setattr(scheduler, "setup_logging", lambda: None)
    monkeypatch.setattr(scheduler, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(scheduler.instance_lock, "acquire", lambda directory, name: seen.append((directory, name)) or (True, ""))

    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop
    monkeypatch.setattr(scheduler, "Bot", stop)
    with pytest.raises(Stop):
        scheduler.main()
    assert seen == [(scheduler.LOG_DIR, f"0dte_{config.TRADER_NAME.lower()}")]
    monkeypatch.setattr(config, "SINGLE_INSTANCE_LOCK", False)
    seen.clear()
    with pytest.raises(Stop):
        scheduler.main()
    assert seen == []


# ---- review follow-ups (2026-10-08): the watchdog clock restarts when the loop is released; live-ES fallback and chase; coverage gaps

def test_a_position_still_unpriced_after_the_wait_alerts_then_flattens_on_the_normal_schedule(bot):
    _open_both(bot)
    bot._entry_wait_end = et(10, 17, 5)                  # the loop was released at 10:17:05 after a 125 s wait
    bot._check_marks(et(10, 17, 20))                     # 15 s after the release: nothing
    assert set(bot.pm.positions) == {"A", "B"} and bot._data_stale is False and events(bot, "STALE_MARK") == []
    bot._check_marks(et(10, 17, 30))                     # 25 s after: no price yet -> alert and entries halted
    assert bot._data_stale is True and set(bot.pm.positions) == {"A", "B"} and {e["key"] for e in events(bot, "STALE_MARK")} == {"A", "B"}
    bot._check_marks(et(10, 18, 40))                     # 95 s after the release and still no price: flattened as before
    assert bot.pm.positions == {} and {e["exit_reason"] for e in events(bot, "EXIT")} == {"STALE_DATA_FLATTEN"}


def test_the_staleness_clock_restarts_only_at_the_end_of_the_wait(bot):
    _open_both(bot)
    bot._entry_wait_end = et(10, 16, 0)
    bot._check_marks(et(10, 16, 31))                     # 91 s since the last price, but only 31 s since the loop was released
    assert set(bot.pm.positions) == {"A", "B"}
    bot._check_marks(et(10, 17, 31))                     # 91 s since the release
    assert bot.pm.positions == {}


def test_a_slow_entry_through_the_real_enter_path_does_not_get_the_open_position_flattened(bot, monkeypatch):
    from datetime import timedelta
    _open_both(bot)
    bot.pm.positions.pop("B")                            # B's slot is free for a new entry; A stays open, last priced 10:15:00
    clock = [et(10, 15)]
    monkeypatch.setattr(scheduler, "now_et", lambda: clock[0])
    monkeypatch.setattr(config, "ENTRY_TIMEOUT_SEC", 0)  # one wait, then give up
    bot.broker.fill_entries = False                      # the order rests unfilled

    def slow_wait(order_id, timeout_sec, poll_sec=2.0):
        clock[0] += timedelta(seconds=125)               # the loop is blocked for 125 s
        return bot.broker.get_order(order_id)
    monkeypatch.setattr(bot.broker, "wait_for_fill", slow_wait)
    bot.enter(clock[0], "ORB", BULLISH, strategies=("B",))
    assert clock[0] == et(10, 17, 5) and bot._entry_wait_end == et(10, 17, 5)       # stamped when the wait ENDED
    bot._check_marks(clock[0] + timedelta(seconds=1))
    assert set(bot.pm.positions) == {"A"} and events(bot, "EXIT") == []


def test_a_failed_live_es_request_carries_on_with_the_spx_candles_in_the_same_call(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    state = {"live": True}
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: state["live"])

    def es_candles(symbol, *a, **k):
        assert symbol == config.ES_SYMBOL
        state["live"] = False                            # the request failed: live ES is off for a while
        return make_candles(et(9, 30), [], minutes=2)
    monkeypatch.setattr(scheduler.market_data, "get_candles", es_candles)
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)]
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert bot.on_setup_symbol == config.SPX_SYMBOL and bot.on_setup.state == bot.on_setup.DONE       # the break fired on SPX candles
    assert events(bot, "SIGNAL")[0]["level_symbol"] == config.SPX_SYMBOL and bot._es_chase_until is None


def test_a_live_es_frame_behind_the_spx_candle_is_chased_until_it_catches_up(bot, monkeypatch):
    from datetime import timedelta
    from conftest import make_candles
    from strategy import Levels
    spx = make_candles(et(9, 30), [(7615, 7625, 7614, 7624), (7624, 7630, 7621, 7628)])          # 09:30 and 09:32
    behind = make_candles(et(9, 30), [(7675, 7685, 7674, 7684)])                                  # ES has only 09:30
    full = make_candles(et(9, 30), [(7675, 7685, 7674, 7684), (7684, 7690, 7681, 7688)])
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)
    frames = {"es": behind}
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: frames["es"])
    bot.overnight = Levels(7680.0, 7620.0, et(9, 29))
    bot.try_on_break_entry(et(9, 34), spx)
    assert bot._es_chase_until == spx.index[-1].to_pydatetime() + timedelta(minutes=scheduler.ES_CHASE_MIN)
    assert events(bot, "SIGNAL") == []
    bot._es_chase_until = None
    frames["es"] = full                                  # the second ES candle has arrived: the momentum entry fires on it
    bot.try_on_break_entry(et(9, 34, 3), spx)
    assert bot._es_chase_until is None and events(bot, "SIGNAL")[0]["level_symbol"] == config.ES_SYMBOL


def test_a_failed_request_with_no_spx_fallback_yet_keeps_chasing(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    spx = make_candles(et(9, 30), [(7615, 7625, 7614, 7624)])
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: False)
    monkeypatch.setattr(scheduler.market_data, "live_es_backoff_active", lambda: True)
    bot.overnight = Levels(7680.0, 7620.0, et(9, 29))
    bot.basis = None                                     # no SPX-terms level yet (Yahoo's delayed ES has no bar): nothing to evaluate
    bot.try_on_break_entry(et(9, 32, 5), spx)
    assert bot._es_chase_until is not None
    bot._es_chase_until = None
    monkeypatch.setattr(scheduler.market_data, "live_es_backoff_active", lambda: False)
    bot.try_on_break_entry(et(9, 32, 6), spx)
    assert bot._es_chase_until is None                   # no back-off, nothing to wait for


def test_an_explicit_es_source_never_chases(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    monkeypatch.setattr(config, "BREAKOUT_LEVEL_SOURCE", "ES")
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: False)
    import pandas as pd
    monkeypatch.setattr(scheduler.market_data, "get_candles",
                        lambda *a, **k: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))    # what market_data returns
    bot.overnight = Levels(7680.0, 7620.0, et(9, 29))
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), [(7615, 7625, 7614, 7624)]))
    assert bot._es_chase_until is None


def test_tick_evaluates_the_breakout_setups_again_only_while_chasing(bot, monkeypatch):
    from conftest import make_candles
    spx = make_candles(et(9, 30), [(7615, 7625, 7614, 7624)])
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: spx)
    monkeypatch.setattr(bot, "refresh_levels", lambda now: None)
    calls = []
    monkeypatch.setattr(bot, "try_on_break_entry", lambda now, c: calls.append("on"))
    monkeypatch.setattr(bot, "try_es_break_entry", lambda now, c: calls.append("es"))
    bot.last_candle = spx.index[-1].to_pydatetime()      # no NEW candle
    bot.tick(et(9, 33, 5))
    assert calls == []
    bot._es_chase_until = et(9, 37)
    bot.tick(et(9, 33, 6))
    assert calls == ["on", "es"] and bot._es_chase_until is None
    bot._es_chase_until = et(9, 37)
    bot.tick(et(9, 37, 1))                               # the chase window has closed
    assert calls == ["on", "es"]


def test_the_es_break_signal_records_its_level_source_too(bot, monkeypatch):
    from conftest import make_candles
    from strategy import Levels
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C"))
    monkeypatch.setattr(scheduler.market_data, "live_es_available", lambda: True)
    monkeypatch.setattr(scheduler.market_data, "get_candles",
                        lambda *a, **k: make_candles(et(9, 30), [(7675, 7685, 7674, 7684), (7684, 7690, 7681, 7688)]))
    bot.overnight = Levels(7680.0, 7620.0, et(9, 29))
    bot.basis = -55.0
    bot.try_es_break_entry(et(9, 34), make_candles(et(9, 30), [(7615, 7625, 7614, 7624)]))
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "ES_BREAK"][0]
    assert sig["level_symbol"] == config.ES_SYMBOL and sig["basis"] == -55.0


def test_only_the_on_break_setup_is_converted_not_the_orb_setup(bot, monkeypatch):
    from strategy import OrbSetup
    _es_on_break(bot, BULLISH, 7679.0, basis=-55.0)      # ON_BREAK in ES terms: 7624 in SPX points
    bot.orb_setup = OrbSetup(7650.0, 7600.0)             # ORB is always in SPX terms
    bot.orb_setup.state = OrbSetup.DONE
    bot.orb_setup.direction = BULLISH
    bot.orb_setup.break_close = 7655.0
    calls = _spy_enter(bot, monkeypatch)
    bot.broker.spot = 7629.0                             # beats ON_BREAK's 7624, but is 26 points BELOW ORB's break close
    bot.check_continuation_reentry(et(9, 39), scheduler.Phase.OVERNIGHT_ONLY)
    assert [args[1] for args, _ in calls] == ["ON_BREAK"]


def test_the_start_report_prints_the_live_es_line(bot, caplog):
    caplog.set_level("INFO")
    bot.report_start(et(9, 0), bot.broker.get_account(), [])
    assert any("Live ES (Schwab /ES): OFF" in r.getMessage() for r in caplog.records)


def test_main_says_so_when_the_lock_cannot_be_used(monkeypatch, caplog):
    import sys
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--once"])
    monkeypatch.setattr(scheduler, "setup_logging", lambda: None)
    monkeypatch.setattr(scheduler, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(scheduler.instance_lock, "acquire", lambda directory, name: (True, "lock unavailable (x); not enforced"))

    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop
    monkeypatch.setattr(scheduler, "Bot", stop)
    with pytest.raises(Stop):
        scheduler.main()
    assert any("NOT enforced" in r.getMessage() and r.keep is True for r in caplog.records)


# ---- 2026-10-09: ASTRA runs D/E loosened

def test_the_scheduler_honours_e_confirm_candles(bot, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("E",))
    monkeypatch.setattr(config, "E_CONFIRM_CANDLES", 3)
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles_cross_session(2, "up"))
    monkeypatch.setattr(scheduler.strategy, "mean_reversion_signal", lambda *a, **k: "BULLISH")
    bot.check_mean_reversion_entry(et(9, 40, day=17), scheduler.Phase.ORB_ONLY)
    assert bot.pm.positions == {}                                      # two same-colour candles are no longer enough
    bot.last_de_candle = None
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: _de_candles_cross_session(3, "up"))
    bot.check_mean_reversion_entry(et(9, 45, day=17), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"E"}


# ---- review follow-ups (2026-10-09): the REAL, unmocked D/E logic with ASTRA's thresholds versus the spec ones

def _astra_de():
    overrides = getattr(config, "PERSONA_OVERRIDES", {}).get("ASTRA")
    if not overrides:
        pytest.skip("the Schwab twin has no ASTRA persona")
    return {k: v for k, v in overrides.items() if k != "STRATEGIES"}


def _frame_from_closes(closes, today_bars=3, green_today=False):
    """5-minute candles for the last `today_bars` of `closes` on today (day 17) and the rest on the day before, opens = the prior close
    (or, with green_today, below the close so the last candles are green)."""
    import pandas as pd
    from conftest import make_candles
    bars, prev = [], None
    for i, c in enumerate(closes):
        o = prev if prev is not None else c
        if green_today and i >= len(closes) - today_bars:
            o = c - 0.3
        bars.append((o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        prev = c
    split = len(bars) - today_bars
    return pd.concat([make_candles(et(10, 0, day=16), bars[:split], minutes=5), make_candles(et(9, 30, day=17), bars[split:], minutes=5)])


def _run_de(bot, monkeypatch, strategies, thresholds, frame, now):
    monkeypatch.setattr(config, "STRATEGIES", strategies)
    for name, value in thresholds.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "EXIT_BB_TOUCH_PCT_B", 0.95, raising=False)       # the exit's own band touch: must not be what D/E read
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: frame)
    bot.check_mean_reversion_entry(now, scheduler.Phase.ORB_ONLY)
    return set(bot.pm.positions)


SPEC_DE = {"D_BAND_TOUCH_PCT_B": 0.95, "D_EMA_CHOP_THRESHOLD_PTS": 2.0, "E_USE_TREND_GATE": True, "E_CONFIRM_CANDLES": 2}


def test_d_takes_a_mild_dip_with_astras_touch_level_and_not_with_the_spec_one(bot, monkeypatch, tmp_path):
    closes = [7600 + 0.4 * i for i in range(60)]
    closes[-1] -= 10                                                    # %B 0.23 on a rising day: a 0.8-sigma dip, far from the 5% touch
    frame = _frame_from_closes(closes)
    assert _run_de(bot, monkeypatch, ("D",), _astra_de(), frame, et(9, 45, day=17)) == {"D"}
    bot.pm.positions.clear()
    bot.last_de_candle, bot.acted = None, set()
    assert _run_de(bot, monkeypatch, ("D",), SPEC_DE, frame, et(9, 45, day=17)) == set()


def test_d_trades_a_narrow_ema_gap_with_astras_chop_threshold_and_sits_out_with_the_spec_one(bot, monkeypatch):
    closes = [7600 + 0.2 * i for i in range(60)]
    closes[-1] -= 9                                                     # EMA9 only 0.8 above EMA30; %B -0.2 (a real touch either way)
    frame = _frame_from_closes(closes)
    assert _run_de(bot, monkeypatch, ("D",), _astra_de(), frame, et(9, 45, day=17)) == {"D"}
    bot.pm.positions.clear()
    bot.last_de_candle, bot.acted = None, set()
    assert _run_de(bot, monkeypatch, ("D",), {**SPEC_DE, "D_BAND_TOUCH_PCT_B": 0.70}, frame, et(9, 45, day=17)) == set()   # chop alone


def test_ungated_e_with_three_green_candles_enters_with_astras_values_and_not_with_the_spec_ones(bot, monkeypatch):
    closes = [7600 + 0.4 * i for i in range(57)]
    closes += [closes[-1] - 14 + 0.5 * j for j in range(3)]             # a sharp drop, then three small green candles
    frame = _frame_from_closes(closes, green_today=True)
    assert _run_de(bot, monkeypatch, ("E",), _astra_de(), frame, et(9, 45, day=17)) == {"E"}
    bot.pm.positions.clear()
    bot.last_de_candle, bot.acted = None, set()
    assert _run_de(bot, monkeypatch, ("E",), SPEC_DE, frame, et(9, 45, day=17)) == set()
    bot.last_de_candle, bot.acted = None, set()
    two_only = {**_astra_de(), "E_CONFIRM_CANDLES": 3}
    short = _frame_from_closes(closes, today_bars=2, green_today=True)                        # only two of today's candles are green
    assert _run_de(bot, monkeypatch, ("E",), two_only, short, et(9, 40, day=17)) == set()


def test_the_exit_bollinger_touch_is_not_what_d_reads(bot, monkeypatch):
    closes = [7600 + 0.4 * i for i in range(60)]
    closes[-1] -= 10
    frame = _frame_from_closes(closes)
    thresholds = {**_astra_de(), "D_BAND_TOUCH_PCT_B": 0.70}
    monkeypatch.setattr(config, "STRATEGIES", ("D",))
    for name, value in thresholds.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "EXIT_BB_TOUCH_PCT_B", 0.99, raising=False)                  # a different exit touch changes nothing for D
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: frame)
    bot.check_mean_reversion_entry(et(9, 45, day=17), scheduler.Phase.ORB_ONLY)
    assert set(bot.pm.positions) == {"D"}


def test_the_module_level_d_e_defaults_are_still_the_spec_values():
    from conftest import DE_SPEC
    assert DE_SPEC == SPEC_DE                                            # the persona overrides change them per persona, never the default


def test_a_weekend_start_says_why_it_exits_where_the_console_can_see_it(bot, caplog, monkeypatch):
    caplog.set_level("INFO")
    monkeypatch.setattr(bot, "loop", lambda: pytest.fail("a weekend start with nothing open must not run its loop"))
    bot.run(et(10, 36, day=19))                                          # 2026-09-19 is a Saturday
    lines = [r for r in caplog.records if "Weekend (Saturday)" in r.getMessage()]
    assert len(lines) == 1 and lines[0].keep is True                     # keep=True: the console filter shows only kept records
    assert "exiting" in lines[0].getMessage() and "trading day" in lines[0].getMessage()


def test_a_start_after_the_hard_stop_says_so_too(bot, caplog, monkeypatch):
    caplog.set_level("INFO")
    monkeypatch.setattr(bot, "loop", lambda: pytest.fail("a bot started after the hard stop with nothing open must not run its loop"))
    bot.run(et(14, 0))
    lines = [r for r in caplog.records if "Started after the hard stop" in r.getMessage()]
    assert len(lines) == 1 and lines[0].keep is True and "13:00" in lines[0].getMessage()


# ------------------------------------------------- 2026-10-15: F on a shared (Schwab) account adopts only the stock spreads it opened

def test_stock_legs_the_bot_did_not_open_are_adopted_only_when_untracked_adoption_is_on(bot, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(config, "STRATEGIES", ("B", "F"))
    soon = TODAY + timedelta(days=3)                                          # inside F's 0-7 day window
    bot.broker.add_spread_position("P", 300.0, 295.0, 1, 4.0, 2.5, soon, root="TSLA")   # the owner's own, or a 45DTE spread near its expiration
    positions = bot.broker.get_positions()
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_STOCK", False)
    assert bot.bot_legs(positions) == []
    assert {leg["strike"] for leg in bot.foreign_legs(positions)} == {300.0, 295.0}
    bot._saved_stock = {("TSLA", "P", soon, 300.0), ("TSLA", "P", soon, 295.0)}   # today's saved state says F opened it (a restart)
    assert {leg["strike"] for leg in bot.bot_legs(positions)} == {300.0, 295.0}
    bot._saved_stock = set()
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_STOCK", True)                 # the dedicated Alpaca paper accounts
    assert {leg["strike"] for leg in bot.bot_legs(positions)} == {300.0, 295.0}


def test_a_restart_remembers_the_stock_spreads_todays_state_says_f_opened(bot, monkeypatch):
    from test_position_manager import f_spread
    monkeypatch.setattr(config, "STRATEGIES", ("B", "F"))
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_STOCK", False)
    spread = f_spread("TSLA")
    bot.pm.open(spread)
    bot.save_state()
    bot.broker.add_spread_position(spread.right, spread.short_strike, spread.long_strike, 1, 0.5, 0.32, spread.expiration, root="TSLA")
    again = scheduler.Bot(dry_run=False, once=True)
    again.today = TODAY
    monkeypatch.setattr(again, "run", lambda now: None)                       # start() ends in the main loop
    again.start()
    assert again._saved_stock == {("TSLA", spread.right, spread.expiration, float(spread.short_strike)),
                                  ("TSLA", spread.right, spread.expiration, float(spread.long_strike))}
    assert {leg["strike"] for leg in again.bot_legs(again.broker.get_positions())} == {float(spread.short_strike), float(spread.long_strike)}


def test_the_bots_own_open_loss_counts_but_the_accounts_value_does_not_on_the_bot_basis(bot, monkeypatch):
    """2026-10-15 (Schwab): DAILY_LOSS_BASIS "bot" -- the loss limit sees this bot's open spread marks, never the account's total."""
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    from test_position_manager import f_spread
    assert bot.loss_limit_strategies() == tuple(config.ALL_STRATEGIES)           # nothing exempt: F counts too
    bot.broker.equity = 100.0                                                   # the account value collapsed: nothing this bot did
    assert bot.entry_possible(100.0) is True
    spread = f_spread("TSLA", credit=0.80, width=5.0)
    spread.current_price = 4.20                                                 # an open F spread marked about -$340
    bot.pm.open(spread)
    assert bot.pm.open_pnl(bot.loss_limit_strategies()) == pytest.approx(-340.0)
    assert bot.entry_possible(100.0) is False                                   # 1% of 10,000 = -$100: this bot's own loss stops entries
    assert all("DAILY_LOSS_LIMIT bot P/L=-340.00" in reason for reason in bot.last_entry_blocks.values())


def test_the_start_report_shows_the_days_limits_in_dollars(bot, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", 0.02, raising=False)
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Daily limits:"))
    assert "1.0% of $10,000.00, measured on this bot's own P/L" in line and "profit target $200.00 (2.0%)" in line
    assert "exempt" not in line
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", None, raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ("F",))
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "equity", raising=False)
    caplog.clear()
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Daily limits:"))
    assert "measured on account equity" in line and "no profit target" in line and "exempt from the loss limit: F" in line


def test_the_start_report_states_the_days_trade_caps(bot, monkeypatch, caplog):
    """2026-11-12: the morning console says how many trades the bot may take (Schwab live: 3 each, 3 together, B+C share 3, 1 open)."""
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "STRATEGIES", ("B", "C", "F"))
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 3)
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 3)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("B", "C"): 3})
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 1)
    monkeypatch.setattr(config, "MAX_CONSECUTIVE_LOSSES", 2)
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    line = next(r for r in caplog.records if r.getMessage().startswith("Trade caps today:"))
    assert line.keep is True
    assert line.getMessage() == ("Trade caps today: per strategy B 3, C 3, F 3; 3 across every strategy together; B+C share 3; "
                                 "1 open at a time; losses in a row that stop a strategy: B 2, C 2, F 7")
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", None)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {})
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", None)
    caplog.clear()
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Trade caps today:"))
    assert "no cap across strategies" in line and "share" not in line and "no cap on open positions" in line


def test_a_refused_entry_reaches_the_log_once_per_reason_set(bot, monkeypatch, caplog):
    """2026-11-12: the first refusal with a given set of reasons is kept (console + log file); repeats of the same reasons are not,
    a different reason set is kept again, and an entry in between resets it."""
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {("A", "B"): 2})
    bot.risk.record_trade("A", 10.0)
    bot.risk.record_trade("B", 10.0)
    equity = bot.broker.get_account()["equity"]
    assert bot.entry_possible(equity, "ORB") is False
    assert bot.entry_possible(equity, "ORB") is False
    kept = [r for r in caplog.records if r.getMessage().startswith("No new entries:") and getattr(r, "keep", False)]
    assert len(kept) == 1 and "SHARED_MAX_TRADES_PER_DAY (A+B 2/2)" in kept[0].getMessage()
    bot.risk.record_trade("A", -10.0)                                                 # a different reason set: kept again
    assert bot.entry_possible(equity, "ORB") is False
    kept = [r for r in caplog.records if r.getMessage().startswith("No new entries:") and getattr(r, "keep", False)]
    assert len(kept) == 2
    bot._last_no_entry = None
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY_SHARED", {})
    assert bot.entry_possible(equity, "ORB") is True and bot._last_no_entry is None


def test_the_per_strategy_entry_check_sees_the_bots_open_loss_on_the_bot_basis(bot, monkeypatch):
    """enter_leg runs its own trading_allowed after entry_possible; it must pass the bot's open marks too, not only entry_possible."""
    from test_position_manager import f_spread
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    spread = f_spread("TSLA", credit=0.80, width=5.0)
    spread.current_price = 4.20                                                 # the open F spread is marked -$340 against a -$100 limit
    bot.pm.open(spread)
    summary, added = bot.enter_leg("B", et(10, 15), "ORB", BULLISH, 100.0, SPOT, 5, "P", [], 42.0, 0.0)
    assert summary.startswith("skip: DAILY_LOSS_LIMIT bot P/L=-340.00") and added == 0.0 and "B" not in bot.pm.positions


def test_on_the_bot_basis_an_exempt_strategys_open_loss_does_not_count(bot, monkeypatch):
    from test_position_manager import f_spread
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ("F",))
    assert "F" not in bot.loss_limit_strategies()
    spread = f_spread("TSLA", credit=0.80, width=5.0)
    spread.current_price = 4.20                                                 # F is down $340, but F is exempt here (the Alpaca rule)
    bot.pm.open(spread)
    assert bot.entry_possible(100.0) is True

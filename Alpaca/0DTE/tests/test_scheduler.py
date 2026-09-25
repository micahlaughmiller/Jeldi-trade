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

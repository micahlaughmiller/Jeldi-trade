"""Entry flow of the Bot with a FakeBroker: one signal opens A and B independently."""

import json
from datetime import date

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


def test_a_skips_the_overnight_setup_b_trades_it(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(9, 40), "OVERNIGHT", BULLISH)
    assert set(bot.pm.positions) == {"B"}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL OVERNIGHT")][0]
    assert "A: skip: OVERNIGHT setup disabled for A" in line     # A trades the overnight levels via ON_BREAK instead


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
    assert set(bot.pm.positions) == {"A"}      # B doesn't trade ON_BREAK at all; A is the one being tested here
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

def test_on_break_momentum_entry_is_a_only(bot, caplog):
    from conftest import make_candles
    from strategy import Levels
    caplog.set_level("INFO")
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624),      # 09:30 break: closes above 7620
            (7624, 7630, 7621, 7628)]      # 09:32 holds and closes above the break close -> momentum
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"A"}
    assert bot.pm.positions["A"].setup == "ON_BREAK"
    sig = [r for r in events(bot, "SIGNAL") if r["setup"] == "ON_BREAK"]
    assert sig and sig[0]["trigger"] == "MOMENTUM" and sig[0]["direction"] == BULLISH
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ON_BREAK BULLISH at 09:34")][0]
    assert "B: skip: ON_BREAK setup disabled for B" in line
    # the same candles again do not re-fire
    bot.try_on_break_entry(et(9, 34), make_candles(et(9, 30), bars))
    assert len([r for r in events(bot, "SIGNAL") if r["setup"] == "ON_BREAK"]) == 1


def test_on_break_pullback_entry(bot):
    from conftest import make_candles
    from strategy import Levels
    bot.overnight = Levels(7620.0, 7560.0, et(9, 29))
    bot.basis = 0.0
    bars = [(7615, 7625, 7614, 7624),      # break
            (7624, 7625, 7619, 7621),      # wick back to the level, holds
            (7621, 7623, 7620.5, 7622)]    # green close above -> pullback entry
    bot.try_on_break_entry(et(9, 36), make_candles(et(9, 30), bars))
    assert set(bot.pm.positions) == {"A"}
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

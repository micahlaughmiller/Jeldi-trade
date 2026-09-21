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


def test_signal_opens_a_then_b(bot, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    a, b = bot.pm.positions["A"], bot.pm.positions["B"]
    assert (a.short_strike, a.long_strike, a.qty, a.entry_credit) == (7610.0, 7605.0, 2, 3.00)
    assert (a.profit_target, a.stop_loss) == (0.30, 0.30)
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
    bot.broker.spread_close_price = 3.35
    bot.record_fills(bot.pm.on_tick(et(10, 30), {"A": 3.35, "B": 0.95}, None))
    assert set(bot.pm.positions) == {"B"}
    assert len(bot.broker.close_calls) == calls_before + 1
    s = bot.risk.state
    assert s.for_strategy("A").trades_today == 1 and s.for_strategy("A").consecutive_losses == 1
    assert s.for_strategy("B").trades_today == 0
    assert bot.journal.trades_path.read_text().splitlines()[1].startswith("2026-09-17,A,")

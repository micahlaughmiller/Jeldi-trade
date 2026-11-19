"""flatten_now.py (19 Nov 2026): the emergency hand-brake closes every leg at the broker, at the market, including a spread
the bot's own state no longer tracks (a close the bot booked that the broker never completed)."""
from datetime import date

import pytest

import config
import flatten_now
import scheduler
from fake_broker import FakeBroker

TODAY = date(2026, 9, 17)


@pytest.fixture
def bot(tmp_path, monkeypatch) -> scheduler.Bot:
    broker = FakeBroker(equity=10_000, today=TODAY)
    broker.spot = 7600.0
    broker.set_chain("P", {7560.0: (1.0, 1.2), 7555.0: (0.4, 0.6), 7540.0: (0.5, 0.7), 7535.0: (0.2, 0.3)})
    broker.set_chain("C", {7600.0: (21.0, 23.0)})
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: broker)
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_SPXW", True)        # --adopt-all
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    b = scheduler.Bot(dry_run=False, once=True)
    b.today = TODAY
    return b


def test_flatten_closes_every_same_day_spxw_leg_at_the_market_even_when_untracked(bot):
    bot.broker.add_spread_position("P", 7560.0, 7555.0, 1, 1.10, 0.50)       # the bot booked this one closed; the broker still holds it
    bot.broker.add_spread_position("P", 7540.0, 7535.0, 1, 0.60, 0.25)
    assert flatten_now.flatten(bot) is True
    assert bot.broker.get_positions() == []
    assert len(bot.broker.close_calls) == 2 and all(bot.broker.close_urgent)   # marketable from the first order
    assert bot.pm.positions == {}


def test_flatten_with_nothing_at_the_broker_is_a_no_op(bot):
    assert flatten_now.flatten(bot) is True
    assert bot.broker.close_calls == []


def test_flatten_leaves_a_leg_that_is_not_this_bots(bot, monkeypatch):
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_SPXW", False)       # the default: only what today's state says the bot opened
    bot.broker.add_spread_position("P", 7560.0, 7555.0, 1, 1.10, 0.50)
    assert flatten_now.flatten(bot) is True
    assert len(bot.broker.get_positions()) == 2 and bot.broker.close_calls == []

"""2026-10-02 manual-override safety: closing/cancelling a position by hand in the broker UI (thinkorswim)
while the bot runs. The bot reconciles its tracked spreads with what the broker holds (confirmed over
several syncs), never sends a close for legs that are not held, freezes a spread with a naked leg instead
of touching it, manages only its own contracts when the owner holds extra on the same strikes, and no
longer cancels every order in the account at the end of the day."""

from datetime import date, timedelta

import pytest

import config
import scheduler
from broker import BrokerError
from conftest import et
from fake_broker import FakeBroker, occ_symbol
from journal import Journal
from position_manager import OpenSpread, PositionManager
from risk_manager import RiskManager
from strategy import BULLISH
from strategy import exit_levels as strategy_exit_levels
from test_scheduler import SPOT, TODAY, put_chain

ENTRY = et(10, 0)


class BrokenReads(FakeBroker):
    reads_fail = False

    def get_positions(self):
        if self.reads_fail:
            raise BrokerError("simulated positions outage")
        return super().get_positions()


@pytest.fixture
def broker() -> BrokenReads:
    return BrokenReads(equity=10_000, today=TODAY)


@pytest.fixture
def pm(broker, tmp_path) -> PositionManager:
    return PositionManager(broker, Journal(tmp_path, "TEST"))


def open_spread(pm, broker, strategy="B", qty=2, credit=2.00, right="P", entry_time=ENTRY) -> OpenSpread:
    short, long = (7450.0, 7445.0) if right == "P" else (7550.0, 7555.0)
    broker.add_spread_position(right, short, long, qty, credit + 5.0, 5.0)
    target, stop = strategy_exit_levels(strategy)
    spread = OpenSpread(strategy=strategy, direction=BULLISH, setup="ORB", right=right, root="SPXW",
                        expiration=TODAY, short_strike=short, long_strike=long, width=5.0, qty=qty,
                        entry_credit=credit, entry_time=entry_time, current_price=credit, best_price=credit,
                        profit_target=target, stop_loss=stop)
    pm.open(spread)
    return spread


def leg(broker, strike, right="P"):
    return broker.positions[occ_symbol("SPXW", TODAY, right, strike)]


# ------------------------------------------------------------------ closed by hand (S1)

def test_hand_closed_spread_is_booked_after_two_confirming_syncs(pm, broker):
    p = open_spread(pm, broker, qty=2, credit=2.00)
    p.current_price = 1.60                                  # the last mark the bot saw
    broker.positions.clear()                                # the owner closed it in thinkorswim
    assert pm.sync_external(et(10, 5, 0)) == []             # first sighting: nothing yet
    assert "B" in pm.positions
    rows = pm.sync_external(et(10, 5, 15))                  # confirmed
    assert len(rows) == 1 and rows[0]["exit_reason"] == "EXTERNAL_CLOSE" and rows[0]["external"] is True
    assert rows[0]["pnl"] == pytest.approx((2.00 - 1.60) * 100 * 2) and rows[0]["position_closed"] is True
    assert pm.positions == {} and broker.close_calls == []   # and no order was ever sent


def test_one_odd_sync_does_nothing_and_a_clean_sync_resets_the_count(pm, broker):
    p = open_spread(pm, broker)
    saved = dict(broker.positions)
    broker.positions.clear()
    assert pm.sync_external(et(10, 5, 0)) == [] and p.sync_obs_count == 1
    broker.positions.update(saved)                           # the positions API was merely lagging
    assert pm.sync_external(et(10, 5, 15)) == [] and p.sync_obs_count == 0
    broker.positions.clear()
    assert pm.sync_external(et(10, 5, 30)) == [] and "B" in pm.positions     # count restarted at 1


def test_a_position_younger_than_the_minimum_age_is_never_touched(pm, broker):
    open_spread(pm, broker, entry_time=et(10, 0, 0))
    broker.positions.clear()
    for s in (5, 15, 25):                                    # inside EXTERNAL_MIN_AGE_SEC (30s)
        assert pm.sync_external(et(10, 0, s)) == []
    assert "B" in pm.positions


def test_a_failed_positions_read_changes_nothing(pm, broker):
    open_spread(pm, broker)
    broker.positions.clear()
    broker.reads_fail = True
    for s in (0, 15, 30, 45):
        assert pm.sync_external(et(10, 5, s)) == []
    assert "B" in pm.positions


def test_a_dry_run_broker_is_never_reconciled(pm, broker):
    open_spread(pm, broker)
    broker.positions.clear()
    broker.dry_run = True
    assert pm.sync_external(et(10, 5, 0)) == [] and pm.sync_external(et(10, 5, 15)) == []
    assert "B" in pm.positions


def test_sync_is_throttled(pm, broker):
    open_spread(pm, broker)
    broker.positions.clear()
    pm.sync_external(et(10, 5, 0))
    assert pm.sync_external(et(10, 5, 5)) == []              # inside EXTERNAL_SYNC_SEC: not even a second look
    assert pm.positions["B"].sync_obs_count == 1


# ------------------------------------------------------------------ partial hand-close (S4)

def test_partial_hand_close_books_the_difference_and_keeps_managing_the_rest(pm, broker):
    p = open_spread(pm, broker, qty=3, credit=2.00)
    p.current_price = 1.80
    leg(broker, 7450.0)["qty"], leg(broker, 7445.0)["qty"] = -1, 1      # the owner closed 2 of 3
    assert pm.sync_external(et(10, 5, 0)) == []
    rows = pm.sync_external(et(10, 5, 15))
    assert [(r["exit_reason"], r["qty"], r["position_closed"]) for r in rows] == [("EXTERNAL_PARTIAL", 2, False)]
    assert pm.positions["B"].remaining == 1 and pm.positions["B"].realized_pnl == pytest.approx(40.0)


# ------------------------------------------------------------------ one leg closed (S2)

def test_a_naked_leg_sends_no_close_alerts_and_clears_when_repaired(pm, broker):
    p = open_spread(pm, broker)
    del broker.positions[occ_symbol("SPXW", TODAY, "P", 7445.0)]         # only the long leg was closed by hand
    pm.sync_external(et(10, 5, 0))
    assert pm.sync_external(et(10, 5, 15)) == [] and p.manual_attention is True
    assert pm.force_close(et(15, 30), {"B": 3.0}) == [] and broker.close_calls == []
    assert pm.on_tick(et(10, 6), {"B": 9.0}, None) == [] and broker.close_calls == []   # a stop-level price
    broker.add_spread_position("P", 7450.0, 7445.0, 2, 7.0, 5.0)         # the owner repaired it
    assert pm.sync_external(et(10, 6, 30)) == [] and p.manual_attention is False


def test_a_close_is_never_sent_for_a_spread_with_a_naked_leg_even_before_the_next_sync(pm, broker):
    p = open_spread(pm, broker)
    del broker.positions[occ_symbol("SPXW", TODAY, "P", 7450.0)]         # only the short leg is gone
    assert pm.force_close(et(15, 30), {"B": 3.0}) == []
    assert p.manual_attention is True and broker.close_calls == []


# -------------------------------------------------------------- owner holds extra contracts (S5)

def test_the_owners_extra_contracts_on_the_same_strikes_are_not_closed_by_the_bot(pm, broker):
    open_spread(pm, broker, qty=2)
    leg(broker, 7450.0)["qty"], leg(broker, 7445.0)["qty"] = -5, 5      # the owner added 3 by hand
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert len(rows) == 1 and rows[0]["exit_reason"] == "FORCE_CLOSE" and rows[0]["qty"] == 2
    assert broker.close_calls == [{"right": "P", "short": 7450.0, "long": 7445.0, "qty": 2}]   # one try, own qty
    assert leg(broker, 7450.0)["qty"] == -3 and pm.positions == {}


def test_extra_owner_contracts_do_not_trigger_the_sync(pm, broker):
    p = open_spread(pm, broker, qty=2)
    leg(broker, 7450.0)["qty"], leg(broker, 7445.0)["qty"] = -5, 5
    assert pm.sync_external(et(10, 5, 0)) == [] and pm.sync_external(et(10, 5, 15)) == []
    assert "B" in pm.positions and p.manual_attention is False


# --------------------------------------------------- the bot's own exit meets a hand-closed spread (S7)

def test_an_exit_after_a_hand_close_books_it_and_sends_no_order(pm, broker):
    open_spread(pm, broker, credit=2.00).current_price = 1.50
    broker.positions.clear()
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [r["exit_reason"] for r in rows] == ["EXTERNAL_CLOSE"] and pm.positions == {}
    assert broker.close_calls == []


def test_an_exit_after_a_partial_hand_close_books_the_gap_then_closes_only_what_is_left(pm, broker):
    p = open_spread(pm, broker, qty=3, credit=2.00)
    p.current_price = 1.50
    leg(broker, 7450.0)["qty"], leg(broker, 7445.0)["qty"] = -1, 1
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [(r["exit_reason"], r["qty"]) for r in rows] == [("EXTERNAL_PARTIAL", 2), ("FORCE_CLOSE", 1)]
    assert broker.close_calls == [{"right": "P", "short": 7450.0, "long": 7445.0, "qty": 1}]
    assert pm.positions == {}


def test_a_brand_new_position_whose_legs_are_not_visible_yet_still_closes_as_before(pm, broker):
    open_spread(pm, broker, entry_time=et(15, 29, 50))
    broker.positions.clear()                                 # positions API lag, 10 seconds after the fill
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [r["exit_reason"] for r in rows] == ["FORCE_CLOSE"] and len(broker.close_calls) == 1


def test_a_failed_read_before_a_close_does_not_block_the_close(pm, broker):
    """If the positions API is down the bot cannot verify anything, so the preflight steps aside and the
    close is sent exactly as before -- an outage must never leave a stop unexecuted. 2026-10-07: the
    post-close verification read then fails too; a close the broker reports as filled is trusted and booked
    (it used to raise out of the whole force-close pass and drop the rows of the positions already closed)."""
    open_spread(pm, broker)
    broker.reads_fail = True
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert len(broker.close_calls) == 1 and [r["exit_reason"] for r in rows] == ["FORCE_CLOSE"] and pm.positions == {}


# ------------------------------------------------------------------ scheduler wiring

@pytest.fixture
def bot(tmp_path, monkeypatch) -> scheduler.Bot:
    fake = BrokenReads(equity=10_000, today=TODAY)
    fake.spot = SPOT
    fake.set_chain("P", put_chain())
    fake.set_chain("C", {7600.0: (21.0, 23.0)})
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: fake)
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    b = scheduler.Bot(dry_run=False, once=True)
    b.today = TODAY
    b.risk = RiskManager(10_000)
    return b


def test_manage_books_a_hand_closed_position_and_the_risk_manager_hears_about_it(bot):
    import pandas as pd
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert {"A", "B"} <= set(bot.pm.positions)
    for p in bot.pm.positions.values():
        p.entry_time = et(10, 15)
    bot.broker.positions.clear()                             # the owner closed everything in thinkorswim
    empty = pd.DataFrame()
    bot.manage(et(10, 20, 0), empty)
    bot.manage(et(10, 20, 15), empty)
    assert bot.pm.positions == {}
    assert bot.risk.state.trades_today == 2                  # both closes counted for the daily limits
    assert bot.broker.close_calls == []


def test_bot_legs_ignore_an_spxw_spread_that_does_not_expire_today(bot):
    bot.broker.add_spread_position("P", 7450.0, 7445.0, 1, 7.0, 5.0, expiration=TODAY + timedelta(days=7))
    bot.broker.add_spread_position("P", 7400.0, 7395.0, 1, 7.0, 5.0)                     # expires today
    positions = bot.broker.get_positions()
    assert {leg["strike"] for leg in bot.bot_legs(positions)} == {7400.0, 7395.0}
    assert {leg["strike"] for leg in bot.foreign_legs(positions)} == {7450.0, 7445.0}


def test_end_of_day_cancels_only_the_bots_own_orders(bot):
    own = occ_symbol("SPXW", TODAY, "P", 7450.0)
    other = occ_symbol("AAPL", TODAY + timedelta(days=50), "P", 200.0)          # e.g. the 45DTE bot's resting GTC
    for oid, sym in (("own", own), ("other", other), ("blind", None)):
        bot.broker.orders[oid] = {"id": oid, "status": "accepted", "symbol": "x", "order_class": "mleg",
                                  "legs": [{"symbol": sym, "side": "buy"}] if sym else []}
    assert bot.cancel_own_orders() == 1
    assert bot.broker.orders["own"]["status"] == "canceled"
    assert bot.broker.orders["other"]["status"] == "accepted" and bot.broker.orders["blind"]["status"] == "accepted"


# ------------------------------------------------------------------ the MANUAL_HOLD file

@pytest.fixture
def hold_dir(tmp_path, monkeypatch):
    import position_manager
    monkeypatch.setattr(position_manager, "HERE", tmp_path)
    folder = tmp_path / config.LOG_DIR
    folder.mkdir()
    return folder


def test_manual_hold_file_stands_every_close_down_until_it_is_deleted(pm, broker, hold_dir):
    open_spread(pm, broker)
    hold = hold_dir / config.MANUAL_HOLD_FILE
    hold.write_text("")
    assert pm.force_close(et(15, 30), {"B": 1.50}) == [] and broker.close_calls == []
    assert pm.on_tick(et(10, 6), {"B": 9.0}, None) == [] and broker.close_calls == []      # a stop-level price
    assert "B" in pm.positions
    hold.unlink()
    rows = pm.force_close(et(15, 31), {"B": 1.50})
    assert [r["exit_reason"] for r in rows] == ["FORCE_CLOSE"]


def test_manual_hold_can_target_one_persona(pm, broker, hold_dir):
    open_spread(pm, broker)
    (hold_dir / f"{config.MANUAL_HOLD_FILE}_SOMEONE_ELSE").write_text("")
    assert [r["exit_reason"] for r in pm.force_close(et(15, 30), {"B": 1.50})] == ["FORCE_CLOSE"]


def test_a_hand_close_is_still_booked_during_a_manual_hold(pm, broker, hold_dir):
    open_spread(pm, broker)
    (hold_dir / config.MANUAL_HOLD_FILE).write_text("")
    broker.positions.clear()
    pm.sync_external(et(10, 5, 0))
    rows = pm.sync_external(et(10, 5, 15))
    assert [r["exit_reason"] for r in rows] == ["EXTERNAL_CLOSE"] and pm.positions == {}


# ------------------------------------------------------ cool-down, then ready to open again (owner's request)

def _external_row(strategy="B", exit_time=None, closed=True):
    return {"strategy": strategy, "setup": "ORB", "exit_time": exit_time or et(10, 20, 15), "external": True,
            "position_closed": closed, "pnl": 40.0}


def test_a_hand_close_starts_a_two_minute_entry_cooldown_for_that_strategy():
    rm = RiskManager(10_000)
    rm.record_trade("B", 40.0, _external_row())
    ok, reason = rm.trading_allowed("B", now=et(10, 21, 0))
    assert ok is False and reason == "B: MANUAL_CLOSE cooldown until 10:22:15"
    assert rm.trading_allowed("B", now=et(10, 22, 14))[0] is False
    assert rm.trading_allowed("B", now=et(10, 22, 15)) == (True, "OK")          # ready again
    assert rm.trading_allowed("A", now=et(10, 21, 0)) == (True, "OK")            # other strategies untouched


def test_the_bots_own_exits_do_not_start_the_manual_close_cooldown():
    rm = RiskManager(10_000)
    rm.record_trade("B", 40.0, {"strategy": "B", "setup": "ORB", "exit_time": et(10, 20, 15), "pnl": 40.0})
    assert rm.trading_allowed("B", now=et(10, 20, 20)) == (True, "OK")


def test_the_cooldown_survives_a_state_save_and_reload():
    from risk_manager import DayState
    rm = RiskManager(10_000)
    rm.record_trade("B", 40.0, _external_row())
    restored = DayState.from_dict(rm.state.to_dict())
    assert RiskManager(10_000, restored).trading_allowed("B", now=et(10, 21, 0))[0] is False


def test_after_a_hand_close_the_bot_waits_two_minutes_then_opens_again(bot):
    import pandas as pd
    bot.enter(et(10, 15), "ORB", BULLISH)
    for p in bot.pm.positions.values():
        p.entry_time = et(10, 15)
    bot.broker.positions.clear()                              # closed by hand in thinkorswim
    bot.manage(et(10, 20, 0), pd.DataFrame())
    bot.manage(et(10, 20, 15), pd.DataFrame())               # recognized
    assert bot.pm.positions == {}
    bot.enter(et(10, 21, 0), "ORB", BULLISH)                  # 45s later: still cooling down
    assert bot.pm.positions == {}
    bot.enter(et(10, 22, 30), "ORB", BULLISH)                 # past 2 minutes: ready again
    assert "B" in bot.pm.positions
    # the continuation re-entry pacing was re-timed so its next check lands right when the cool-down ends
    assert bot.last_continuation_check["ORB"] == et(10, 20, 15) + timedelta(seconds=120) - timedelta(
        minutes=config.REENTRY_PAUSE_MIN)


# ------------------------------------------------- adversarial-review fixes (2026-10-02)

class ScriptedReads(BrokenReads):
    """get_positions() answers from `script` first (each item a list of rows), then truthfully."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.script: list[list[dict]] = []

    def get_positions(self):
        if self.script:
            return self.script.pop(0)
        return super().get_positions()


class HandClosesWhileTheBotsCloseIsWorking(BrokenReads):
    """The bot's first close attempt fails (its order was cancelled) because the owner closed the spread by hand."""
    def close_spread_at_market(self, *a, **k):
        self.close_calls.append({"attempt": len(self.close_calls) + 1})
        if len(self.close_calls) == 1:
            self.positions.clear()
            raise BrokerError("order cancelled")
        raise AssertionError("a second close must never be sent for legs that are gone")


def test_one_empty_read_at_close_time_does_not_drop_a_live_position(tmp_path):
    """A stale/empty positions response at the moment a stop fires must not make the bot forget the position."""
    broker = ScriptedReads(equity=10_000, today=TODAY)
    pm = PositionManager(broker, Journal(tmp_path, "TEST"))
    open_spread(pm, broker)
    broker.script = [[]]                                    # the FIRST read is empty, every later read is the truth
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [r["exit_reason"] for r in rows] == ["FORCE_CLOSE"] and len(broker.close_calls) == 1


def test_two_empty_reads_do_book_the_hand_close(tmp_path):
    broker = ScriptedReads(equity=10_000, today=TODAY)
    pm = PositionManager(broker, Journal(tmp_path, "TEST"))
    open_spread(pm, broker)
    broker.script = [[], []]
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [r["exit_reason"] for r in rows] == ["EXTERNAL_CLOSE"] and broker.close_calls == []


def test_the_retry_loop_re_checks_the_broker_before_every_attempt(tmp_path):
    broker = HandClosesWhileTheBotsCloseIsWorking(equity=10_000, today=TODAY)
    pm = PositionManager(broker, Journal(tmp_path, "TEST"))
    open_spread(pm, broker, credit=2.00).current_price = 1.60
    rows = pm.force_close(et(15, 30), {"B": 1.60})
    assert [r["exit_reason"] for r in rows] == ["EXTERNAL_CLOSE"] and len(broker.close_calls) == 1
    assert pm.positions == {}


def test_an_unbalanced_spread_closes_only_its_matched_pairs(pm, broker):
    p = open_spread(pm, broker, qty=3, credit=2.00)
    p.current_price = 1.50
    leg(broker, 7445.0)["qty"] = 2                          # one long contract was closed by hand: short 3 / long 2
    rows = pm.force_close(et(15, 30), {"B": 1.50})
    assert [(r["exit_reason"], r["qty"]) for r in rows] == [("EXTERNAL_PARTIAL", 1), ("FORCE_CLOSE", 2)]
    assert broker.close_calls == [{"right": "P", "short": 7450.0, "long": 7445.0, "qty": 2}]
    assert p.manual_attention is True                       # and the odd leg was flagged loudly


def test_a_spread_with_a_naked_leg_still_gets_fresh_marks(pm, broker):
    p = open_spread(pm, broker, credit=2.00)
    del broker.positions[occ_symbol("SPXW", TODAY, "P", 7445.0)]
    pm.on_tick(et(10, 6), {"B": 2.20}, None)
    assert p.current_price == 2.20 and broker.close_calls == []


def test_the_manual_close_cooldown_does_not_apply_to_stock_strategy_f():
    rm = RiskManager(10_000)
    rm.record_trade("F", 40.0, _external_row(strategy="F"))
    assert rm.trading_allowed("F", now=et(10, 21, 0)) == (True, "OK")      # F has per-ticker re-entry logic


def test_re_adoption_never_absorbs_the_owners_extra_contracts_on_the_bots_strikes(pm, broker):
    saved = OpenSpread(strategy="B", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                       short_strike=7450.0, long_strike=7445.0, width=5.0, qty=2, entry_credit=2.0, entry_time=ENTRY,
                       current_price=2.0, best_price=2.0, profit_target=0.3, stop_loss=0.5)
    broker.add_spread_position("P", 7450.0, 7445.0, 3, 7.0, 5.0)         # the owner added 1 by hand: 3/3 at the broker
    adopted = pm.adopt_from_broker(broker.get_positions(), et(10, 15), 7500.0, {"B": saved.to_dict()})
    assert [a.remaining for a in adopted] == [2]


def test_the_staged_exits_band_touch_is_its_own_constant_not_the_d_entry_threshold():
    import inspect
    src = inspect.getsource(PositionManager._staged_tick)
    assert "EXIT_BB_TOUCH_PCT_B" in src and "D_BAND_TOUCH_PCT_B" not in src
    assert config.EXIT_BB_TOUCH_PCT_B == 0.95


# ---- scheduler level

def test_bot_legs_on_a_shared_account_only_adopt_spxw_the_bot_opened(bot, monkeypatch):
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_SPXW", False)
    bot.broker.add_spread_position("P", 7400.0, 7395.0, 1, 7.0, 5.0)     # the owner's own same-day spread
    bot.broker.add_spread_position("C", 7650.0, 7655.0, 1, 7.0, 5.0)     # a contract today's saved state says is ours
    bot._saved_spxw = {("C", 7650.0, TODAY), ("C", 7655.0, TODAY)}
    positions = bot.broker.get_positions()
    assert {l["strike"] for l in bot.bot_legs(positions)} == {7650.0, 7655.0}
    assert {l["strike"] for l in bot.foreign_legs(positions)} == {7400.0, 7395.0}
    bot.enter(et(10, 15), "ORB", BULLISH)                                # a position the bot opens now is tracked too
    assert any(l["strike"] == 7610.0 for l in bot.bot_legs(bot.broker.get_positions()))
    monkeypatch.setattr(config, "ADOPT_UNTRACKED_SPXW", True)
    assert {7400.0, 7395.0} <= {l["strike"] for l in bot.bot_legs(bot.broker.get_positions())}


def test_end_of_day_leaves_an_spxw_order_on_another_expiration_alone(bot):
    today_leg = occ_symbol("SPXW", TODAY, "P", 7450.0)
    weekly = occ_symbol("SPXW", TODAY + timedelta(days=7), "P", 7300.0)   # the owner's own resting GTC
    for oid, sym in (("today", today_leg), ("weekly", weekly)):
        bot.broker.orders[oid] = {"id": oid, "status": "accepted", "symbol": "x", "order_class": "mleg",
                                  "legs": [{"symbol": sym, "side": "buy"}]}
    assert bot.cancel_own_orders() == 1
    assert bot.broker.orders["today"]["status"] == "canceled" and bot.broker.orders["weekly"]["status"] == "accepted"


def test_record_fills_survives_a_partial_hand_close_row_followed_by_the_bots_own_full_close(bot):
    bot.enter(et(10, 15), "ORB", BULLISH)
    b = bot.pm.positions["B"]
    b.qty = 3
    leg(bot.broker, b.short_strike)["qty"], leg(bot.broker, b.long_strike)["qty"] = -2, 2   # the owner closed 1 of 3
    for p in bot.pm.positions.values():
        p.entry_time = et(10, 15)
    rows = bot.pm.force_close(et(15, 30), {"A": 3.0, "B": 0.9})
    assert [r["exit_reason"] for r in rows if r["strategy"] == "B"] == ["EXTERNAL_PARTIAL", "FORCE_CLOSE"]
    bot.record_fills(rows)                                    # used to raise KeyError on the partial row
    assert bot.pm.positions == {} and bot.risk.state.for_strategy("B").trades_today == 1


def test_a_position_closed_by_hand_while_the_bot_was_down_is_booked_on_restart(bot):
    spread = open_spread(bot.pm, bot.broker, strategy="B", credit=2.00)
    spread.current_price = 1.50
    saved = {"positions": {"B": spread.to_dict()}}
    bot.pm.positions.clear()
    bot.broker.positions.clear()                              # closed in thinkorswim while the bot was off
    bot._reconcile_saved_positions(saved, [], et(10, 20, 0))
    assert bot.pm.positions == {} and bot.risk.state.for_strategy("B").trades_today == 1
    assert bot.risk.trading_allowed("B", now=et(10, 20, 30))[0] is False      # the cool-down applies here too


def test_a_saved_position_that_is_still_at_the_broker_is_not_booked_on_restart(bot):
    spread = open_spread(bot.pm, bot.broker, strategy="B", credit=2.00)
    saved = {"positions": {"B": spread.to_dict()}}
    bot.pm.positions.clear()
    bot._reconcile_saved_positions(saved, bot.broker.get_positions(), et(10, 20, 0))
    assert bot.risk.state.for_strategy("B").trades_today == 0


def test_manual_hold_blocks_new_entries_and_keeps_the_bot_up_at_the_forced_close(bot, hold_dir, monkeypatch):
    monkeypatch.setattr(config, "MANUAL_HOLD_DELAYS_FORCE_CLOSE", True)      # the older, opt-in behaviour
    bot.enter(et(10, 15), "ORB", BULLISH)
    held = set(bot.pm.positions)
    hold = hold_dir / config.MANUAL_HOLD_FILE
    hold.write_text("")
    bot.pm.positions.pop("B")                                 # (a slot is free, but entries are held)
    assert bot.entry_possible(10_000.0) is False
    bot.pm.positions["B"] = open_spread(bot.pm, bot.broker, strategy="B", credit=2.00, right="C")
    assert bot.end_of_day(et(15, 30)) is False and bot.pm.positions     # nothing closed, bot not finished
    hold.unlink()
    assert bot.end_of_day(et(15, 31)) is True and bot.pm.positions == {}
    assert held == {"A", "B"}


# ------------------------------------------------- 1:00 pm ET hard stop (2026-10-05)

def test_the_hard_stop_is_one_pm_eastern_and_has_no_wind_down_phase():
    import strategy
    from datetime import time as dtime
    assert config.LAST_ENTRY_TIME == dtime(13, 0) and config.FORCE_CLOSE_TIME == dtime(13, 0)
    assert config.MANUAL_HOLD_DELAYS_FORCE_CLOSE is False
    assert strategy.phase(et(12, 59, 59)) == strategy.Phase.ORB_ONLY
    assert strategy.phase(et(13, 0, 0)) == strategy.Phase.CLOSED
    assert strategy.Phase.NO_NEW_ENTRIES not in {strategy.phase(et(h, m)) for h in range(9, 17) for m in range(0, 60)}


def test_the_hard_stop_closes_everything_even_with_a_manual_hold_file(bot, hold_dir):
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert bot.pm.positions
    (hold_dir / config.MANUAL_HOLD_FILE).write_text("")
    assert bot.end_of_day(et(13, 0)) is True                     # done: closed anyway, the hold does not delay it
    assert bot.pm.positions == {}
    assert any(c for c in bot.broker.close_calls)


def test_the_tick_at_one_pm_stops_the_bot_for_the_day(bot, hold_dir):
    bot.enter(et(10, 15), "ORB", BULLISH)
    (hold_dir / config.MANUAL_HOLD_FILE).write_text("")
    bot.tick(et(13, 0, 5))
    assert bot.done is True and bot.pm.positions == {}


def test_no_new_entry_is_taken_at_one_pm(bot):
    bot.tick(et(13, 0, 5))
    assert bot.pm.positions == {} and bot.done is True

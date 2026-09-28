"""Strategy F engine: candle assembly, ranking, confirmation, the pending queue, re-entry and state --
with stubbed data functions and a synchronous executor (no network, no threads)."""

import json
from datetime import timedelta

import pandas as pd
import pytest

import config
import stock_orb
from conftest import et
from f_fixtures import (BREAK, HOLD, INSIDE, MOMENTUM, OR_HIGH, OR_LOW, RANK_AT, STOCKS, TODAY, UNIVERSE, DataStub,
                        bars, candidate, daily_frame, intraday_frame, make_bot_f, rank, scan_result, today_closes)
from stock_orb import StockOrbTrader, StockSignal, completed_candles, confirm_break, normal_volume
from strategy import BEARISH, BULLISH, OrbSetup


@pytest.fixture
def data() -> DataStub:
    return DataStub()


@pytest.fixture
def bot(tmp_path, monkeypatch, data):
    return make_bot_f(tmp_path, monkeypatch, data)


def events(bot, kind: str) -> list[dict]:
    rows = [json.loads(line) for path in sorted(bot.journal.dir.glob(f"events_{bot.journal.slug}_*.jsonl"))
            for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def close_f(bot, key: str, when, price: float) -> list[dict]:
    """Drive one F position out through the stop and book it like the scheduler does."""
    bot.broker.spread_close_price = price
    fills = bot.pm.on_tick(when, {key: price}, None, None)
    bot.record_fills(fills)
    return fills


# ------------------------------------------------------------------------- candle assembly

def test_completed_candles_needs_the_last_one_minute_bar():
    closes = [430.0 + 0.1 * i for i in range(24)]                 # 09:30 .. 09:53
    frame = bars(TODAY, closes)
    candles, waiting = completed_candles(frame, et(9, 55, 10), 5)
    assert list(candles.index.strftime("%H:%M")) == ["09:30", "09:35", "09:40", "09:45"]
    assert waiting == [et(9, 55)]                                  # 09:50 candle: 4 bars, 09:54 missing
    assert candles.loc[et(9, 45), "close"] == pytest.approx(closes[19])
    assert candles.loc[et(9, 45), "volume"] == 500.0
    # F_CANDLE_MAX_WAIT_SEC bounds only the logging: 91 s after the close it is STILL withheld
    _, waiting = completed_candles(frame, et(9, 56, 31), 5)
    assert waiting == [et(9, 55)]
    # the last bar arriving completes it at once
    full = bars(TODAY, closes + [432.4])
    candles, waiting = completed_candles(full, et(9, 55, 10), 5)
    assert waiting == [] and candles.index[-1] == et(9, 50) and candles.loc[et(9, 50), "close"] == 432.4
    # a candle whose window has not ended yet is neither complete nor waiting
    candles, waiting = completed_candles(full, et(9, 53, 30), 5)
    assert candles.index[-1] == et(9, 45) and waiting == []
    # a missing MIDDLE bar (Yahoo drops zero-volume minutes) does not hold a candle whose last bar is present
    gapped = full.drop(et(9, 52))
    candles, waiting = completed_candles(gapped, et(9, 55, 10), 5)
    assert candles.index[-1] == et(9, 50) and waiting == [] and candles.loc[et(9, 50), "volume"] == 400.0
    empty = pd.DataFrame(columns=stock_orb.COLUMNS)
    assert completed_candles(empty, et(9, 55), 5)[0].empty and completed_candles(None, et(9, 55), 5)[0].empty
    assert completed_candles(pd.DataFrame({"close": [1.0]}), et(9, 55), 5)[0].empty


def test_normal_volume_uses_same_window_over_prior_sessions():
    frame = intraday_frame(today_closes(BREAK, MOMENTUM), today_volume=150.0, prior_volume=100.0, prior_sessions=5)
    assert normal_volume(frame, TODAY, et(9, 50), 5) == 500.0     # 5 bars x 100 per prior session, not today's 150s
    assert normal_volume(frame, TODAY, et(11, 0), 5) is None      # prior sessions end at 10:29 in the fixture
    assert normal_volume(bars(TODAY, [430.0] * 20), TODAY, et(9, 45), 5) is None
    assert normal_volume(None, TODAY, et(9, 45), 5) is None


def test_confirm_break_reasons():
    m = candidate("TSLA")            # Candidate shares the Metrics fields confirm_break reads
    candle = {"volume": 750.0}
    assert confirm_break(BULLISH, candle, m, 500.0) == []
    assert confirm_break(BULLISH, candle, m, 800.0) == ["LOW_VOLUME"]
    assert confirm_break(BULLISH, candle, m, None) == []                     # no baseline: volume not judged
    assert confirm_break(BULLISH, None, m, 800.0) == []                      # re-check: no candle, no volume rule
    assert confirm_break(BEARISH, candle, m, 500.0) == ["WRONG_SIDE_OF_VWAP", "EMA_NOT_ALIGNED"]
    exhausted = candidate("TSLA")
    exhausted.remaining_em = 6.9                                             # < 35% of the 20-point EM
    assert confirm_break(BULLISH, candle, exhausted, 500.0) == ["EM_EXHAUSTED"]
    assert confirm_break(BULLISH, candle, None, 500.0) == ["NO_METRICS"]


# --------------------------------------------------------------------------------- ranking

def test_rank_keeps_top5_over_60_excludes_index_broken_and_partial(bot, data):
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD), STOCKS + ["SPX"])
    cands = [candidate("SPX", "IDX", 99.0), candidate("TSLA", "A", 90.0), candidate("AMZN", "A", 85.0, already_broken=True),
             candidate("META", "B", 70.0, direction=BEARISH), candidate("PFE", "C", 59.9),
             candidate("HOOD", "A", 65.0), candidate("BE", "A", 61.0), candidate("INTC", "A", 62.0),
             candidate("AMD", "A", 63.0), candidate("BA", "A", 64.0)]
    extra = ["HOOD", "BE", "INTC", "AMD", "BA"]
    bot.f.universe = {"index": "SPX", "tiers": {"A": ["TSLA", "AMZN", *extra], "B": ["META"], "C": ["PFE"]}}
    bot.f.names = [s for s, _ in stock_orb.orb_scanner.universe_members(bot.f.universe)]
    for s in extra:
        data.frames[s] = data.frames["TSLA"]
    # HOOD has 20+ bars since 09:30 but a hole INSIDE the opening range (09:44 never served): its
    # or15 would be seeded from 14 bars, so it is a partial range however many later bars exist
    data.frames["HOOD"] = data.frames["TSLA"].drop(et(9, 44))
    bot.f.step(et(9, 45, 30))
    assert data.calls["scan"] == [] and bot.f.ranked_at is None            # not before 09:45:45
    rank(bot, data, cands)
    assert [w.symbol for w in bot.f.watch] == ["TSLA", "AMZN", "META", "BA", "AMD"]   # scanner order, top 5
    assert [w.rank for w in bot.f.watch] == [1, 2, 3, 4, 5]
    tsla = bot.f.watch[0]
    assert (tsla.setup.or_high, tsla.setup.or_low, tsla.setup.state) == (OR_HIGH, OR_LOW, OrbSetup.WAITING)
    assert tsla.direction == BULLISH and bot.f.watch[2].direction == BEARISH
    assert bot.f.next_check == et(9, 50)
    ev = events(bot, "F_RANKING")
    assert len(ev) == 1 and [w["symbol"] for w in ev[0]["watch"]] == ["TSLA", "AMZN", "META", "BA", "AMD"]
    assert ev[0]["skipped"]["HOOD"] == ["partial OR15: 14 of 15 opening-range bars"]
    # a 1-min close outside the range at 09:45:45 is the first 5-min candle's business, not a reason
    # to drop the name (the on-time scan can never see a completed post-OR candle)
    assert "AMZN" not in ev[0]["skipped"]
    assert ev[0]["ranking"][0]["ticker"] == "TSLA" and all(r["ticker"] != "SPX" for r in ev[0]["ranking"])
    scan_call = data.calls["scan"][0]
    assert scan_call["intraday"] is not None and scan_call["daily"] is not None and scan_call["chain_fn"] is not None
    assert json.loads(bot.journal.state_path.read_text())["f"]["watch"][0]["symbol"] == "TSLA"


def test_already_outside_the_range_only_excludes_on_a_late_start(bot, data, monkeypatch):
    # Lag retry at 09:46:45: TSLA's 09:45 1-min close is above its OR high, so the scanner flags it
    # already_broken -- but that close belongs to the 09:45 candle the OrbSetup is about to judge, and
    # excluding it would drop exactly the strongest breakout. Only a scan past 09:50 (a late start,
    # when a whole candle may already have broken without us) applies the exclusion.
    data.set_frames(today_closes(BREAK))
    rank(bot, data, [candidate("TSLA", score=85.0, already_broken=True), candidate("AMZN")], now=et(9, 46, 45))
    assert [w.symbol for w in bot.f.watch] == ["TSLA", "AMZN"] and events(bot, "F_RANKING")[-1]["skipped"] == {}
    bot2 = make_bot_f(bot.journal.dir, monkeypatch, data)
    rank(bot2, data, [candidate("TSLA", score=85.0, already_broken=True), candidate("AMZN")], now=et(9, 52))
    assert [w.symbol for w in bot2.f.watch] == ["AMZN"]
    assert events(bot2, "F_RANKING")[-1]["skipped"]["TSLA"] == ["already outside OR15 at rank time (late start)"]


def test_a_withheld_candle_is_never_stepped_over(bot, data, caplog):
    # 09:45 breaks, 09:50 closes back INSIDE (a reset) but its last bar (09:54) is missing, 09:55 closes
    # above the break close. The feed stops in front of the withheld candle: 09:55 must not be turned
    # into a MOMENTUM fire on a break that had already failed.
    caplog.set_level("INFO")
    closes = today_closes(BREAK, INSIDE, MOMENTUM)
    gapped = intraday_frame(closes).drop(et(9, 54))
    for s in STOCKS:
        data.frames[s] = gapped
    rank(bot, data)
    bot.f.step(et(9, 55, 30))                                               # 09:45 fed; 09:50 waiting for 30 s ...
    w = bot.f.watch[0]
    assert (w.setup.state, w.last_candle, bot.f.next_check) == (OrbSetup.BROKEN, et(9, 45), et(9, 55, 50))   # ... polled
    bot.f.step(et(10, 0, 10))                                               # 09:55 complete, 09:50 still withheld
    assert (w.setup.state, w.last_candle) == (OrbSetup.BROKEN, et(9, 45))
    assert events(bot, "SIGNAL") == [] and "F:TSLA" not in bot.pm.positions
    assert list(bot.f.candles5["TSLA"].index.strftime("%H:%M"))[-2:] == ["09:45", "09:55"]
    # the hole is older than F_CANDLE_MAX_WAIT_SEC: logged once, no more 20-s polling, next look at 10:05
    assert bot.f.next_check == et(10, 5)
    assert sum("still missing its last 1-min bar" in r.getMessage() for r in caplog.records) == 1
    bot.f.step(et(10, 2))
    assert len(data.calls["bars"]) == 3                                     # scan + two monitor passes: not polled
    for s in STOCKS:                                                        # the bar arrives: fed in order
        data.frames[s] = intraday_frame(closes)
    bot.f.step(et(10, 5, 10))
    assert (w.setup.state, w.last_candle) == (OrbSetup.BROKEN, et(9, 55))   # 09:50 reset it, 09:55 re-armed it
    assert events(bot, "SIGNAL") == []


def test_lagging_bars_are_retried_until_they_catch_up_without_scoring(bot, data):
    data.set_frames(today_closes()[:12])                                    # only 12 completed minutes at 09:45:45
    data.result = scan_result([candidate("TSLA")])
    bot.f.step(RANK_AT)
    bot.f.step(RANK_AT)
    assert data.calls["scan"] == [] and bot.f.ranked_at is None and bot.f.watch == []
    lag = events(bot, "F_SCAN_LAGGING")
    assert len(lag) == 1 and lag[0]["median_minutes"] == 12
    bot.f.step(RANK_AT + timedelta(seconds=30))
    assert len(data.calls["bars"]) == 1                                     # nothing before F_SCAN_RETRY_SEC
    data.set_frames(today_closes()[:14])
    bot.f.step(RANK_AT + timedelta(seconds=60))
    bot.f.step(RANK_AT + timedelta(seconds=60))
    assert len(events(bot, "F_SCAN_LAGGING")) == 2 and bot.f.ranked_at is None
    data.set_frames(today_closes(BREAK))
    bot.f.step(RANK_AT + timedelta(seconds=120))
    bot.f.step(RANK_AT + timedelta(seconds=120))
    assert len(data.calls["scan"]) == 1 and [w.symbol for w in bot.f.watch] == ["TSLA"]
    assert events(bot, "F_DISABLED") == [] and events(bot, "F_SCAN_FAILED") == []


def test_scan_failure_retries_then_disables_and_ab_keep_trading(bot, data, monkeypatch):
    monkeypatch.setattr(config, "F_SCAN_MAX_RETRIES", 2)
    data.set_frames(today_closes(BREAK))
    data.scan_error = RuntimeError("yahoo down")
    bot.f.step(RANK_AT)
    bot.f.step(RANK_AT)
    assert len(events(bot, "F_SCAN_FAILED")) == 1 and bot.f.disabled_reason is None
    bot.f.step(RANK_AT + timedelta(seconds=10))                             # inside the retry pause
    assert len(data.calls["scan"]) == 1
    bot.f.step(RANK_AT + timedelta(seconds=60))
    bot.f.step(RANK_AT + timedelta(seconds=60))
    assert len(events(bot, "F_SCAN_FAILED")) == 2 and "yahoo down" in bot.f.disabled_reason
    assert len(events(bot, "F_DISABLED")) == 1
    bot.f.step(RANK_AT + timedelta(seconds=120))
    assert len(data.calls["scan"]) == 2                                     # disabled: no more scans
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}


def test_step_swallows_any_exception_and_journals_f_error(bot, data, monkeypatch):
    monkeypatch.setattr(config, "F_MAX_ERRORS_PER_DAY", 3)
    data.set_frames(today_closes(BREAK))
    rank(bot, data)
    data.bars_error = TypeError("bad frame")                                # the monitor's fetch now blows up
    for i in range(3):
        bot.f.step(et(9, 55, 10 + i))
    errs = events(bot, "F_ERROR")
    assert [e["consecutive"] for e in errs] == [1, 2, 3] and errs[0]["type"] == "TypeError"
    assert "3 consecutive errors" in bot.f.disabled_reason and len(events(bot, "F_DISABLED")) == 1
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}


# ----------------------------------------------------------------------- signals + queue

def test_momentum_break_on_newest_candle_queues_signal_once(bot, data):
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    rank(bot, data)
    bot.f.step(et(9, 50, 30))                                               # 09:45 candle: armed, no signal
    assert bot.f.watch[0].setup.state == OrbSetup.BROKEN and events(bot, "SIGNAL") == []
    assert bot.f.next_check == et(9, 55)
    bot.f.step(et(9, 55, 10))                                               # 09:50 candle: MOMENTUM fire -> entered
    sig = events(bot, "SIGNAL")
    assert len(sig) == 1 and sig[0]["setup"] == "STOCK_ORB" and sig[0]["ticker"] == "TSLA"
    assert (sig[0]["direction"], sig[0]["trigger"], sig[0]["scanner_direction"], sig[0]["direction_match"]) == \
        (BULLISH, "MOMENTUM", BULLISH, True)
    assert sig[0]["candles"] == [et(9, 50).isoformat()] and sig[0]["high"] == OR_HIGH and sig[0]["rank"] == 1
    assert "STOCK_ORB:TSLA:" + et(9, 50).isoformat() in bot.acted
    assert "F:TSLA" in bot.pm.positions and bot.f.pending == []
    bot.f.step(et(9, 55, 11))                                               # same candles again: nothing new
    assert len(events(bot, "SIGNAL")) == 1
    # a fresh engine restored from state replays the candles: the fire lands on an acted key and is dropped
    restored = StockOrbTrader(bot, universe=UNIVERSE, scan_fn=data.scan_fn, bars_fn=data.bars_fn,
                              daily_fn=data.daily_fn, executor=bot.f.executor)
    restored.restore(bot.f.to_state(), et(9, 56))
    assert [w.setup.state for w in restored.watch] == [OrbSetup.WAITING]
    restored.step(et(9, 56))
    assert restored.watch[0].setup.state == OrbSetup.DONE and restored.pending == []
    assert len(events(bot, "SIGNAL")) == 1


def test_counter_scanner_break_is_flagged_and_optionally_skipped(bot, data, monkeypatch):
    bear_break = [429.7, 429.5, 429.3, 429.1, 428.9]
    bear_momentum = [428.8, 428.7, 428.6, 428.5, 428.4]
    data.set_frames(today_closes(bear_break, bear_momentum, [428.4] * 5))
    bot.broker.spots["TSLA"] = 428.0
    rank(bot, data, [candidate("TSLA", direction=BULLISH)])
    bot.f.step(et(9, 55, 10))
    sig = events(bot, "SIGNAL")[0]
    assert (sig["direction"], sig["scanner_direction"], sig["direction_match"]) == (BEARISH, BULLISH, False)
    assert events(bot, "F_SKIPPED") == []                                   # traded either way by default
    assert events(bot, "ENTRY_REJECTED")[-1]["direction"] == BEARISH        # went to entry (call chain has no ATM)
    monkeypatch.setattr(config, "F_REQUIRE_SCANNER_DIRECTION", True)
    bot2 = make_bot_f(bot.journal.dir, monkeypatch, data)
    bot2.broker.spots["TSLA"] = 428.0
    rank(bot2, data, [candidate("TSLA", direction=BULLISH)])
    bot2.f.step(et(9, 55, 10))
    skipped = events(bot2, "F_SKIPPED")
    assert skipped and skipped[-1]["reason"].startswith("DIRECTION_MISMATCH") and bot2.f.pending == []


def test_confirmation_rejections_are_final(bot, data, monkeypatch):
    def fire(closes=None, **frame_kw):
        data.set_frames(closes or today_closes(BREAK, MOMENTUM, HOLD), **frame_kw)
        b = make_bot_f(bot.journal.dir, monkeypatch, data)
        rank(b, data)
        b.f.step(et(9, 55, 10))
        return b

    b = fire(today_volume=100.0, prior_volume=150.0)                        # break candle 500 < normal 750
    rej = events(b, "F_CONFIRM_REJECTED")
    assert rej and rej[-1]["reasons"] == ["LOW_VOLUME"] and rej[-1]["stale"] is False
    assert rej[-1]["candle_volume"] == 500.0 and rej[-1]["normal_volume"] == 750.0
    assert b.f.pending == [] and "F:TSLA" not in b.pm.positions and "STOCK_ORB:TSLA:" + et(9, 50).isoformat() in b.acted
    b.f.step(et(9, 55, 20))
    assert len(events(b, "F_CONFIRM_REJECTED")) == 1                        # final: never re-queued

    monkeypatch.setattr(config, "F_MIN_REMAINING_EM_FRAC", 0.90)             # remaining 14.9 < 18
    b = fire()
    assert events(b, "F_CONFIRM_REJECTED")[-1]["reasons"] == ["EM_EXHAUSTED"]
    monkeypatch.setattr(config, "F_MIN_REMAINING_EM_FRAC", 0.35)

    monkeypatch.setattr(stock_orb.orb_scanner, "compute_metrics", lambda *a, **k: (None, ["no daily bars"]))
    b = fire()
    assert events(b, "F_CONFIRM_REJECTED")[-1]["reasons"] == ["NO_METRICS"]


def test_pending_drains_one_entry_per_tick_in_rank_order_and_caps_at_three(bot, data, monkeypatch):
    monkeypatch.setattr(config, "F_SIGNAL_TTL_MIN", 10)
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    rank(bot, data, [candidate(s, "A", 90.0 - i) for i, s in enumerate(STOCKS)])
    bot.f.step(et(9, 55, 10))
    assert set(bot.pm.positions) == {"F:TSLA"} and [s.symbol for s in bot.f.pending] == ["AMZN", "META", "PFE"]
    bot.f.step(et(9, 55, 11))
    bot.f.step(et(9, 55, 12))
    assert set(bot.pm.positions) == {"F:TSLA", "F:AMZN", "F:META"} and [s.symbol for s in bot.f.pending] == ["PFE"]
    bot.f.step(et(9, 55, 13))
    assert "F:PFE" not in bot.pm.positions and len(bot.f.pending) == 1       # 3 open: waits for a slot
    close_f(bot, "F:AMZN", et(9, 56), 1.30)
    assert "F:AMZN" not in bot.pm.positions
    bot.f.step(et(9, 56, 5))
    assert set(bot.pm.positions) == {"F:TSLA", "F:META", "F:PFE"} and bot.f.pending == []
    assert bot.risk.state.for_strategy("F").entries_today == 4
    # a signal that never gets a slot expires after F_SIGNAL_TTL_MIN past its candle end
    bot.f.pending.append(StockSignal("AMZN", "A", 2, BULLISH, "MOMENTUM", et(9, 50), 5, {"volume": 750.0}, 434.6,
                                     OR_HIGH, OR_LOW, BULLISH, {}, confirm={}, confirmed_at=et(9, 55, 10)))
    bot.f.step(et(10, 5, 1))
    assert bot.f.pending == [] and events(bot, "F_SKIPPED")[-1]["reason"] == "SIGNAL_EXPIRED"


def test_pending_skips_open_ticker_and_break_failed(bot, data):
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" in bot.pm.positions
    bot.f.pending.append(StockSignal("TSLA", "A", 1, BULLISH, "PULLBACK", et(9, 55), 5, {"volume": 750.0}, 434.6,
                                     OR_HIGH, OR_LOW, BULLISH, {}, confirm={}, confirmed_at=et(10, 0)))
    bot.f.step(et(10, 0, 1))
    assert bot.f.pending == [] and events(bot, "F_SKIPPED")[-1]["reason"] == "TICKER_OPEN"
    close_f(bot, "F:TSLA", et(10, 1), 1.30)
    bot.broker.spots["TSLA"] = 433.0                                        # back inside the range
    bot.f.pending.append(StockSignal("TSLA", "A", 1, BULLISH, "PULLBACK", et(9, 55), 5, {"volume": 750.0}, 434.6,
                                     OR_HIGH, OR_LOW, BULLISH, {}, confirm={}, confirmed_at=et(10, 1, 30)))
    bot.f.step(et(10, 1, 31))
    assert "F:TSLA" not in bot.pm.positions and events(bot, "F_SKIPPED")[-1]["reason"].startswith("BREAK_FAILED spot 433.00")


def test_stale_pending_signal_is_reconfirmed_before_entry(bot, data, monkeypatch):
    fade = [433.6, 433.4, 433.2, 433.0, 432.8, 432.6, 432.4, 432.2, 432.0, 431.8]   # 10:00-10:09: back inside, under VWAP
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, fade))
    rank(bot, data)
    monkeypatch.setattr(config, "NEWS_EVENTS", {TODAY.isoformat(): [et(9, 55).time()]})   # blackout 09:50-10:00
    bot.f.step(et(9, 55, 10))
    assert [s.symbol for s in bot.f.pending] == ["TSLA"] and "F:TSLA" not in bot.pm.positions
    bot.f.step(et(10, 10, 5))                                               # blackout over, but the move has faded
    rej = events(bot, "F_CONFIRM_REJECTED")
    assert rej and rej[-1]["stale"] is True and "WRONG_SIDE_OF_VWAP" in rej[-1]["reasons"]
    assert bot.f.pending == [] and "F:TSLA" not in bot.pm.positions


# ------------------------------------------------------------------------------ re-entry

def test_momentum_reentry_after_exit_needs_four_consecutive_one_minute_closes(bot, data):
    after_exit = [435.3, 435.4, 435.45, 435.5, 435.6, 435.7, 435.8]          # 10:00-10:06
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, after_exit))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" in bot.pm.positions and bot.f.watch[0].setup.state == OrbSetup.DONE
    close_f(bot, "F:TSLA", et(10, 2, 30), 1.30)
    assert bot.f.watch[0].reentry_from == et(10, 2, 30)
    bot.f.step(et(10, 6, 10))                                               # only 10:03-10:05 completed: 3 closes
    assert "F:TSLA" not in bot.pm.positions and len(events(bot, "SIGNAL")) == 1
    bot.f.step(et(10, 7, 10))                                               # 10:03 < 10:04 < 10:05 < 10:06: four
    sig = events(bot, "SIGNAL")
    assert len(sig) == 2 and sig[1]["trigger"] == "MOMENTUM_REENTRY" and sig[1]["direction"] == BULLISH
    assert sig[1]["candles"] == [et(10, 3).isoformat()] and sig[1]["direction_match"] is True
    assert "F:TSLA" in bot.pm.positions and bot.pm.positions["F:TSLA"].setup == "STOCK_ORB"
    assert bot.pm.positions["F:TSLA"].scan["trigger"] == "MOMENTUM_REENTRY"
    assert bot.risk.state.for_strategy("F").entries_today == 2 and bot.f.watch[0].reentry_from is None


def test_momentum_reentry_is_denied_by_a_broken_streak_or_a_lost_level(bot, data):
    broken = [435.3, 435.4, 435.45, 435.5, 435.2, 435.7, 435.8]             # 10:04 closes lower: streak broken
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, broken))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    close_f(bot, "F:TSLA", et(10, 2, 30), 1.30)
    bot.f.step(et(10, 7, 10))
    assert len(events(bot, "SIGNAL")) == 1 and "F:TSLA" not in bot.pm.positions
    # four rising closes that are still below the OR high do not count either
    faded = [433.0, 432.9, 432.8, 432.7, 432.8, 432.9, 433.0, 433.1]         # 10:00-10:07
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, faded))
    bot.f.step(et(10, 8, 10))
    assert len(events(bot, "SIGNAL")) == 1


def test_momentum_reentry_needs_adjacent_minutes(bot, data):
    after_exit = [435.3, 435.4, 435.45, 435.5, 435.6, 435.7, 435.8, 435.9]   # 10:00-10:07, every close higher
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, after_exit))
    for s in STOCKS:
        data.frames[s] = data.frames[s].drop(et(10, 4))                     # Yahoo never served 10:04
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    close_f(bot, "F:TSLA", et(10, 2, 30), 1.30)
    bot.f.step(et(10, 8, 10))                                               # 10:03, 10:05, 10:06, 10:07 rise but are not consecutive
    assert len(events(bot, "SIGNAL")) == 1 and "F:TSLA" not in bot.pm.positions


def test_reentry_watch_dies_with_the_break_it_belonged_to(bot, data, monkeypatch):
    # exit -> close back inside (reset) -> fresh break rejected -> four rising minutes: the stale watch
    # from the first cycle must not hand the rejected break a second bite as MOMENTUM_REENTRY
    break2 = [434.0, 434.2, 434.4, 434.6, 434.8]                              # 10:00-10:04
    momentum2 = [434.9, 435.0, 435.1, 435.2, 435.3]                           # 10:05-10:09: MOMENTUM fires
    rising = [435.4, 435.5, 435.6, 435.7]                                     # 10:10-10:13
    data.set_frames(today_closes(BREAK, MOMENTUM, INSIDE, break2, momentum2, rising))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    close_f(bot, "F:TSLA", et(9, 57), 1.30)
    w = bot.f.watch[0]
    assert w.reentry_from == et(9, 57)
    bot.f.step(et(10, 0, 10))                                               # 09:55 closed inside: the break reset ...
    assert (w.setup.state, w.reentry_from) == (OrbSetup.WAITING, None)      # ... and the watch with it
    monkeypatch.setattr(config, "F_MIN_REMAINING_EM_FRAC", 0.90)             # the fresh 10:05 break is rejected
    bot.f.step(et(10, 10, 10))
    assert events(bot, "F_CONFIRM_REJECTED")[-1]["reasons"] == ["EM_EXHAUSTED"] and w.setup.state == OrbSetup.DONE
    bot.f.next_check = None
    bot.f.step(et(10, 14, 10))                                              # 10:10-10:13 each close higher, above the OR high
    assert len(events(bot, "SIGNAL")) == 2 and "F:TSLA" not in bot.pm.positions


def test_fresh_break_after_close_back_inside_reenters(bot, data):
    break2 = [434.0, 434.2, 434.4, 434.6, 434.8]
    momentum2 = [434.9, 435.0, 435.1, 435.2, 435.3]
    data.set_frames(today_closes(BREAK, MOMENTUM, INSIDE, break2, momentum2))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    close_f(bot, "F:TSLA", et(9, 57), 1.30)
    bot.f.step(et(10, 0, 10))                                               # 09:55 closed inside: setup re-arms
    assert bot.f.watch[0].setup.state == OrbSetup.WAITING and "F:TSLA" not in bot.pm.positions
    assert bot.f.watch[0].reentry_from is None
    bot.f.step(et(10, 10, 10))                                              # 10:00 break, 10:05 momentum -> new signal
    sig = events(bot, "SIGNAL")
    assert len(sig) == 2 and sig[1]["trigger"] == "MOMENTUM" and sig[1]["candles"] == [et(10, 5).isoformat()]
    assert "F:TSLA" in bot.pm.positions and bot.risk.state.for_strategy("F").entries_today == 2


# --------------------------------------------------------------------------------- state

def test_state_roundtrip_restores_watch_without_rescan(bot, data):
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    rank(bot, data, [candidate("TSLA"), candidate("AMZN", score=70.0)])
    bot.f.watch[1].reentry_from = et(9, 58)
    bot.f.pending.append(StockSignal("META", "B", 3, BULLISH, "MOMENTUM", et(9, 50), 5, {}, None, 1, 0, BULLISH, {}))
    state = json.loads(json.dumps(bot.f.to_state(), default=str))
    fresh = StockOrbTrader(bot, universe=UNIVERSE, scan_fn=data.scan_fn, bars_fn=data.bars_fn,
                           daily_fn=data.daily_fn, executor=bot.f.executor)
    fresh.restore(state, et(10, 0))
    assert fresh.ranked_at == RANK_AT and [w.symbol for w in fresh.watch] == ["TSLA", "AMZN"]
    assert (fresh.watch[0].or_high, fresh.watch[0].or_low) == (OR_HIGH, OR_LOW)
    assert fresh.watch[0].scan_row["score"] == 80.0 and fresh.watch[1].reentry_from == et(9, 58)
    assert fresh.pending == [] and fresh.next_check == et(9, 50)
    fresh.step(et(10, 0))
    assert data.calls["scan"] == [data.calls["scan"][0]]                    # no re-scan
    stale = StockOrbTrader(bot, universe=UNIVERSE, scan_fn=data.scan_fn, bars_fn=data.bars_fn,
                           daily_fn=data.daily_fn, executor=bot.f.executor)
    stale.restore(state, et(10, 0, day=18))
    assert stale.ranked_at is None and stale.watch == []
    stale.restore(None, et(10, 0))
    assert stale.ranked_at is None


def test_chain_fn_scores_option_quality_with_a_dedicated_broker(bot, data):
    factories = []

    def factory():
        factories.append(bot.broker)
        return bot.broker

    bot.f.chain_broker_factory = factory
    data.set_frames(today_closes(BREAK))
    rank(bot, data)
    chain_fn = data.calls["scan"][0]["chain_fn"]
    assert len(factories) == 1
    rows_ = chain_fn("TSLA", 436.0, BULLISH)
    assert rows_ and {r["right"] for r in rows_} == {"C"} and all(r["root"] == "TSLA" for r in rows_)
    assert all(abs(r["strike"] - 436.0) <= 0.03 * 436.0 for r in rows_)
    assert {r["right"] for r in chain_fn("TSLA", 436.0, BEARISH)} == {"P"}
    bot.broker.expirations["PFE"] = []
    assert chain_fn("PFE", 30.0, BULLISH) is None
    assert len(factories) == 1                                              # one broker per scan


def test_candles_by_key_and_pending_never_touch_the_spx_frame(bot, data):
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    by_key = bot.f.candles_by_key()
    assert set(by_key) == {"F:TSLA"} and list(by_key["F:TSLA"].index.strftime("%H:%M"))[-1] == "09:50"
    assert bot.f.daily["TSLA"].equals(daily_frame())

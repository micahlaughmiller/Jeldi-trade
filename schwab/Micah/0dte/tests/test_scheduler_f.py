"""Strategy F through the Bot: entries, marks, exits, risk accounting, force close, restart, reports."""

import json
import sys
from datetime import timedelta

import pytest

import config
import scheduler
from conftest import et, make_candles
from f_fixtures import (BREAK, HOLD, MOMENTUM, OR_HIGH, STOCKS, TODAY, DataStub, make_bot_f, make_broker, rank,
                        today_closes, tsla_put_chain)
from position_manager import OpenSpread, occ_root, option_legs
from risk_manager import RiskManager
import stock_orb
from stock_orb import StockSignal, WatchItem
from strategy import BULLISH, OrbSetup

EXP = TODAY + timedelta(days=2)

pytestmark = pytest.mark.usefixtures("pre_f_stop_fix")


@pytest.fixture(autouse=True)
def _legacy_exit_flow(monkeypatch):
    # 2026-09-30: F now defaults to config.STAGED_EXIT_ENABLED=True too (test_staged_exit.py covers
    # that path); this file's exit tests predate it and exercise the flat target/stop/lock/runner
    # sequence _tick_position still falls back to when it's off.
    monkeypatch.setattr(config, "STAGED_EXIT_ENABLED", False)


def bare_bot_f(tmp_path, monkeypatch, broker) -> scheduler.Bot:
    """A Bot constructed the way scheduler.main() does it (F persona), without f_fixtures swapping in
    the stubbed engine -- for the paths where StockOrbTrader itself is what fails."""
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "F"))
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: broker)
    bot = scheduler.Bot(dry_run=False, once=True)
    bot.today = TODAY
    return bot


@pytest.fixture
def data() -> DataStub:
    d = DataStub()
    d.set_frames(today_closes(BREAK, MOMENTUM, HOLD))
    return d


@pytest.fixture
def bot(tmp_path, monkeypatch, data):
    return make_bot_f(tmp_path, monkeypatch, data)


def events(bot, kind: str) -> list[dict]:
    rows = [json.loads(line) for path in sorted(bot.journal.dir.glob(f"events_{bot.journal.slug}_*.jsonl"))
            for line in path.read_text().splitlines()]
    return [r for r in rows if r["event"] == kind]


def enter_f(bot, data, symbol: str = "TSLA"):
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert f"F:{symbol}" in bot.pm.positions
    return bot.pm.positions[f"F:{symbol}"]


def signal(symbol: str = "TSLA", rank_: int = 1) -> StockSignal:
    return StockSignal(symbol, "A", rank_, BULLISH, "MOMENTUM", et(9, 50), 5,
                       {"open": 434.7, "high": 435.15, "low": 434.65, "close": 435.1, "volume": 750.0}, 434.6,
                       OR_HIGH, 429.95, BULLISH, {"score": 80.0, "tier": "A"}, confirm={"remaining_em": 14.9},
                       confirmed_at=et(9, 55, 10))


def f_spread(root: str, qty: int = 1, credit: float = 0.80, width: float = 5.0, current: float | None = None) -> OpenSpread:
    return OpenSpread(strategy="F", direction=BULLISH, setup="STOCK_ORB", right="P", root=root, expiration=EXP,
                      short_strike=417.5, long_strike=412.5, width=width, qty=qty, entry_credit=credit,
                      entry_time=et(9, 55), current_price=credit if current is None else current, best_price=credit,
                      profit_target=0.30, stop_loss=0.50, entry_mid=credit)


# ----------------------------------------------------------------------------------- entry

def test_confirmed_break_opens_stock_spread_with_root_ticker_and_width_scaled_exits(bot, data, caplog):
    caplog.set_level("INFO")
    p = enter_f(bot, data)
    assert (p.strategy, p.setup, p.root, p.expiration, p.right, p.direction) == ("F", "STOCK_ORB", "TSLA", EXP, "P", BULLISH)
    assert (p.short_strike, p.long_strike, p.width, p.qty, p.entry_credit) == (417.5, 412.5, 5.0, 1, 0.80)
    assert 0.13 * p.width <= p.entry_credit <= 0.22 * p.width
    assert (p.profit_target, p.stop_loss) == (0.30, 0.50)
    assert p.overrides == {"PROFIT_LOCK_ARM": 0.15, "PROFIT_LOCK_GIVEBACK": 0.10, "TRAIL_AMOUNT": 0.30}
    assert (p.entry_bid, p.entry_ask, p.entry_mid) == (0.60, 1.00, 0.80)
    assert p.key == "F:TSLA" and p.ticker == "TSLA" and p.underlying == "TSLA"
    order = next(o for o in bot.broker.orders.values() if o["symbol"].startswith("TSLA"))
    assert order["symbol"].startswith("TSLA260919P00417500/TSLA260919P00412500")
    legs = option_legs(bot.broker.get_positions(), {"TSLA"})
    assert {l["underlying"] for l in legs} == {"TSLA"} and len(legs) == 2
    assert bot.risk.state.for_strategy("F").entries_today == 1 and bot.f.entries_by_ticker == {"TSLA": 1}
    card = next(r.getMessage() for r in caplog.records if "TRADE #1   STRATEGY F - STOCK ORB TSLA" in r.getMessage())
    assert "Underlying      TSLA  expires 2026-09-19" in card and "Score" in card and "Quote" in card and "Width" in card
    assert "Sold            417.5 P" in card and "Stop            $1.30" in card and "Target          $0.50" in card
    summary = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL STOCK_ORB TSLA BULLISH (MOMENTUM) at"))
    assert summary.endswith(" -- F: sell 417.5P buy 412.5P x1 @ 0.80 width 5 EM 17.00 exp 2026-09-19")
    assert bot.journal.load_state()["positions"].keys() == {"F:TSLA"}


def test_entry_event_carries_scan_row_confirmation_and_quote(bot, data):
    enter_f(bot, data)
    entry = [e for e in events(bot, "ENTRY") if e["strategy"] == "F"][0]
    pos = entry["position"]
    scan = pos["scan"]
    assert (pos["root"], pos["width"], pos["entry_bid"], pos["entry_ask"], pos["entry_mid"]) == ("TSLA", 5.0, 0.60, 1.00, 0.80)
    assert pos["overrides"]["TRAIL_AMOUNT"] == 0.30
    for key in ("score", "score_with_tier", "component_relative_volume", "or15_high", "or15_low", "or15_width", "atr",
                "expected_move", "remaining_em", "rvol", "vwap_relation", "ema_state", "ticker"):
        assert key in scan
    assert (scan["scanner_direction"], scan["direction_match"], scan["trigger"], scan["rank"]) == (BULLISH, True, "MOMENTUM", 1)
    assert scan["em_straddle"] == 17.0 and scan["increment"] == 2.5 and scan["expiration"] == EXP.isoformat()
    assert scan["break_candle"]["time"] == et(9, 50).isoformat() and scan["break_candle"]["close"] == 435.1
    assert scan["confirm"]["candle_volume"] == 750.0 and scan["confirm"]["normal_volume"] == 500.0
    assert scan["confirm"]["remaining_em"] == pytest.approx(14.9) and scan["confirm"]["vwap"] < 435.1


def test_spx_signal_never_reaches_f(bot, data, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at"))
    assert "F:" not in line and "A: sell" in line and "B: sell" in line
    assert all(e["strategy"] != "F" for e in events(bot, "ENTRY") + events(bot, "ENTRY_REJECTED"))
    assert bot.entry_possible(10_000.0) is False                            # A and B open: F is not an SPX slot
    bot.pm.positions.clear()
    assert bot.entry_possible(10_000.0) is True


def test_entry_rejections_are_journaled_with_ticker(bot, data):
    rank(bot, data)
    b = bot.broker
    b.set_chain("P", {**b._chain("P", "TSLA"), 417.5: (1.25, 2.45)}, underlying="TSLA")   # 0.10 / 1.50: blown
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: BID_ASK_BLOWN")
    b.set_chain("P", {k: v for k, v in b._chain("P", "TSLA").items() if k != 417.5} | {417.5: (1.75, 1.95)}, underlying="TSLA")
    b.expirations["TSLA"] = []
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: NO_EXPIRATION")
    b.expirations["TSLA"] = [EXP]
    b.set_chain("C", {}, underlying="TSLA")
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: NO_EXPECTED_MOVE")
    b.set_chain("C", {435.0: (8.9, 9.1)}, underlying="TSLA")
    b.set_chain("P", {435.0: (7.9, 8.1)}, underlying="TSLA")
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: NO_STRIKE_INCREMENT")
    b.set_chain("P", {435.0: (7.9, 8.1), 432.5: (6.4, 6.6)}, underlying="TSLA")
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: F_NO_STRIKE_IN_RANGE")
    rejected = events(bot, "ENTRY_REJECTED")
    assert [r["reason"].split()[0] for r in rejected] == ["BID_ASK_BLOWN", "NO_EXPIRATION", "NO_EXPECTED_MOVE",
                                                          "NO_STRIKE_INCREMENT", "F_NO_STRIKE_IN_RANGE"]
    assert all(r["strategy"] == "F" and r["ticker"] == "TSLA" and r["setup"] == "STOCK_ORB" for r in rejected)
    bot.pm.open(f_spread("AMZN", qty=6, width=10.0, credit=1.0))           # 5,400 open risk: nothing fits under 52%
    b.set_chain("P", tsla_put_chain(), underlying="TSLA")
    assert bot.f.enter(et(10, 0), signal()).startswith("skip: SIZING_ZERO")
    assert events(bot, "ENTRY_REJECTED")[-1]["reason"].startswith("SIZING_ZERO")
    b.fill_entries = False
    bot.pm.positions.clear()
    assert bot.f.enter(et(10, 0), signal()).startswith("not filled (canceled)")
    assert events(bot, "ENTRY_UNFILLED")[-1]["ticker"] == "TSLA"


def test_two_tickers_on_identical_strikes_do_not_collide(bot):
    bot.pm.open(f_spread("TSLA"))
    assert bot.pm.find_collision("TSLA", EXP, "P", 417.5, 412.5) == "F:TSLA"
    assert bot.pm.find_collision("AMZN", EXP, "P", 417.5, 412.5) is None
    assert bot.pm.find_collision("TSLA", TODAY, "P", 417.5, 412.5) is None
    bot.pm.open(f_spread("AMZN"))
    assert set(bot.pm.positions) == {"F:TSLA", "F:AMZN"} and bot.pm.open_count("F") == 2


# ----------------------------------------------------------------------------------- manage

def test_spread_prices_groups_by_root_isolates_stock_errors_and_honours_the_throttle(bot, data, monkeypatch):
    bot.enter(et(10, 15), "ORB", BULLISH)
    bot.pm.open(f_spread("TSLA"))
    bot.pm.open(f_spread("AMZN"))
    bot.broker.chain_calls.clear()
    prices = bot.spread_prices()
    assert prices == {"A": 3.00, "B": 0.90, "F:TSLA": 0.80, "F:AMZN": 0.80}
    assert sorted(bot.broker.chain_calls) == [("AMZN", EXP, "P"), ("SPXW", TODAY, "P"), ("TSLA", EXP, "P")]
    bot.broker.chain_calls.clear()
    assert bot.spread_prices() == {"A": 3.00, "B": 0.90}                    # F marked at most every 15 s
    assert bot.broker.chain_calls == [("SPXW", TODAY, "P")]
    assert set(bot.spread_prices(force=True)) == {"A", "B", "F:TSLA", "F:AMZN"}
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 0)
    bot.broker.chain_errors.add("TSLA")
    prices = bot.spread_prices()
    assert prices == {"A": 3.00, "B": 0.90, "F:AMZN": 0.80}
    err = events(bot, "BROKER_ERROR")
    assert err and err[-1]["ticker"] == "TSLA" and err[-1]["strategy"] == "F"
    # a FAILING stock chain is throttled like a successful one: not re-issued (with the broker's 429
    # back-off sleeps and an ERROR line) on every 1-second tick
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 15)
    bot._f_last_priced.clear()
    bot.broker.chain_calls.clear()
    assert bot.spread_prices() == {"A": 3.00, "B": 0.90, "F:AMZN": 0.80}
    bot.broker.chain_calls.clear()
    assert bot.spread_prices() == {"A": 3.00, "B": 0.90} and bot.broker.chain_calls == [("SPXW", TODAY, "P")]
    # any exception out of a stock chain call is isolated, not only BrokerError
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 0)
    bot.broker.chain_errors.clear()
    real = bot.broker.get_option_chain

    def flaky(underlying, *a, **k):
        if underlying == "TSLA":
            raise ValueError("truncated body")
        return real(underlying, *a, **k)

    monkeypatch.setattr(bot.broker, "get_option_chain", flaky)
    assert bot.spread_prices() == {"A": 3.00, "B": 0.90, "F:AMZN": 0.80}
    assert events(bot, "BROKER_ERROR")[-1]["type"] == "ValueError"
    bot.broker.chain_errors = {"SPXW"}
    with pytest.raises(scheduler.BrokerError):
        bot.spread_prices()                                                 # SPX chain failures still raise


def test_manage_routes_ticker_candles_to_f_and_spx_candles_to_ab(bot, data, monkeypatch):
    bot.broker.equity = 20_000.0
    bot.risk = RiskManager(20_000.0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert enter_f(bot, data).qty == 2
    seen = {}
    on_tick = bot.pm.on_tick

    def spy(now, prices, candles, candles_by_key=None):
        seen["candles"], seen["by_key"] = candles, candles_by_key
        return []

    monkeypatch.setattr(bot.pm, "on_tick", spy)
    spx = make_candles(et(10, 0), [(7600, 7601, 7599, 7600.5)] * 3)
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 0)
    bot.manage(et(10, 16), spx)
    assert seen["candles"] is spx and set(seen["by_key"]) == {"F:TSLA"}
    assert list(seen["by_key"]["F:TSLA"].index.strftime("%H:%M")) == ["09:30", "09:35", "09:40", "09:45", "09:50"]
    # without a cached frame the F position sees None: at the target the runner gate is false -> full close
    bot.f.candles5.clear()
    bot.broker.spread_close_price = 0.50
    fills = on_tick(et(10, 17), {"F:TSLA": 0.50}, spx, bot.f.candles_by_key())
    assert fills[0]["exit_reason"] == "PROFIT_TARGET" and fills[0]["qty"] == 2


def test_f_exit_row_csv_and_close_card(bot, data, caplog):
    caplog.set_level("INFO")
    p = enter_f(bot, data)
    assert bot.pm.on_tick(et(9, 56), {"F:TSLA": 0.70}, None, bot.f.candles_by_key()) == []   # best 0.70
    bot.broker.spread_close_price = 1.32
    fills = bot.pm.on_tick(et(9, 57), {"F:TSLA": 1.30}, None, bot.f.candles_by_key())
    bot.record_fills(fills)
    row = fills[0]
    assert (row["strategy"], row["key"], row["ticker"], row["expiration"], row["root"]) == ("F", "F:TSLA", "TSLA", EXP.isoformat(), "TSLA")
    assert (row["exit_reason"], row["exit_price"], row["exit_slippage"]) == ("STOP_LOSS", 1.32, pytest.approx(0.02))
    assert (row["mfe"], row["mae"], row["scanner_score"], row["direction_match"]) == (pytest.approx(0.10), pytest.approx(0.50), 80.0, True)
    assert row["pnl"] == pytest.approx(10.0) and bot.pm.positions == {} and bot.broker.close_roots[-1] == "TSLA"
    assert row["pnl_actual_fill"] == pytest.approx(-52.0)
    assert option_legs(bot.broker.get_positions()) == []
    line = bot.journal.trades_path.read_text().splitlines()[1]
    assert line.startswith("2026-09-17,F,") and ",TSLA," in line and line.endswith(",0.1,0.5,80.0")
    s = bot.risk.state.for_strategy("F")
    assert s.trades_today == 1 and s.consecutive_losses == 0 and s.realized_pnl == pytest.approx(10.0)
    card = next(r.getMessage() for r in caplog.records if "STRATEGY F - STOCK ORB TSLA" in r.getMessage() and " CLOSE" in r.getMessage())
    assert "P/L today F     +$10.00" in card and "MFE / MAE       +0.10 / -0.50" in card and "Slippage" in card
    assert "Underlying      TSLA  expires 2026-09-19" in card              # the spread's expiration, not the close date
    assert bot.f.watch[0].reentry_from == et(9, 57)
    exit_ev = events(bot, "EXIT")[-1]
    assert exit_ev["ticker"] == "TSLA" and exit_ev["scanner_direction"] == BULLISH and exit_ev["key"] == "F:TSLA"


def test_partial_runner_fill_resolves_by_key(bot, data, caplog):
    caplog.set_level("INFO")
    bot.broker.equity = 20_000.0
    bot.risk = RiskManager(20_000.0)
    p = enter_f(bot, data)
    assert p.qty == 2
    rising = make_candles(et(9, 40), [(434 + i, 435 + i, 433.5 + i, 434.8 + i) for i in range(3)], minutes=5)
    bot.broker.spread_close_price = 0.50
    fills = bot.pm.on_tick(et(9, 58), {"F:TSLA": 0.50}, None, {"F:TSLA": rising})
    assert fills[0]["exit_reason"] == "TARGET_HALF" and fills[0]["qty"] == 1 and fills[0]["position_closed"] is False
    bot.record_fills(fills)
    assert bot.pm.positions["F:TSLA"].runner is True and bot.pm.positions["F:TSLA"].remaining == 1
    card = next(r.getMessage() for r in caplog.records if "Bought back     1 of 2" in r.getMessage())
    assert "(1 still open)" in card and "STRATEGY F - STOCK ORB TSLA" in card
    assert bot.risk.state.for_strategy("F").trades_today == 0 and bot.f.watch[0].reentry_from is None


# ----------------------------------------------------------------------------------- risk

def test_five_entries_per_day_counts_open_positions_and_recorded_entries(bot, data, f_five_a_day):
    rank(bot, data)
    for _ in range(3):
        bot.risk.record_trade("F", 10.0)
    bot.pm.open(f_spread("AMZN"))
    bot.pm.open(f_spread("META"))
    bot.f.pending += [signal("TSLA", 1), signal("PFE", 2)]
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" not in bot.pm.positions and bot.f.pending == []
    skipped = events(bot, "F_SKIPPED")
    assert [s["ticker"] for s in skipped] == ["TSLA", "PFE"] and all(s["reason"] == "F: MAX_TRADES_PER_DAY (5)" for s in skipped)
    assert bot.risk.trading_allowed("A", 10_000.0) == (True, "OK")
    fresh = RiskManager(10_000.0)
    for _ in range(5):
        fresh.record_entry("F")
    assert fresh.trading_allowed("F")[1] == "F: MAX_TRADES_PER_DAY (5)"
    assert fresh.trading_allowed("F", open_count=2)[0] is False
    assert RiskManager(10_000.0).trading_allowed("F", open_count=3) == (True, "OK")


def test_seven_consecutive_f_losses_block_only_f(bot, data):
    # set the streak directly (not via record_trade) so it isolates MAX_CONSECUTIVE_LOSSES (7) from
    # the now much lower MAX_TRADES_PER_DAY (5), which 7 recorded trades would trip first.
    bot.risk.state.for_strategy("F").consecutive_losses = 7
    ok, reason = bot.risk.trading_allowed("F")
    assert ok is False and reason == "F: MAX_CONSECUTIVE_LOSSES (7)"
    assert bot.risk.trading_allowed("A") == (True, "OK") and bot.risk.trading_allowed("B") == (True, "OK")
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}


def test_daily_loss_limit_excludes_f_pnl_and_an_ab_halt_never_stops_f(bot, data, monkeypatch):
    rm = bot.risk
    rm.record_trade("F", -1_500.0)
    assert rm.state.realized_pnl == -1_500.0 and rm.state.loss_limit_pnl == 0.0 and rm.state.exempt_realized_pnl == -1_500.0
    assert rm.trading_allowed("A", 8_500.0) == (True, "OK")                 # equity includes the realized F loss
    bot.broker.equity = 8_500.0
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"A", "B"}
    bot.pm.positions.clear()
    # three open F spreads marked -$1,020 unrealized move account equity but not the A/B basis
    for sym in ("TSLA", "AMZN", "META"):
        bot.pm.open(f_spread(sym, current=4.20))
    assert bot.pm.unrealized_pnl(("F",)) == pytest.approx(-1_020.0)
    bot.broker.equity = 7_480.0
    assert rm.trading_allowed("A", 7_480.0)[0] is False                    # the raw number would trip it ...
    bot.enter(et(10, 20), "ORB", BULLISH)
    assert {"A", "B"} <= set(bot.pm.positions)                              # ... entry_possible adds F's marks back
    bot.pm.positions.clear()
    assert rm.trading_allowed("A", 7_480.0, exempt_open_pnl=-1_020.0) == (True, "OK")     # ex-F: 10,000
    assert rm.trading_allowed("A", 6_479.0, exempt_open_pnl=-1_020.0)[0] is False         # ex-F: 8,999
    # A+B trip the combined limit: A and B stop, F does not
    rm.record_trade("A", -1_000.0)
    assert rm.trading_allowed("A")[0] is False and rm.trading_allowed("B")[0] is False
    assert rm.trading_allowed("F", 7_000.0) == (True, "OK")
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" in bot.pm.positions
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_HALTS_EXEMPT", True)
    ok, reason = rm.trading_allowed("F", 7_000.0)
    assert ok is False and reason.startswith("DAILY_LOSS_LIMIT realized=-1000.00")


def test_daily_loss_basis_excludes_what_f_partial_fills_already_booked(bot, data):
    # $10k start, limit -$1,000. A/B: -$950 realized plus an open B spread marked -$100 (true ex-F
    # drawdown -$1,050 -> must halt). An F runner booked +$120 at TARGET_HALF and its 2-lot remainder is
    # flat: that +$120 sits in broker equity but in neither the day's realized nor F's marks.
    rm = bot.risk
    rm.record_trade("A", -950.0)
    bot.pm.open(OpenSpread(strategy="B", direction=BULLISH, setup="ORB", right="P", root="SPXW", expiration=TODAY,
                           short_strike=7560.0, long_strike=7555.0, width=5, qty=1, entry_credit=0.90,
                           entry_time=et(10, 0), current_price=1.90, best_price=0.90, profit_target=0.30, stop_loss=0.50))
    f = f_spread("TSLA", qty=4, credit=0.90)
    f.closed_qty, f.realized_pnl = 2, 120.0
    bot.pm.open(f)
    assert bot.pm.open_pnl(("F",)) == 120.0 and bot.pm.unrealized_pnl(("F",)) == 0.0
    equity = 10_000.0 - 950.0 - 100.0 + 120.0
    assert rm.trading_allowed("A", equity, exempt_open_pnl=0.0)[0] is True          # the leak the basis used to have
    assert rm.trading_allowed("A", equity, exempt_open_pnl=120.0)[0] is False
    assert bot.entry_possible(equity) is False


def test_fifty_two_pct_cap_includes_open_f_risk(bot, data, caplog):
    caplog.set_level("INFO")
    for sym in ("TSLA", "AMZN", "META"):
        bot.pm.open(f_spread(sym, qty=4))                                    # 3 x 4 x $420 = $5,040 open risk
    assert bot.pm.total_open_risk() == pytest.approx(5_040.0)
    bot.enter(et(10, 15), "ORB", BULLISH)
    assert set(bot.pm.positions) == {"F:TSLA", "F:AMZN", "F:META"}
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("SIGNAL ORB BULLISH at"))
    assert "A: skip: sizing 0 contracts" in line and "B: skip: sizing 0 contracts" in line


# ------------------------------------------------------------------------- end of day / close

def test_force_close_closes_f_with_ab_and_reports_stuck_stock_legs(bot, data, monkeypatch, caplog):
    caplog.set_level("INFO")
    bot.enter(et(10, 15), "ORB", BULLISH)
    enter_f(bot, data)
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 0)
    bot.end_of_day(et(15, 30))
    assert bot.pm.positions == {} and bot.broker.get_positions() == []
    assert sorted(bot.broker.close_roots) == ["SPXW", "SPXW", "TSLA"]
    assert {e["strategy"] for e in events(bot, "EXIT")} == {"A", "B", "F"}
    assert all(e["exit_reason"] == "FORCE_CLOSE" for e in events(bot, "EXIT"))
    end = events(bot, "END")[-1]
    assert end["mismatches"] == [] and end["day"]["strategies"]["F"]["trades_today"] == 1
    # a stock leg the broker will not close is retried, reported and left for manual action
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    bot2 = make_bot_f(bot.journal.dir, monkeypatch, data)
    bot2.pm.open(f_spread("TSLA"))
    bot2.broker.add_spread_position("P", 417.5, 412.5, 1, 1.85, 1.05, EXP, root="TSLA")
    bot2.broker.close_failures = 99
    assert bot2.end_of_day(et(15, 30)) is False        # 2026-10-08: before the session ends the bot stays up and retries
    assert events(bot2, "EOD_CLOSE_STILL_OPEN")
    assert bot2.end_of_day(et(16, 1)) is True           # the session is over: report what is left and finish
    assert "F:TSLA" in bot2.pm.positions and len(option_legs(bot2.broker.get_positions(), {"TSLA"})) == 2
    end = events(bot2, "END")[-1]
    assert any("broker still holds option legs" in m and "TSLA" in m for m in end["mismatches"])
    assert any("CLOSE_FAILED: option legs still at broker" in r.getMessage() for r in caplog.records)


def test_failed_stock_close_is_not_booked_as_filled(bot, data, monkeypatch):
    monkeypatch.setattr(config, "CLOSE_MAX_RETRIES", 2)
    p = enter_f(bot, data)
    monkeypatch.setattr(bot.broker, "_reduce_spread", lambda *a, **k: None)   # the broker "fills" but the legs stay
    fills = bot.pm.on_tick(et(9, 57), {"F:TSLA": 1.30}, None, None)
    assert fills == [] and "F:TSLA" in bot.pm.positions and p.remaining == 1
    failed = events(bot, "CLOSE_FAILED")
    assert len(failed) == 2 and failed[0]["broker_qty"] == 1 and failed[0]["strategy"] == "F"


def test_interrupt_closes_f_positions_too(bot, data):
    enter_f(bot, data)
    bot.on_interrupt()
    assert bot.pm.positions == {} and bot.broker.close_roots == ["TSLA"]
    assert events(bot, "EXIT")[-1]["exit_reason"] == "INTERRUPT_CLOSE"


# ------------------------------------------------------------------------------------ restart

def test_restart_readopts_f_positions_from_state_and_broker_legs(bot, data, monkeypatch, caplog):
    caplog.set_level("INFO")
    p = enter_f(bot, data)
    bot.pm.on_tick(et(9, 56), {"F:TSLA": 0.70}, None, bot.f.candles_by_key())
    bot.pm.on_tick(et(9, 57), {"F:TSLA": 0.95}, None, bot.f.candles_by_key())
    bot.save_state()
    saved = json.loads(bot.journal.state_path.read_text())
    assert saved["positions"]["F:TSLA"]["worst_price"] == 0.95 and saved["f"]["watch"][0]["symbol"] == "TSLA"
    assert saved["day"]["strategies"]["F"]["entries_today"] == 1
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 30))
    bot2 = make_bot_f(bot.journal.dir, monkeypatch, data, broker=bot.broker)
    bot2.run = lambda now: None
    bot2.start()
    q = bot2.pm.positions["F:TSLA"]
    assert (q.entry_credit, q.best_price, q.worst_price, q.overrides, q.setup) == (0.80, 0.70, 0.95, p.overrides, "STOCK_ORB")
    assert q.scan["score"] == 80.0 and q.qty == 1
    assert bot2.risk.state.for_strategy("F").entries_today == 1
    assert [w.symbol for w in bot2.f.watch] == ["TSLA"] and bot2.f.ranked_at == bot.f.ranked_at
    assert bot2.f.next_check == et(9, 50) and data.calls["scan"] == [data.calls["scan"][0]]
    assert any("Strategy F watch (restored)" in r.getMessage() for r in caplog.records)
    # no state file: the TSLA pair at the broker is adopted as F with width-scaled exits, even when the
    # ticker is no longer in the universe
    bot.journal.state_path.unlink()
    bot3 = make_bot_f(bot.journal.dir, monkeypatch, data, broker=bot.broker)
    bot3.f.universe = {"index": "SPX", "tiers": {"A": ["AMZN"]}}
    bot3.run = lambda now: None
    bot3.start()
    a = bot3.pm.positions["F:TSLA"]
    assert (a.strategy, a.setup, a.root, a.width, a.qty, a.entry_credit) == ("F", "ADOPTED", "TSLA", 5.0, 1, 0.80)
    assert (a.profit_target, a.stop_loss, a.overrides["TRAIL_AMOUNT"]) == (0.30, 0.50, 0.30)
    adopted = events(bot3, "ADOPTED")                                       # bot2 (from state) + bot3 (untracked)
    assert [e["position"]["setup"] for e in adopted] == ["STOCK_ORB", "ADOPTED"] and {e["strategy"] for e in adopted} == {"F"}
    assert bot3.f.watch == [] and bot3.f.ranked_at is None


def test_personas_without_f_ignore_stock_legs(tmp_path, monkeypatch, data):
    bot = make_bot_f(tmp_path, monkeypatch, data, strategies=("A", "B"))
    assert bot.f is None
    bot.broker.add_spread_position("P", 417.5, 412.5, 1, 1.85, 1.05, EXP, root="TSLA")
    bot.broker.add_spread_position("P", 7510.0, 7505.0, 1, 8.1, 5.05)
    legs = bot.broker.get_positions()
    assert {l["symbol"][:4] for l in bot.bot_legs(legs)} == {"SPXW"} and len(bot.foreign_legs(legs)) == 2
    bot.broker.positions = {k: v for k, v in bot.broker.positions.items() if k.startswith("TSLA")}
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 30))
    bot.run = lambda now: None
    bot.start()
    assert bot.pm.positions == {} and "f" not in json.loads(bot.journal.state_path.read_text())
    assert bot.spread_prices() == {}
    bot.end_of_day(et(15, 30))
    assert len(option_legs(bot.broker.get_positions(), {"TSLA"})) == 2      # untouched: not this persona's


def test_f_configured_but_not_built_still_adopts_marks_and_closes_its_stock_legs(tmp_path, monkeypatch, data, caplog):
    # Day 2 after a CLOSE_FAILED: the universe file is broken, so StockOrbTrader never builds -- the
    # TSLA legs are still this persona's and must be re-adopted, marked, force-closed and reconciled.
    caplog.set_level("INFO")
    monkeypatch.setattr(config, "F_UNIVERSE_PATH", "data/does_not_exist.json")
    broker = make_broker()
    broker.add_spread_position("P", 417.5, 412.5, 1, 1.85, 1.05, EXP, root="TSLA")
    bot = bare_bot_f(tmp_path, monkeypatch, broker)
    assert bot.f is None and any("Strategy F disabled for today" in r.getMessage() for r in caplog.records)
    assert {occ_root(l["symbol"]) for l in bot.bot_legs(broker.get_positions())} == {"TSLA"}
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 30))
    bot.run = lambda now: None
    bot.start()
    a = bot.pm.positions["F:TSLA"]
    assert (a.strategy, a.setup, a.width, a.profit_target, a.stop_loss) == ("F", "ADOPTED", 5.0, 0.30, 0.50)
    assert bot.spread_prices() == {"F:TSLA": 0.80}
    bot.end_of_day(et(15, 30))
    assert bot.pm.positions == {} and option_legs(broker.get_positions()) == []
    assert events(bot, "END")[-1]["mismatches"] == [] and events(bot, "EXIT")[-1]["strategy"] == "F"


def test_longer_dated_stock_spreads_belong_to_someone_else(bot, data, monkeypatch, caplog):
    # The 45DTE bot (or a manual trade) in the same account: its AAPL 52-DTE spread, and anything past
    # F_MAX_DTE, is neither adopted as F, nor force-closed at 15:30, nor reported as a mismatch.
    caplog.set_level("INFO")
    b = bot.broker
    b.add_spread_position("P", 417.5, 412.5, 1, 1.85, 1.05, EXP, root="TSLA")                              # 2 DTE: F's
    b.add_spread_position("P", 220.0, 215.0, 1, 3.10, 1.70, TODAY + timedelta(days=52), root="AAPL")       # 45DTE bot's
    b.add_spread_position("P", 417.5, 412.5, 1, 1.85, 1.05, TODAY + timedelta(days=config.F_MAX_DTE + 1), root="AMZN")
    positions = b.get_positions()
    assert {occ_root(l["symbol"]) for l in bot.bot_legs(positions)} == {"TSLA"}
    assert {occ_root(l["symbol"]) for l in bot.foreign_legs(positions)} == {"AAPL", "AMZN"}
    assert bot.f_owns_expiration(TODAY) and bot.f_owns_expiration((TODAY + timedelta(days=7)).isoformat())
    assert not bot.f_owns_expiration(TODAY + timedelta(days=8)) and not bot.f_owns_expiration(None)
    monkeypatch.setattr(scheduler, "now_et", lambda: et(10, 30))
    bot.run = lambda now: None
    bot.start()
    assert set(bot.pm.positions) == {"F:TSLA"}
    assert any(r.getMessage().startswith("Ignoring 4 option leg(s) in the account") for r in caplog.records)
    monkeypatch.setattr(config, "CLOSE_RETRY_SEC", 0)
    bot.end_of_day(et(15, 30))
    assert bot.pm.positions == {} and {occ_root(l["symbol"]) for l in option_legs(b.get_positions())} == {"AAPL", "AMZN"}
    assert events(bot, "END")[-1]["mismatches"] == []
    assert not any("CLOSE_FAILED" in r.getMessage() for r in caplog.records)
    assert any("Ignored option legs not owned by this persona" in r.getMessage() for r in caplog.records)


def test_a_broken_stock_orb_import_costs_only_the_f_persona_its_f(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setitem(sys.modules, "stock_orb", None)                     # `from stock_orb import ...` -> ImportError
    bot = bare_bot_f(tmp_path, monkeypatch, make_broker())
    assert bot.f is None and any("Strategy F disabled for today" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "C", "D", "E"))
    assert scheduler.Bot(dry_run=False, once=True).f is None                 # never even imports it


# ------------------------------------------------------------------------------ phases / reports


def test_f_frames_refresh_in_every_phase_and_after_a_self_disable(bot, data, monkeypatch, wind_down_times):
    enter_f(bot, data)                                                      # 09:55:10: TSLA frame cached through 09:50
    assert list(bot.f.candles_by_key()["F:TSLA"].index.strftime("%H:%M"))[-1] == "09:50"
    bot.refresh_levels = lambda now: None
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 30), [], minutes=2))
    monkeypatch.setattr(config, "F_PRICE_EVERY_SEC", 0)
    steps, seen = [], []
    monkeypatch.setattr(bot.f, "step", lambda now: steps.append(now))
    on_tick = bot.pm.on_tick
    monkeypatch.setattr(bot.pm, "on_tick", lambda now, prices, candles, by_key=None: seen.append(by_key) or on_tick(now, prices, candles, by_key))
    bot.tick(et(15, 5))                                                     # NO_NEW_ENTRIES: step() does not run ...
    assert steps == [] and list(seen[-1]["F:TSLA"].index.strftime("%H:%M"))[-1] == "09:55"   # ... the frame still moved on
    bot.f.disable("test", et(15, 5, 1))
    data.set_frames(today_closes(BREAK, MOMENTUM, HOLD, HOLD))              # the 10:00 candle is available now
    bars_before = len(data.calls["bars"])
    bot.tick(et(15, 6))                                                     # paced: nothing new before the next boundary
    assert len(data.calls["bars"]) == bars_before and list(seen[-1]["F:TSLA"].index.strftime("%H:%M"))[-1] == "09:55"
    bot.tick(et(15, 10, 1))                                                 # disabled F still feeds its open position
    assert list(seen[-1]["F:TSLA"].index.strftime("%H:%M"))[-1] == "10:00" and "F:TSLA" in bot.pm.positions
    assert events(bot, "F_ERROR") == []

def test_news_blackout_and_no_new_entries_phase_hold_f(bot, data, monkeypatch, wind_down_times):
    calls = []
    monkeypatch.setattr(bot.f, "step", lambda now: calls.append(now))
    bot.refresh_levels = lambda now: None
    monkeypatch.setattr(scheduler.market_data, "get_candles", lambda *a, **k: make_candles(et(9, 30), [], minutes=2))
    bot.tick(et(15, 5))                                                     # NO_NEW_ENTRIES
    assert calls == []
    bot.tick(et(10, 5))                                                     # ORB_ONLY: runs after manage()
    assert calls == [et(10, 5)]
    monkeypatch.setattr(config, "NEWS_EVENTS", {TODAY.isoformat(): [et(9, 55).time()]})
    bot.news_blackout_was_active = True
    bot.f.watch.append(WatchItem("TSLA", "A", 1, BULLISH, 80.0, 82.0, OR_HIGH, 429.95, {}, OrbSetup(OR_HIGH, 429.95),
                                 reentry_from=et(9, 58)))
    bot.f.watch[0].setup.state = OrbSetup.DONE
    bot.tick(et(10, 1))                                                     # blackout just cleared: setups reset ...
    assert bot.f.watch[0].setup.state == OrbSetup.WAITING and bot.f.watch[0].reentry_from is None   # ... watch too


def test_report_start_has_f_line_and_prewarm_runs_pre_open(bot, data, caplog):
    caplog.set_level("INFO")
    bot.report_start(et(9, 0), bot.broker.get_account(), [])
    f_line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Strategy F:"))
    assert "universe 4 names (A:2, B:1, C:1)" in f_line and "rank at 09:45 +45s" in f_line
    assert "min score 60" in f_line and "watch top 5" in f_line and "max open 3" in f_line and "limits 3/day 7 losses" in f_line
    assert "exits 6%/10% of width (floor $0.05)" in f_line and "DTE 0-7" in f_line
    bot.f.step(et(9, 19, 59))
    assert data.calls["bars"] == []
    bot.f.step(et(9, 20))
    assert bot.f.executor.submitted == ["_prewarm_job"] and data.calls["bars"][0][0] == ["SPX"] + STOCKS
    bot.f.step(et(9, 21))
    assert len(data.calls["bars"]) == 1                                     # once per day
    bot.f.disable("test", et(9, 22))
    bot.report_start(et(9, 22), bot.broker.get_account(), [])
    assert any("Strategy F DISABLED: test" in r.getMessage() for r in caplog.records)


def test_claude_persona_has_no_f(tmp_path, monkeypatch, data, caplog):
    caplog.set_level("INFO")
    bot = make_bot_f(tmp_path, monkeypatch, data, strategies=("A", "B", "C", "D", "E"))
    assert bot.f is None
    bot.report_start(et(9, 0), bot.broker.get_account(), [])
    assert not any(r.getMessage().startswith("Strategy F") for r in caplog.records)
    assert set(bot.risk.state.strategies) == {"A", "B", "C", "D", "E"}


def test_f_entry_is_refused_when_the_quote_spread_is_wider_than_half_the_stop(bot, data, monkeypatch):
    # 2026-10-07: the live gate (not the pre-fix pin): quote spread 0.40 on a $5 vertical vs a stop of 0.40
    monkeypatch.setattr(config, "F_MAX_QUOTE_SPREAD_FRAC", 0.12)           # old width gate would pass 0.40 (<= 0.60) ...
    monkeypatch.setattr(config, "F_MAX_QUOTE_SPREAD_STOP_RATIO", 0.5)       # ... the stop ratio does not (0.40 > 0.5 x 0.40)
    rank(bot, data)
    out = bot.f.enter(et(10, 0), signal())
    assert out.startswith("skip: BID_ASK_BLOWN") and "quote spread 0.40 vs stop 0.40" in out


def test_the_entry_hold_runs_from_the_fill_not_from_the_signal_tick(bot, data, monkeypatch):
    import types
    clock = [1000.0]
    monkeypatch.setattr(stock_orb, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None))
    original = bot.work_entry

    def slow_fill(order, *a, **k):
        clock[0] += 37.0                                               # the order worked for 37 s before it filled
        return original(order, *a, **k)
    monkeypatch.setattr(bot, "work_entry", slow_fill)
    enter_f(bot, data)
    pos = bot.pm.positions["F:TSLA"]
    assert pos.entry_time == et(9, 55, 10) + timedelta(seconds=37)


def test_f_is_refused_by_the_total_cap_too(bot, data, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", 5)
    rank(bot, data)
    for strat in ("A", "A", "A", "B", "B"):
        bot.risk.record_trade(strat, 10.0)
    out = bot.f.enter(et(10, 0), signal())
    assert out.startswith("skip:") and "TOTAL_MAX_TRADES_PER_DAY" in out and "F:TSLA" not in bot.pm.positions


def test_f_is_refused_while_the_persona_already_has_two_trades_open(bot, data, monkeypatch):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 2)
    rank(bot, data)
    bot.pm.open(f_spread("AMZN"))
    bot.pm.open(f_spread("META"))
    out = bot.f.enter(et(10, 0), signal())
    assert out.startswith("skip:") and "MAX_OPEN_POSITIONS (2/2)" in out and "F:TSLA" not in bot.pm.positions


# ----------------------------------------------------------------------------------- waiting out a transient block (2026-10-08)

def _two_open(bot, monkeypatch, cap=2):
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", cap)
    bot.pm.open(f_spread("AMZN"))
    bot.pm.open(f_spread("META"))


def test_a_confirmed_f_signal_waits_for_a_slot_instead_of_being_dropped(bot, data, monkeypatch, caplog):
    caplog.set_level("INFO")
    rank(bot, data)
    _two_open(bot, monkeypatch)
    bot.f.pending += [signal("TSLA", 1)]
    for sec in (10, 11, 12):
        bot.f.step(et(9, 55, sec))                                    # the cap is full on every one of these ticks
    assert "F:TSLA" not in bot.pm.positions and [s.symbol for s in bot.f.pending] == ["TSLA"]
    assert events(bot, "F_SKIPPED") == []
    waiting = [r for r in caplog.records if "confirmed signal(s) waiting" in r.getMessage()]
    assert len(waiting) == 1 and "MAX_OPEN_POSITIONS (2/2)" in waiting[0].getMessage()      # said once, not every tick
    bot.pm.positions.pop("F:META")                                    # a slot opens
    bot.f.step(et(9, 55, 20))
    assert "F:TSLA" in bot.pm.positions and bot.f.pending == []


def test_an_f_signal_that_waits_too_long_still_expires(bot, data, monkeypatch):
    rank(bot, data)
    _two_open(bot, monkeypatch)
    bot.f.pending += [signal("TSLA", 1)]
    bot.f.step(et(9, 55, 10))
    bot.f.step(et(9, 50) + timedelta(minutes=5 + config.F_SIGNAL_TTL_MIN, seconds=1))      # candle end + TTL has passed
    assert bot.f.pending == []
    assert [e["reason"] for e in events(bot, "F_SKIPPED")] == ["SIGNAL_EXPIRED"]


def test_f_signals_wait_through_a_failing_close_and_stale_prices_too(bot, data, monkeypatch):
    rank(bot, data)
    bot.f.pending += [signal("TSLA", 1)]
    monkeypatch.setattr(bot.pm, "any_close_blocked", lambda: True)
    bot.f.step(et(9, 55, 10))
    assert [s.symbol for s in bot.f.pending] == ["TSLA"] and "F:TSLA" not in bot.pm.positions
    monkeypatch.setattr(bot.pm, "any_close_blocked", lambda: False)
    bot._data_stale = True
    bot.f.step(et(9, 55, 11))
    assert [s.symbol for s in bot.f.pending] == ["TSLA"] and bot.f._waiting_on.startswith("DATA_STALE")
    bot._data_stale = False
    bot.f.step(et(9, 55, 12))
    assert "F:TSLA" in bot.pm.positions and bot.f._waiting_on is None


def test_a_day_limit_still_drops_the_f_signals_at_once(bot, data, monkeypatch):
    rank(bot, data)
    monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", 2)
    for _ in range(3):
        bot.risk.record_trade("F", 10.0)                              # F's 3 trades a day are used
    bot.f.pending += [signal("TSLA", 1), signal("PFE", 2)]
    bot.f.step(et(9, 55, 10))
    assert bot.f.pending == [] and {e["reason"] for e in events(bot, "F_SKIPPED")} == {"F: MAX_TRADES_PER_DAY (3)"}


def test_an_f_spread_that_shares_a_strike_with_an_open_f_spread_on_the_same_ticker_is_refused(bot, data):
    rank(bot, data)
    other = f_spread("TSLA")                                          # 417.5/412.5P already open on TSLA
    other.long_strike, other.short_strike = 412.5, 417.5
    bot.pm.open(other)
    assert bot.pm.find_collision("TSLA", EXP, "P", 422.5, 417.5) == "F:TSLA"                   # new short = open short
    assert bot.pm.find_collision("TSLA", EXP, "P", 412.5, 407.5) == "F:TSLA"                   # new short = open long
    assert bot.pm.find_collision("TSLA", EXP, "P", 407.5, 402.5) is None
    assert bot.pm.collision_reason("F:TSLA", "P", 422.5, 417.5) == \
        "STRIKE_COLLISION 422.5/417.5P shares a strike with 417.5/412.5P open under F:TSLA"


def test_f_entry_refuses_an_identical_or_overlapping_spread_on_the_same_ticker(bot, data):
    rank(bot, data)
    bot.pm.open(f_spread("TSLA"))                                      # 417.5/412.5P, exactly what the strike walk picks
    out = bot.f.enter(et(10, 0), signal())
    assert out.startswith("skip:") and "STRIKE_COLLISION 417.5/412.5P already open under F:TSLA" in out
    bot.pm.positions.pop("F:TSLA")
    shifted = f_spread("TSLA")
    shifted.short_strike, shifted.long_strike = 422.5, 417.5           # its long leg is the new spread's short leg
    bot.pm.open(shifted)
    out = bot.f.enter(et(10, 0, 1), signal())
    assert out.startswith("skip:") and "STRIKE_COLLISION 417.5/412.5P shares a strike with 422.5/417.5P open under F:TSLA" in out
    assert len(bot.pm.positions) == 1


# ---- review follow-ups (2026-10-08): journal the wait, supersede a waiting signal, reset the waiting note

def test_waiting_is_journaled_once_per_block_and_expiry_resets_the_note(bot, data, monkeypatch):
    rank(bot, data)
    _two_open(bot, monkeypatch)
    bot.f.pending += [signal("TSLA", 1)]
    for sec in (10, 11, 12):
        bot.f.step(et(9, 55, sec))
    waiting = events(bot, "F_WAITING")
    assert len(waiting) == 1 and waiting[0]["reason"] == "MAX_OPEN_POSITIONS (2/2)" and waiting[0]["tickers"] == ["TSLA"]
    bot.f.step(et(9, 50) + timedelta(minutes=5 + config.F_SIGNAL_TTL_MIN, seconds=1))
    assert bot.f.pending == [] and bot.f._waiting_on is None                # nothing waits any more: the next block is announced again
    fresh = signal("PFE", 2)
    fresh.candle_time = et(10, 20)                                          # a signal that has not expired yet
    bot.f.pending.append(fresh)
    bot.f.step(et(10, 30))
    assert len(events(bot, "F_WAITING")) == 2


def test_a_newer_break_on_the_same_ticker_replaces_the_waiting_signal(bot, data, monkeypatch):
    rank(bot, data)
    _two_open(bot, monkeypatch)
    old = signal("TSLA", 1)
    bot.f.pending.append(old)
    newer = signal("TSLA", 1)
    newer.candle_time = et(10, 5)
    newer.direction = "BEARISH"
    monkeypatch.setattr(bot.f, "_confirmation", lambda sig, now, check_volume, refresh=False: ({"checked_at": now.isoformat()}, []))
    bot.f._confirm_and_queue(newer, et(10, 10))
    assert bot.f.pending == [newer]
    skipped = events(bot, "F_SKIPPED")
    assert len(skipped) == 1 and skipped[0]["ticker"] == "TSLA" and skipped[0]["reason"].startswith("SUPERSEDED by the MOMENTUM break of 10:05")
    assert old.key in bot.acted
    bot.f._confirm_and_queue(newer, et(10, 11))                             # the very same signal again: left alone
    assert bot.f.pending == [newer] and len(events(bot, "F_SKIPPED")) == 1


def test_f_does_not_enter_after_the_daily_loss_limit_when_nothing_is_exempt(bot, data, monkeypatch):
    """2026-10-15 (Schwab 0DTE): F follows the daily loss limit. The same scenario as the exempt case above, with the exemption off."""
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    rm = bot.risk
    rm.record_trade("B", -1_000.0)
    assert rm.trading_allowed("F", 9_000.0)[0] is False
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" not in bot.pm.positions and not any(k.startswith("F:") for k in bot.pm.positions)


def test_f_sees_the_bots_open_loss_on_the_bot_basis(bot, data, monkeypatch):
    """2026-10-15 (Schwab): F's own entry check passes the bot's open marks, so an open trade down past the limit stops a new F entry."""
    monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "bot", raising=False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.01)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ())
    bot.pm.open(f_spread("AMZN", current=4.20))                                # -$340 open on a $10,000 day: the limit is -$100
    assert bot.risk.state.realized_pnl == 0.0
    rank(bot, data)
    bot.f.step(et(9, 55, 10))
    assert "F:TSLA" not in bot.pm.positions


def test_f_fetches_a_chain_wide_enough_for_the_coarse_cap_and_tells_the_strike_walk_the_grid(bot, data, monkeypatch):
    """2026-10-16: stock_orb must ask the broker for strikes out to the WIDER cap (a 2.5x EM strike is useless if it was never fetched)
    and pass the strike increment it already measured, so the walk applies the coarse-grid cap from the real grid."""
    import strategy
    chain_calls, select_calls = [], []
    real_chain, real_select = bot.broker.get_option_chain, strategy.select_strikes_f

    def chain_spy(underlying, *a, **k):
        if underlying == "TSLA":
            chain_calls.append((a, k))
        return real_chain(underlying, *a, **k)

    def select_spy(*a, **k):
        select_calls.append((a, k))
        return real_select(*a, **k)

    monkeypatch.setattr(bot.broker, "get_option_chain", chain_spy)
    monkeypatch.setattr(strategy, "select_strikes_f", select_spy)
    enter_f(bot, data)
    assert len(select_calls) == 1
    (spot, right, width, em, _chain), kw = select_calls[0]
    assert kw.get("increment") and kw["increment"] > 0                                   # the measured grid is passed, not re-derived
    span = strategy.f_chain_span(em, width)
    assert span == pytest.approx(2.5 * em + 2 * width)
    assert any(len(a) >= 4 and a[2] == pytest.approx(spot - span) and a[3] == pytest.approx(spot + span) for a, k in chain_calls), chain_calls


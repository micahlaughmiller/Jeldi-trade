import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from journal import TABLE_WIDTH, TRADE_FIELDS, format_close_card, format_day_table, format_entry_card
from position_manager import OpenSpread

ET = ZoneInfo("US/Eastern")


def _spread(qty: int = 2, strategy: str = "A") -> OpenSpread:
    credit, target, stop = (3.10, 0.30, 0.30) if strategy == "A" else (1.50, 0.30, 0.50)
    return OpenSpread(strategy=strategy, direction="BULLISH", setup="ORB", right="P", root="SPXW",
                      expiration=date(2026, 9, 18), short_strike=7510, long_strike=7505, width=5, qty=qty,
                      entry_credit=credit, entry_time=datetime(2026, 9, 18, 10, 14, 32, tzinfo=ET),
                      current_price=credit, best_price=credit, profit_target=target, stop_loss=stop)


def _row(qty: int = 2, remaining_total: int = 2, pnl: float = 60.0, strategy: str = "A") -> dict:
    return {"date": "2026-09-18", "strategy": strategy, "entry_time": datetime(2026, 9, 18, 10, 14, 32, tzinfo=ET),
            "exit_time": "2026-09-18T10:41:06-04:00", "direction": "BULLISH", "setup": "ORB", "right": "P",
            "short_strike": 7510, "long_strike": 7505, "width": 5, "qty": qty, "entry_credit": 3.10,
            "exit_price": 2.80, "pnl": pnl, "exit_reason": "PROFIT_TARGET", "runner": "n",
            "position_closed": True, "position_pnl": pnl, "total_qty": remaining_total, "root": "SPXW"}


def test_trade_fields_have_strategy_second():
    assert TRADE_FIELDS[:3] == ["date", "strategy", "entry_time"]


def test_entry_card_rows():
    card = format_entry_card(_spread(), 1)
    lines = card.splitlines()
    assert lines[1] == " TRADE #1   STRATEGY A - ITM"
    assert any(l.startswith(" Strategy") and l.endswith("A - ITM") for l in lines)
    assert any(l.startswith(" Setup") and l.endswith("ORB") for l in lines)
    assert any("Sold" in l and "7510 P" in l for l in lines)
    assert any("Bought" in l and "7505 P" in l for l in lines)
    assert any("Time" in l and "10:14:32 ET" in l for l in lines)
    assert any("Credit" in l and "$3.10 per spread" in l and "+$620.00 total" in l for l in lines)
    assert any("Stop" in l and "$3.40" in l for l in lines)
    assert any("Target" in l and "$2.80" in l for l in lines)


def test_entry_card_strategy_b_uses_its_own_exits():
    card = format_entry_card(_spread(qty=1, strategy="B"), 3)
    lines = card.splitlines()
    assert lines[1] == " TRADE #3   STRATEGY B - OTM (expected move)"
    assert any("Stop" in l and "$2.00" in l for l in lines)
    assert any("Target" in l and "$1.20" in l for l in lines)


def test_close_card_full_close():
    card = format_close_card(_row(), 1, day_pnl={"A": 60.0, "B": -20.0}, closed_today=2, remaining=0)
    assert " TRADE #1   STRATEGY A - ITM" in card
    assert " CLOSE" in card
    assert "Bought back     2 of 2" in card
    assert "Debit           $2.80 per spread   (-$560.00 total)" in card
    assert "Reason          PROFIT_TARGET" in card
    assert "P/L this trade  +$60.00" in card
    assert "P/L today A     +$60.00" in card
    assert "P/L today B     -$20.00" in card
    assert "P/L today total +$40.00   (2 trades closed)" in card
    assert "still open" not in card


def test_close_card_partial_runner_fill():
    row = _row(qty=1, remaining_total=2, pnl=30.0, strategy="B")
    row["position_closed"] = False
    card = format_close_card(row, 3, day_pnl={"A": 100.0, "B": 30.0}, closed_today=1, remaining=1)
    assert " TRADE #3   STRATEGY B - OTM (expected move)" in card
    assert "Bought back     1 of 2   (1 still open)" in card
    assert "P/L this fill   +$30.00" in card
    assert "P/L position    +$30.00 so far" in card
    assert "P/L today total +$130.00   (1 trade closed)" in card


def test_day_table_lists_each_trade_with_strategy_and_subtotals():
    rows = [_row(pnl=60.0), dict(_row(pnl=-40.0), exit_reason="STOP_LOSS"), _row(pnl=25.0, strategy="B")]
    table = format_day_table(rows, 45.0)
    lines = table.splitlines()
    assert " TRADES TODAY" in lines[1]
    assert "STRAT" in lines[3]
    body = [l for l in lines if l.startswith((" 1 ", " 2 ", " 3 "))]
    assert len(body) == 3 and "PROFIT_TARGET" in body[0] and "STOP_LOSS" in body[1]
    assert body[0].split()[1] == "A" and body[2].split()[1] == "B"
    assert body[0].rstrip().endswith("+$60.00") and body[1].rstrip().endswith("-$40.00")
    sub_a = next(l for l in lines if l.startswith(" A - ITM"))
    sub_b = next(l for l in lines if l.startswith(" B - OTM (expected move)"))
    assert "trades 2" in sub_a and "wins 1" in sub_a and "losses 1" in sub_a and "win rate 50%" in sub_a
    assert sub_a.rstrip().endswith("+$20.00")
    assert "trades 1" in sub_b and "win rate 100%" in sub_b and sub_b.rstrip().endswith("+$25.00")
    assert lines[-2].startswith(" P/L TODAY (A+B)") and lines[-2].rstrip().endswith("+$45.00")


def test_day_table_empty():
    table = format_day_table([], 0.0)
    assert "(no trades closed today)" in table
    assert "win rate n/a" in table


def test_trades_csv_with_old_header_is_parked(tmp_path):
    from journal import TRADE_FIELDS, Journal
    j = Journal(tmp_path, "TEST")
    old_fields = TRADE_FIELDS[:16]
    with j.trades_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=old_fields)
        w.writeheader()
        w.writerow({k: "x" for k in old_fields})
    j.trade({"date": "2026-09-22", "strategy": "A", "exit_reason": "STOP_LOSS", "trigger_price": 3.56})
    parked = tmp_path / "trades_test_until_2026-09-22.csv"
    assert parked.exists() and parked.read_text().splitlines()[0] == ",".join(old_fields)
    lines = j.trades_path.read_text().splitlines()
    assert lines[0] == ",".join(TRADE_FIELDS) and len(lines) == 2 and "3.56" in lines[1]
    # a second trade appends to the new file without parking again
    j.trade({"date": "2026-09-22", "strategy": "B", "exit_reason": "PROFIT_TARGET"})
    assert len(j.trades_path.read_text().splitlines()) == 3
    assert len(list(tmp_path.glob("trades_test_until_*.csv"))) == 1


# ------------------------------------------------------------ Strategy F: ticker on cards, TICKER column

def _f_spread() -> OpenSpread:
    return OpenSpread(strategy="F", direction="BULLISH", setup="STOCK_ORB", right="P", root="TSLA",
                      expiration=date(2026, 9, 19), short_strike=417.5, long_strike=412.5, width=5.0, qty=1,
                      entry_credit=0.80, entry_time=datetime(2026, 9, 18, 9, 55, 12, tzinfo=ET), current_price=0.80,
                      best_price=0.80, profit_target=0.30, stop_loss=0.50, entry_mid=0.80, entry_bid=0.60, entry_ask=1.00,
                      overrides={"PROFIT_LOCK_ARM": 0.15, "PROFIT_LOCK_GIVEBACK": 0.10, "TRAIL_AMOUNT": 0.30},
                      scan={"score": 81.5, "score_with_tier": 83.5, "tier": "A", "rank": 1, "scanner_direction": "BULLISH",
                            "direction_match": True, "trigger": "MOMENTUM", "expected_move": 20.0, "em_straddle": 17.0,
                            "rvol": 1.53, "increment": 2.5,
                            "confirm": {"remaining_em": 14.9, "candle_volume": 750.0, "normal_volume": 500.0}})


def _f_row() -> dict:
    return dict(_row(qty=1, remaining_total=1, pnl=-52.0, strategy="F"), root="TSLA", ticker="TSLA", setup="STOCK_ORB",
                short_strike=417.5, long_strike=412.5, entry_credit=0.80, exit_price=1.32, exit_reason="STOP_LOSS",
                expiration="2026-09-19", mfe=0.10, mae=0.50, entry_slippage=0.0, exit_slippage=0.02, scanner_score=81.5)


def test_trade_fields_gain_strategy_neutral_columns():
    assert TRADE_FIELDS[-5:] == ["ticker", "expiration", "mfe", "mae", "scanner_score"]
    assert TRADE_FIELDS[:20] == TRADE_FIELDS[:16] + ["trigger_price", "exit_slippage", "entry_mid", "entry_slippage"]


def test_f_entry_card_appends_ticker_and_scan_rows():
    lines = format_entry_card(_f_spread(), 2).splitlines()
    assert lines[1] == " TRADE #2   STRATEGY F - STOCK ORB TSLA"
    assert any(l.startswith(" Strategy") and l.endswith("F - STOCK ORB TSLA") for l in lines)
    assert any("Underlying" in l and "TSLA  expires 2026-09-19" in l for l in lines)
    assert any(l.strip().startswith("Score") and "81.5 raw / 83.5 tier A" in l and "rank 1" in l and "(match)" in l for l in lines)
    assert any(l.strip().startswith("Trigger") and l.endswith("MOMENTUM") for l in lines)
    assert any(l.strip().startswith("Exp move") and "straddle $17.00" in l and "remaining $14.90" in l for l in lines)
    assert any(l.strip().startswith("RVOL") and "1.53x" in l and "750 vs normal 500" in l for l in lines)
    assert any(l.strip().startswith("Width") and "$5" in l and "strike step 2.5" in l for l in lines)
    assert any(l.strip().startswith("Quote") and "bid 0.60 / ask 1.00 / mid 0.80" in l for l in lines)
    assert any("Stop" in l and "$1.30" in l for l in lines) and any("Target" in l and "$0.50" in l for l in lines)
    assert lines[-1] == "=" * 70
    # A/B cards do not change: no ticker, no scan rows
    a_lines = format_entry_card(_spread(), 1).splitlines()
    assert a_lines[1] == " TRADE #1   STRATEGY A - ITM" and not any("Score" in l or "Quote" in l for l in a_lines)


def test_f_close_card_shows_mfe_mae_slippage_and_pnl_today_f(monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "F"))
    card = format_close_card(_f_row(), 1, day_pnl={"A": 60.0, "B": 0.0, "F": -52.0}, closed_today=2, remaining=0)
    assert " TRADE #1   STRATEGY F - STOCK ORB TSLA" in card
    assert "Underlying      TSLA  expires 2026-09-19" in card            # the row's expiration, not the close date
    assert "expires 2026-09-18" in format_close_card(_row(), 1, {"A": 60.0, "B": 0.0}, 1, 0)   # 0DTE: same day
    assert "MFE / MAE       +0.10 / -0.50" in card and "Slippage        entry +0.00   exit +0.02" in card
    assert "P/L today F     -$52.00" in card and "P/L today total +$8.00   (2 trades closed)" in card
    a_card = format_close_card(_row(), 1, day_pnl={"A": 60.0, "B": 0.0, "F": 0.0}, closed_today=1, remaining=0)
    assert "MFE / MAE" not in a_card and "P/L today F     +$0.00" in a_card


def test_day_table_ticker_column_and_f_subtotal(monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B", "F"))
    rows = [_row(pnl=60.0), _f_row(), _row(pnl=25.0, strategy="B")]
    lines = format_day_table(rows, 33.0).splitlines()
    assert lines[3].startswith(" #  STRAT TICKER ENTRY") and len(lines[3]) == TABLE_WIDTH
    body = [l for l in lines if l.startswith((" 1 ", " 2 ", " 3 "))]
    assert body[0].split()[:3] == ["1", "A", "10:14:32"]                     # blank TICKER for SPX rows
    assert body[1].split()[:4] == ["2", "F", "TSLA", "10:14:32"] and "417.5P" in body[1] and "STOP_LOSS" in body[1]
    assert body[2].split()[1:3] == ["B", "10:14:32"]
    sub_f = next(l for l in lines if l.startswith(" F - STOCK ORB"))
    assert "trades 1" in sub_f and "losses 1" in sub_f and sub_f.rstrip().endswith("-$52.00") and len(sub_f) == TABLE_WIDTH
    assert lines[-2].startswith(" P/L TODAY (A+B+F)") and lines[-2].rstrip().endswith("+$33.00") and len(lines[-2]) == TABLE_WIDTH

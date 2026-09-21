from datetime import date, datetime
from zoneinfo import ZoneInfo

from journal import TRADE_FIELDS, format_close_card, format_day_table, format_entry_card
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

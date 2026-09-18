"""Persona-scoped JSONL event log, trades.csv, and state persistence."""

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

TRADE_FIELDS = [
    "date", "entry_time", "exit_time", "direction", "setup", "right",
    "short_strike", "long_strike", "width", "qty", "entry_credit", "exit_price",
    "pnl", "exit_reason", "runner",
]

RULE = "=" * 70
THIN = "-" * 70


def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _clock(dt: datetime | str | None) -> str:
    if dt is None:
        return "?"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%H:%M:%S ET")


def _row(label: str, value: Any, indent: int = 1) -> str:
    return f"{' ' * indent}{label:<16}{value}"


def _header(trade_no: int, setup: str, direction: str, right: str, root: str, expiration: date | str, qty: int) -> list[str]:
    kind = "PUT CREDIT SPREAD" if right == "P" else "CALL CREDIT SPREAD"
    return [
        RULE,
        f" TRADE #{trade_no}",
        THIN,
        _row("Setup", setup),
        _row("Direction", direction),
        _row("Spread", kind),
        _row("Underlying", f"{root}  expires {expiration}"),
        _row("Contracts", qty),
    ]


def _entry_rows(entry_time: Any, right: str, short_strike: float, long_strike: float,
                credit: float, qty: int) -> list[str]:
    return [
        THIN,
        " ENTRY",
        _row("Time", _clock(entry_time), 3),
        _row("Sold", f"{short_strike:g} {right}", 3),
        _row("Bought", f"{long_strike:g} {right}", 3),
        _row("Credit", f"${credit:.2f} per spread   ({_money(credit * 100 * qty)} total)", 3),
    ]


def format_entry_card(spread: Any, trade_no: int) -> str:
    lines = _header(trade_no, spread.setup, spread.direction, spread.right, spread.root, spread.expiration, spread.qty)
    lines += _entry_rows(spread.entry_time, spread.right, spread.short_strike, spread.long_strike,
                         spread.entry_credit, spread.qty)
    lines += [
        _row("Stop", f"${spread.stop_price:.2f}  (spread price rises to)", 3),
        _row("Target", f"${spread.target_price:.2f}  (spread price falls to)", 3),
        RULE,
    ]
    return "\n".join(lines)


def format_close_card(row: dict, trade_no: int, day_pnl: float, closed_today: int, remaining: int) -> str:
    total_qty = int(row.get("total_qty") or row["qty"] + remaining)
    lines = _header(trade_no, row["setup"], row["direction"], row["right"], row.get("root", "SPXW"), row["date"], total_qty)
    lines += _entry_rows(row["entry_time"], row["right"], row["short_strike"], row["long_strike"],
                         row["entry_credit"], total_qty)
    lines += [
        THIN,
        " CLOSE",
        _row("Time", _clock(row["exit_time"]), 3),
        _row("Bought back", f"{row['qty']} of {total_qty}" + (f"   ({remaining} still open)" if remaining else ""), 3),
        _row("Debit", f"${row['exit_price']:.2f} per spread   ({_money(-row['exit_price'] * 100 * row['qty'])} total)", 3),
        _row("Reason", row["exit_reason"], 3),
        THIN,
        _row("P/L this fill" if remaining else "P/L this trade", _money(row["pnl"])),
    ]
    if remaining:
        lines.append(_row("P/L position", f"{_money(row['position_pnl'])} so far"))
    lines += [
        _row("P/L today", f"{_money(day_pnl)}   ({closed_today} trade{'s' if closed_today != 1 else ''} closed)"),
        RULE,
    ]
    return "\n".join(lines)


def format_day_table(closed_trades: list[dict], day_pnl: float) -> str:
    """End-of-day grid, one row per closed trade."""
    head = f" {'#':<3}{'ENTRY':<10}{'SETUP':<10}{'DIR':<9}{'SOLD':<8}{'BOUGHT':<8}{'QTY':<5}{'CREDIT':<8}{'CLOSE':<10}{'DEBIT':<8}{'REASON':<16}{'P/L':>10}"
    lines = [RULE, " TRADES TODAY", THIN, head, THIN]
    for i, r in enumerate(closed_trades, 1):
        lines.append(
            f" {i:<3}{_clock(r['entry_time'])[:8]:<10}{r['setup']:<10}{r['direction']:<9}"
            f"{f'{r['short_strike']:g}{r['right']}':<8}{f'{r['long_strike']:g}{r['right']}':<8}{r['qty']:<5}"
            f"{r['entry_credit']:<8.2f}{_clock(r['exit_time'])[:8]:<10}{r['exit_price']:<8.2f}"
            f"{str(r['exit_reason'])[:15]:<16}{_money(r['pnl']):>10}")
    if not closed_trades:
        lines.append(" (no trades closed today)")
    lines += [THIN, f" {'P/L TODAY':<98}{_money(day_pnl):>10}", RULE]
    return "\n".join(lines)


def jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "item"):
        return obj.item()
    return obj


class Journal:
    def __init__(self, log_dir: str | Path, trader_name: str):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trader = trader_name.upper()
        self.slug = self.trader.lower()
        self.state_path = self.dir / f"state_{self.slug}.json"
        self.trades_path = self.dir / f"trades_{self.slug}.csv"

    def event(self, kind: str, now: datetime, **data: Any) -> None:
        path = self.dir / f"events_{self.slug}_{now.date().isoformat()}.jsonl"
        record = {"ts": now.isoformat(), "trader": self.trader, "event": kind, **jsonable(data)}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def trade(self, row: dict) -> None:
        exists = self.trades_path.exists()
        with self.trades_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(jsonable(row))

    def card(self, text: str, now: datetime) -> None:
        path = self.dir / f"trades_{self.slug}_{now.date().isoformat()}.txt"
        with path.open("a", encoding="utf-8") as f:
            f.write(text + "\n\n")

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(jsonable(state), indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def load_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

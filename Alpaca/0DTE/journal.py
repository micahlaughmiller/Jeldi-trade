"""JSONL event log, trades.csv, and state.json persistence."""

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
        self.trader = trader_name
        self.state_path = self.dir / f"state_{trader_name.lower()}.json"
        self.trades_path = self.dir / "trades.csv"

    def event(self, kind: str, now: datetime, **data: Any) -> None:
        path = self.dir / f"events_{now.date().isoformat()}.jsonl"
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

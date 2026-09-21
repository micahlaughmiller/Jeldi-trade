"""JSONL event log, trades CSV and end-of-day summary."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

TRADE_COLUMNS = [
    "timestamp", "action", "spread_id", "symbol", "right", "expiration", "short_strike", "long_strike",
    "qty", "entry_credit", "exit_debit", "realized_pl", "reason", "order_id",
]


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class TradingLogger:
    def __init__(self, log_dir: str | Path, now_fn: Callable[[], datetime] | None = None, echo: bool = True) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.now_fn = now_fn or (lambda: datetime.now(ET))
        self.echo = echo
        self.counts: Counter[str] = Counter()
        self.counts_date: date = self.now_fn().date()

    def session_file(self, day: date | None = None) -> Path:
        return self.log_dir / f"session_{(day or self.now_fn().date()).isoformat()}.jsonl"

    def trades_file(self, day: date | None = None) -> Path:
        return self.log_dir / f"trades_{(day or self.now_fn().date()).isoformat()}.csv"

    def summary_file(self, day: date | None = None) -> Path:
        return self.log_dir / f"summary_{(day or self.now_fn().date()).isoformat()}.json"

    def _roll_counts(self, today: date) -> None:
        if today != self.counts_date:
            self.counts = Counter()
            self.counts_date = today

    def log_event(self, event_type: str, message: str, **fields: Any) -> dict[str, Any]:
        now = self.now_fn()
        self._roll_counts(now.date())
        self.counts[event_type] += 1
        event = {"timestamp": now.isoformat(), "type": event_type, "message": message, **fields}
        line = json.dumps(event, default=_json_default)
        with self.session_file(now.date()).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.echo:
            print(f"{now.strftime('%H:%M:%S')} [{event_type}] {message}", flush=True)
        return event

    def scan_start(self, ticker_count: int) -> None:
        self.log_event("SCAN_START", f"Scanning {ticker_count} tickers", ticker_count=ticker_count)

    def scan_result(self, ok: int, failed: int, elapsed_sec: float, signals: list[dict[str, Any]],
                    failed_symbols: list[str]) -> None:
        self.log_event(
            "SCAN_RESULT",
            f"Downloaded {ok} ok / {failed} failed in {elapsed_sec:.1f}s; {len(signals)} signal(s)",
            ok=ok, failed=failed, elapsed_sec=elapsed_sec,
            signals=[s["symbol"] for s in signals], failed_symbols=failed_symbols,
        )

    def signal(self, row: dict[str, Any]) -> None:
        strong = " STRONG" if row.get("strong") else ""
        self.log_event(
            "SIGNAL",
            f"{row['symbol']}: {row['signal']}{strong} RSI14={row['rsi14']:.1f} RSI28={row['rsi28']:.1f} close={row['close']}",
            **row,
        )

    def trade_spec(self, spec: dict[str, Any]) -> None:
        if spec.get("accepted"):
            msg = (f"{spec['symbol']} {spec['right']} {spec['short_strike']}/{spec['long_strike']} (${spec['width']:g} wide) "
                   f"exp {spec['expiration']} dte={spec['dte']} delta={spec['short_delta']:.2f} "
                   f"credit={spec['credit']:.2f} (min {spec['min_credit']:.2f}) bid_side={spec['bid_side']:.2f}")
        else:
            msg = f"{spec['symbol']} skipped: {spec['reason']}"
        self.log_event("TRADE_SPEC", msg, **spec)

    def risk_check(self, symbol: str, allowed: bool, qty: int, reason: str, **details: Any) -> None:
        verdict = "ALLOWED" if allowed else "REJECTED"
        self.log_event("RISK_CHECK", f"{symbol}: {verdict} qty={qty} {reason}", symbol=symbol, allowed=allowed,
                       qty=qty, reason=reason, **details)

    def order_submitted(self, kind: str, spread: dict[str, Any], order: dict[str, Any], limit: float) -> None:
        self.log_event(
            f"{kind.upper()}_SUBMITTED",
            f"{spread['symbol']} {spread['right']} {spread['short_strike']}/{spread['long_strike']} x{spread['qty']} "
            f"limit={limit:.2f} tif={order.get('time_in_force')} id={order.get('id')} status={order.get('status')}",
            spread_id=spread.get("id"), order_id=order.get("id"), status=order.get("status"), limit=limit,
            qty=spread["qty"], symbol=spread["symbol"],
        )

    def fill(self, spread: dict[str, Any], order: dict[str, Any]) -> None:
        self.log_event(
            "FILL",
            f"{spread['symbol']} entry filled x{order.get('filled_qty')} @ {order.get('filled_avg_price')} (id={order.get('id')})",
            spread_id=spread.get("id"), order_id=order.get("id"), filled_qty=order.get("filled_qty"),
            filled_avg_price=order.get("filled_avg_price"), symbol=spread["symbol"],
        )
        self.record_trade("ENTRY_FILL", spread, order_id=order.get("id"))

    def close_submitted(self, spread: dict[str, Any], order: dict[str, Any], limit: float) -> None:
        self.order_submitted("close", spread, order, limit)

    def price_reduction(self, symbol: str, old_id: str, new_id: str, old_limit: float, new_limit: float) -> None:
        self.log_event("PRICE_REDUCTION", f"{symbol}: {old_limit:.2f} -> {new_limit:.2f} ({old_id} -> {new_id})",
                       symbol=symbol, old_order_id=old_id, new_order_id=new_id, old_limit=old_limit, new_limit=new_limit)

    def entry_canceled(self, symbol: str, order_id: str, reason: str) -> None:
        self.log_event("ENTRY_CANCELED", f"{symbol}: canceled {order_id} ({reason})", symbol=symbol,
                       order_id=order_id, reason=reason)

    def position_closed(self, spread: dict[str, Any], exit_debit: float | None, realized_pl: float | None,
                        reason: str, order_id: str | None) -> None:
        pl = "n/a" if realized_pl is None else f"{realized_pl:+.2f}"
        exit_txt = "n/a" if exit_debit is None else f"{exit_debit:.2f}"
        self.log_event(
            "POSITION_CLOSED",
            f"{spread['symbol']} {spread['right']} {spread['short_strike']}/{spread['long_strike']} x{spread['qty']} "
            f"exit={exit_txt} P&L={pl} ({reason})",
            spread_id=spread.get("id"), symbol=spread["symbol"], exit_debit=exit_debit, realized_pl=realized_pl,
            reason=reason, order_id=order_id,
        )
        self.record_trade("CLOSE", spread, exit_debit=exit_debit, realized_pl=realized_pl, reason=reason, order_id=order_id)

    def breaker(self, tripped: bool, hits: int, message: str) -> None:
        self.log_event("BREAKER", message, tripped=tripped, hits=hits)

    def reconcile(self, ok: bool, mismatches: list[dict[str, Any]], path: Path | None) -> None:
        kind = "RECONCILE_OK" if ok else "RECONCILE_MISMATCH"
        self.log_event(kind, f"{len(mismatches)} mismatch(es)", mismatches=mismatches, path=path)

    def start_of_day(self, **fields: Any) -> None:
        self.log_event("START_OF_DAY", "Start-of-day report", **fields)

    def end_of_day(self, **fields: Any) -> None:
        self.log_event("END_OF_DAY", "End-of-day summary written", **fields)

    def record_trade(self, action: str, spread: dict[str, Any], exit_debit: float | None = None,
                     realized_pl: float | None = None, reason: str = "", order_id: str | None = None) -> None:
        path = self.trades_file()
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
            if new:
                writer.writeheader()
            writer.writerow({
                "timestamp": self.now_fn().isoformat(), "action": action, "spread_id": spread.get("id"),
                "symbol": spread["symbol"], "right": spread["right"], "expiration": spread["expiration"],
                "short_strike": spread["short_strike"], "long_strike": spread["long_strike"], "qty": spread["qty"],
                "entry_credit": spread.get("entry_credit"), "exit_debit": exit_debit, "realized_pl": realized_pl,
                "reason": reason, "order_id": order_id,
            })

    def write_daily_summary(self, summary: dict[str, Any]) -> Path:
        now = self.now_fn()
        self._roll_counts(now.date())
        payload = {
            "date": now.date().isoformat(),
            "written_at": now.isoformat(),
            "counts": {
                "scans": self.counts["SCAN_RESULT"],
                "signals": self.counts["SIGNAL"],
                "entries_submitted": self.counts["ENTRY_SUBMITTED"],
                "fills": self.counts["FILL"],
                "closes": self.counts["POSITION_CLOSED"],
                "price_reductions": self.counts["PRICE_REDUCTION"],
            },
            **summary,
        }
        path = self.summary_file(now.date())
        path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
        self.end_of_day(path=path)
        return path

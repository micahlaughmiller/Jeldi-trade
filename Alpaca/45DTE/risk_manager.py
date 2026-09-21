"""Position sizing, portfolio-risk caps, per-day limits and the max-loss circuit breaker."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Callable
from zoneinfo import ZoneInfo

import config_45dte

ET = ZoneInfo("US/Eastern")


@dataclass
class RiskDecision:
    allowed: bool
    qty: int
    reason: str
    details: dict[str, Any]


def spread_risk_dollars(qty: int, max_loss: float) -> float:
    return qty * max_loss * 100.0


class RiskManager:
    def __init__(self, config: ModuleType = config_45dte, breaker_state: dict[str, Any] | None = None,
                 on_change: Callable[[], None] | None = None, log: Any = None) -> None:
        self.config = config
        self.breaker = breaker_state if breaker_state is not None else {}
        self.breaker.setdefault("tripped", False)
        self.breaker.setdefault("hits", [])
        self.breaker.setdefault("tripped_at", None)
        self.on_change = on_change or (lambda: None)
        self.log = log

    @property
    def breaker_tripped(self) -> bool:
        return bool(self.breaker["tripped"])

    @property
    def max_loss_hits(self) -> int:
        return len(self.breaker["hits"])

    def tier_cap(self, equity: float) -> int:
        for limit in sorted(self.config.POSITION_SIZE_TIERS):
            if equity < limit:
                return self.config.POSITION_SIZE_TIERS[limit]
        return self.config.POSITION_SIZE_TIERS[max(self.config.POSITION_SIZE_TIERS)]

    def get_current_portfolio_risk(self, open_spreads: list[dict[str, Any]],
                                   pending_entries: list[dict[str, Any]] | None = None) -> float:
        """Dollars at risk: qty x max_loss x 100 over open spreads plus working entries."""
        if open_spreads is None:
            raise ValueError("get_current_portfolio_risk requires a list of open spreads, not None")
        total = sum(spread_risk_dollars(s["qty"], s["max_loss"]) for s in open_spreads)
        for entry in pending_entries or []:
            total += spread_risk_dollars(entry["qty"], entry["max_loss"])
        return total

    def size_position(self, equity: float, max_loss: float, open_risk: float) -> tuple[int, str]:
        cfg = self.config
        if equity <= 0 or max_loss <= 0:
            return 0, "invalid equity or max loss"
        per_contract = max_loss * 100.0
        budget = equity * cfg.MAX_RISK_PER_TRADE_PCT
        cap = self.tier_cap(equity)
        qty = min(math.floor(budget / per_contract), cap)
        note = f"budget={budget:.0f} per_contract={per_contract:.0f} tier_cap={cap}"
        if qty == 0 and cfg.ALLOW_MIN_CONTRACT_OVERRIDE:
            if (open_risk + per_contract) / equity <= cfg.MAX_PORTFOLIO_RISK_PCT:
                qty = 1
                note += " min-contract override"
        reduced = False
        while qty > 0 and (open_risk + qty * per_contract) / equity > cfg.MAX_PORTFOLIO_RISK_PCT:
            qty -= 1
            reduced = True
        if reduced:
            note += " reduced for portfolio cap"
        return qty, note

    def check_new_entry(self, spec: dict[str, Any], equity: float, open_spreads: list[dict[str, Any]],
                        pending_entries: list[dict[str, Any]], entries_today: int,
                        blocked_symbols: set[str] | None = None) -> RiskDecision:
        cfg = self.config
        symbol = spec["broker_symbol"]
        open_risk = self.get_current_portfolio_risk(open_spreads, pending_entries)
        details = {"equity": equity, "open_risk": open_risk, "open_risk_pct": open_risk / equity if equity else None}
        if self.breaker_tripped:
            return RiskDecision(False, 0, "circuit breaker tripped (run with --reset-breaker to clear)", details)
        if blocked_symbols and symbol in blocked_symbols:
            return RiskDecision(False, 0, "symbol blocked (unpaired leg at broker)", details)
        busy = {s["broker_symbol"] for s in open_spreads} | {e["broker_symbol"] for e in pending_entries}
        if symbol in busy:
            return RiskDecision(False, 0, "already has an open spread or working entry", details)
        if entries_today >= cfg.MAX_NEW_POSITIONS_PER_DAY:
            return RiskDecision(False, 0, f"daily entry limit {cfg.MAX_NEW_POSITIONS_PER_DAY} reached", details)
        qty, note = self.size_position(equity, spec["max_loss"], open_risk)
        details["sizing"] = note
        if qty <= 0:
            return RiskDecision(False, 0, f"size 0 ({note})", details)
        new_pct = (open_risk + spread_risk_dollars(qty, spec["max_loss"])) / equity
        details["portfolio_risk_pct_after"] = new_pct
        return RiskDecision(True, qty, f"risk_after={new_pct:.1%} ({note})", details)

    def is_max_loss_hit(self, entry_credit: float, current_price: float | None, width: float) -> bool:
        if current_price is None:
            return False
        return current_price >= entry_credit + self.config.MAX_LOSS_HIT_PCT * (width - entry_credit) - 1e-9

    def is_realized_max_loss(self, entry_credit: float, exit_debit: float | None, width: float) -> bool:
        if exit_debit is None:
            return False
        max_loss = width - entry_credit
        return (exit_debit - entry_credit) >= self.config.MAX_LOSS_HIT_PCT * max_loss - 1e-9

    def record_max_loss_hit(self, spread_id: str, now: datetime | None = None) -> bool:
        """Count one hit per spread; returns True when this call trips the breaker."""
        if spread_id in self.breaker["hits"]:
            return False
        self.breaker["hits"].append(spread_id)
        hits = self.max_loss_hits
        tripped_now = False
        if hits >= self.config.MAX_LOSS_HITS_CIRCUIT_BREAKER and not self.breaker["tripped"]:
            self.breaker["tripped"] = True
            self.breaker["tripped_at"] = (now or datetime.now(ET)).isoformat()
            tripped_now = True
        if self.log:
            msg = (f"CIRCUIT BREAKER TRIPPED after {hits} max-loss hits; no new entries until --reset-breaker"
                   if tripped_now else
                   f"max-loss hit recorded for {spread_id} ({hits}/{self.config.MAX_LOSS_HITS_CIRCUIT_BREAKER})")
            self.log.breaker(self.breaker["tripped"], hits, msg)
        self.on_change()
        return tripped_now

    def reset_breaker(self) -> None:
        self.breaker["tripped"] = False
        self.breaker["hits"] = []
        self.breaker["tripped_at"] = None
        if self.log:
            self.log.breaker(False, 0, "circuit breaker manually reset")
        self.on_change()

    def daily_loss_alert(self, start_equity: float | None, current_equity: float) -> tuple[bool, float]:
        if not start_equity:
            return False, 0.0
        drop = (start_equity - current_equity) / start_equity
        return drop >= self.config.DAILY_LOSS_ALERT_PCT, drop

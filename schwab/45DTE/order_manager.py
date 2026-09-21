"""Spread lifecycle: entry, fill detection, GTC profit-target close, maintenance, exits,
adoption of broker positions on startup, and reconciliation. State lives in LOG_DIR/state.json."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from zoneinfo import ZoneInfo

import config_45dte
from broker import BrokerError
from market_data_handler import load_universe
from risk_manager import RiskManager
from signal_generator import round_to_nickel

ET = ZoneInfo("US/Eastern")
TERMINAL = {"filled", "canceled", "rejected", "expired", "replaced"}
DEAD_CLOSE = {"canceled", "rejected", "expired", "replaced", "unknown"}


def spread_id(symbol: str, expiration: date | str, right: str, short_strike: float, long_strike: float) -> str:
    exp = expiration if isinstance(expiration, str) else expiration.isoformat()
    return f"{symbol}_{exp}_{right}_{short_strike:g}_{long_strike:g}"


def spread_width(position: dict[str, Any]) -> float:
    return float(position.get("width") or round(abs(position["short_strike"] - position["long_strike"]), 2))


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _exp(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


class OrderManager:
    def __init__(self, broker: Any, log: Any, config: ModuleType = config_45dte,
                 now_fn: Callable[[], datetime] | None = None, state_path: str | Path | None = None,
                 risk: RiskManager | None = None, universe: set[str] | None = None) -> None:
        self.broker = broker
        self.log = log
        self.config = config
        self.universe = universe if universe is not None else set(load_universe(config=config))
        self.now_fn = now_fn or (lambda: datetime.now(ET))
        self.state_path = Path(state_path) if state_path else Path(config.LOG_DIR) / "state.json"
        self.state: dict[str, Any] = self._load_state()
        self.risk = risk or RiskManager(config, self.state["breaker"], self.save, log)
        self.risk.breaker = self.state["breaker"]
        self.risk.on_change = self.save
        self.on_position_change: Callable[[], None] | None = None

    # ------------------------------------------------------------------ state

    def _load_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        state.setdefault("positions", {})
        state.setdefault("working_entries", {})
        state.setdefault("closed", [])
        state.setdefault("breaker", {"tripped": False, "hits": [], "tripped_at": None})
        state.setdefault("blocked_symbols", [])
        state.setdefault("day", {"date": None, "entries": 0, "start_equity": None, "unfilled_canceled": False})
        return state

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def roll_day(self, equity: float) -> None:
        today = self.now_fn().date().isoformat()
        if self.state["day"].get("date") != today:
            self.state["day"] = {"date": today, "entries": 0, "start_equity": equity, "unfilled_canceled": False}
            self.save()

    @property
    def positions(self) -> list[dict[str, Any]]:
        return list(self.state["positions"].values())

    @property
    def working_entries(self) -> list[dict[str, Any]]:
        return list(self.state["working_entries"].values())

    @property
    def blocked_symbols(self) -> set[str]:
        return set(self.state["blocked_symbols"])

    @property
    def entries_today(self) -> int:
        return int(self.state["day"].get("entries", 0))

    def closed_today(self) -> list[dict[str, Any]]:
        today = self.now_fn().date().isoformat()
        return [c for c in self.state["closed"] if str(c.get("closed_at", ""))[:10] == today]

    def open_risk_dollars(self) -> float:
        return self.risk.get_current_portfolio_risk(self.positions, self.working_entries)

    def unrealized_total(self) -> float:
        return sum(p.get("unrealized_pl") or 0.0 for p in self.positions)

    def realized_today_total(self) -> float:
        return sum(c.get("realized_pl") or 0.0 for c in self.closed_today())

    def _notify(self) -> None:
        if self.on_position_change:
            self.on_position_change()

    # ------------------------------------------------------------------ entry

    def submit_entry(self, spec: dict[str, Any], qty: int) -> str | None:
        cfg = self.config
        sid = spread_id(spec["broker_symbol"], spec["expiration"], spec["right"], spec["short_strike"], spec["long_strike"])
        try:
            order = self.broker.place_credit_spread(
                spec["broker_symbol"], _exp(spec["expiration"]), spec["right"], spec["short_strike"],
                spec["long_strike"], qty, limit_credit=spec["credit"], time_in_force=cfg.ENTRY_TIME_IN_FORCE,
            )
        except BrokerError as exc:
            self.log.log_event("ENTRY_ERROR", f"{spec['broker_symbol']}: {exc}", symbol=spec["broker_symbol"])
            return None
        now = self.now_fn()
        entry = {
            "order_id": order["id"], "spread_id": sid, "symbol": spec["symbol"], "broker_symbol": spec["broker_symbol"],
            "right": spec["right"], "expiration": _exp(spec["expiration"]).isoformat(),
            "short_strike": spec["short_strike"], "long_strike": spec["long_strike"], "qty": qty,
            "limit_credit": spec["credit"], "initial_credit": spec["credit"], "max_loss": spec["max_loss"],
            "width": spec["width"], "strong": spec["strong"], "floor": spec["min_credit"],
            "submitted_at": now.isoformat(), "last_reduction_at": now.isoformat(), "filled_qty": 0,
            "status": order["status"], "dte": spec.get("dte"), "short_delta": spec.get("short_delta"),
            "dte_out_of_range": spec.get("dte_out_of_range", False), "source": "bot",
        }
        self.state["working_entries"][order["id"]] = entry
        self.state["day"]["entries"] = self.entries_today + 1
        self.save()
        self.log.order_submitted("entry", {**entry, "id": sid}, order, spec["credit"])
        return order["id"]

    def poll_entries(self) -> None:
        for order_id, entry in list(self.state["working_entries"].items()):
            if order_id.startswith("DRY-"):
                continue
            try:
                order = self.broker.get_order(order_id)
            except BrokerError as exc:
                self.log.log_event("POLL_ERROR", f"{entry['broker_symbol']} entry {order_id}: {exc}", order_id=order_id)
                continue
            if order is None:
                self.log.log_event("ENTRY_MISSING", f"{entry['broker_symbol']}: entry order {order_id} not found; dropping",
                                   order_id=order_id)
                self._finish_entry(order_id)
                continue
            entry["status"] = order["status"]
            filled_qty = int(order.get("filled_qty") or 0)
            if filled_qty > entry["filled_qty"]:
                self._handle_fill(entry, order, filled_qty)
            if order["status"] == "filled":
                self._finish_entry(order_id)
            elif order["status"] in TERMINAL:
                self.log.log_event("ENTRY_TERMINAL", f"{entry['broker_symbol']}: entry {order_id} {order['status']}",
                                   order_id=order_id, status=order["status"], filled_qty=filled_qty)
                self._finish_entry(order_id)
        self.save()

    def _finish_entry(self, order_id: str) -> None:
        self.state["working_entries"].pop(order_id, None)

    def _handle_fill(self, entry: dict[str, Any], order: dict[str, Any], filled_qty: int) -> None:
        credit = float(order.get("filled_avg_price") or entry["limit_credit"])
        sid = entry["spread_id"]
        position = self.state["positions"].get(sid)
        if position is None:
            position = {
                "id": sid, "symbol": entry["symbol"], "broker_symbol": entry["broker_symbol"], "right": entry["right"],
                "expiration": entry["expiration"], "short_strike": entry["short_strike"],
                "long_strike": entry["long_strike"], "qty": 0, "entry_credit": credit, "entry_order_id": order["id"],
                "opened_at": self.now_fn().isoformat(), "strong": entry["strong"], "current_price": None,
                "unrealized_pl": None, "close_order_id": None, "close_limit": None, "close_status": None,
                "max_loss_hit": False, "source": "bot", "short_delta": entry.get("short_delta"),
                "width": entry["width"],
            }
            self.state["positions"][sid] = position
        position["qty"] = filled_qty
        position["entry_credit"] = credit
        position["max_loss"] = round(spread_width(position) - credit, 2)
        entry["filled_qty"] = filled_qty
        self.log.fill(position, order)
        self._ensure_close_order(position, force_replace=True)
        self._notify()

    def reduce_prices(self) -> None:
        cfg = self.config
        now = self.now_fn()
        for order_id, entry in list(self.state["working_entries"].items()):
            last = datetime.fromisoformat(entry["last_reduction_at"])
            if (now - last).total_seconds() < cfg.PRICE_REDUCTION_INTERVAL_MIN * 60:
                continue
            new_limit = round(entry["limit_credit"] - cfg.PRICE_REDUCTION_AMOUNT, 2)
            if new_limit + 1e-9 < entry["floor"]:
                self._cancel_entry(order_id, entry, f"limit {entry['limit_credit']:.2f} at floor {entry['floor']:.2f}")
                continue
            try:
                order = self.broker.replace_order_price(order_id, new_limit)
            except BrokerError as exc:
                self.log.log_event("REPLACE_ERROR", f"{entry['broker_symbol']} {order_id}: {exc}", order_id=order_id)
                continue
            self.log.price_reduction(entry["broker_symbol"], order_id, order["id"], entry["limit_credit"], new_limit)
            entry["limit_credit"] = new_limit
            entry["last_reduction_at"] = now.isoformat()
            entry["status"] = order["status"]
            if order["id"] != order_id:
                self.state["working_entries"].pop(order_id)
                entry["order_id"] = order["id"]
                self.state["working_entries"][order["id"]] = entry
        self.save()

    def _cancel_entry(self, order_id: str, entry: dict[str, Any], reason: str) -> None:
        try:
            self.broker.cancel_order(order_id)
        except BrokerError as exc:
            self.log.log_event("CANCEL_ERROR", f"{entry['broker_symbol']} {order_id}: {exc}", order_id=order_id)
            return
        self.log.entry_canceled(entry["broker_symbol"], order_id, reason)
        self._finish_entry(order_id)

    def cancel_unfilled_entries(self, reason: str = "end-of-day cancel") -> int:
        count = 0
        for order_id, entry in list(self.state["working_entries"].items()):
            self._cancel_entry(order_id, entry, reason)
            count += 1
        self.state["day"]["unfilled_canceled"] = True
        self.save()
        return count

    # ------------------------------------------------------------ close orders

    def _profit_target(self, entry_credit: float) -> float:
        return max(0.05, round_to_nickel(entry_credit * (1 - self.config.PROFIT_TARGET_PCT)))

    def _ensure_close_order(self, position: dict[str, Any], force_replace: bool = False) -> None:
        close_id = position.get("close_order_id")
        if close_id and not force_replace:
            if close_id.startswith("DRY-"):
                return
            try:
                order = self.broker.get_order(close_id)
            except BrokerError as exc:
                self.log.log_event("POLL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
                return
            if order is None or order["status"] in DEAD_CLOSE:
                status = order["status"] if order else "missing"
                self.log.log_event("CLOSE_ORDER_LOST", f"{position['broker_symbol']}: close {close_id} {status}; re-creating",
                                   spread_id=position["id"], order_id=close_id, status=status)
            else:
                position["close_status"] = order["status"]
                return
        elif close_id and force_replace and not close_id.startswith("DRY-"):
            try:
                self.broker.cancel_order(close_id)
            except BrokerError as exc:
                self.log.log_event("CANCEL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
        limit = self._profit_target(position["entry_credit"])
        try:
            order = self.broker.place_close_spread(
                position["broker_symbol"], _exp(position["expiration"]), position["right"], position["short_strike"],
                position["long_strike"], position["qty"], limit_debit=limit, time_in_force="gtc",
            )
        except BrokerError as exc:
            self.log.log_event("CLOSE_ERROR", f"{position['broker_symbol']}: {exc}", spread_id=position["id"])
            return
        position["close_order_id"] = order["id"]
        position["close_limit"] = limit
        position["close_status"] = order["status"]
        self.log.close_submitted(position, order, limit)

    def _check_close_filled(self, position: dict[str, Any]) -> bool:
        close_id = position.get("close_order_id")
        if not close_id or close_id.startswith("DRY-"):
            return False
        try:
            order = self.broker.get_order(close_id)
        except BrokerError:
            return False
        if order is None or order["status"] != "filled":
            return False
        self._record_close(position, float(order.get("filled_avg_price") or position["close_limit"]),
                           "profit target", order["id"])
        return True

    def _record_close(self, position: dict[str, Any], exit_debit: float | None, reason: str,
                      order_id: str | None) -> None:
        realized = None if exit_debit is None else round((position["entry_credit"] - exit_debit) * 100 * position["qty"], 2)
        record = {**position, "exit_debit": exit_debit, "realized_pl": realized, "close_reason": reason,
                  "closed_at": self.now_fn().isoformat(), "close_order_id": order_id}
        self.state["closed"].append(record)
        self.state["positions"].pop(position["id"], None)
        self.log.position_closed(position, exit_debit, realized, reason, order_id)
        if self.risk.is_realized_max_loss(position["entry_credit"], exit_debit, spread_width(position)):
            self.risk.record_max_loss_hit(position["id"], self.now_fn())
        self.save()
        self._notify()

    # -------------------------------------------------------------- maintenance

    def refresh_price(self, position: dict[str, Any]) -> float | None:
        try:
            chain = self.broker.get_option_chain(position["broker_symbol"], _exp(position["expiration"]), position["right"])
        except BrokerError as exc:
            self.log.log_event("CHAIN_ERROR", f"{position['broker_symbol']}: {exc}", spread_id=position["id"])
            return position.get("current_price")
        short = next((q for q in chain if abs(q["strike"] - position["short_strike"]) < 1e-6), None)
        long = next((q for q in chain if abs(q["strike"] - position["long_strike"]) < 1e-6), None)
        if short is None or long is None:
            return position.get("current_price")
        price = round(short["mid"] - long["mid"], 2)
        position["current_price"] = price
        position["unrealized_pl"] = round((position["entry_credit"] - price) * 100 * position["qty"], 2)
        return price

    def dte(self, position: dict[str, Any]) -> int:
        return (_exp(position["expiration"]) - self.now_fn().date()).days

    def maintain(self) -> None:
        cfg = self.config
        for position in list(self.state["positions"].values()):
            if self._check_close_filled(position):
                continue
            if self.dte(position) <= cfg.EXIT_DTE:
                self.exit_at_market(position, f"DTE {self.dte(position)} <= {cfg.EXIT_DTE}")
                continue
            self._ensure_close_order(position)
            price = self.refresh_price(position)
            if self.risk.is_max_loss_hit(position["entry_credit"], price, spread_width(position)):
                if not position.get("max_loss_hit"):
                    position["max_loss_hit"] = True
                    self.risk.record_max_loss_hit(position["id"], self.now_fn())
                if cfg.MAX_LOSS_EXIT:
                    self.exit_at_market(position, f"max loss hit (price {price})")
        self.save()

    def exit_at_market(self, position: dict[str, Any], reason: str) -> bool:
        close_id = position.get("close_order_id")
        if close_id and not close_id.startswith("DRY-"):
            try:
                self.broker.cancel_order(close_id)
            except BrokerError as exc:
                self.log.log_event("CANCEL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
        try:
            order = self.broker.close_spread_at_market(
                position["broker_symbol"], _exp(position["expiration"]), position["right"], position["short_strike"],
                position["long_strike"], position["qty"],
            )
        except BrokerError as exc:
            self.log.log_event("EXIT_ERROR", f"{position['broker_symbol']}: {exc}; will retry next cycle",
                               spread_id=position["id"], reason=reason)
            position["close_order_id"] = None
            self.save()
            return False
        if order["status"] == "dry_run":
            self.log.log_event("EXIT_DRY_RUN", f"{position['broker_symbol']}: would close at market ({reason})",
                               spread_id=position["id"])
            return False
        remaining = self._legs_still_open(position)
        if remaining:
            self.log.log_event("EXIT_VERIFY_FAILED", f"{position['broker_symbol']}: legs still open after close: {remaining}",
                               spread_id=position["id"], legs=remaining)
            position["close_order_id"] = None
            self.save()
            return False
        self._record_close(position, order.get("filled_avg_price"), reason, order["id"])
        return True

    def _legs_still_open(self, position: dict[str, Any]) -> list[str]:
        try:
            legs = self.broker.get_positions()
        except BrokerError:
            return []
        exp = _exp(position["expiration"])
        return [
            leg["symbol"] for leg in legs
            if leg["asset_class"] == "option" and leg["underlying"] == position["broker_symbol"]
            and leg["expiration"] == exp and leg["right"] == position["right"]
            and leg["strike"] in (position["short_strike"], position["long_strike"])
        ]

    # -------------------------------------------------------------------- adopt

    def adopt(self) -> dict[str, Any]:
        """Rebuild spreads from broker legs, match their close orders, flag unpaired legs."""
        legs = [p for p in self.broker.get_positions() if p["asset_class"] == "option"]
        open_orders = self.broker.get_open_orders()
        groups: dict[tuple[str, date, str], list[dict[str, Any]]] = defaultdict(list)
        for leg in legs:
            groups[(leg["underlying"], leg["expiration"], leg["right"])].append(leg)

        seen_ids: set[str] = set()
        blocked: set[str] = set()
        adopted = 0
        for (underlying, expiration, right), members in groups.items():
            shorts = [m for m in members if m["qty"] < 0]
            longs = [m for m in members if m["qty"] > 0]
            paired = len(shorts) == 1 and len(longs) == 1 and abs(shorts[0]["qty"]) == longs[0]["qty"]
            if paired:
                short, long = shorts[0], longs[0]
                paired = (long["strike"] < short["strike"]) if right == "P" else (long["strike"] > short["strike"])
            if not paired:
                blocked.add(underlying)
                self.log.log_event("UNPAIRED_LEG", f"{underlying} {expiration} {right}: {len(members)} leg(s) do not form a "
                                   f"credit spread; will not trade this underlying",
                                   underlying=underlying, legs=[m["symbol"] for m in members])
                continue
            sid = spread_id(underlying, expiration, right, short["strike"], long["strike"])
            seen_ids.add(sid)
            position = self.state["positions"].get(sid)
            if position is None:
                position = {
                    "id": sid, "symbol": underlying, "broker_symbol": underlying, "right": right,
                    "expiration": expiration.isoformat(), "short_strike": short["strike"], "long_strike": long["strike"],
                    "qty": abs(short["qty"]), "entry_credit": round(short["avg_price"] - long["avg_price"], 2),
                    "entry_order_id": None, "opened_at": self.now_fn().isoformat(), "strong": False,
                    "current_price": None, "unrealized_pl": None, "close_order_id": None, "close_limit": None,
                    "close_status": None, "max_loss_hit": False, "source": "adopted", "short_delta": None,
                    "width": round(abs(short["strike"] - long["strike"]), 2),
                }
                position["max_loss"] = round(position["width"] - position["entry_credit"], 2)
                self.state["positions"][sid] = position
                adopted += 1
            elif position["qty"] != abs(short["qty"]):
                self.log.log_event("QTY_MISMATCH", f"{sid}: state qty {position['qty']} vs broker {abs(short['qty'])}; using broker",
                                   spread_id=sid)
                position["qty"] = abs(short["qty"])
            if short.get("current_price") is not None and long.get("current_price") is not None:
                position["current_price"] = round(short["current_price"] - long["current_price"], 2)
                position["unrealized_pl"] = round((position["entry_credit"] - position["current_price"]) * 100 * position["qty"], 2)
            match = self._match_close_order(open_orders, short["symbol"], long["symbol"])
            if match:
                position["close_order_id"] = match["id"]
                position["close_limit"] = match.get("limit_price")
                position["close_status"] = match["status"]

        for sid, position in list(self.state["positions"].items()):
            if sid not in seen_ids:
                self._resolve_vanished(position)

        self._adopt_unknown_entries(open_orders, legs)
        self.state["blocked_symbols"] = sorted(blocked)
        self.save()
        summary = {"adopted": adopted, "positions": len(self.state["positions"]), "unpaired": sorted(blocked),
                   "working_entries": len(self.state["working_entries"])}
        self.log.log_event("ADOPT", f"adopted {adopted} new spread(s); {len(self.state['positions'])} open; "
                           f"unpaired underlyings: {sorted(blocked) or 'none'}", **summary)
        return summary

    def _resolve_vanished(self, position: dict[str, Any]) -> None:
        close_id = position.get("close_order_id")
        order = None
        if close_id and not close_id.startswith("DRY-"):
            try:
                order = self.broker.get_order(close_id)
            except BrokerError:
                order = None
        if order and order["status"] == "filled":
            self._record_close(position, float(order.get("filled_avg_price") or 0.0), "profit target (while offline)", close_id)
        else:
            self.log.log_event("RECONCILE_MISMATCH", f"{position['id']}: in local state but not at broker; closing record with unknown exit",
                               spread_id=position["id"])
            self._record_close(position, None, "vanished at broker", close_id)

    @staticmethod
    def _match_close_order(open_orders: list[dict[str, Any]], short_symbol: str, long_symbol: str) -> dict[str, Any] | None:
        for order in open_orders:
            if order.get("order_class") != "mleg":
                continue
            sides = {leg["symbol"]: leg["side"] for leg in order.get("legs", [])}
            if sides.get(short_symbol) == "buy" and sides.get(long_symbol) == "sell":
                return order
        return None

    def _adopt_unknown_entries(self, open_orders: list[dict[str, Any]], legs: list[dict[str, Any]]) -> None:
        """Adopt untracked opening spreads only for universe names; other bots (0DTE SPXW) share the account."""
        known = set(self.state["working_entries"]) | {p.get("close_order_id") for p in self.positions}
        held = {leg["symbol"] for leg in legs}
        for order in open_orders:
            if order["id"] in known or order.get("order_class") != "mleg" or len(order.get("legs", [])) != 2:
                continue
            sell = next((l for l in order["legs"] if l["side"] == "sell"), None)
            buy = next((l for l in order["legs"] if l["side"] == "buy"), None)
            s = self.broker.parse_occ(sell["symbol"]) if sell else None
            l = self.broker.parse_occ(buy["symbol"]) if buy else None
            adoptable = (
                s is not None and l is not None and s["root"] in self.universe
                and sell["symbol"] not in held and buy["symbol"] not in held
                and s["right"] == l["right"] and s["expiration"] == l["expiration"]
                and any(abs(abs(s["strike"] - l["strike"]) - w) < 1e-6 for w in self.config.SPREAD_WIDTHS)
            )
            if not adoptable:
                self.log.log_event("UNKNOWN_OPEN_ORDER", f"{order['symbol']}: open order {order['id']} not tracked; left alone",
                                   order_id=order["id"], status=order["status"])
                continue
            width = round(abs(s["strike"] - l["strike"]), 2)
            floor = round(self.config.MIN_CREDIT * width / self.config.SPREAD_WIDTH, 2)
            limit = float(order.get("limit_price") or floor)
            now = self.now_fn().isoformat()
            self.state["working_entries"][order["id"]] = {
                "order_id": order["id"], "spread_id": spread_id(s["root"], s["expiration"], s["right"], s["strike"], l["strike"]),
                "symbol": s["root"], "broker_symbol": s["root"], "right": s["right"],
                "expiration": s["expiration"].isoformat(), "short_strike": s["strike"], "long_strike": l["strike"],
                "qty": int(order["qty"]), "limit_credit": limit, "initial_credit": limit,
                "max_loss": round(width - limit, 2), "width": width, "strong": False,
                "floor": floor, "submitted_at": order["submitted_at"].isoformat()
                if isinstance(order.get("submitted_at"), datetime) else now, "last_reduction_at": now,
                "filled_qty": int(order.get("filled_qty") or 0), "status": order["status"], "dte": None,
                "short_delta": None, "dte_out_of_range": False, "source": "adopted",
            }
            self.log.log_event("ADOPT_ENTRY", f"{s['root']}: adopted working entry {order['id']} limit {limit:.2f}",
                               order_id=order["id"])

    # ---------------------------------------------------------------- reconcile

    def reconcile(self) -> dict[str, Any]:
        mismatches: list[dict[str, Any]] = []
        try:
            legs = [p for p in self.broker.get_positions() if p["asset_class"] == "option"]
            open_orders = self.broker.get_open_orders()
        except BrokerError as exc:
            mismatches.append({"kind": "broker_error", "detail": str(exc)})
            legs, open_orders = [], []
        expected: dict[str, int] = {}
        for p in self.positions:
            exp = _exp(p["expiration"])
            expected[self.broker.occ_symbol(p["broker_symbol"], exp, p["right"], p["short_strike"])] = -p["qty"]
            expected[self.broker.occ_symbol(p["broker_symbol"], exp, p["right"], p["long_strike"])] = p["qty"]
        actual = {leg["symbol"]: int(leg["qty"]) for leg in legs}
        for sym, qty in expected.items():
            if actual.get(sym) != qty:
                mismatches.append({"kind": "position", "symbol": sym, "local_qty": qty, "broker_qty": actual.get(sym)})
        for sym, qty in actual.items():
            if sym not in expected:
                mismatches.append({"kind": "position", "symbol": sym, "local_qty": None, "broker_qty": qty})
        broker_ids = {o["id"] for o in open_orders}
        local_ids = set(self.state["working_entries"]) | {
            p["close_order_id"] for p in self.positions if p.get("close_order_id") and not p["close_order_id"].startswith("DRY-")
        }
        for oid in local_ids - broker_ids:
            mismatches.append({"kind": "order", "order_id": oid, "detail": "tracked locally, not open at broker"})
        for oid in broker_ids - local_ids:
            mismatches.append({"kind": "order", "order_id": oid, "detail": "open at broker, not tracked locally"})
        now = self.now_fn()
        report = {"as_of": now.isoformat(), "ok": not mismatches, "mismatches": mismatches,
                  "local_positions": len(self.positions), "broker_legs": len(legs),
                  "local_orders": len(local_ids), "broker_open_orders": len(open_orders)}
        path = self.state_path.parent / f"reconcile_{now.date().isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        self.log.reconcile(report["ok"], mismatches, path)
        return report

    # ------------------------------------------------------------------ reports

    def position_report(self, equity: float | None) -> str:
        lines = ["", "=" * 96, f"POSITION REPORT  {self.now_fn().strftime('%Y-%m-%d %H:%M:%S %Z')}", "=" * 96]
        lines.append(f"{'SYMBOL':<7}{'R':<2}{'STRIKES':<14}{'EXP':<12}{'DTE':>4}{'QTY':>4}{'ENTRY':>7}{'NOW':>7}{'UNREAL':>10}  CLOSE ORDER")
        for p in sorted(self.positions, key=lambda x: x["symbol"]):
            now_px = "n/a" if p.get("current_price") is None else f"{p['current_price']:.2f}"
            unreal = "n/a" if p.get("unrealized_pl") is None else f"{p['unrealized_pl']:+.2f}"
            close = f"{p.get('close_status') or '-'} @ {p.get('close_limit')}" if p.get("close_order_id") else "MISSING"
            lines.append(f"{p['symbol']:<7}{p['right']:<2}{p['short_strike']:g}/{p['long_strike']:<8g}{p['expiration']:<12}"
                         f"{self.dte(p):>4}{p['qty']:>4}{p['entry_credit']:>7.2f}{now_px:>7}{unreal:>10}  {close}")
        if not self.positions:
            lines.append("  (no open positions)")
        if self.working_entries:
            lines.append("-- working entries --")
            for e in self.working_entries:
                lines.append(f"  {e['broker_symbol']} {e['right']} {e['short_strike']:g}/{e['long_strike']:g} x{e['qty']} "
                             f"limit {e['limit_credit']:.2f} floor {e['floor']:.2f} status {e['status']} id {e['order_id']}")
        closed = self.closed_today()
        if closed:
            lines.append("-- closed today --")
            for c in closed:
                pl = "n/a" if c.get("realized_pl") is None else f"{c['realized_pl']:+.2f}"
                exit_px = "n/a" if c.get("exit_debit") is None else f"{c['exit_debit']:.2f}"
                lines.append(f"  {c['symbol']} {c['right']} {c['short_strike']:g}/{c['long_strike']:g} x{c['qty']} "
                             f"entry {c['entry_credit']:.2f} exit {exit_px} P&L {pl} ({c['close_reason']})")
        open_risk = self.open_risk_dollars()
        risk_pct = f"{open_risk / equity:.1%}" if equity else "n/a"
        lines.append("-" * 96)
        lines.append(f"unrealized {self.unrealized_total():+,.2f} | realized today {self.realized_today_total():+,.2f} | "
                     f"portfolio risk ${open_risk:,.0f} ({risk_pct} of equity) | breaker "
                     f"{'TRIPPED' if self.risk.breaker_tripped else 'ok'} ({self.risk.max_loss_hits} hits)")
        lines.append("=" * 96)
        return "\n".join(lines)

"""Spread lifecycle: entry, fill detection, GTC profit-target close, maintenance, exits,
adoption of broker positions on startup, and reconciliation. State lives in LOG_DIR/state.json.

Manual overrides: the owner may close or cancel things by hand in the broker's own platform (thinkorswim)
at any time. sync_external() compares the tracked spreads with the broker's positions every maintenance
cycle and reconciles the books to what the broker actually holds, instead of fighting the owner; see the
"manual overrides" section below and the README for exactly what happens in each case."""

from __future__ import annotations

import json
import re
import time as time_module
from collections import defaultdict
from datetime import date, datetime, time, timedelta
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
# A GTC close in one of these states was taken off the book by someone else (the owner, in thinkorswim): a
# cancel, or an edit that the broker records as the old order "replaced" by a new order id. 'rejected',
# 'expired' and 'unknown' are not owner actions and keep the re-create behaviour.
OWNER_CLOSE_CHANGE = {"canceled", "replaced"}


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


def _norm_root(value: Any) -> str:
    """'BRK.B' / 'BRK/B' / 'BRKB' -> 'BRKB': brokers disagree on punctuation in dotted tickers."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


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
        # Spread ids sync_external() saw an inconsistent-but-not-yet-confirmed picture for this cycle; maintain()
        # leaves those alone for one cycle. Per-cycle scratch, deliberately not persisted.
        self._sync_pending: set[str] = set()

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
        state.setdefault("manual_cooldown_until", None)   # ISO time; no new entries before it (see entries_paused)
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

    def entries_paused(self, now: datetime | None = None) -> bool:
        """True while the short cool-down after a manual (external) full close is running: the owner is
        actively working in the broker's platform, so no NEW entry is submitted until it has passed."""
        return self.manual_cooldown_until(now) is not None

    def manual_cooldown_until(self, now: datetime | None = None) -> datetime | None:
        """The end of the manual-close cool-down if it is still running at `now`, else None."""
        raw = self.state.get("manual_cooldown_until")
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return until if (now or self.now_fn()) < until else None

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
        # Contracts the owner already closed by hand (sync_external) stay out of the position when a later partial
        # fill of the same entry order raises the filled total.
        position["qty"] = max(filled_qty - int(position.get("external_closed_qty") or 0), 1)
        position["entry_credit"] = credit
        position["max_loss"] = round(spread_width(position) - credit, 2)
        position["last_fill_at"] = self.now_fn().isoformat()   # sync_external's min-age guard counts from here
        entry["filled_qty"] = filled_qty
        self.log.fill(position, order)
        self._ensure_close_order(position, force_replace=True, new_fill=True)
        self._notify()

    def reduce_prices(self) -> None:
        cfg = self.config
        now = self.now_fn()
        for order_id, entry in list(self.state["working_entries"].items()):
            last = datetime.fromisoformat(entry["last_reduction_at"])
            if (now - last).total_seconds() < cfg.PRICE_REDUCTION_INTERVAL_MIN * 60:
                continue
            # Walk down to the floor and then sit there; only the CANCEL_UNFILLED_AT sweep cancels.
            new_limit = max(round(entry["limit_credit"] - cfg.PRICE_REDUCTION_AMOUNT, 2), entry["floor"])
            if new_limit + 1e-9 >= entry["limit_credit"]:
                entry["last_reduction_at"] = now.isoformat()
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

    def _profit_floor_arm_price(self, entry_credit: float) -> float:
        """Spread price at which the profit floor arms: PROFIT_FLOOR_ARM_PCT_OF_TARGET of the way
        to the profit target (e.g. 0.85 of a 50%-of-max target = 42.5% of max profit achieved)."""
        arm_pct = self.config.PROFIT_TARGET_PCT * self.config.PROFIT_FLOOR_ARM_PCT_OF_TARGET
        return max(0.05, round_to_nickel(entry_credit * (1 - arm_pct)))

    def _profit_floor_price(self, entry_credit: float) -> float:
        """Once armed, a hard exit floor at PROFIT_FLOOR_PCT_OF_MAX of max profit -- a fixed level,
        not a trail off the best price seen, so a position that armed then gave back everything
        still exits with at least this much rather than round-tripping to a loss."""
        return max(0.05, round_to_nickel(entry_credit * (1 - self.config.PROFIT_FLOOR_PCT_OF_MAX)))

    def _ensure_close_order(self, position: dict[str, Any], force_replace: bool = False,
                            new_fill: bool = False) -> None:
        """Make sure the spread has a resting GTC profit-target close. `new_fill` marks the call from
        _handle_fill (the position just grew)."""
        if position.get("manual_attention"):
            return   # a leg is missing at the broker: no order activity at all until the owner sorts it out
        if position.get("manual_hold"):
            if not new_fill:
                return   # the owner took our GTC off the book in thinkorswim; never put it back on their behalf
            # A genuinely new fill (_handle_fill) changes what is held: the owner's earlier cancel was a decision
            # about the OLD size, and a bigger position with no resting target is the worse default, so it
            # resets the hold and the close is placed for the new quantity. (A manual partial close, by
            # contrast, calls this with force_replace only -- the hold stays and nothing is placed.)
            position.pop("manual_hold", None)
            self.log.log_event("MANUAL_HOLD_CLEARED", f"{position['broker_symbol']}: new fill; placing a close order "
                               f"for qty {position['qty']} again", spread_id=position["id"])
        close_id = position.get("close_order_id")
        if close_id and not force_replace:
            if close_id.startswith("DRY-"):
                return
            try:
                order = self.broker.get_order(close_id)
            except BrokerError as exc:
                self.log.log_event("POLL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
                return
            if order is not None and order["status"] in OWNER_CLOSE_CHANGE:
                self._close_order_changed_by_owner(position, order)
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
                # Our own cancel: forget the id so that, if the new order cannot be placed, the next cycle sees
                # "no close order" instead of mistaking this cancel for an owner action.
                position["close_order_id"] = None
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

    def _close_order_changed_by_owner(self, position: dict[str, Any], old_order: dict[str, Any]) -> None:
        """The tracked GTC close is 'canceled' or 'replaced' (the owner cancelled or edited it in thinkorswim;
        sync_external has already established that the legs are still held). An edit shows up as the old id
        'replaced' plus a NEW open order for the same legs: adopt that one. If there is no such order the
        owner removed the close on purpose: set manual_hold and never re-create it."""
        sym, old_id, status = position["broker_symbol"], position.get("close_order_id"), old_order["status"]
        exp = _exp(position["expiration"])
        short_occ = self.broker.occ_symbol(sym, exp, position["right"], position["short_strike"])
        long_occ = self.broker.occ_symbol(sym, exp, position["right"], position["long_strike"])
        try:
            open_orders = self.broker.get_open_orders()
        except BrokerError as exc:
            self.log.log_event("POLL_ERROR", f"{sym} close {old_id} {status}: could not list open orders ({exc}); "
                               f"deciding next cycle", spread_id=position["id"], order_id=old_id)
            return
        match = self._match_close_order(open_orders, short_occ, long_occ)
        if match:
            position["close_order_id"] = match["id"]
            position["close_limit"] = match.get("limit_price")
            position["close_status"] = match["status"]
            self.log.log_event("CLOSE_ORDER_ADOPTED", f"{sym}: close {old_id} was {status} by the owner; adopted their "
                               f"open order {match['id']} @ {match.get('limit_price')}", spread_id=position["id"],
                               old_order_id=old_id, order_id=match["id"], limit=match.get("limit_price"))
            return
        position["manual_hold"] = True
        position["close_order_id"] = None
        position["close_status"] = f"{status} (owner)"
        self.log.log_event("CLOSE_ORDER_CANCELED_BY_OWNER", f"{sym}: close {old_id} {status} by the owner and no replacement "
                           f"is open; NOT re-creating it (the profit-floor trail and other exits keep running)",
                           spread_id=position["id"], order_id=old_id, status=status)

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
                           "profit target", order["id"], limit_debit=position.get("close_limit"))
        return True

    def _record_close(self, position: dict[str, Any], exit_debit: float | None, reason: str,
                      order_id: str | None, *, trigger_price: float | None = None, prev_price: float | None = None,
                      limit_debit: float | None = None) -> None:
        """Book a close. `exit_debit` is the broker's ACTUAL fill (None when unknown). The OFFICIAL exit price --
        the one realized_pl, the circuit breaker and the daily report run on -- follows config.EXIT_PNL_BASIS:
        "prev_tick" uses the mark one tick before the tick that triggered the exit (`prev_price`), falling back to
        the trigger mark itself (`trigger_price`) and then to the fill; a resting limit close (`limit_debit`, no
        trigger tick) books at its fill but never worse than its limit. "fill" is the broker's fill as-is. The
        real fill and its P/L are always kept beside the official figures."""
        fill_debit = exit_debit
        official = fill_debit
        basis = "fill"
        if self.config.EXIT_PNL_BASIS == "prev_tick":
            if prev_price is not None:
                official, basis = prev_price, "prev_tick"
            elif trigger_price is not None:
                official, basis = trigger_price, "trigger_mark"
            elif limit_debit is not None and fill_debit is not None and fill_debit > limit_debit:
                official, basis = limit_debit, "limit"
        qty = position["qty"]

        def pl(debit: float | None) -> float | None:
            return None if debit is None else round((position["entry_credit"] - debit) * 100 * qty, 2)

        realized, realized_fill = pl(official), pl(fill_debit)
        slippage = None if fill_debit is None or trigger_price is None else round(fill_debit - trigger_price, 2)
        detail = {"trigger_price": trigger_price, "prev_tick_price": prev_price, "fill_debit": fill_debit,
                  "realized_pl_actual_fill": realized_fill, "exit_slippage": slippage, "exit_basis": basis}
        record = {**position, "exit_debit": official, "realized_pl": realized, "close_reason": reason,
                  "closed_at": self.now_fn().isoformat(), "close_order_id": order_id, **detail}
        self.state["closed"].append(record)
        self.state["positions"].pop(position["id"], None)
        self.log.position_closed(position, official, realized, reason, order_id, **detail)
        # The breaker counts a REALIZED max loss. On the tick basis the bot's own decision mark counts too: a
        # max-loss stop is triggered by a mark at the line, and the tick before it may sit just short of it. The
        # broker's fill never does -- a paper fill far from the marks must not trip the breaker.
        debits = [d for d in (official, trigger_price if basis != "fill" else None) if d is not None]
        if debits and self.risk.is_realized_max_loss(position["entry_credit"], max(debits), spread_width(position)):
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
        # Keep the tick we are replacing (and when it was taken): an exit books at the tick BEFORE the one that
        # triggered it (EXIT_PNL_BASIS "prev_tick"). Without a timestamp on the old mark it cannot be trusted.
        if position.get("current_price") is not None and position.get("price_at"):
            position["prev_price"], position["prev_price_at"] = position["current_price"], position["price_at"]
        else:
            position.pop("prev_price", None)
            position.pop("prev_price_at", None)
        position["current_price"] = price
        position["price_at"] = self.now_fn().isoformat()
        position["unrealized_pl"] = round((position["entry_credit"] - price) * 100 * position["qty"], 2)
        return price

    def _exit_marks(self, position: dict[str, Any]) -> tuple[float | None, float | None]:
        """(trigger mark, prior-tick mark) for an exit being sent now. The trigger mark is the latest refreshed
        price; the prior tick is the one before it. Either is dropped when too old to be "just before the close"
        (EXIT_PREV_TICK_MAX_AGE_SEC) -- e.g. yesterday's last mark on the first cycle of the day."""
        trigger, prev = position.get("current_price"), position.get("prev_price")
        max_age = self.config.EXIT_PREV_TICK_MAX_AGE_SEC
        try:
            at = datetime.fromisoformat(str(position["price_at"]))
        except (KeyError, ValueError, TypeError):
            return None, None
        if trigger is None or (self.now_fn() - at).total_seconds() > max_age:
            return None, None
        try:
            gap = (at - datetime.fromisoformat(str(position["prev_price_at"]))).total_seconds()
        except (KeyError, ValueError, TypeError):
            return trigger, None
        return trigger, (prev if prev is not None and gap <= max_age else None)

    def dte(self, position: dict[str, Any]) -> int:
        return (_exp(position["expiration"]) - self.now_fn().date()).days

    def _resolve_expired(self, position: dict[str, Any]) -> None:
        """A position past its expiration date (only reachable when EXIT_DTE_ENABLED is off and it was
        left to ride): the broker settles the legs itself, so there is nothing left to manage or close.
        Once the legs are gone, book the record as expired. The realized P/L is not knowable from here
        (cash settlement / assignment is the broker's), so exit_debit and realized_pl stay None -- but the
        last mark before expiry and the unrealized P/L at that mark are kept on the record as an estimate
        (last_price_before_expiry / est_realized_pl) for analysis. Equity options settle physically, so
        shares of the underlying showing up at the broker (assignment or exercise) are flagged loudly;
        this bot never manages them. Legs still showing keep the position untouched and re-alert daily."""
        try:
            legs = self.broker.get_positions()
        except BrokerError as exc:
            self.log.log_event("POLL_ERROR", f"{position['broker_symbol']} expired-position check: {exc}",
                               spread_id=position["id"])
            return
        today = self.now_fn().date().isoformat()
        if self._legs_still_open(position, legs):
            if position.get("expired_alerted_on") != today:
                position["expired_alerted_on"] = today
                self.log.log_event("EXPIRED_STILL_OPEN", f"{position['broker_symbol']}: expired {position['expiration']} "
                                   f"but legs still at broker; needs manual attention", spread_id=position["id"])
            return
        shares = sum(int(leg["qty"]) for leg in legs
                     if leg.get("asset_class") != "option"
                     and str(leg.get("underlying") or leg.get("symbol", "")).upper() == position["broker_symbol"].upper())
        if shares:
            self.log.log_event("EXPIRED_SHARES", f"{position['broker_symbol']}: {shares:+d} shares at the broker after "
                               f"{position['expiration']} expiry (assignment/exercise?); the bot does not manage "
                               f"shares -- handle manually", spread_id=position["id"], shares=shares)
        position["last_price_before_expiry"] = position.get("current_price")
        position["est_realized_pl"] = position.get("unrealized_pl")
        # The expiry loss is realized (the position can no longer recover) but its exact size is not knowable
        # from here, so the last mark stands in: at/beyond the max-loss line counts as a realized max-loss hit.
        if self.risk.is_max_loss_hit(position["entry_credit"], position.get("current_price"), spread_width(position)):
            self.risk.record_max_loss_hit(position["id"], self.now_fn())
        self.log.log_event("EXPIRED", f"{position['broker_symbol']}: expired {position['expiration']}; settled at broker "
                           f"(last mark {position['last_price_before_expiry']}, est. P/L {position['est_realized_pl']})",
                           spread_id=position["id"])
        self._record_close(position, None, "expired (settled at broker; exit price unknown)",
                           position.get("close_order_id"))

    def _profit_floor_trail_exit(self, position: dict[str, Any], price: float) -> bool:
        """PROFIT_FLOOR_MODE "trail": once armed, track the best (lowest) spread price seen and exit at
        market on the first PROFIT_FLOOR_TRAIL_TICK give-back from it -- but only if the mid price still shows
        at least PROFIT_FLOOR_MIN_PROFIT of profit (a market close fills up to a half-spread worse). If the
        move reversed through entry before the exit could fire, do not sell the loss: disarm, and let the position ride to the profit target or
        expiration, re-arming only if it again reaches the arm level. Returns True if it exited."""
        cfg = self.config
        best = min(position.get("profit_floor_best", price), price)
        position["profit_floor_best"] = best
        if price - best < cfg.PROFIT_FLOOR_TRAIL_TICK - 1e-9:
            return False
        if price <= position["entry_credit"] - cfg.PROFIT_FLOOR_MIN_PROFIT + 1e-9:
            return self.exit_at_market(
                position, f"profit floor trail (armed, price {price} gave back {cfg.PROFIT_FLOOR_TRAIL_TICK:g} "
                          f"from best {best})")
        position["profit_floor_armed"] = False
        position.pop("profit_floor_best", None)
        self.log.log_event(
            "PROFIT_FLOOR_DISARMED", f"{position['broker_symbol']}: reversed to {price} (entry {position['entry_credit']}) "
            f"before the trail exit could book a profit; riding", spread_id=position["id"])
        return False

    # ---------------------------------------------------------- manual overrides

    def _position_age_sec(self, position: dict[str, Any], now: datetime | None = None) -> float:
        """Seconds since the position was opened or last grew (a fill); inf when neither is recorded."""
        now = now or self.now_fn()
        stamps = []
        for key in ("opened_at", "last_fill_at"):
            try:
                stamps.append(datetime.fromisoformat(str(position[key])))
            except (KeyError, ValueError, TypeError):
                pass
        return (now - max(stamps)).total_seconds() if stamps else float("inf")

    def _leg_matches(self, leg: dict[str, Any], position: dict[str, Any], strike: float) -> bool:
        """Is this broker position the option at `strike` of the tracked spread? Exact OCC symbol first (what
        reconcile() uses); falling back to the structured fields with punctuation-insensitive roots, so a
        dotted ticker the broker spells differently (BRK.B vs BRKB) is not mistaken for a closed leg."""
        if leg.get("asset_class") != "option":
            return False
        exp = _exp(position["expiration"])
        if leg.get("symbol") == self.broker.occ_symbol(position["broker_symbol"], exp, position["right"], strike):
            return True
        try:
            return (leg.get("expiration") == exp and leg.get("right") == position["right"]
                    and abs(float(leg["strike"]) - float(strike)) < 1e-6
                    and _norm_root(leg.get("underlying")) == _norm_root(position["broker_symbol"]))
        except (TypeError, ValueError, KeyError):
            return False

    def _leg_counts(self, position: dict[str, Any], legs: list[dict[str, Any]]) -> tuple[int, int]:
        """(contracts short at the spread's short strike, contracts long at its long strike) at the broker."""
        short = long = 0
        for leg in legs:
            qty = int(leg.get("qty") or 0)
            if qty < 0 and self._leg_matches(leg, position, position["short_strike"]):
                short += -qty
            elif qty > 0 and self._leg_matches(leg, position, position["long_strike"]):
                long += qty
        return short, long

    @staticmethod
    def _classify_legs(qty: int, short: int, long: int) -> str:
        """How the broker's holdings compare with the tracked spread of `qty` contracts:
        gone       neither leg held                          -> closed outside the bot
        naked      exactly one leg held                      -> freeze (manual_attention)
        unbalanced legs differ AND fewer than `qty` of one   -> freeze: shrinking to the smaller leg would
                   (not explainable as extra owner contracts)   leave an unmanaged naked contract behind
        partial    both legs equal and fewer than `qty`      -> manual partial close: shrink
        extra      more than `qty` of a leg (or unequal legs with `qty` fully held) -> the owner's contracts
        ok         exactly `qty` of each"""
        if short == 0 and long == 0:
            return "gone"
        if short == 0 or long == 0:
            return "naked"
        if min(short, long) < qty:
            return "partial" if short == long else "unbalanced"
        return "ok" if short == long == qty else "extra"

    def sync_external(self) -> None:
        """Compare every tracked spread with what the broker actually holds (one get_positions() call per
        cycle) and make the books follow the owner's manual actions instead of fighting them. An inconsistent
        picture must repeat identically for EXTERNAL_SYNC_CONFIRM_CYCLES consecutive cycles before anything
        changes (a positions API that lags a fill must never make the bot drop a real position); positions
        younger than EXTERNAL_SYNC_MIN_AGE_SEC and expired ones (_resolve_expired's job) are not looked at.
        Never acts on a failed read."""
        cfg = self.config
        self._sync_pending = set()
        if not cfg.EXTERNAL_SYNC_ENABLED or getattr(self.broker, "dry_run", False):
            return
        now = self.now_fn()
        candidates = [p for p in self.state["positions"].values()
                      if self.dte(p) >= 0 and self._position_age_sec(p, now) >= cfg.EXTERNAL_SYNC_MIN_AGE_SEC]
        if not candidates:
            return
        try:
            legs = self.broker.get_positions()
        except BrokerError as exc:
            self.log.log_event("POLL_ERROR", f"external-sync positions read failed: {exc}; skipping the sync this cycle")
            return
        for position in candidates:
            self._sync_one(position, legs)

    def _sync_one(self, position: dict[str, Any], legs: list[dict[str, Any]]) -> None:
        confirm = max(int(self.config.EXTERNAL_SYNC_CONFIRM_CYCLES), 1)
        qty = int(position["qty"])
        short, long = self._leg_counts(position, legs)
        kind = self._classify_legs(qty, short, long)
        frozen = bool(position.get("manual_attention"))
        if not frozen and kind in ("ok", "extra"):
            position.pop("ext_obs", None)
            self._note_extra(position, kind, short, long)
            return
        if frozen and kind in ("naked", "unbalanced"):
            position.pop("ext_obs", None)
            self._flag_manual_attention(position, kind, short, long)   # still frozen; re-alerts at most once a day
            return
        if kind == "partial":
            # Fewer contracts than tracked could be our own resting GTC close part-filling, not the owner.
            explained = self._own_partial_close(position, qty - short)
            if explained is None:
                self._sync_pending.add(position["id"])
                return
            if explained:
                position.pop("ext_obs", None)
                return
        sig = [kind, short, long]
        obs = position.get("ext_obs")
        count = int(obs["n"]) + 1 if isinstance(obs, dict) and obs.get("sig") == sig else 1
        if count < confirm:
            position["ext_obs"] = {"sig": sig, "n": count}   # lives in the position dict, so it survives state.json
            self._sync_pending.add(position["id"])
            return
        position.pop("ext_obs", None)
        if kind in ("naked", "unbalanced"):
            self._flag_manual_attention(position, kind, short, long)
            return
        if frozen and kind != "gone":   # both gone -> the position is booked as closed below
            position.pop("manual_attention", None)
            self.log.log_event("MANUAL_ATTENTION_CLEARED", f"{position['broker_symbol']}: legs consistent again "
                               f"(short {short}/long {long}); resuming normal management", spread_id=position["id"])
        if kind == "gone":
            self._book_external_close(position, via="sync_external")
        elif kind == "partial":
            self._apply_external_partial(position, short)
        else:
            self._note_extra(position, kind, short, long)

    def _own_partial_close(self, position: dict[str, Any], missing: int) -> bool | None:
        """True when the tracked close order is part-filled by at least the `missing` contracts (the bot's own
        GTC doing it), None when that could not be determined."""
        close_id = position.get("close_order_id")
        if not close_id or close_id.startswith("DRY-"):
            return False
        try:
            order = self.broker.get_order(close_id)
        except BrokerError as exc:
            self.log.log_event("POLL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
            return None
        return bool(order and order["status"] == "partially_filled" and int(order.get("filled_qty") or 0) >= missing)

    def _note_extra(self, position: dict[str, Any], kind: str, short: int, long: int) -> None:
        """The owner holds more contracts on our strikes: log it when it appears or changes, never touch them."""
        if kind != "extra":
            position.pop("external_extra", None)
            return
        if position.get("external_extra") == [short, long]:
            return
        position["external_extra"] = [short, long]
        self.log.log_event("EXTERNAL_EXTRA_CONTRACTS", f"{position['broker_symbol']} {position['short_strike']:g}/"
                           f"{position['long_strike']:g}: broker holds short {short}/long {long} but the bot tracks "
                           f"{position['qty']}; the extra contracts are the owner's and will never be managed or closed",
                           spread_id=position["id"], short_held=short, long_held=long, qty=position["qty"])

    def _flag_manual_attention(self, position: dict[str, Any], kind: str, short: int, long: int) -> None:
        """A naked (or unbalanced) leg: freeze all order activity for the position and alert loudly, once when it
        starts and then at most once per day while it persists."""
        today = self.now_fn().date().isoformat()
        newly = not position.get("manual_attention")
        position["manual_attention"] = True
        position.pop("ext_obs", None)
        if newly or position.get("naked_alerted_on") != today:
            position["naked_alerted_on"] = today
            what = "ONE LEG ONLY" if kind == "naked" else "UNBALANCED LEGS"
            self.log.log_event(
                "NAKED_LEG", f"{position['broker_symbol']} {position['right']} {position['short_strike']:g}/"
                f"{position['long_strike']:g}: {what} -- broker holds short {short} / long {long}, bot tracks qty "
                f"{position['qty']}. A leg was closed or changed by hand; the bot will NOT place, cancel or send any order "
                f"for this spread until the legs are consistent again or both are gone. Fix it in thinkorswim.",
                spread_id=position["id"], kind=kind, short_held=short, long_held=long, qty=position["qty"])

    def _apply_external_partial(self, position: dict[str, Any], held: int) -> None:
        """The owner closed part of the spread by hand: shrink the tracked qty to what is held and re-place the
        GTC close at that size (unless the owner has taken the GTC away: manual_hold)."""
        old_qty = int(position["qty"])
        position["qty"] = held
        position["external_closed_qty"] = int(position.get("external_closed_qty") or 0) + (old_qty - held)
        position["max_loss"] = round(spread_width(position) - position["entry_credit"], 2)
        if position.get("current_price") is not None:
            position["unrealized_pl"] = round((position["entry_credit"] - position["current_price"]) * 100 * held, 2)
        self.log.log_event("EXTERNAL_PARTIAL_CLOSE", f"{position['broker_symbol']} {position['short_strike']:g}/"
                           f"{position['long_strike']:g}: {old_qty - held} of {old_qty} contract(s) closed outside the bot; "
                           f"now tracking {held}", spread_id=position["id"], old_qty=old_qty, new_qty=held)
        self._ensure_close_order(position, force_replace=True)
        self.save()
        self._notify()

    def _book_external_close(self, position: dict[str, Any], via: str = "sync_external") -> None:
        """Both legs are gone and our own close order did not do it: the owner closed the spread by hand. Cancel
        our GTC (best effort), keep the last mark as an estimate, book the record with an unknown exit, and
        start the short manual-close cool-down on new entries. Never feeds the circuit breaker."""
        if self._check_close_filled(position):
            return   # it was our own GTC target that filled after all
        close_id = position.get("close_order_id")
        if close_id and not close_id.startswith("DRY-"):
            try:
                self.broker.cancel_order(close_id)
            except BrokerError as exc:
                self.log.log_event("CANCEL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}", order_id=close_id)
        position.pop("ext_obs", None)
        position["last_price_before_exit"] = position.get("current_price")
        position["est_realized_pl"] = position.get("unrealized_pl")
        self.log.log_event("EXTERNAL_CLOSE", f"{position['broker_symbol']} {position['right']} {position['short_strike']:g}/"
                           f"{position['long_strike']:g} x{position['qty']}: both legs gone at the broker ({via}); closed "
                           f"outside the bot (last mark {position['last_price_before_exit']}, est. P/L "
                           f"{position['est_realized_pl']})", spread_id=position["id"], via=via)
        now = self.now_fn()
        self.state["manual_cooldown_until"] = (now + timedelta(seconds=self.config.MANUAL_CLOSE_COOLDOWN_SEC)).isoformat()
        self._record_close(position, None, "closed outside the bot (manual)", close_id)   # exit_debit None: no breaker feed

    def maintain(self) -> None:
        cfg = self.config
        self.sync_external()   # first: reconcile the books with what the owner may have done by hand
        for position in list(self.state["positions"].values()):
            if self._check_close_filled(position):
                continue
            if self.dte(position) < 0:
                self._resolve_expired(position)
                continue
            if position.get("manual_attention") or position["id"] in self._sync_pending:
                # A leg is missing/mismatched at the broker (confirmed: manual_attention) or the picture is
                # inconsistent but not yet confirmed (_sync_pending): no GTC upkeep, no exits, no max-loss or
                # trail logic until it resolves one way or the other.
                continue
            if cfg.EXIT_DTE_ENABLED and self.dte(position) <= cfg.EXIT_DTE:
                self.exit_at_market(position, f"DTE {self.dte(position)} <= {cfg.EXIT_DTE}")
                continue
            self._ensure_close_order(position)
            price = self.refresh_price(position)
            if cfg.PROFIT_FLOOR_ENABLED and price is not None:
                arm_price = self._profit_floor_arm_price(position["entry_credit"])
                if not position.get("profit_floor_armed") and price <= arm_price:
                    position["profit_floor_armed"] = True
                    position["profit_floor_best"] = price
                    self.log.log_event(
                        "PROFIT_FLOOR_ARMED", f"{position['broker_symbol']}: armed at price {price} (arm level {arm_price})",
                        spread_id=position["id"])
                if position.get("profit_floor_armed"):
                    if cfg.PROFIT_FLOOR_MODE == "trail":
                        if self._profit_floor_trail_exit(position, price):
                            continue
                    else:
                        floor_price = self._profit_floor_price(position["entry_credit"])
                        if price >= floor_price:
                            self.exit_at_market(position, f"profit floor (armed, price {price} >= floor {floor_price})")
                            continue
            if self.risk.is_max_loss_hit(position["entry_credit"], price, spread_width(position)):
                # Informational only: the MARK has reached the max-loss line. The circuit breaker counts a
                # REALIZED loss (a close at >= MAX_LOSS_HIT_PCT of max loss in _record_close, or an expiry whose last
                # mark was at/beyond the line in _resolve_expired) -- a spread that dips there and recovers is
                # not a loss and must not move the breaker toward halting new entries.
                position["max_loss_hit"] = True
                if cfg.MAX_LOSS_EXIT:
                    self.exit_at_market(position, f"max loss hit (price {price})")
        self.save()

    def exit_at_market(self, position: dict[str, Any], reason: str) -> bool:
        """Close the spread at market. Returns True when the position is off the books (closed by us, or found
        already closed by the owner), False when nothing was closed (retried next cycle or left to the owner).
        A fresh positions read comes first so a close is never sent into legs the owner already closed, and the
        owner's own contracts on the same strikes are neither closed by us nor mistaken for a failed close."""
        if position.get("manual_attention"):
            return False
        sym = position["broker_symbol"]
        dry = bool(getattr(self.broker, "dry_run", False))
        close_qty = int(position["qty"])
        held_short = held_long = 0
        if not dry:
            try:
                legs = self.broker.get_positions()
            except BrokerError as exc:
                self.log.log_event("POLL_ERROR", f"{sym}: positions read failed before the exit ({exc}); not sending the "
                                   f"close this cycle ({reason})", spread_id=position["id"], reason=reason)
                return False
            held_short, held_long = self._leg_counts(position, legs)
            kind = self._classify_legs(close_qty, held_short, held_long)
            if kind in ("gone", "naked", "unbalanced") and self._position_age_sec(position) < \
                    self.config.EXTERNAL_SYNC_MIN_AGE_SEC:
                self.log.log_event("EXIT_DEFERRED", f"{sym}: legs not visible at the broker yet (short {held_short}/long "
                                   f"{held_long}) but the position is brand new; retrying next cycle ({reason})",
                                   spread_id=position["id"], reason=reason)
                return False
            if kind in ("gone", "naked", "unbalanced"):
                # one read can be empty or stale: look again, and act only if the two reads agree
                if self.config.EXTERNAL_EXIT_RECHECK_SEC > 0:
                    time_module.sleep(self.config.EXTERNAL_EXIT_RECHECK_SEC)
                try:
                    again = self._leg_counts(position, self.broker.get_positions())
                except BrokerError as exc:
                    self.log.log_event("POLL_ERROR", f"{sym}: positions re-read failed before the exit ({exc}); not "
                                       f"acting on it this cycle ({reason})", spread_id=position["id"], reason=reason)
                    return False
                if again != (held_short, held_long):
                    self.log.log_event("EXIT_DEFERRED", f"{sym}: two positions reads disagree ({held_short}/{held_long} "
                                       f"vs {again[0]}/{again[1]}); retrying next cycle ({reason})",
                                       spread_id=position["id"], reason=reason)
                    return False
            if kind == "gone":
                self.log.log_event("EXIT_SKIPPED", f"{sym}: both legs are already gone at the broker; not sending a close "
                                   f"({reason})", spread_id=position["id"], reason=reason)
                self._book_external_close(position, via="exit pre-flight")
                return True
            if kind in ("naked", "unbalanced"):
                self._flag_manual_attention(position, kind, held_short, held_long)
                return False
            close_qty = min(close_qty, held_short, held_long)
        close_id = position.get("close_order_id")
        if close_id and not close_id.startswith("DRY-"):
            try:
                self.broker.cancel_order(close_id)
            except BrokerError as exc:
                self.log.log_event("CANCEL_ERROR", f"{sym} close {close_id}: {exc}", order_id=close_id)
        try:
            order = self.broker.close_spread_at_market(
                sym, _exp(position["expiration"]), position["right"], position["short_strike"],
                position["long_strike"], close_qty,
            )
        except BrokerError as exc:
            self.log.log_event("EXIT_ERROR", f"{sym}: {exc}; will retry next cycle",
                               spread_id=position["id"], reason=reason)
            position["close_order_id"] = None
            self.save()
            return False
        if order["status"] == "dry_run":
            self.log.log_event("EXIT_DRY_RUN", f"{sym}: would close at market ({reason})",
                               spread_id=position["id"])
            return False
        if not dry:
            try:
                after = self.broker.get_positions()
            except BrokerError:
                after = None   # as before: an unreadable verification does not undo a close the broker reports filled
            if after is not None:
                short_after, long_after = self._leg_counts(position, after)
                # A count check, not "no legs left": the owner may hold more contracts on these strikes.
                if short_after > held_short - close_qty or long_after > held_long - close_qty:
                    self.log.log_event(
                        "EXIT_VERIFY_FAILED", f"{sym}: held after close short {short_after}/long {long_after}, expected at "
                        f"most {held_short - close_qty}/{held_long - close_qty} (was {held_short}/{held_long}, closed "
                        f"{close_qty}); will retry", spread_id=position["id"],
                        legs=[leg["symbol"] for leg in after
                              if self._leg_matches(leg, position, position["short_strike"])
                              or self._leg_matches(leg, position, position["long_strike"])])
                    position["close_order_id"] = None
                    self.save()
                    return False
        if close_qty != position["qty"]:
            position["qty"] = close_qty   # the rest was already closed by the owner; book only what we closed
        trigger, prev = self._exit_marks(position)
        self._record_close(position, order.get("filled_avg_price"), reason, order["id"],
                           trigger_price=trigger, prev_price=prev)
        return True

    def _legs_still_open(self, position: dict[str, Any], legs: list[dict[str, Any]] | None = None) -> list[str]:
        if legs is None:
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
                # A tracked spread whose legs sit inside this group (typically next to a leg the owner added) is
                # not paired by the arithmetic above but is not gone either: keep it tracked so it is not written
                # off as vanished while its legs are live. Any live leg of it counts (not only both), so a spread
                # that lost one leg while the bot was offline stays tracked and is flagged NAKED_LEG by
                # sync_external instead of being booked as vanished with a leg still open.
                for tracked_id, tracked in self.state["positions"].items():
                    if (tracked["broker_symbol"] == underlying and _exp(tracked["expiration"]) == expiration
                            and tracked["right"] == right
                            and any((m["qty"] < 0 and abs(m["strike"] - tracked["short_strike"]) < 1e-6)
                                    or (m["qty"] > 0 and abs(m["strike"] - tracked["long_strike"]) < 1e-6)
                                    for m in members)):
                        seen_ids.add(tracked_id)
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

        canceled_orphans: set[str] = set()
        for sid, position in list(self.state["positions"].items()):
            if sid not in seen_ids:
                orphan = self._resolve_vanished(position)
                if orphan:
                    canceled_orphans.add(orphan)

        # open_orders was read before those cancels; do not let a just-canceled close order look like an open one
        self._adopt_unknown_entries([o for o in open_orders if o["id"] not in canceled_orphans], legs)
        self.state["blocked_symbols"] = sorted(blocked)
        self.save()
        summary = {"adopted": adopted, "positions": len(self.state["positions"]), "unpaired": sorted(blocked),
                   "working_entries": len(self.state["working_entries"])}
        self.log.log_event("ADOPT", f"adopted {adopted} new spread(s); {len(self.state['positions'])} open; "
                           f"unpaired underlyings: {sorted(blocked) or 'none'}", **summary)
        return summary

    def _resolve_vanished(self, position: dict[str, Any]) -> str | None:
        """Book a tracked spread that is no longer at the broker; returns the id of the leftover close order it
        cancelled, if any."""
        close_id = position.get("close_order_id")
        canceled_id: str | None = None
        order = None
        if close_id and not close_id.startswith("DRY-"):
            try:
                order = self.broker.get_order(close_id)
            except BrokerError:
                order = None
        if order and order["status"] == "filled":
            self._record_close(position, float(order.get("filled_avg_price") or 0.0), "profit target (while offline)",
                               close_id, limit_debit=position.get("close_limit"))
        else:
            if close_id and not close_id.startswith("DRY-") and not (order and order["status"] in TERMINAL):
                # The legs are gone, so a still-working close order is an orphan (and could be mistaken for an
                # entry by _adopt_unknown_entries): take it off the book, best effort.
                try:
                    self.broker.cancel_order(close_id)
                    canceled_id = close_id
                except BrokerError as exc:
                    self.log.log_event("CANCEL_ERROR", f"{position['broker_symbol']} close {close_id}: {exc}",
                                       order_id=close_id)
            self.log.log_event("RECONCILE_MISMATCH", f"{position['id']}: in local state but not at broker; closing record with unknown exit",
                               spread_id=position["id"])
            self._record_close(position, None, "vanished at broker", close_id)
        return canceled_id

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
                # Only a real CREDIT spread opens a position: put sold strike > bought strike, call sold < bought.
                # The inverted shape is a closing order (buy back the short, sell the long), e.g. a still-working
                # GTC close, and adopting it as an entry would build a bogus inverted spread.
                and ((s["strike"] > l["strike"]) if s["right"] == "P" else (s["strike"] < l["strike"]))
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
            if p.get("manual_attention"):
                close = "MANUAL ATTENTION (leg mismatch; bot hands off)"
            elif p.get("manual_hold"):
                close = "NONE (canceled by owner; bot hands off)"
            else:
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
                fill = c.get("fill_debit")
                fill_txt = "" if fill is None or fill == c.get("exit_debit") else \
                    f" [fill {fill:.2f} = {c['realized_pl_actual_fill']:+.2f}]"
                lines.append(f"  {c['symbol']} {c['right']} {c['short_strike']:g}/{c['long_strike']:g} x{c['qty']} "
                             f"entry {c['entry_credit']:.2f} exit {exit_px} P&L {pl}{fill_txt} ({c['close_reason']})")
        open_risk = self.open_risk_dollars()
        risk_pct = f"{open_risk / equity:.1%}" if equity else "n/a"
        lines.append("-" * 96)
        lines.append(f"unrealized {self.unrealized_total():+,.2f} | realized today {self.realized_today_total():+,.2f} | "
                     f"portfolio risk ${open_risk:,.0f} ({risk_pct} of equity) | breaker "
                     f"{'TRIPPED' if self.risk.breaker_tripped else 'ok'} ({self.risk.max_loss_hits} hits)")
        lines.append("=" * 96)
        return "\n".join(lines)

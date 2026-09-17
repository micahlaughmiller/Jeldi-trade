"""Local paper-trading simulator for the Schwab bots (docs/BROKER_INTERFACE.md).

Schwab has no paper-trading API. PaperBroker keeps a simulated account in
LOG_DIR/sim_state.json and fills its orders against LIVE quotes fetched through a
wrapped read-only data broker (a dry-run SchwabBroker in production).

Fill model: all-or-nothing at the order's limit, regular hours only. A credit
(opening) spread fills when the natural credit (short bid - long ask) reaches the
limit; a debit (closing) spread fills when the natural debit (short ask - long bid)
falls to the limit. Legs past expiration cash-settle at intrinsic value.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:
    from .broker import BrokerError, occ_symbol, parse_occ
except ImportError:  # running as a plain script from the folder
    from broker import BrokerError, occ_symbol, parse_occ  # type: ignore

ET = ZoneInfo("US/Eastern")
HERE = Path(__file__).resolve().parent
TERMINAL_STATUSES = {"filled", "canceled", "rejected", "expired", "replaced"}
INDEX_UNDERLYINGS = {"SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "VIX", "VIXW", "DJX", "XSP"}
_EPS = 1e-9


def _hhmm(value: str) -> dtime:
    hh, mm = str(value).split(":")
    return dtime(int(hh), int(mm))


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class PaperBroker:
    name = "schwab-sim"
    is_paper = True
    dry_run = False

    occ_symbol = staticmethod(occ_symbol)
    parse_occ = staticmethod(parse_occ)

    def __init__(self, data_broker: Any, config_module: Any, log: Any = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self.data = data_broker
        self.config = config_module
        self._log_fn = self._pick_logger(log)
        self.now_fn = now_fn or (lambda: datetime.now(ET))
        self._sleep = time.sleep
        self.close_slippage = float(self._cfg("CLOSE_SLIPPAGE", 0.05))
        self.starting_equity = float(self._cfg("SIM_STARTING_EQUITY", 2000.0))
        self.cache_sec = float(self._cfg("SIM_QUOTE_CACHE_SEC", 10))
        self.fill_start = _hhmm(self._cfg("SIM_FILL_START", "09:30"))
        self.fill_end = _hhmm(self._cfg("SIM_FILL_END", "16:00"))
        self.index_fill_end = _hhmm(self._cfg("SIM_INDEX_FILL_END", "16:15"))
        log_dir = Path(self._cfg("LOG_DIR", None) or "logs")
        self.log_dir = log_dir if log_dir.is_absolute() else (HERE / log_dir).resolve()
        self.state_path = self.log_dir / "sim_state.json"
        self.equity_path = self.log_dir / "sim_equity_history.csv"
        self._chain_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self.state: dict[str, Any] = self._load_state()
        if hasattr(data_broker, "get_expiration_roots"):
            self.get_expiration_roots = data_broker.get_expiration_roots

    # ------------------------------------------------------------------ plumbing

    def _cfg(self, key: str, default: Any) -> Any:
        return getattr(self.config, key, default)

    @staticmethod
    def _pick_logger(log: Any) -> Callable[[str], None]:
        # Strategies pass a plain callable (0DTE: log.info, 45DTE: a lambda); tests pass a Logger.
        if log is None:
            return logging.getLogger("broker.sim").info
        return log.info if hasattr(log, "info") else log

    def _log(self, message: str) -> None:
        self._log_fn(message)

    def _fresh_state(self, starting_equity: float) -> dict[str, Any]:
        return {"cash": float(starting_equity), "starting_equity": float(starting_equity),
                "realized_pnl_total": 0.0, "positions": {}, "orders": {}, "fills": [], "day_open": None}

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return self._fresh_state(self.starting_equity)
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise BrokerError(f"corrupt simulator state {self.state_path}: {exc}") from exc
        state = self._fresh_state(saved.get("starting_equity", self.starting_equity))
        state.update(saved)
        return state

    def _save(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def reset(self, starting_equity: float | None = None) -> None:
        equity = self.starting_equity if starting_equity is None else float(starting_equity)
        self.state = self._fresh_state(equity)
        self._chain_cache.clear()
        self._save()
        if self.equity_path.exists():
            self.equity_path.unlink()
        self._log(f"SIM RESET account to ${equity:,.2f}")

    # ------------------------------------------------------------------ market data

    def get_expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]:
        return self.data.get_expirations(underlying, min_dte, max_dte)

    def get_spot(self, symbol: str) -> float:
        return self.data.get_spot(symbol)

    def get_option_chain(self, underlying: str, expiration: date, right: str,
                         strike_min: float | None = None, strike_max: float | None = None,
                         spot: float | None = None) -> list[dict]:
        key = (underlying.upper(), expiration.isoformat(), right.upper()[0], strike_min, strike_max)
        cached = self._chain_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self.cache_sec:
            rows = cached[1]
        else:
            rows = self.data.get_option_chain(underlying, expiration, right, strike_min=strike_min,
                                              strike_max=strike_max, spot=spot)
            self._chain_cache[key] = (time.monotonic(), rows)
        positions = self.state["positions"]
        for q in rows:
            if q["symbol"] in positions:
                positions[q["symbol"]]["last_mid"] = q["mid"]
        return [dict(q) for q in rows]

    @staticmethod
    def _pick(chain: list[dict], root: str, strike: float) -> dict | None:
        rows = [q for q in chain if abs(q["strike"] - strike) < 1e-6 and not (q["bid"] == 0 and q["ask"] == 0)]
        return next((q for q in rows if q["root"] == root), rows[0] if rows else None)

    def _quote_pair(self, spec: dict) -> tuple[dict, dict] | None:
        lo, hi = sorted((spec["short_strike"], spec["long_strike"]))
        try:
            chain = self.get_option_chain(spec["underlying"], date.fromisoformat(spec["expiration"]), spec["right"],
                                          strike_min=lo - 1e-6, strike_max=hi + 1e-6)
        except BrokerError as exc:
            self._log(f"SIM quote fetch failed for {spec['underlying']} {spec['expiration']} {spec['right']}: {exc}")
            return None
        short_q = self._pick(chain, spec["root"], spec["short_strike"])
        long_q = self._pick(chain, spec["root"], spec["long_strike"])
        return None if short_q is None or long_q is None else (short_q, long_q)

    def _refresh_mids(self) -> dict[str, float]:
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for pos in self.state["positions"].values():
            groups[(pos["underlying"], pos["expiration"], pos["right"])].append(pos)
        for (underlying, expiration, right), legs in groups.items():
            strikes = [p["strike"] for p in legs]
            try:
                chain = self.get_option_chain(underlying, date.fromisoformat(expiration), right,
                                              strike_min=min(strikes) - 1e-6, strike_max=max(strikes) + 1e-6)
            except BrokerError as exc:
                self._log(f"SIM mark refresh failed for {underlying} {expiration} {right}: {exc}")
                continue
            for pos in legs:
                q = self._pick(chain, pos["root"], pos["strike"])
                if q is not None:
                    pos["last_mid"] = q["mid"]
        return {sym: p["last_mid"] for sym, p in self.state["positions"].items()}

    # ------------------------------------------------------------------ sessions

    def _session_end(self, underlying: str) -> dtime:
        return self.index_fill_end if underlying.upper() in INDEX_UNDERLYINGS else self.fill_end

    def _fill_window_open(self, underlying: str, now: datetime) -> bool:
        return now.weekday() < 5 and self.fill_start <= now.time() < self._session_end(underlying)

    def _past_expiry(self, expiration: date, now: datetime) -> bool:
        return now.date() > expiration or (now.date() == expiration and now.time() >= self.index_fill_end)

    def _order_expired(self, order: dict, now: datetime) -> bool:
        spec = order["raw"]
        if self._past_expiry(date.fromisoformat(spec["expiration"]), now):
            return True
        if order["time_in_force"] != "day":
            return False
        submitted = datetime.fromisoformat(order["submitted_at"])
        return now.date() > submitted.date() or now.time() >= self._session_end(spec["underlying"])

    # ------------------------------------------------------------------ fill engine

    def poll(self) -> None:
        """Advance the simulation to `now`: settle expired legs, expire/fill working orders."""
        now = self.now_fn()
        changed = self._settle_expired(now)
        for order in list(self.state["orders"].values()):
            if order["status"] != "new":
                continue
            spec = order["raw"]
            if self._order_expired(order, now):
                self._set_status(order, "expired")
                self._log(f"SIM EXPIRE {order['id']} {self._describe(order)}")
                changed = True
                continue
            if not self._fill_window_open(spec["underlying"], now):
                continue
            quotes = self._quote_pair(spec)
            if quotes is None:
                continue
            short_q, long_q = quotes
            if spec["kind"] == "credit":
                if short_q["bid"] - long_q["ask"] + _EPS < order["limit_price"]:
                    continue
                short_px = short_q["bid"]
            else:
                if short_q["ask"] - long_q["bid"] - _EPS > order["limit_price"]:
                    continue
                short_px = short_q["ask"]
            self._fill(order, order["limit_price"], short_px, now)
            changed = True
        if changed:
            self._save()

    def _settle_expired(self, now: datetime) -> bool:
        changed = False
        for sym, pos in list(self.state["positions"].items()):
            if not self._past_expiry(date.fromisoformat(pos["expiration"]), now):
                continue
            try:
                spot = self.data.get_spot(pos["underlying"])
            except BrokerError as exc:
                self._log(f"SIM SETTLE deferred for {sym}: no spot ({exc})")
                continue
            intrinsic = max(spot - pos["strike"], 0.0) if pos["right"] == "C" else max(pos["strike"] - spot, 0.0)
            qty = pos["qty"]
            realized = self._apply_leg(sym, pos, -qty, round(intrinsic, 4), now)
            self.state["fills"].append({"order_id": None, "at": now.isoformat(), "kind": "settle", "symbol": sym,
                                        "qty": qty, "price": round(intrinsic, 4), "realized": round(realized, 2)})
            self._log(f"SIM SETTLE {sym} qty {qty} at intrinsic {intrinsic:.2f} (spot {spot:.2f}) "
                      f"realized {realized:+.2f} cash {self.state['cash']:,.2f}")
            changed = True
        return changed

    def _apply_leg(self, symbol: str, meta: dict, delta_qty: int, price: float, now: datetime) -> float:
        """Apply a signed quantity change at `price`; return realized P&L from any reduction."""
        positions = self.state["positions"]
        self.state["cash"] -= delta_qty * price * 100.0
        pos = positions.get(symbol)
        if pos is None:
            positions[symbol] = {"symbol": symbol, "root": meta["root"], "underlying": meta["underlying"],
                                 "expiration": meta["expiration"], "right": meta["right"], "strike": meta["strike"],
                                 "qty": delta_qty, "avg_price": price, "opened_at": now.isoformat(), "last_mid": price}
            return 0.0
        qty = pos["qty"]
        realized = 0.0
        if (qty > 0) == (delta_qty > 0):
            total = qty + delta_qty
            pos["avg_price"] = (abs(qty) * pos["avg_price"] + abs(delta_qty) * price) / abs(total)
            pos["qty"] = total
        else:
            closed = min(abs(qty), abs(delta_qty))
            sign = 1 if qty > 0 else -1
            realized = sign * (price - pos["avg_price"]) * 100.0 * closed
            pos["qty"] = qty + delta_qty
            if pos["qty"] != 0 and abs(delta_qty) > closed:
                # Flipped through zero: the leftover contracts open a fresh position at this price.
                pos["avg_price"] = price
                pos["opened_at"] = now.isoformat()
        pos["last_mid"] = price
        if pos["qty"] == 0:
            del positions[symbol]
        self.state["realized_pnl_total"] = round(self.state["realized_pnl_total"] + realized, 4)
        return realized

    def _holds_spread(self, short_sym: str, long_sym: str, qty: int) -> bool:
        positions = self.state["positions"]
        short_pos, long_pos = positions.get(short_sym), positions.get(long_sym)
        return (short_pos is not None and short_pos["qty"] <= -qty
                and long_pos is not None and long_pos["qty"] >= qty)

    def _fill(self, order: dict, net: float, short_px: float, now: datetime) -> None:
        spec, qty = order["raw"], order["qty"]
        short_sym, long_sym = order["legs"][0]["symbol"], order["legs"][1]["symbol"]
        credit = spec["kind"] == "credit"
        if not credit and not self._holds_spread(short_sym, long_sym, qty):
            self._set_status(order, "rejected")
            self._log(f"SIM REJECT {order['id']} {self._describe(order)}: legs no longer held")
            return
        long_px = round(short_px - net, 4)
        if long_px < 0:
            # Debit limit above the short's ask: keep the legs summing to the net price.
            long_px, short_px = 0.0, round(net, 4)
        meta = {"root": spec["root"], "underlying": spec["underlying"], "expiration": spec["expiration"],
                "right": spec["right"]}
        sign = -1 if credit else 1
        realized = self._apply_leg(short_sym, {**meta, "strike": spec["short_strike"]}, sign * qty, short_px, now)
        realized += self._apply_leg(long_sym, {**meta, "strike": spec["long_strike"]}, -sign * qty, long_px, now)
        order.update(status="filled", filled_qty=qty, filled_avg_price=round(net, 4), filled_at=now.isoformat())
        for leg, px in zip(order["legs"], (short_px, long_px)):
            leg["status"] = "filled"
            leg["filled_avg_price"] = round(px, 4)
        self.state["fills"].append({"order_id": order["id"], "at": now.isoformat(), "kind": spec["kind"],
                                    "qty": qty, "net": round(net, 4), "realized": round(realized, 2)})
        self._log(f"SIM FILL {order['id']} {self._describe(order)} @ {net:.2f} (legs {short_px:.2f}/{long_px:.2f})"
                  f" realized {realized:+.2f} cash {self.state['cash']:,.2f}")

    # ------------------------------------------------------------------ orders (read)

    @staticmethod
    def _set_status(order: dict, status: str) -> None:
        order["status"] = status
        for leg in order["legs"]:
            leg["status"] = status

    @staticmethod
    def _describe(order: dict) -> str:
        s = order["raw"]
        return (f"{s['kind']} {s['underlying']} {s['expiration']} {s['right']} {s['short_strike']:g}/{s['long_strike']:g}"
                f" x{order['qty']} limit {order['limit_price']:.2f} {order['time_in_force']}")

    @staticmethod
    def _public(order: dict) -> dict:
        out = dict(order, legs=[dict(leg) for leg in order["legs"]], raw=dict(order["raw"]))
        out["submitted_at"] = _dt(order["submitted_at"])
        out["filled_at"] = _dt(order["filled_at"])
        return out

    def get_order(self, order_id: str) -> dict | None:
        self.poll()
        order = self.state["orders"].get(str(order_id))
        return None if order is None else self._public(order)

    def get_open_orders(self) -> list[dict]:
        self.poll()
        return [self._public(o) for o in self.state["orders"].values() if o["status"] not in TERMINAL_STATUSES]

    def wait_for_fill(self, order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout_sec
        order = self.get_order(order_id)
        while order is not None and order["status"] not in TERMINAL_STATUSES and time.monotonic() < deadline:
            self._sleep(poll_sec)
            order = self.get_order(order_id)
        if order is None:
            raise BrokerError(f"order {order_id} not found while waiting for fill")
        return order

    # ------------------------------------------------------------------ orders (write)

    def _new_order(self, kind: str, underlying: str, expiration: date, right: str, short_strike: float,
                   long_strike: float, qty: int, limit: float, time_in_force: str, root: str | None,
                   client_tag: str | None) -> dict:
        if qty <= 0:
            raise BrokerError("qty must be positive")
        if limit <= 0:
            raise BrokerError("limit price must be positive")
        tif = time_in_force.lower()
        if tif not in ("day", "gtc"):
            raise BrokerError(f"unsupported time_in_force for spreads: {time_in_force!r}")
        right = right.upper()[0]
        if right not in ("P", "C"):
            raise BrokerError(f"right must be 'P' or 'C', got {right!r}")
        und = underlying.strip().upper().lstrip("$")
        r = (root or und).upper()
        opening = kind == "credit"
        legs = [
            {"symbol": occ_symbol(r, expiration, right, short_strike), "side": "sell" if opening else "buy",
             "position_intent": "sell_to_open" if opening else "buy_to_close", "ratio_qty": 1, "status": "new",
             "filled_avg_price": None},
            {"symbol": occ_symbol(r, expiration, right, long_strike), "side": "buy" if opening else "sell",
             "position_intent": "buy_to_open" if opening else "sell_to_close", "ratio_qty": 1, "status": "new",
             "filled_avg_price": None},
        ]
        return {
            "id": f"SIM-{uuid.uuid4().hex[:12]}", "status": "new", "symbol": und, "order_class": "mleg",
            "side": None, "qty": int(qty), "filled_qty": 0, "limit_price": round(float(limit), 4),
            "filled_avg_price": None, "time_in_force": tif, "legs": legs,
            "submitted_at": self.now_fn().isoformat(), "filled_at": None,
            "raw": {"kind": kind, "underlying": und, "root": r, "expiration": expiration.isoformat(), "right": right,
                    "short_strike": float(short_strike), "long_strike": float(long_strike),
                    "width": round(abs(float(short_strike) - float(long_strike)), 4), "client_tag": client_tag},
        }

    def _register(self, order: dict) -> dict:
        self.state["orders"][order["id"]] = order
        self._log(f"SIM ORDER new {order['id']} {self._describe(order)}")
        self._save()
        self.poll()
        return self._public(order)

    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict:
        order = self._new_order("credit", underlying, expiration, right, short_strike, long_strike, qty,
                                limit_credit, time_in_force, root, client_tag)
        self.poll()
        margin = max(order["raw"]["width"] - limit_credit, 0.0) * 100.0 * qty
        available = self._buying_power(self._refresh_mids())
        if available - margin < 0:
            raise BrokerError(f"SIM insufficient buying power: {available:,.2f} available, "
                              f"{margin:,.2f} required for {self._describe(order)}")
        return self._register(order)

    def place_close_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                           long_strike: float, qty: int, limit_debit: float, time_in_force: str = "gtc",
                           root: str | None = None, client_tag: str | None = None) -> dict:
        order = self._new_order("debit", underlying, expiration, right, short_strike, long_strike, qty,
                                limit_debit, time_in_force, root, client_tag)
        self.poll()
        if not self._holds_spread(order["legs"][0]["symbol"], order["legs"][1]["symbol"], qty):
            raise BrokerError(f"SIM cannot close {self._describe(order)}: spread not held in that size")
        return self._register(order)

    def close_spread_at_market(self, underlying: str, expiration: date, right: str, short_strike: float,
                               long_strike: float, qty: int, time_in_force: str = "gtc",
                               root: str | None = None, client_tag: str | None = None) -> dict:
        probe = self._new_order("debit", underlying, expiration, right, short_strike, long_strike, qty, 0.01,
                                time_in_force, root, client_tag or "close_at_market")
        self.poll()
        short_sym, long_sym = probe["legs"][0]["symbol"], probe["legs"][1]["symbol"]
        if not self._holds_spread(short_sym, long_sym, qty):
            raise BrokerError(f"SIM cannot close {self._describe(probe)}: spread not held in that size")
        quotes = self._quote_pair(probe["raw"])
        if quotes is not None:
            short_q, long_q = quotes
            natural, short_px = short_q["ask"] - long_q["bid"], short_q["ask"]
        else:
            positions = self.state["positions"]
            short_px = positions[short_sym]["last_mid"]
            natural = short_px - positions[long_sym]["last_mid"]
            self._log(f"SIM quotes unavailable for {self._describe(probe)}; closing at last known mids")
        price = round(min(max(natural, 0.01) + self.close_slippage, probe["raw"]["width"]), 2)
        order = dict(probe, limit_price=price, raw=dict(probe["raw"], market=True))
        self.state["orders"][order["id"]] = order
        self._log(f"SIM ORDER new {order['id']} {self._describe(order)} (marketable close)")
        self._fill(order, price, short_px, self.now_fn())
        self._save()
        return self._public(order)

    def replace_order_price(self, order_id: str, new_limit: float) -> dict:
        self.poll()
        old = self.state["orders"].get(str(order_id))
        if old is None:
            raise BrokerError(f"order {order_id} not found")
        if old["status"] != "new":
            raise BrokerError(f"order {order_id} is {old['status']}; only working orders can be replaced")
        if new_limit <= 0:
            raise BrokerError("limit price must be positive")
        self._set_status(old, "replaced")
        new = json.loads(json.dumps(old))
        new.update(id=f"SIM-{uuid.uuid4().hex[:12]}", status="new", limit_price=round(float(new_limit), 4),
                   submitted_at=self.now_fn().isoformat())
        self._set_status(new, "new")
        new["raw"]["replaces"] = old["id"]
        self._log(f"SIM REPLACE {old['id']} -> {new['id']} limit {old['limit_price']:.2f} -> {new_limit:.2f}")
        return self._register(new)

    def cancel_order(self, order_id: str) -> bool:
        self.poll()
        order = self.state["orders"].get(str(order_id))
        if order is None or order["status"] != "new":
            return False
        self._set_status(order, "canceled")
        self._log(f"SIM CANCEL {order['id']} {self._describe(order)}")
        self._save()
        return True

    def cancel_all_orders(self) -> int:
        self.poll()
        working = [o["id"] for o in self.state["orders"].values() if o["status"] == "new"]
        return sum(1 for oid in working if self.cancel_order(oid))

    # ------------------------------------------------------------------ account

    def _total_margin(self, mids: dict[str, float]) -> float:
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for pos in self.state["positions"].values():
            groups[(pos["underlying"], pos["expiration"], pos["right"])].append(dict(pos))
        total = 0.0
        for legs in groups.values():
            shorts = sorted((p for p in legs if p["qty"] < 0), key=lambda p: p["strike"])
            longs = sorted((p for p in legs if p["qty"] > 0), key=lambda p: p["strike"])
            for s in shorts:
                for l in longs:
                    paired = min(-s["qty"], l["qty"])
                    if paired <= 0:
                        continue
                    credit = s["avg_price"] - l["avg_price"]
                    total += max(abs(s["strike"] - l["strike"]) - credit, 0.0) * 100.0 * paired
                    s["qty"] += paired
                    l["qty"] -= paired
            for leg in shorts + longs:
                if leg["qty"] != 0:
                    total += abs(leg["qty"]) * mids.get(leg["symbol"], leg["last_mid"]) * 100.0
        return total

    def _buying_power(self, mids: dict[str, float]) -> float:
        return self.state["cash"] - self._total_margin(mids)

    def _equity(self, mids: dict[str, float]) -> float:
        return self.state["cash"] + sum(p["qty"] * mids.get(sym, p["last_mid"]) * 100.0
                                        for sym, p in self.state["positions"].items())

    def get_account(self) -> dict:
        self.poll()
        mids = self._refresh_mids()
        bp = round(self._buying_power(mids), 2)
        return {"equity": round(self._equity(mids), 2), "cash": round(self.state["cash"], 2),
                "buying_power": bp, "options_buying_power": bp, "account_id": "SIM"}

    def get_positions(self) -> list[dict]:
        self.poll()
        mids = self._refresh_mids()
        out = []
        for sym in sorted(self.state["positions"]):
            p = self.state["positions"][sym]
            mid = mids.get(sym, p["last_mid"])
            out.append({
                "symbol": sym, "underlying": p["underlying"], "qty": p["qty"], "avg_price": round(p["avg_price"], 4),
                "current_price": mid, "market_value": round(mid * 100.0 * p["qty"], 2),
                "unrealized_pl": round((mid - p["avg_price"]) * 100.0 * p["qty"], 2), "asset_class": "option",
                "expiration": date.fromisoformat(p["expiration"]), "strike": p["strike"], "right": p["right"],
            })
        return out

    # ------------------------------------------------------------------ equity journal

    def _read_history(self) -> list[tuple[date, float]]:
        if not self.equity_path.is_file():
            return []
        rows: list[tuple[date, float]] = []
        with self.equity_path.open(newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or row[0] == "date":
                    continue
                try:
                    rows.append((date.fromisoformat(row[0]), float(row[1])))
                except ValueError:
                    continue
        rows.sort(key=lambda r: r[0])
        return rows

    def record_daily_equity(self) -> None:
        equity = self.get_account()["equity"]
        today = self.now_fn().date()
        day_open = self.state.get("day_open")
        if not day_open or day_open.get("date") != today.isoformat():
            self.state["day_open"] = {"date": today.isoformat(), "equity": equity}
            self._save()
        rows = [r for r in self._read_history() if r[0] != today]
        rows.append((today, equity))
        rows.sort(key=lambda r: r[0])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.equity_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "equity"])
            for d, e in rows:
                writer.writerow([d.isoformat(), f"{e:.2f}"])

    def get_pnl_summary(self) -> dict:
        now = self.now_fn()
        today = now.date()
        result: dict[str, Any] = {"ytd": None, "mtd": None, "today": None, "as_of": now}
        try:
            equity = self.get_account()["equity"]
            prior = [r for r in self._read_history() if r[0] < today]
            day_open = self.state.get("day_open")
            if day_open and day_open.get("date") == today.isoformat():
                base = day_open["equity"]
            else:
                base = prior[-1][1] if prior else self.state["starting_equity"]
            result["today"] = round(equity - base, 2)
            year_rows = [r for r in prior if r[0].year == today.year]
            month_rows = [r for r in year_rows if r[0].month == today.month]
            if year_rows:
                result["ytd"] = round(equity - year_rows[0][1], 2)
            if month_rows:
                result["mtd"] = round(equity - month_rows[0][1], 2)
        except Exception as exc:
            self._log(f"SIM get_pnl_summary failed: {exc}")
        return result

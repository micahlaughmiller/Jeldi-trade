"""In-memory Broker implementing docs/BROKER_INTERFACE.md for tests."""

from __future__ import annotations

import itertools
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from broker import BrokerError

ET = ZoneInfo("US/Eastern")


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root}{expiration:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> dict[str, Any]:
    body = symbol[-15:]
    return {
        "root": symbol[:-15],
        "expiration": datetime.strptime(body[:6], "%y%m%d").date(),
        "right": body[6],
        "strike": int(body[7:]) / 1000.0,
    }


def make_quote(underlying: str, expiration: date, right: str, strike: float, bid: float, ask: float,
               delta: float | None) -> dict[str, Any]:
    return {
        "symbol": occ_symbol(underlying, expiration, right, strike), "underlying": underlying, "root": underlying,
        "expiration": expiration, "strike": strike, "right": right, "bid": bid, "ask": ask,
        "mid": round((bid + ask) / 2, 4), "last": None, "delta": delta, "iv": None, "quote_time": None,
    }


class FakeBroker:
    name = "fake"
    is_paper = True
    dry_run = False

    def __init__(self, equity: float = 25_000.0, now: datetime | None = None) -> None:
        self.equity = equity
        self.now = now or datetime(2026, 9, 17, 10, 0, tzinfo=ET)
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = []
        self.expirations: dict[str, list[date]] = {}
        self.chains: dict[tuple[str, date, str], list[dict[str, Any]]] = {}
        self.spots: dict[str, float] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_on: set[str] = set()
        self._ids = itertools.count(1)
        self.equity_journal: list[float] = []

    # ------------------------------------------------------------ helpers

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))
        if method in self.fail_on:
            raise BrokerError(f"forced failure in {method}")

    def _new_order(self, **fields: Any) -> dict[str, Any]:
        order = {
            "id": f"O{next(self._ids)}", "status": "accepted", "symbol": fields.get("symbol", ""),
            "order_class": "mleg", "side": None, "qty": fields.get("qty", 0), "filled_qty": 0,
            "limit_price": fields.get("limit_price"), "filled_avg_price": None,
            "time_in_force": fields.get("time_in_force", "gtc"), "legs": fields.get("legs", []),
            "submitted_at": self.now, "filled_at": None, "raw": {}, "kind": fields.get("kind"),
        }
        self.orders[order["id"]] = order
        return order

    def _legs(self, underlying: str, expiration: date, right: str, short_strike: float, long_strike: float,
              closing: bool) -> list[dict[str, Any]]:
        short_sym = occ_symbol(underlying, expiration, right, short_strike)
        long_sym = occ_symbol(underlying, expiration, right, long_strike)
        if closing:
            return [
                {"symbol": short_sym, "side": "buy", "position_intent": "buy_to_close", "ratio_qty": 1, "status": "accepted", "filled_avg_price": None},
                {"symbol": long_sym, "side": "sell", "position_intent": "sell_to_close", "ratio_qty": 1, "status": "accepted", "filled_avg_price": None},
            ]
        return [
            {"symbol": short_sym, "side": "sell", "position_intent": "sell_to_open", "ratio_qty": 1, "status": "accepted", "filled_avg_price": None},
            {"symbol": long_sym, "side": "buy", "position_intent": "buy_to_open", "ratio_qty": 1, "status": "accepted", "filled_avg_price": None},
        ]

    def add_position(self, underlying: str, expiration: date, right: str, strike: float, qty: int,
                     avg_price: float, current_price: float | None = None) -> dict[str, Any]:
        pos = {
            "symbol": occ_symbol(underlying, expiration, right, strike), "underlying": underlying, "qty": qty,
            "avg_price": avg_price, "current_price": current_price, "market_value": None, "unrealized_pl": None,
            "asset_class": "option", "expiration": expiration, "strike": strike, "right": right,
        }
        self.positions.append(pos)
        return pos

    def fill_order(self, order_id: str, price: float, qty: int | None = None) -> dict[str, Any]:
        order = self.orders[order_id]
        total = order["qty"]
        filled = total if qty is None else qty
        order["filled_qty"] = filled
        order["filled_avg_price"] = price
        order["status"] = "filled" if filled >= total else "partially_filled"
        order["filled_at"] = self.now
        spec = order["spec"]
        if order["kind"] == "entry":
            self._apply_entry_fill(spec, filled - order.get("_applied", 0), price)
        else:
            self._apply_close_fill(spec, filled - order.get("_applied", 0))
        order["_applied"] = filled
        return order

    def _apply_entry_fill(self, spec: dict[str, Any], qty: int, credit: float) -> None:
        u, e, r, s, l = spec["underlying"], spec["expiration"], spec["right"], spec["short"], spec["long"]
        for strike, signed, avg in ((s, -qty, round(credit + 0.50, 2)), (l, qty, 0.50)):
            sym = occ_symbol(u, e, r, strike)
            existing = next((p for p in self.positions if p["symbol"] == sym), None)
            if existing:
                existing["qty"] += signed
            else:
                self.add_position(u, e, r, strike, signed, avg)

    def _apply_close_fill(self, spec: dict[str, Any], qty: int) -> None:
        u, e, r = spec["underlying"], spec["expiration"], spec["right"]
        for strike, signed in ((spec["short"], qty), (spec["long"], -qty)):
            sym = occ_symbol(u, e, r, strike)
            pos = next((p for p in self.positions if p["symbol"] == sym), None)
            if pos:
                pos["qty"] += signed
        self.positions = [p for p in self.positions if p["qty"] != 0]

    def spread_price(self, underlying: str, expiration: date, right: str, short: float, long: float) -> float:
        chain = self.chains.get((underlying, expiration, right), [])
        s = next((q for q in chain if q["strike"] == short), None)
        l = next((q for q in chain if q["strike"] == long), None)
        return round(s["mid"] - l["mid"], 2) if s and l else 0.0

    # ------------------------------------------------------------ account

    def get_account(self) -> dict[str, Any]:
        self._record("get_account")
        return {"equity": self.equity, "cash": self.equity * 0.8, "buying_power": self.equity * 2,
                "options_buying_power": self.equity * 0.8, "account_id": "FAKE"}

    def get_pnl_summary(self) -> dict[str, Any]:
        return {"ytd": 1000.0, "mtd": 100.0, "today": 10.0, "as_of": self.now}

    def record_daily_equity(self) -> None:
        self.equity_journal.append(self.equity)

    # --------------------------------------------------- positions / orders

    def get_positions(self) -> list[dict[str, Any]]:
        self._record("get_positions")
        return [dict(p) for p in self.positions]

    def get_open_orders(self) -> list[dict[str, Any]]:
        self._record("get_open_orders")
        return [dict(o) for o in self.orders.values() if o["status"] in ("new", "accepted", "pending", "partially_filled")]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        self._record("get_order", order_id)
        order = self.orders.get(order_id)
        return dict(order) if order else None

    def wait_for_fill(self, order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> dict[str, Any]:
        return self.get_order(order_id) or {}

    # ------------------------------------------------------------- chain

    def get_expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]:
        self._record("get_expirations", underlying, min_dte, max_dte)
        today = self.now.date()
        return sorted(e for e in self.expirations.get(underlying, []) if min_dte <= (e - today).days <= max_dte)

    def get_option_chain(self, underlying: str, expiration: date, right: str, strike_min: float | None = None,
                         strike_max: float | None = None, spot: float | None = None) -> list[dict[str, Any]]:
        self._record("get_option_chain", underlying, expiration, right, spot=spot)
        chain = self.chains.get((underlying, expiration, right), [])
        return sorted((dict(q) for q in chain if not (q["bid"] == 0 and q["ask"] == 0)), key=lambda q: q["strike"])

    def get_spot(self, symbol: str) -> float:
        self._record("get_spot", symbol)
        return self.spots.get(symbol, 100.0)

    # ------------------------------------------------------------ orders

    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict[str, Any]:
        self._record("place_credit_spread", underlying, expiration, right, short_strike, long_strike, qty,
                     limit_credit=limit_credit, time_in_force=time_in_force)
        order = self._new_order(symbol=underlying, qty=qty, limit_price=limit_credit, time_in_force=time_in_force,
                                legs=self._legs(underlying, expiration, right, short_strike, long_strike, False), kind="entry")
        order["spec"] = {"underlying": underlying, "expiration": expiration, "right": right, "short": short_strike, "long": long_strike}
        return dict(order)

    def place_close_spread(self, underlying: str, expiration: date, right: str, short_strike: float, long_strike: float,
                           qty: int, limit_debit: float, time_in_force: str = "gtc", root: str | None = None,
                           client_tag: str | None = None) -> dict[str, Any]:
        self._record("place_close_spread", underlying, expiration, right, short_strike, long_strike, qty,
                     limit_debit=limit_debit, time_in_force=time_in_force)
        order = self._new_order(symbol=underlying, qty=qty, limit_price=limit_debit, time_in_force=time_in_force,
                                legs=self._legs(underlying, expiration, right, short_strike, long_strike, True), kind="close")
        order["spec"] = {"underlying": underlying, "expiration": expiration, "right": right, "short": short_strike, "long": long_strike}
        return dict(order)

    def close_spread_at_market(self, underlying: str, expiration: date, right: str, short_strike: float,
                               long_strike: float, qty: int, root: str | None = None,
                               client_tag: str | None = None) -> dict[str, Any]:
        self._record("close_spread_at_market", underlying, expiration, right, short_strike, long_strike, qty)
        order = self.place_close_spread(underlying, expiration, right, short_strike, long_strike, qty,
                                        limit_debit=self.spread_price(underlying, expiration, right, short_strike, long_strike),
                                        time_in_force="day")
        return dict(self.fill_order(order["id"], order["limit_price"]))

    def replace_order_price(self, order_id: str, new_limit: float) -> dict[str, Any]:
        self._record("replace_order_price", order_id, new_limit)
        old = self.orders[order_id]
        if old["status"] not in ("new", "accepted", "pending", "partially_filled"):
            raise BrokerError(f"order {order_id} is {old['status']}")
        old["status"] = "replaced"
        new = self._new_order(symbol=old["symbol"], qty=old["qty"], limit_price=new_limit,
                              time_in_force=old["time_in_force"], legs=old["legs"], kind=old["kind"])
        new["spec"] = old["spec"]
        return dict(new)

    def cancel_order(self, order_id: str) -> bool:
        self._record("cancel_order", order_id)
        order = self.orders.get(order_id)
        if not order or order["status"] in ("filled", "canceled"):
            return False
        order["status"] = "canceled"
        return True

    def cancel_all_orders(self) -> int:
        return sum(1 for o in list(self.orders.values()) if self.cancel_order(o["id"]))

    # ---------------------------------------------------------- utilities

    @staticmethod
    def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
        return occ_symbol(root, expiration, right, strike)

    @staticmethod
    def parse_occ(symbol: str) -> dict[str, Any]:
        return parse_occ(symbol)

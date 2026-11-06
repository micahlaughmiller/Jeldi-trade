"""In-memory Broker implementing docs/BROKER_INTERFACE.md for tests."""

import uuid
from datetime import date, datetime

import config
from broker import BrokerError

SPX_NAMES = ("SPX", "^GSPC", "SPXW")


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root}{expiration.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


def _chain_key(root: str) -> str | None:
    return None if root == "SPXW" else root


class FakeBroker:
    name = "fake"
    is_paper = True
    dry_run = False
    supports_resting_exits = False      # tests of the resting exits set this True on their broker

    def __init__(self, equity: float = 10_000.0, today: date | None = None):
        self.equity = equity
        self.today = today or date(2026, 9, 17)
        self.positions: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        # (underlying, right) -> {strike: (bid, ask)}; underlying None is the SPX chain
        self.chains: dict[tuple[str | None, str], dict[float, tuple[float, float]]] = {(None, "P"): {}, (None, "C"): {}}
        self.expirations: dict[str, list[date]] = {}
        self.spots: dict[str, float] = {}
        self.spread_close_price: float | None = None
        self.close_failures = 0
        self.close_calls: list[dict] = []
        self.close_roots: list[str] = []
        self.chain_calls: list[tuple[str, date, str]] = []
        self.chain_errors: set[str] = set()
        self.fill_entries = True
        self.oco_calls: list[dict] = []
        self.oco_status = "accepted"         # what a placed OCO reports; "rejected" simulates Schwab refusing it
        self.oco_reason: str | None = None
        self.oco_failures = 0
        self.oco_mode = "oco"                # "target": the broker rests the profit target alone (Schwab refused the OCO form)
        self.spot = 7500.0
        self.equity_history: list[float] = []

    # ---------------------------------------------------------------- account
    def get_account(self) -> dict:
        return {"equity": self.equity, "cash": self.equity, "buying_power": self.equity * 2,
                "options_buying_power": self.equity, "account_id": "FAKE"}

    def get_pnl_summary(self) -> dict:
        return {"ytd": 0.0, "mtd": 0.0, "today": 0.0, "as_of": datetime.now(config.ET)}

    def record_daily_equity(self) -> None:
        self.equity_history.append(self.equity)

    # -------------------------------------------------------------- positions
    def set_chain(self, right: str, quotes: dict[float, tuple[float, float]], underlying: str | None = None) -> None:
        self.chains[(underlying.upper() if underlying else None, right)] = dict(quotes)

    def _chain(self, right: str, underlying: str | None = None) -> dict[float, tuple[float, float]]:
        return self.chains.get((underlying, right), self.chains[(None, right)])

    def _leg_price(self, right: str, strike: float, underlying: str | None = None) -> float:
        bid, ask = self._chain(right, underlying).get(strike, (0.0, 0.0))
        return round((bid + ask) / 2, 2)

    def add_spread_position(self, right: str, short_strike: float, long_strike: float, qty: int,
                            short_avg: float, long_avg: float, expiration: date | None = None,
                            root: str = "SPXW") -> None:
        exp = expiration or self.today
        underlying = "SPX" if root == "SPXW" else root
        for strike, signed_qty, avg in ((short_strike, -qty, short_avg), (long_strike, qty, long_avg)):
            sym = occ_symbol(root, exp, right, strike)
            self.positions[sym] = {
                "symbol": sym, "underlying": underlying, "qty": signed_qty, "avg_price": avg,
                "current_price": self._leg_price(right, strike, _chain_key(root)) or avg, "market_value": None,
                "unrealized_pl": None, "asset_class": "option", "expiration": exp,
                "strike": float(strike), "right": right,
            }

    def _reduce_spread(self, root: str, exp: date, right: str, short_strike: float, long_strike: float, qty: int) -> None:
        for strike, delta in ((short_strike, qty), (long_strike, -qty)):
            sym = occ_symbol(root, exp, right, strike)
            pos = self.positions.get(sym)
            if pos is None:
                continue
            pos["qty"] += delta
            if pos["qty"] == 0:
                del self.positions[sym]

    def get_positions(self) -> list[dict]:
        return [dict(p) for p in self.positions.values()]

    # ----------------------------------------------------------------- orders
    def _order(self, symbol: str, qty: int, limit: float | None, status: str, filled_price: float | None,
               filled_qty: int) -> dict:
        o = {"id": str(uuid.uuid4()), "status": status, "symbol": symbol, "order_class": "mleg",
             "side": None, "qty": qty, "filled_qty": filled_qty, "limit_price": limit,
             "filled_avg_price": filled_price, "time_in_force": "day", "legs": [],
             "submitted_at": datetime.now(config.ET), "filled_at": None, "raw": {}}
        self.orders[o["id"]] = o
        return dict(o)

    def get_open_orders(self) -> list[dict]:
        return [dict(o) for o in self.orders.values()
                if o["status"] in ("new", "accepted", "pending", "partially_filled")]

    def get_order(self, order_id: str) -> dict | None:
        o = self.orders.get(order_id)
        return dict(o) if o else None

    def wait_for_fill(self, order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> dict:
        return self.get_order(order_id)

    # ------------------------------------------------------------------ chain
    def get_expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]:
        exps = self.expirations.get(underlying.upper())
        if exps is None:
            return [self.today]
        return [d for d in sorted(exps) if min_dte <= (d - self.today).days <= max_dte]

    def get_option_chain(self, underlying: str, expiration: date, right: str,
                         strike_min: float | None = None, strike_max: float | None = None,
                         spot: float | None = None) -> list[dict]:
        is_spx = underlying.upper() in SPX_NAMES
        root = "SPXW" if is_spx else underlying.upper()
        self.chain_calls.append((root, expiration, right))
        if root in self.chain_errors:
            raise BrokerError(f"simulated chain failure for {root}")
        out = []
        for strike, (bid, ask) in sorted(self._chain(right, _chain_key(root)).items()):
            if strike_min is not None and strike < strike_min:
                continue
            if strike_max is not None and strike > strike_max:
                continue
            if bid == 0 and ask == 0:
                continue
            out.append({"symbol": occ_symbol(root, expiration, right, strike), "underlying": underlying,
                        "root": root, "expiration": expiration, "strike": float(strike), "right": right,
                        "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2), "last": None,
                        "delta": None, "iv": None, "quote_time": None})
        return out

    def get_spot(self, symbol: str) -> float:
        return self.spots.get(symbol.upper(), self.spot)

    # ------------------------------------------------------------- mutations
    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict:
        root = root or underlying
        sym = f"{occ_symbol(root, expiration, right, short_strike)}/{occ_symbol(root, expiration, right, long_strike)}"
        if not self.fill_entries:
            return self._order(sym, qty, limit_credit, "accepted", None, 0)
        long_avg = self._leg_price(right, long_strike, _chain_key(root))
        self.add_spread_position(right, short_strike, long_strike, qty, round(limit_credit + long_avg, 2),
                                 long_avg, expiration, root)
        return self._order(sym, qty, limit_credit, "filled", limit_credit, qty)

    def place_close_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                           long_strike: float, qty: int, limit_debit: float, time_in_force: str = "gtc",
                           root: str | None = None, client_tag: str | None = None) -> dict:
        root = root or underlying
        self._reduce_spread(root, expiration, right, short_strike, long_strike, qty)
        return self._order("close", qty, limit_debit, "filled", limit_debit, qty)

    def close_spread_at_market(self, underlying: str, expiration: date, right: str, short_strike: float,
                               long_strike: float, qty: int, time_in_force: str = "gtc",
                               root: str | None = None, client_tag: str | None = None) -> dict:
        self.close_calls.append({"right": right, "short": short_strike, "long": long_strike, "qty": qty})
        self.close_roots.append(root or underlying)
        if self.close_failures > 0:
            self.close_failures -= 1
            raise BrokerError("simulated close failure")
        root = root or underlying
        price = self.spread_close_price
        if price is None:
            price = round(self._leg_price(right, short_strike, _chain_key(root))
                          - self._leg_price(right, long_strike, _chain_key(root)), 2)
        self._reduce_spread(root, expiration, right, short_strike, long_strike, qty)
        return self._order("close", qty, price, "filled", price, qty)

    def replace_order_price(self, order_id: str, new_limit: float) -> dict:
        old = self.orders[order_id]
        old["status"] = "replaced"
        return self._order(old["symbol"], old["qty"], new_limit, "accepted", None, 0)

    def cancel_order(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if o is None or o["status"] in ("filled", "canceled"):
            return False
        o["status"] = "canceled"
        for child in o.get("children") or []:
            if child["status"] not in ("filled", "canceled"):
                child["status"] = "canceled"
        return True

    # ------------------------------------------------------------------ resting exits (OCO)
    def place_oco_close(self, underlying: str, expiration: date, right: str, short_strike: float, long_strike: float,
                        qty: int, target_debit: float, stop_price: float, stop_limit: float, time_in_force: str = "day",
                        root: str | None = None, client_tag: str | None = None) -> dict:
        root = root or underlying
        self.oco_calls.append({"right": right, "short": short_strike, "long": long_strike, "qty": qty, "target": target_debit,
                               "stop": stop_price, "limit": stop_limit, "tif": time_in_force, "root": root})
        if self.oco_failures > 0:
            self.oco_failures -= 1
            raise BrokerError("simulated OCO failure")
        if self.oco_mode == "target":
            order = self._order(root, qty, target_debit, self.oco_status, None, 0)
            stored = self.orders[order["id"]]
            stored["exit_role"], stored["spread"] = "target", (root, expiration, right, short_strike, long_strike)
            stored["status_description"] = self.oco_reason
            return dict(stored, exit_mode="target", children=[dict(stored)])
        target = self._order(root, qty, target_debit, self.oco_status, None, 0)
        stop = self._order(root, qty, stop_limit, self.oco_status, None, 0)
        target, stop = self.orders[target["id"]], self.orders[stop["id"]]
        target["exit_role"], stop["exit_role"], stop["stop_price"] = "target", "stop", stop_price
        parent = {"id": str(uuid.uuid4()), "status": self.oco_status, "status_description": self.oco_reason, "symbol": root,
                  "order_class": "oco", "side": None, "qty": qty, "filled_qty": 0, "limit_price": None, "filled_avg_price": None,
                  "time_in_force": time_in_force, "legs": [], "children": [target, stop],
                  "spread": (root, expiration, right, short_strike, long_strike),
                  "submitted_at": datetime.now(config.ET), "filled_at": None, "raw": {}}
        target["oco_parent_id"] = stop["oco_parent_id"] = parent["id"]
        self.orders[parent["id"]] = parent
        return dict(parent)

    def fill_oco(self, parent_id: str, role: str, price: float, qty: int | None = None) -> None:
        """Schwab fills one child of the OCO (and cancels the other); the spread leaves the account."""
        parent = self.orders[parent_id]
        if "children" not in parent:                              # a plain resting target
            parent["status"], parent["filled_qty"], parent["filled_avg_price"] = "filled", qty or parent["qty"], price
            root, expiration, right, short, long = parent["spread"]
            self._reduce_spread(root, expiration, right, short, long, qty or parent["qty"])
            return
        for child in parent["children"]:
            if child["exit_role"] == role:
                child["status"], child["filled_qty"], child["filled_avg_price"] = "filled", qty or child["qty"], price
            elif child["status"] not in ("filled",):
                child["status"] = "canceled"
        parent["status"], parent["filled_qty"], parent["filled_avg_price"] = "filled", qty or parent["qty"], price
        root, expiration, right, short, long = parent["spread"]
        self._reduce_spread(root, expiration, right, short, long, qty or parent["qty"])

    def cancel_all_orders(self) -> int:
        return sum(self.cancel_order(oid) for oid in list(self.orders))

"""In-memory Broker implementing docs/BROKER_INTERFACE.md for tests."""

import uuid
from datetime import date, datetime

import config
from broker import BrokerError


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root}{expiration.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


class FakeBroker:
    name = "fake"
    is_paper = True
    dry_run = False

    def __init__(self, equity: float = 10_000.0, today: date | None = None):
        self.equity = equity
        self.today = today or date(2026, 9, 17)
        self.positions: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.chains: dict[str, dict[float, tuple[float, float]]] = {"P": {}, "C": {}}
        self.spread_close_price: float | None = None
        self.close_failures = 0
        self.close_calls: list[dict] = []
        self.fill_entries = True
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
    def set_chain(self, right: str, quotes: dict[float, tuple[float, float]]) -> None:
        self.chains[right] = dict(quotes)

    def _leg_price(self, right: str, strike: float) -> float:
        bid, ask = self.chains[right].get(strike, (0.0, 0.0))
        return round((bid + ask) / 2, 2)

    def add_spread_position(self, right: str, short_strike: float, long_strike: float, qty: int,
                            short_avg: float, long_avg: float, expiration: date | None = None,
                            root: str = "SPXW") -> None:
        exp = expiration or self.today
        for strike, signed_qty, avg in ((short_strike, -qty, short_avg), (long_strike, qty, long_avg)):
            sym = occ_symbol(root, exp, right, strike)
            self.positions[sym] = {
                "symbol": sym, "underlying": "SPX", "qty": signed_qty, "avg_price": avg,
                "current_price": self._leg_price(right, strike) or avg, "market_value": None,
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
        return [self.today]

    def get_option_chain(self, underlying: str, expiration: date, right: str,
                         strike_min: float | None = None, strike_max: float | None = None,
                         spot: float | None = None) -> list[dict]:
        out = []
        for strike, (bid, ask) in sorted(self.chains[right].items()):
            if strike_min is not None and strike < strike_min:
                continue
            if strike_max is not None and strike > strike_max:
                continue
            if bid == 0 and ask == 0:
                continue
            out.append({"symbol": occ_symbol("SPXW", expiration, right, strike), "underlying": underlying,
                        "root": "SPXW", "expiration": expiration, "strike": float(strike), "right": right,
                        "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2), "last": None,
                        "delta": None, "iv": None, "quote_time": None})
        return out

    def get_spot(self, symbol: str) -> float:
        return self.spot

    # ------------------------------------------------------------- mutations
    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict:
        root = root or underlying
        sym = f"{occ_symbol(root, expiration, right, short_strike)}/{occ_symbol(root, expiration, right, long_strike)}"
        if not self.fill_entries:
            return self._order(sym, qty, limit_credit, "accepted", None, 0)
        long_avg = self._leg_price(right, long_strike)
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
        if self.close_failures > 0:
            self.close_failures -= 1
            raise BrokerError("simulated close failure")
        root = root or underlying
        price = self.spread_close_price
        if price is None:
            price = round(self._leg_price(right, short_strike) - self._leg_price(right, long_strike), 2)
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
        return True

    def cancel_all_orders(self) -> int:
        return sum(self.cancel_order(oid) for oid in list(self.orders))

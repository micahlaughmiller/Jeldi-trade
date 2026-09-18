"""Alpaca paper-trading implementation of the Broker interface (docs/BROKER_INTERFACE.md).

Talks to the Alpaca REST API directly with `requests`. Greeks are computed
locally with options_math because the indicative options feed returns none.
"""

from __future__ import annotations

import csv
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import options_math

ET = ZoneInfo("US/Eastern")
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

DEFAULT_TRADING_URL = "https://paper-api.alpaca.markets"
DEFAULT_DATA_URL = "https://data.alpaca.markets"

TERMINAL_STATUSES = {"filled", "canceled", "rejected", "expired", "replaced", "dry_run"}

_STATUS_MAP = {
    "new": "new",
    "accepted": "accepted",
    "accepted_for_bidding": "accepted",
    "pending_new": "pending",
    "pending_cancel": "pending",
    "pending_replace": "pending",
    "pending_review": "pending",
    "held": "pending",
    "calculated": "pending",
    "stopped": "pending",
    "suspended": "pending",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "done_for_day": "canceled",
    "rejected": "rejected",
    "expired": "expired",
    "replaced": "replaced",
    "dry_run": "dry_run",
}

_ROOT_TO_UNDERLYING = {
    "SPXW": "SPX",
    "NDXP": "NDX",
    "RUTW": "RUT",
    "VIXW": "VIX",
    "DJXW": "DJX",
}
_INDEX_UNDERLYINGS = {"SPX", "^GSPC", "SPXW", "^SPX"}
_YF_INDEX = {"SPX": "^GSPC", "SPXW": "^GSPC", "^SPX": "^GSPC", "^GSPC": "^GSPC"}

_OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<date>\d{6})(?P<right>[PC])(?P<strike>\d{8})$")


class BrokerError(Exception):
    pass


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    right = right.upper()[0]
    if right not in ("P", "C"):
        raise BrokerError(f"invalid right {right!r}")
    return f"{root.upper()}{expiration.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> dict:
    m = _OCC_RE.match(symbol.strip().upper())
    if not m:
        raise BrokerError(f"not an OCC symbol: {symbol!r}")
    return {
        "root": m.group("root"),
        "expiration": datetime.strptime(m.group("date"), "%y%m%d").date(),
        "right": m.group("right"),
        "strike": int(m.group("strike")) / 1000.0,
    }


def underlying_for_root(root: str) -> str:
    return _ROOT_TO_UNDERLYING.get(root.upper(), root.upper())


def normalize_status(raw: str | None) -> str:
    if not raw:
        return "unknown"
    return _STATUS_MAP.get(str(raw).lower(), "unknown")


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int:
    f = _f(value)
    return int(round(f)) if f is not None else 0


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
    s = str(value).strip()
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo("UTC"))
    return d.astimezone(ET)


class Broker:
    name = "alpaca"
    is_paper = True

    def __init__(self, config_module: Any, dry_run: bool | None = None, log: Callable[[str], None] | None = None):
        self.config = config_module
        self.log = log or print
        self.dry_run = bool(getattr(config_module, "DRY_RUN", False)) if dry_run is None else bool(dry_run)

        self.base_url = (os.getenv("ALPACA_BASE_URL") or getattr(config_module, "ALPACA_BASE_URL", None) or DEFAULT_TRADING_URL).rstrip("/")
        self.data_url = (os.getenv("ALPACA_DATA_URL") or getattr(config_module, "ALPACA_DATA_URL", None) or DEFAULT_DATA_URL).rstrip("/")
        self.is_paper = "paper" in self.base_url

        self.risk_free_rate = float(getattr(config_module, "RISK_FREE_RATE", 0.04))
        self.close_slippage = float(getattr(config_module, "CLOSE_SLIPPAGE", 0.05))
        self.close_retry_sec = float(getattr(config_module, "CLOSE_RETRY_SEC", 15))
        self.close_max_retries = int(getattr(config_module, "CLOSE_MAX_RETRIES", 6))
        self.timeout = float(getattr(config_module, "HTTP_TIMEOUT_SEC", 20))
        log_dir = getattr(config_module, "LOG_DIR", None) or "logs"
        self.log_dir = Path(log_dir) if Path(log_dir).is_absolute() else _HERE / log_dir

        self.api_key, self.secret_key, self.trader = self._resolve_keys(config_module)
        if not self.api_key or not self.secret_key:
            raise BrokerError("Alpaca API keys not found (ALPACA_API_KEY/ALPACA_SECRET_KEY or ASTRA_*/CLAUDE_* in .env)")

        self._session = requests.Session()
        self._sleep = time.sleep
        self._dry_orders: dict[str, dict] = {}
        self._exp_cache: dict[tuple, tuple[float, dict[date, set[str]]]] = {}

    @staticmethod
    def _resolve_keys(config_module: Any) -> tuple[str | None, str | None, str]:
        key = os.getenv("ALPACA_API_KEY") or getattr(config_module, "ALPACA_API_KEY", None)
        sec = os.getenv("ALPACA_SECRET_KEY") or getattr(config_module, "ALPACA_SECRET_KEY", None)
        trader = (os.getenv("ACTIVE_TRADER") or getattr(config_module, "ACTIVE_TRADER", None) or "ASTRA").upper()
        if key and sec:
            return key, sec, trader
        if trader == "CLAUDE":
            return os.getenv("CLAUDE_API_KEY"), os.getenv("CLAUDE_API_SECRET"), trader
        return os.getenv("ASTRA_API_KEY"), os.getenv("ASTRA_API_SECRET"), "ASTRA"

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, params: dict | None = None, json: dict | None = None,
                 ok: tuple[int, ...] = (200,), retries: int = 3) -> Any:
        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = self._session.request(method, url, headers=self._headers(), params=params, json=json, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = BrokerError(f"network error: {e}")
                if attempt < retries:
                    self._sleep(0.5 * 2 ** attempt)
                    continue
                raise last_err
            if resp.status_code in ok:
                if resp.status_code == 204 or not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = BrokerError(f"{resp.status_code} {resp.text}")
                if attempt < retries:
                    self._sleep(0.5 * 2 ** attempt)
                    continue
                raise last_err
            raise BrokerError(f"{resp.status_code} {resp.text}")
        raise last_err or BrokerError("request failed")

    def _t(self, method: str, path: str, **kw) -> Any:
        return self._request(method, f"{self.base_url}{path}", **kw)

    def _d(self, method: str, path: str, **kw) -> Any:
        return self._request(method, f"{self.data_url}{path}", **kw)

    # --------------------------------------------------------------- Account

    def get_account(self) -> dict:
        a = self._t("GET", "/v2/account")
        return {
            "equity": _f(a.get("equity")) or 0.0,
            "cash": _f(a.get("cash")) or 0.0,
            "buying_power": _f(a.get("buying_power")) or 0.0,
            "options_buying_power": _f(a.get("options_buying_power")) or 0.0,
            "account_id": str(a.get("id", "")),
        }

    def get_pnl_summary(self) -> dict:
        now = datetime.now(ET)
        out = {"ytd": None, "mtd": None, "today": None, "as_of": now}
        try:
            equity_now = self.get_account()["equity"]
            hist = self._t("GET", "/v2/account/portfolio/history", params={"period": "1A", "timeframe": "1D"})
            series = []
            for ts, eq in zip(hist.get("timestamp") or [], hist.get("equity") or []):
                eqf = _f(eq)
                if eqf and eqf > 0:
                    series.append((datetime.fromtimestamp(ts, tz=ET).date(), eqf))
            series.sort()
            today = now.date()

            def base_for(start: date) -> float | None:
                before = [e for d, e in series if d < start]
                if before:
                    return before[-1]
                within = [e for d, e in series if start <= d < today]
                return within[0] if within else None

            y = base_for(date(today.year, 1, 1))
            m = base_for(date(today.year, today.month, 1))
            prev = [e for d, e in series if d < today]
            t = prev[-1] if prev else None
            out["ytd"] = round(equity_now - y, 2) if y is not None else None
            out["mtd"] = round(equity_now - m, 2) if m is not None else None
            out["today"] = round(equity_now - t, 2) if t is not None else None
        except Exception as e:
            self.log(f"[broker] get_pnl_summary failed: {e}")
        return out

    def record_daily_equity(self) -> None:
        equity = self.get_account()["equity"]
        today = datetime.now(ET).date().isoformat()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / "equity_history.csv"
        rows: list[list[str]] = []
        if path.exists():
            with path.open(newline="") as fh:
                rows = [r for r in csv.reader(fh) if r and r[0] != "date" and r[0] != today]
        rows.append([today, f"{equity:.2f}"])
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "equity"])
            w.writerows(rows)

    # ----------------------------------------------------- Positions/orders

    def get_positions(self) -> list[dict]:
        raw = self._t("GET", "/v2/positions") or []
        out = []
        for p in raw:
            sym = p.get("symbol", "")
            asset_class = "option" if p.get("asset_class") == "us_option" else "stock"
            exp = strike = right = None
            underlying = sym
            if asset_class == "option":
                try:
                    o = parse_occ(sym)
                    exp, strike, right = o["expiration"], o["strike"], o["right"]
                    underlying = underlying_for_root(o["root"])
                except BrokerError:
                    pass
            out.append({
                "symbol": sym,
                "underlying": underlying,
                "qty": _i(p.get("qty")),
                "avg_price": _f(p.get("avg_entry_price")) or 0.0,
                "current_price": _f(p.get("current_price")),
                "market_value": _f(p.get("market_value")),
                "unrealized_pl": _f(p.get("unrealized_pl")),
                "asset_class": asset_class,
                "expiration": exp,
                "strike": strike,
                "right": right,
            })
        return out

    def _normalize_order(self, o: dict) -> dict:
        legs_raw = o.get("legs") or []
        legs = []
        for lg in legs_raw:
            legs.append({
                "symbol": lg.get("symbol", ""),
                "side": lg.get("side"),
                "position_intent": lg.get("position_intent"),
                "ratio_qty": _i(lg.get("ratio_qty") or 1),
                "status": normalize_status(lg.get("status")),
                "filled_avg_price": _f(lg.get("filled_avg_price")),
            })
        is_mleg = (o.get("order_class") == "mleg") or bool(legs)
        top_fill = _f(o.get("filled_avg_price"))
        fill = None
        if is_mleg:
            if legs and all(l["filled_avg_price"] is not None for l in legs):
                net = 0.0
                for l in legs:
                    sign = 1.0 if l["side"] == "sell" else -1.0
                    net += sign * l["filled_avg_price"] * l["ratio_qty"]
                fill = round(abs(net), 4)
            elif top_fill is not None:
                fill = round(abs(top_fill), 4)
        else:
            fill = top_fill
        symbol = o.get("symbol") or ""
        if not symbol and legs:
            try:
                symbol = underlying_for_root(parse_occ(legs[0]["symbol"])["root"])
            except BrokerError:
                symbol = legs[0]["symbol"]
        return {
            "id": str(o.get("id", "")),
            "status": normalize_status(o.get("status")),
            "symbol": symbol,
            "order_class": "mleg" if is_mleg else "simple",
            "side": o.get("side"),
            "qty": _i(o.get("qty")),
            "filled_qty": _i(o.get("filled_qty")),
            "limit_price": _f(o.get("limit_price")),
            "filled_avg_price": fill,
            "time_in_force": o.get("time_in_force") or "",
            "legs": legs,
            "submitted_at": _dt(o.get("submitted_at") or o.get("created_at")) or datetime.now(ET),
            "filled_at": _dt(o.get("filled_at")),
            "raw": o,
        }

    def get_open_orders(self) -> list[dict]:
        raw = self._t("GET", "/v2/orders", params={"status": "open", "limit": 500, "nested": "true"}) or []
        return [self._normalize_order(o) for o in raw]

    def get_order(self, order_id: str) -> dict | None:
        if order_id in self._dry_orders:
            return self._dry_orders[order_id]
        try:
            raw = self._t("GET", f"/v2/orders/{order_id}", params={"nested": "true"})
        except BrokerError as e:
            if str(e).startswith("404"):
                return None
            raise
        return self._normalize_order(raw) if raw else None

    def wait_for_fill(self, order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout_sec
        last = None
        while True:
            order = self.get_order(order_id)
            if order is not None:
                last = order
                if order["status"] in TERMINAL_STATUSES:
                    return order
            if time.monotonic() >= deadline:
                break
            self._sleep(min(poll_sec, max(0.0, deadline - time.monotonic())) or poll_sec)
        if last is None:
            raise BrokerError(f"order {order_id} not found")
        return last

    # ------------------------------------------------------------ Chain data

    def get_expiration_roots(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> dict[date, set[str]]:
        underlying = underlying.upper()
        if underlying in ("^GSPC", "SPXW", "^SPX"):
            underlying = "SPX"
        today = datetime.now(ET).date()
        gte, lte = today + timedelta(days=min_dte), today + timedelta(days=max_dte)
        key = (underlying, gte, lte)
        cached = self._exp_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return {d: set(r) for d, r in cached[1].items()}
        result: dict[date, set[str]] = {}
        params = {
            "underlying_symbols": underlying,
            "type": "put",
            "expiration_date_gte": gte.isoformat(),
            "expiration_date_lte": lte.isoformat(),
            "limit": 10000,
        }
        while True:
            page = self._t("GET", "/v2/options/contracts", params=params)
            for c in page.get("option_contracts") or []:
                try:
                    d = date.fromisoformat(c["expiration_date"])
                except (KeyError, ValueError):
                    continue
                result.setdefault(d, set()).add(c.get("root_symbol") or underlying)
            token = page.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        self._exp_cache[key] = (time.monotonic(), {d: set(r) for d, r in result.items()})
        return result

    def get_expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]:
        return sorted(self.get_expiration_roots(underlying, min_dte, max_dte))

    def get_option_chain(self, underlying: str, expiration: date, right: str,
                         strike_min: float | None = None, strike_max: float | None = None,
                         spot: float | None = None) -> list[dict]:
        right = right.upper()[0]
        underlying = underlying.upper()
        symbols = ["SPXW", "SPX"] if underlying in ("SPX", "^GSPC") else [underlying]
        params_base = {
            "feed": "indicative",
            "type": "put" if right == "P" else "call",
            "expiration_date": expiration.isoformat(),
            "limit": 1000,
        }
        if strike_min is not None:
            params_base["strike_price_gte"] = f"{strike_min:.2f}"
        if strike_max is not None:
            params_base["strike_price_lte"] = f"{strike_max:.2f}"
        snaps: dict[str, dict] = {}
        for sym in symbols:
            params = dict(params_base)
            while True:
                page = self._d("GET", f"/v1beta1/options/snapshots/{sym}", params=params)
                snaps.update(page.get("snapshots") or {})
                token = page.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        out = []
        for occ, s in snaps.items():
            try:
                o = parse_occ(occ)
            except BrokerError:
                continue
            if o["expiration"] != expiration or o["right"] != right:
                continue
            q = s.get("latestQuote") or {}
            bid, ask = _f(q.get("bp")) or 0.0, _f(q.get("ap")) or 0.0
            if bid == 0 and ask == 0:
                continue
            mid = round((bid + ask) / 2.0, 4)
            trade = s.get("latestTrade") or {}
            greeks = s.get("greeks") or {}
            delta, iv = _f(greeks.get("delta")), _f(s.get("impliedVolatility"))
            if delta is None and spot is not None and spot > 0 and mid > 0:
                delta, iv = options_math.delta_from_quote(mid, spot, o["strike"], expiration, right, r=self.risk_free_rate)
            out.append({
                "symbol": occ,
                "underlying": underlying_for_root(o["root"]),
                "root": o["root"],
                "expiration": expiration,
                "strike": o["strike"],
                "right": right,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": _f(trade.get("p")),
                "delta": delta,
                "iv": iv,
                "quote_time": _dt(q.get("t")),
            })
        out.sort(key=lambda r: (r["strike"], r["root"]))
        return out

    def get_spot(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol in _INDEX_UNDERLYINGS:
            return self._yf_spot(_YF_INDEX.get(symbol, "^GSPC"))
        try:
            q = (self._d("GET", f"/v2/stocks/{symbol}/quotes/latest", params={"feed": "iex"}) or {}).get("quote") or {}
            bid, ask = _f(q.get("bp")) or 0.0, _f(q.get("ap")) or 0.0
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2.0, 4)
        except BrokerError as e:
            self.log(f"[broker] latest quote failed for {symbol}: {e}")
        bar = (self._d("GET", f"/v2/stocks/{symbol}/bars/latest", params={"feed": "iex"}) or {}).get("bar") or {}
        close = _f(bar.get("c"))
        if close is None or close <= 0:
            raise BrokerError(f"no spot available for {symbol}")
        return close

    @staticmethod
    def _yf_spot(ticker: str) -> float:
        try:
            import yfinance as yf
        except ImportError as e:
            raise BrokerError(f"yfinance required for index spot: {e}")
        t = yf.Ticker(ticker)
        try:
            px = t.fast_info["last_price"]
            if px and px > 0:
                return float(px)
        except Exception:
            pass
        hist = t.history(period="1d", interval="1m")
        if hist is None or hist.empty:
            raise BrokerError(f"yfinance returned no data for {ticker}")
        return float(hist["Close"].iloc[-1])

    # ---------------------------------------------------------------- Orders

    def _dry_order(self, payload: dict, label: str) -> dict:
        self.log(f"[broker] DRY RUN {label}: {payload}")
        oid = f"DRY-{uuid.uuid4()}"
        raw = dict(payload, id=oid, status="dry_run", filled_qty="0", submitted_at=datetime.now(ET).isoformat())
        order = self._normalize_order(raw)
        order["status"] = "dry_run"
        self._dry_orders[oid] = order
        return order

    def _spread_payload(self, root: str, expiration: date, right: str, short_strike: float, long_strike: float,
                        qty: int, limit: float, tif: str, opening: bool, client_tag: str | None) -> dict:
        short_sym = occ_symbol(root, expiration, right, short_strike)
        long_sym = occ_symbol(root, expiration, right, long_strike)
        if opening:
            legs = [
                {"symbol": short_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                {"symbol": long_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            ]
        else:
            legs = [
                {"symbol": short_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"},
                {"symbol": long_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
            ]
        payload = {
            "order_class": "mleg",
            "qty": str(int(qty)),
            "type": "limit",
            "limit_price": f"{abs(float(limit)):.2f}",
            "time_in_force": tif,
            "legs": legs,
        }
        if client_tag:
            # Alpaca requires client_order_id to be unique across every order the account has
            # ever placed, so a fixed tag would be rejected from the second use onward.
            payload["client_order_id"] = f"{str(client_tag)[:110]}-{uuid.uuid4().hex[:12]}"
        return payload

    def _submit(self, payload: dict, label: str) -> dict:
        if self.dry_run:
            return self._dry_order(payload, label)
        self.log(f"[broker] {label}: {payload}")
        raw = self._t("POST", "/v2/orders", json=payload)
        order = self._normalize_order(raw)
        self.log(f"[broker] {label} -> id={order['id']} status={order['status']}")
        return order

    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict:
        payload = self._spread_payload(root or underlying, expiration, right, short_strike, long_strike,
                                       qty, limit_credit, time_in_force, True, client_tag)
        return self._submit(payload, "place_credit_spread")

    def place_close_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                           long_strike: float, qty: int, limit_debit: float, time_in_force: str = "gtc",
                           root: str | None = None, client_tag: str | None = None) -> dict:
        payload = self._spread_payload(root or underlying, expiration, right, short_strike, long_strike,
                                       qty, limit_debit, time_in_force, False, client_tag)
        return self._submit(payload, "place_close_spread")

    def _spread_close_price(self, root: str, expiration: date, right: str, short_strike: float, long_strike: float) -> float | None:
        lo, hi = min(short_strike, long_strike), max(short_strike, long_strike)
        chain = self.get_option_chain(root, expiration, right, strike_min=lo - 0.01, strike_max=hi + 0.01)
        by_strike = {round(r["strike"], 3): r for r in chain if r["root"] == root.upper()} or {round(r["strike"], 3): r for r in chain}
        s, l = by_strike.get(round(short_strike, 3)), by_strike.get(round(long_strike, 3))
        if not s or not l:
            return None
        return max(s["ask"] - l["bid"], 0.0)

    def close_spread_at_market(self, underlying: str, expiration: date, right: str, short_strike: float,
                               long_strike: float, qty: int, time_in_force: str = "day",
                               root: str | None = None, client_tag: str | None = None) -> dict:
        """Escalating-limit close: ask-side spread price + CLOSE_SLIPPAGE, worse by CLOSE_SLIPPAGE each retry."""
        root = (root or underlying).upper()
        remaining = int(qty)
        last_order = None
        last_price = None
        for attempt in range(self.close_max_retries + 1):
            try:
                px = self._spread_close_price(root, expiration, right, short_strike, long_strike)
            except BrokerError as e:
                self.log(f"[broker] chain lookup failed during close ({e}); using fallback price")
                px = None
            if px is None:
                px = last_price if last_price is not None else max(abs(short_strike - long_strike) * 0.5, 0.05)
            last_price = px
            limit = round(px + self.close_slippage * (attempt + 1), 2)
            limit = max(limit, 0.01)
            width = abs(short_strike - long_strike)
            if width > 0:
                limit = min(limit, round(width, 2))
            self.log(f"[broker] close_spread_at_market attempt {attempt + 1}/{self.close_max_retries + 1} qty={remaining} limit={limit:.2f}")
            order = self.place_close_spread(underlying, expiration, right, short_strike, long_strike, remaining,
                                            limit, time_in_force, root=root, client_tag=client_tag)
            last_order = order
            if order["status"] == "dry_run":
                return order
            order = self.wait_for_fill(order["id"], self.close_retry_sec)
            last_order = order
            if order["status"] == "filled":
                return order
            if order["status"] in ("rejected", "expired", "canceled"):
                if order["status"] == "rejected":
                    self.log(f"[broker] close order rejected: {order['raw'].get('reject_reason') or order['raw']}")
            else:
                self.cancel_order(order["id"])
                final = self.wait_for_fill(order["id"], 10, poll_sec=1.0)
                last_order = final
                if final["status"] == "filled":
                    return final
                remaining -= final["filled_qty"]
            if remaining <= 0:
                return last_order
        raise BrokerError(f"close_spread_at_market exhausted {self.close_max_retries + 1} attempts; last status={last_order['status'] if last_order else None} remaining={remaining}")

    def replace_order_price(self, order_id: str, new_limit: float) -> dict:
        """PATCH /v2/orders/{id} with {"limit_price"}. Verified live on paper 2026-09-17: PATCH
        works on mleg orders too and returns a NEW order (raw["replaces"] = old id; the old order
        moves to status "replaced"). If PATCH is rejected for an mleg order (e.g. 422 "order is
        not open" while pending), the order is canceled and resubmitted with identical
        legs/qty/tif at the new price. Returns the new Order in both cases."""
        if self.dry_run:
            return self._dry_order({"order_id": order_id, "limit_price": f"{abs(float(new_limit)):.2f}"}, "replace_order_price")
        body = {"limit_price": f"{abs(float(new_limit)):.2f}"}
        current = self.get_order(order_id)
        if current is None:
            raise BrokerError(f"order {order_id} not found")
        if current["order_class"] != "mleg":
            raw = self._t("PATCH", f"/v2/orders/{order_id}", json=body)
            return self._normalize_order(raw)
        try:
            raw = self._t("PATCH", f"/v2/orders/{order_id}", json=body)
            return self._normalize_order(raw)
        except BrokerError as e:
            self.log(f"[broker] PATCH on mleg failed ({e}); falling back to cancel+resubmit")
        return self._resubmit_mleg(current, body["limit_price"])

    def _resubmit_mleg(self, current: dict, limit_price: str) -> dict:
        raw = current["raw"]
        if current["status"] in TERMINAL_STATUSES:
            raise BrokerError(f"order {current['id']} is {current['status']}; cannot replace")
        self.cancel_order(current["id"])
        final = self.wait_for_fill(current["id"], 10, poll_sec=1.0)
        if final["status"] == "filled":
            return final
        remaining = current["qty"] - final["filled_qty"]
        if remaining <= 0:
            return final
        payload = {
            "order_class": "mleg",
            "qty": str(remaining),
            "type": "limit",
            "limit_price": limit_price,
            "time_in_force": raw.get("time_in_force") or current["time_in_force"] or "day",
            "legs": [
                {"symbol": l["symbol"], "ratio_qty": str(l["ratio_qty"]), "side": l["side"], "position_intent": l["position_intent"]}
                for l in current["legs"]
            ],
        }
        return self._submit(payload, "replace_order_price(resubmit)")

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._dry_orders:
            self.log(f"[broker] DRY RUN cancel_order {order_id}")
            self._dry_orders[order_id]["status"] = "canceled"
            return True
        if self.dry_run:
            self.log(f"[broker] DRY RUN cancel_order {order_id}")
            return True
        try:
            self._t("DELETE", f"/v2/orders/{order_id}", ok=(200, 204))
            return True
        except BrokerError as e:
            msg = str(e)
            if msg.startswith("404") or msg.startswith("422"):
                self.log(f"[broker] cancel_order {order_id}: {msg}")
                return False
            raise

    def cancel_all_orders(self) -> int:
        if self.dry_run:
            self.log("[broker] DRY RUN cancel_all_orders")
            n = 0
            for o in self._dry_orders.values():
                if o["status"] not in TERMINAL_STATUSES:
                    o["status"] = "canceled"
                    n += 1
            return n
        resp = self._t("DELETE", "/v2/orders", ok=(200, 204, 207))
        if isinstance(resp, list):
            return len(resp)
        return 0

    occ_symbol = staticmethod(occ_symbol)
    parse_occ = staticmethod(parse_occ)

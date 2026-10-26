"""Read-only option data from Alpaca's market-data API.

Used by the Schwab broker as a fallback when Schwab returns no option chain for
an index (Schwab answers 404 for $SPX chains on some accounts). Only quotes and
listed expirations are read; nothing is ever ordered through Alpaca here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

try:
    from . import options_math
except ImportError:  # running as a plain script from the folder
    import options_math  # type: ignore

ET = ZoneInfo("US/Eastern")
_INDEX_ROOTS = {"SPX": ("SPXW", "SPX"), "NDX": ("NDXP", "NDX"), "RUT": ("RUTW", "RUT"), "VIX": ("VIXW", "VIX")}


class AlpacaDataError(Exception):
    pass


def _parse_occ(symbol: str) -> tuple[str, date, str, float]:
    i = 0
    while i < len(symbol) and not symbol[i].isdigit():
        i += 1
    root, rest = symbol[:i], symbol[i:]
    return root, datetime.strptime(rest[:6], "%y%m%d").date(), rest[6], int(rest[7:15]) / 1000.0


class AlpacaOptionData:
    def __init__(self, api_key: str, secret_key: str,
                 trading_url: str = "https://paper-api.alpaca.markets",
                 data_url: str = "https://data.alpaca.markets",
                 risk_free_rate: float = 0.04, log: Callable[[str], Any] | None = None):
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
        self._trading_url = trading_url.rstrip("/")
        self._data_url = data_url.rstrip("/")
        self._r = risk_free_rate
        self._log = log or (lambda m: None)

    def _get(self, url: str, params: dict[str, Any]) -> dict:
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise AlpacaDataError(f"Alpaca {resp.status_code} for {url}: {resp.text[:200]}")
        return resp.json()

    def expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> dict[date, set[str]]:
        today = datetime.now(ET).date()
        params: dict[str, Any] = {
            "underlying_symbols": underlying.upper().lstrip("$"),
            "expiration_date_gte": (today + timedelta(days=min_dte)).isoformat(),
            "expiration_date_lte": (today + timedelta(days=max_dte)).isoformat(),
            "limit": 10000,
        }
        out: dict[date, set[str]] = {}
        while True:
            data = self._get(f"{self._trading_url}/v2/options/contracts", params)
            for c in data.get("option_contracts") or []:
                out.setdefault(date.fromisoformat(c["expiration_date"]), set()).add(c.get("root_symbol") or underlying)
            token = data.get("next_page_token")
            if not token:
                return out
            params["page_token"] = token

    def chain(self, underlying: str, expiration: date, right: str,
              strike_min: float | None = None, strike_max: float | None = None,
              spot: float | None = None) -> list[dict]:
        und = underlying.upper().lstrip("$")
        roots = _INDEX_ROOTS.get(und, (und,))
        right = right.upper()[0]
        rows: list[dict] = []
        for root in roots:
            params: dict[str, Any] = {"feed": "indicative", "type": "put" if right == "P" else "call",
                                      "expiration_date": expiration.isoformat(), "limit": 1000}
            if strike_min is not None:
                params["strike_price_gte"] = strike_min
            if strike_max is not None:
                params["strike_price_lte"] = strike_max
            while True:
                data = self._get(f"{self._data_url}/v1beta1/options/snapshots/{root}", params)
                for symbol, snap in (data.get("snapshots") or {}).items():
                    row = self._row(symbol, snap, und, right, spot)
                    if row is not None:
                        rows.append(row)
                token = data.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        rows.sort(key=lambda q: (q["strike"], q["root"]))
        return rows

    def _row(self, symbol: str, snap: dict, underlying: str, right: str, spot: float | None) -> dict | None:
        quote = snap.get("latestQuote") or {}
        bid, ask = float(quote.get("bp") or 0.0), float(quote.get("ap") or 0.0)
        if bid == 0 and ask == 0:
            return None
        root, expiration, sym_right, strike = _parse_occ(symbol)
        if sym_right != right:
            return None
        mid = round((bid + ask) / 2.0, 4)
        delta, iv = (None, None)
        if spot:
            delta, iv = options_math.delta_from_quote(mid, spot, strike, expiration, right, r=self._r)
        trade = snap.get("latestTrade") or {}
        ts = quote.get("t")
        quote_time = None
        if ts:
            quote_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
        return {
            "symbol": symbol, "underlying": underlying, "root": root, "expiration": expiration,
            "strike": strike, "right": right, "bid": bid, "ask": ask, "mid": mid,
            "last": float(trade["p"]) if trade.get("p") is not None else None,
            "delta": delta, "iv": iv, "quote_time": quote_time,
        }

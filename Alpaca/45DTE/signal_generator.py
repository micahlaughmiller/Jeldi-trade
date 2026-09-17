"""Turn an RSI signal into a fully specified $5-wide credit spread."""

from __future__ import annotations

import math
from datetime import date, datetime
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import config_45dte

ET = ZoneInfo("US/Eastern")


def round_down_to_nickel(value: float) -> float:
    return round(math.floor(value / 0.05 + 1e-9) * 0.05, 2)


def round_to_nickel(value: float) -> float:
    return round(round(value / 0.05) * 0.05, 2)


def choose_expiration(expirations: list[date], today: date,
                      config: ModuleType = config_45dte) -> tuple[date | None, int, bool, str]:
    """(expiration, dte, out_of_range, reason). Inside [DTE_MIN, DTE_MAX] closest to DTE_TARGET wins;
    otherwise the nearest within DTE_TOLERANCE_DAYS of the window, flagged out_of_range."""
    dated = [(exp, (exp - today).days) for exp in expirations]
    inside = [(exp, dte) for exp, dte in dated if config.DTE_MIN <= dte <= config.DTE_MAX]
    if inside:
        exp, dte = min(inside, key=lambda p: (abs(p[1] - config.DTE_TARGET), p[1]))
        return exp, dte, False, "inside window"
    lo = config.DTE_MIN - config.DTE_TOLERANCE_DAYS
    hi = config.DTE_MAX + config.DTE_TOLERANCE_DAYS

    def distance(dte: int) -> int:
        return config.DTE_MIN - dte if dte < config.DTE_MIN else dte - config.DTE_MAX

    near = [(exp, dte) for exp, dte in dated if lo <= dte <= hi]
    if near:
        exp, dte = min(near, key=lambda p: (distance(p[1]), p[1]))
        return exp, dte, True, f"nearest outside window ({dte} DTE)"
    return None, 0, False, f"no expiration within {lo}-{hi} DTE (have {[d for _, d in dated]})"


def _find_strike(chain: list[dict[str, Any]], strike: float) -> dict[str, Any] | None:
    for quote in chain:
        if abs(quote["strike"] - strike) < 1e-6:
            return quote
    return None


def select_strikes(chain: list[dict[str, Any]], right: str,
                   config: ModuleType = config_45dte) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Short = |delta| closest to TARGET_DELTA within [DELTA_MIN, DELTA_MAX]; long = $SPREAD_WIDTH further OTM."""
    candidates = [
        q for q in chain
        if q.get("delta") is not None and config.DELTA_MIN <= abs(q["delta"]) <= config.DELTA_MAX
    ]
    if not candidates:
        return None, None, f"no strike with |delta| in [{config.DELTA_MIN}, {config.DELTA_MAX}]"
    candidates.sort(key=lambda q: abs(abs(q["delta"]) - config.TARGET_DELTA))
    offset = -config.SPREAD_WIDTH if right == "P" else config.SPREAD_WIDTH
    for short in candidates:
        long = _find_strike(chain, short["strike"] + offset)
        if long is not None:
            return short, long, "ok"
    return None, None, "no $5-wide pair"


def build_trade(broker: Any, row: dict[str, Any], today: date | None = None,
                config: ModuleType = config_45dte) -> dict[str, Any]:
    """Return a trade spec; `accepted` False carries a `reason`."""
    today = today or datetime.now(ET).date()
    symbol = row["broker_symbol"]
    right = "P" if row["signal"] == "oversold" else "C"
    strong = bool(row.get("strong"))
    spec: dict[str, Any] = {
        "symbol": row["symbol"], "broker_symbol": symbol, "right": right, "signal": row["signal"], "strong": strong,
        "rsi14": row.get("rsi14"), "rsi28": row.get("rsi28"), "accepted": False, "reason": "", "dte_out_of_range": False,
    }
    expirations = broker.get_expirations(symbol, config.DTE_SEARCH_MIN, config.DTE_SEARCH_MAX)
    expiration, dte, out_of_range, why = choose_expiration(expirations, today, config)
    if expiration is None:
        spec["reason"] = why
        return spec
    spec.update({"expiration": expiration, "dte": dte, "dte_out_of_range": out_of_range})

    spot = broker.get_spot(symbol)
    chain = broker.get_option_chain(symbol, expiration, right, spot=spot)
    spec["spot"] = spot
    if not chain:
        spec["reason"] = "empty option chain"
        return spec
    short, long, why = select_strikes(chain, right, config)
    if short is None or long is None:
        spec["reason"] = why
        return spec

    credit = round_down_to_nickel(short["mid"] - long["mid"])
    bid_side = round(short["bid"] - long["ask"], 2)
    min_credit = config.MIN_CREDIT_STRONG if strong else config.MIN_CREDIT
    spec.update({
        "short_strike": short["strike"], "long_strike": long["strike"], "short_symbol": short["symbol"],
        "long_symbol": long["symbol"], "short_delta": short["delta"], "long_delta": long.get("delta"),
        "credit": credit, "bid_side": bid_side, "min_credit": min_credit,
        "max_loss": round(config.SPREAD_WIDTH - credit, 2), "width": config.SPREAD_WIDTH,
    })
    if credit + 1e-9 < min_credit:
        spec["reason"] = f"credit {credit:.2f} < min {min_credit:.2f}{' (strong)' if strong else ''}"
        return spec
    spec["accepted"] = True
    spec["reason"] = "ok" if not out_of_range else f"ok ({why})"
    return spec

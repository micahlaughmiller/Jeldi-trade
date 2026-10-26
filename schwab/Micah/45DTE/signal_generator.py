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


def min_credit_for(width: float, strong: bool, config: ModuleType = config_45dte) -> float:
    """Credit floor scales with width: MIN_CREDIT is quoted per SPREAD_WIDTH ($5)."""
    base = config.MIN_CREDIT_STRONG if strong else config.MIN_CREDIT
    return round(base * width / config.SPREAD_WIDTH, 2)


def select_strikes(chain: list[dict[str, Any]], right: str, strong: bool = False,
                   config: ModuleType = config_45dte) -> tuple[dict[str, Any] | None, dict[str, Any] | None, float | None, str]:
    """Short = |delta| closest to TARGET_DELTA within [DELTA_MIN, DELTA_MAX]; long one width further OTM.

    Widths are tried in SPREAD_WIDTHS order ($5, then $2.50, then $1) and, within a width, the
    next-best delta strikes, so an illiquid partner strike does not kill the trade. A width is only
    accepted once its own scaled credit floor (min_credit_for) is cleared: a pair with real but too-
    thin credit at a wider width falls through to a narrower one (whose floor scales down with it)
    instead of stopping there -- 2026-09-24: with the "no pair" case mostly fixed by the width
    fallback itself, "pair exists but $5-wide credit is below floor" became the dominant rejection
    (94% of one real session) and this is the case that fix never actually reached, since the old
    code stopped at the first width with any positive credit before the floor was ever checked.
    Pairs whose quotes imply a non-positive credit or a zero-bid short leg are always skipped as
    unusable. If every width's best pair still misses its own floor, the richest such pair is
    returned anyway (short/long/width all set) so the caller can report full diagnostics on the
    rejection; `reason` is a placeholder in that case -- the caller re-derives and overwrites it
    from the returned width's own credit and floor.
    Returns (short, long, width, reason).
    """
    candidates = [
        q for q in chain
        if q.get("delta") is not None and config.DELTA_MIN <= abs(q["delta"]) <= config.DELTA_MAX
    ]
    if not candidates:
        return None, None, None, f"no strike with |delta| in [{config.DELTA_MIN}, {config.DELTA_MAX}]"
    candidates.sort(key=lambda q: abs(abs(q["delta"]) - config.TARGET_DELTA))
    saw_pair = False
    fallback: tuple[dict[str, Any], dict[str, Any], float] | None = None   # richest below-floor pair seen
    for width in config.SPREAD_WIDTHS:
        offset = -width if right == "P" else width
        floor = min_credit_for(width, strong, config)
        # Next-best delta is only for a missing or junk partner at this width (old behavior,
        # unchanged) -- once a real pair is found for this width, it is this width's answer; a floor
        # miss moves to the next (narrower) width, it does not go hunting for a richer delta here.
        chosen = None
        for short in candidates:
            long = _find_strike(chain, short["strike"] + offset)
            if long is None:
                continue
            saw_pair = True
            if short["bid"] <= 0 or short["mid"] - long["mid"] <= 0:
                continue
            chosen = (short, long)
            break
        if chosen is None:
            continue
        short, long = chosen
        credit = round_down_to_nickel(short["mid"] - long["mid"])
        if credit + 1e-9 >= floor:
            return short, long, width, "ok"
        if fallback is None or credit > round_down_to_nickel(fallback[0]["mid"] - fallback[1]["mid"]):
            fallback = (short, long, width)
    widths = "/".join(f"${w:g}" for w in config.SPREAD_WIDTHS)
    if fallback is not None:
        short, long, width = fallback
        return short, long, width, f"best of {widths} still below its own width's credit floor"
    if saw_pair:
        return None, None, None, f"no usable quotes for any {widths}-wide pair (zero bid or credit <= 0)"
    return None, None, None, f"no {widths}-wide pair"


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
    short, long, width, why = select_strikes(chain, right, strong, config)
    if short is None or long is None or width is None:
        spec["reason"] = why
        return spec

    credit = round_down_to_nickel(short["mid"] - long["mid"])
    bid_side = round(short["bid"] - long["ask"], 2)
    min_credit = min_credit_for(width, strong, config)
    spec.update({
        "short_strike": short["strike"], "long_strike": long["strike"], "short_symbol": short["symbol"],
        "long_symbol": long["symbol"], "short_delta": short["delta"], "long_delta": long.get("delta"),
        "credit": credit, "bid_side": bid_side, "min_credit": min_credit,
        "max_loss": round(width - credit, 2), "width": width,
    })
    if credit + 1e-9 < min_credit:
        spec["reason"] = (f"credit {credit:.2f} < min {min_credit:.2f} for ${width:g} width"
                          f"{' (strong)' if strong else ''}")
        return spec
    spec["accepted"] = True
    spec["reason"] = "ok" if not out_of_range else f"ok ({why})"
    return spec

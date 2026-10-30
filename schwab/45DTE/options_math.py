"""Black-Scholes helpers for computing IV and delta from quotes.

Used when the broker feed does not include greeks (Alpaca indicative feed).
"""

import math
from datetime import date, datetime, time, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

ET = ZoneInfo("US/Eastern")
MARKET_CLOSE = time(16, 0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years_to_expiry(expiration: date, now: datetime | None = None) -> float:
    """Fraction of a year until 4:00 PM ET on the expiration date (floor 1 minute)."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    exp_dt = datetime.combine(expiration, MARKET_CLOSE, tzinfo=ET)
    seconds = (exp_dt - now.astimezone(ET)).total_seconds()
    seconds = max(seconds, 60.0)
    return seconds / (365.0 * 24 * 3600)


def bs_price(spot: float, strike: float, t: float, r: float, sigma: float, right: str) -> float:
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(spot - strike, 0.0) if right.upper() == "C" else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if right.upper() == "C":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t: float, r: float, sigma: float, right: str) -> float:
    """Signed delta: calls in (0, 1), puts in (-1, 0)."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        if right.upper() == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    if right.upper() == "C":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def implied_vol(price: float, spot: float, strike: float, t: float, r: float, right: str,
                lo: float = 0.01, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 100) -> float | None:
    """Bisection IV. Returns None if the price is outside the no-arbitrage band."""
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = max(spot - strike * math.exp(-r * t), 0.0) if right.upper() == "C" else max(strike * math.exp(-r * t) - spot, 0.0)
    if price < intrinsic - 1e-9:
        return None
    if bs_price(spot, strike, t, r, hi, right) < price:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        diff = bs_price(spot, strike, t, r, mid, right) - price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def delta_from_quote(mid: float, spot: float, strike: float, expiration: date, right: str,
                     r: float = 0.04, now: datetime | None = None) -> tuple[float | None, float | None]:
    """Return (delta, iv) computed from a mid quote, or (None, None) if not solvable."""
    t = years_to_expiry(expiration, now)
    iv = implied_vol(mid, spot, strike, t, r, right)
    if iv is None:
        return None, None
    return bs_delta(spot, strike, t, r, iv, right), iv

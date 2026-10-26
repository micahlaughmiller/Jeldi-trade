"""Refuse to run on a wrong PC clock.

Everything the bot does is scheduled from this PC's clock: whether it is a trading day, the 09:30 open, the entry window, the 13:00
hard stop, the Schwab token's age and expiry. A PC with the wrong date or time (found 2026-10-12: a bot launched on a Monday saw
Saturday) makes it exit as if the market were closed -- or, worse, trade at the wrong hours. At start-up this compares the PC clock
with the `Date` header of a few well-known HTTPS endpoints (it needs no login, sends nothing but a HEAD request, and takes the median
so one bad source cannot decide) and says so, loudly, when they disagree by more than CLOCK_MAX_SKEW_SEC. With no source reachable the
answer is "unknown" and the bot carries on (it could not trade offline anyway).
"""

from __future__ import annotations

import email.utils
import logging
import statistics
import time
from typing import Callable

log = logging.getLogger("0dte.clock_check")

SOURCES = ("https://www.google.com", "https://api.alpaca.markets", "https://www.cloudflare.com")


def server_time(url: str, timeout: float = 5.0) -> float | None:
    """The `Date` header of an HTTPS HEAD response, as epoch seconds (None when the endpoint cannot be read). A clock far enough off
    makes certificate validation fail, so a failed verification retries once without it: only the Date header is read, nothing is sent."""
    import requests
    for verify in (True, False):
        try:
            if not verify:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            response = requests.head(url, timeout=timeout, allow_redirects=False, verify=verify)
            header = response.headers.get("Date")
            return email.utils.parsedate_to_datetime(header).timestamp() if header else None
        except requests.exceptions.SSLError:
            continue
        except Exception:                                # noqa: BLE001 -- an unreachable source is simply skipped
            return None
    return None


def check(max_skew: float = 120.0, sources: tuple = SOURCES, fetch: Callable[[str], float | None] = server_time,
          clock: Callable[[], float] = time.time) -> dict:
    """{"ok": True | False | None, "skew": seconds the PC is AHEAD of true time (negative = behind), "sources": how many answered,
    "detail": text}. ok is None when no source answered. The skew is the median over the sources that answered, each measured against
    the middle of its own request so a slow network does not look like a wrong clock."""
    skews = []
    for url in sources:
        before = clock()
        remote = fetch(url)
        after = clock()
        if remote is not None:
            skews.append((before + after) / 2.0 - remote)
    if not skews:
        return {"ok": None, "skew": None, "sources": 0, "detail": "no time source could be reached"}
    skew = statistics.median(skews)
    return {"ok": abs(skew) <= max_skew, "skew": skew, "sources": len(skews),
            "detail": f"PC clock is {human(abs(skew))} {'AHEAD of' if skew > 0 else 'BEHIND'} internet time ({len(skews)} source(s))"}


def human(seconds: float) -> str:
    seconds = int(round(abs(seconds)))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    if hours:
        parts.append(f"{hours} h")
    if minutes and not days:
        parts.append(f"{minutes} min")
    if not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)

"""Health of the Schwab OAuth token (the schwab-py token.json next to each bot).

Schwab refresh tokens die exactly 7 days after the login that created them -- refreshing the 30-minute access token does not
extend that -- and then every API call fails until schwab_login.py is run again by hand. A bot that only notices when a call
fails loses its stops mid-session, so the age is checked up front: the file's own `creation_timestamp` is read (nothing else in
the file is touched) and turned into a state the schedulers act on.

    OK        plenty of time left
    WARN      age >= TOKEN_WARN_DAYS: re-login soon (today or tomorrow, before the open)
    EXPIRING  it will expire before today's hard stop (+ margin): new entries are refused, re-login before the next session
    DEAD      past 7 days: nothing will work
    MISSING   no token file
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")
REFRESH_TOKEN_DAYS = 7.0
DEFAULT_HARD_STOP = time(16, 0)


def token_created_at(path, retries: int = 3, wait_sec: float = 0.05) -> datetime | None:
    """When the login that produced this token file happened, or None (missing file, unreadable, no timestamp). A file that is not valid
    JSON is read again a few times first: another bot on the same login rewrites it (non-atomically) at every token refresh (2026-10-26)."""
    for attempt in range(max(retries, 1)):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            stamp = raw.get("creation_timestamp")
            return datetime.fromtimestamp(float(stamp), tz=ET) if stamp else None
        except OSError:
            return None
        except (ValueError, TypeError, AttributeError):
            if attempt + 1 >= max(retries, 1):
                return None
            _time.sleep(wait_sec)
    return None


def assess(path, now: datetime | None = None, hard_stop: time | None = None, margin_min: float = 30.0,
           warn_days: float = 5.0, relogin: str = "python schwab_login.py --force") -> dict:
    """The token's state at `now` (see the module docstring). Always returns a dict with state/message/relogin."""
    now = (now or datetime.now(ET)).astimezone(ET)
    base = {"state": "OK", "relogin": relogin, "age_days": None, "created_at": None, "expires_at": None}
    if not Path(path).is_file():
        return {**base, "state": "MISSING", "message": f"no token file at {path}"}
    created = token_created_at(path)
    if created is None:
        return {**base, "state": "WARN", "message": "token file has no creation timestamp: age unknown"}
    age = (now - created).total_seconds() / 86400.0
    expires = created + timedelta(days=REFRESH_TOKEN_DAYS)
    stop = datetime.combine(now.date(), hard_stop or DEFAULT_HARD_STOP, tzinfo=ET) + timedelta(minutes=margin_min)
    info = {**base, "age_days": round(age, 2), "created_at": created.isoformat(), "expires_at": expires.isoformat()}
    when = expires.strftime("%a %b %d %H:%M ET")
    if now >= expires:
        return {**info, "state": "DEAD", "message": f"token is {age:.1f} days old: it expired {when}"}
    if expires <= stop:
        return {**info, "state": "EXPIRING",
                "message": f"token is {age:.1f} days old and expires {when}, before today's hard stop"}
    if age >= warn_days:
        return {**info, "state": "WARN", "message": f"token is {age:.1f} days old; it expires {when}"}
    return {**info, "message": f"token is {age:.1f} days old; it expires {when}"}

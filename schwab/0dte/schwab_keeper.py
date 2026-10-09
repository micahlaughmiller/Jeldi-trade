"""ASTRA owns the Schwab token: one background thread keeps schwab/0dte/token.json's access token fresh.

Why: the Alpaca persona reads that file read-only for live /ES futures candles (market_data.py), and only a process that REFRESHES
it ever updates the access token (30 minutes). Until 2026-10-09 the Schwab sim bots were that process; with only ASTRA running
nothing refreshed it, so live ES existed for ~30 minutes after a manual login and then fell back to the delayed proxy. The
2026-10-01 rule "only the Schwab bot may refresh" was a guess that refresh tokens rotate single-use; the repo's own two token
files hold byte-identical refresh tokens despite independent refreshes, i.e. they do not rotate. The owner decided ASTRA refreshes.

What it does (config.SCHWAB_TOKEN_KEEPER, True only for ASTRA; needs SCHWAB_APP_KEY + SCHWAB_APP_SECRET + SCHWAB_TOKEN_PATH):
  * every SCHWAB_KEEPER_POLL_SEC it reads the token file; when the access token has <= SCHWAB_REFRESH_MARGIN_SEC left (or is
    expired) it performs ONE refresh_token grant with a FRESH schwab-py client built from the file, and writes the result back
    atomically (temp file + os.replace, retried on Windows sharing violations);
  * nothing is ever refreshed on the tick path: a stalled connection must not delay position management (10 s timeout, own thread);
  * a refreshed token that lacks an access/refresh token or a future expiry is never saved; a login written by schwab_login.py while
    a refresh was in flight is never overwritten;
  * failures back off (transient: 15/30/60/120 s; the refresh token rejected: every 15 min with a loud re-login instruction);
  * the 7-day login age is watched (schwab_health) and warned about well before the refresh token dies. A refresh never extends it.
It never halts entries: live ES simply falls back to SPX candles (market_data). Events are queued for the main thread to journal
(the journal file has no lock); log lines carry keep=True so they reach the console/file log.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path

import config
import schwab_health

log = logging.getLogger("0dte.schwab_keeper")
TOKEN_ENDPOINT = "https://api.schwabapi.com/v1/oauth/token"


def _cfg(name: str, default):
    return getattr(config, name, default)


_TOKEN_IO = threading.RLock()      # in-process: the keeper's writer and market_data's reader never touch the file at the same moment
_KEEPER: dict = {
    "thread": None, "stop": threading.Event(), "first": threading.Event(), "events": collections.deque(maxlen=200),
    "fails": 0, "next_try": 0.0, "kind": None, "detail": "", "last_ok": None, "sig": None, "expires_at": None,
    "health": None, "logged": {}, "client_factory": None, "pending": None, "unreadable": 0,
}


class _WriteFailed(Exception):
    """The refresh grant succeeded but the new token could not be saved (the granted token is kept in _KEEPER["pending"])."""


class _LoginReplaced(Exception):
    """schwab_login.py wrote a newer login while a refresh of the older one was in flight: the refreshed token must not overwrite it."""


def _path() -> str:
    return str(_cfg("SCHWAB_TOKEN_PATH", "") or "").strip()


def _relogin_command() -> str:
    folder = Path(_path()).parent if _path() else Path(".")
    return f'cd "{folder}" && python schwab_login.py --force'


# ------------------------------------------------------------------------------------------------ the token file

def read_token_file(path: str, attempts: int = 4, pause: float = 0.02) -> dict:
    """The token file as a dict. Tolerates the two transient states a concurrent writer leaves on Windows (a truncated file ->
    JSONDecodeError; a file mid-replace -> PermissionError) by retrying a few milliseconds later; FileNotFoundError is not retried."""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with _TOKEN_IO:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("token file is not a JSON object")
            return raw
        except FileNotFoundError:
            raise
        except (json.JSONDecodeError, PermissionError) as e:
            last = e
            time.sleep(pause)
    assert last is not None
    raise last


def _validate_wrapped(wrapped: dict) -> None:
    token = wrapped["token"]
    if not (isinstance(token.get("access_token"), str) and token["access_token"]
            and isinstance(token.get("refresh_token"), str) and token["refresh_token"]
            and isinstance(token.get("expires_at"), (int, float)) and token["expires_at"] > time.time()):
        raise ValueError("refreshed token is missing an access_token / refresh_token / future expires_at: not saved")
    if not isinstance(wrapped.get("creation_timestamp"), (int, float)):
        raise ValueError("refreshed token has no creation_timestamp: not saved")


def _write_token_file(path: str, wrapped: dict, expect_created) -> None:
    """Validate, then write atomically (temp file in the same folder + os.replace) so a crash or a concurrent reader can never see
    a half-written file -- the refresh token lives ONLY in this file. A reader holding the file open makes os.replace fail on
    Windows, so it is retried. The temp name ends in .token: matched by the repo .gitignore (*.token)."""
    _validate_wrapped(wrapped)
    tmp = f"{path}.keeper.token"
    last: Exception | None = None
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wrapped, f)
            f.flush()
            os.fsync(f.fileno())
        for _ in range(40):
            try:
                with _TOKEN_IO:
                    if expect_created is not None:
                        try:
                            on_disk = read_token_file(path).get("creation_timestamp")
                        except FileNotFoundError:
                            on_disk = expect_created
                        if on_disk != expect_created:
                            raise _LoginReplaced(f"login changed on disk ({expect_created} -> {on_disk})")
                    os.replace(tmp, path)
                return
            except PermissionError as e:
                last = e
                time.sleep(0.05)
        assert last is not None
        raise last
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _build_client(path: str):
    """A FRESH schwab-py client built from the file for every refresh, so a stale in-memory token never outlives a re-login."""
    factory = _KEEPER["client_factory"]
    if factory is not None:
        return factory(path)
    from schwab.auth import client_from_access_functions      # already imported by start() on the main thread
    created = read_token_file(path).get("creation_timestamp")

    def save(wrapped, *args, **kwargs):
        _validate_wrapped(wrapped)                       # a bad response raises ValueError here and is never kept or saved
        try:
            _write_token_file(path, wrapped, created)
        except _LoginReplaced:
            raise
        except OSError as e:
            # The grant went through, so if Schwab rotates refresh tokens the old one in the file is already dead: KEEP the new token
            # and retry only the save (no new grant) on the next cycles. Dropping it here would lose the login mid-week.
            _KEEPER["pending"] = (wrapped, created)
            raise _WriteFailed(f"{type(e).__name__}: {str(e)[:80]}") from e
        _KEEPER["pending"] = None

    client = client_from_access_functions(
        str(_cfg("SCHWAB_APP_KEY", "")).strip(), str(_cfg("SCHWAB_APP_SECRET", "")).strip(),
        lambda: read_token_file(path), save)
    try:
        import httpx
        client.set_timeout(httpx.Timeout(10.0, connect=5.0))
    except Exception as e:                               # noqa: BLE001 -- the default timeout applies; this runs in the keeper thread, not the tick loop
        log.warning("could not set the Schwab token refresh timeout (%s: %s)", type(e).__name__, e, extra={"keep": True})
    return client


def _classify(exc: BaseException) -> tuple[str, str]:
    """('auth' | 'transient' | 'replaced' | 'file', short reason). 'auth' = a human must act (the refresh token or the app
    credentials are rejected); 'transient' = try again later."""
    if isinstance(exc, _LoginReplaced):
        return "replaced", str(exc)
    if isinstance(exc, _WriteFailed):
        return "write", str(exc)
    name = type(exc).__name__
    try:
        from authlib.integrations.base_client.errors import OAuthError
    except Exception:                                    # noqa: BLE001
        OAuthError = ()                                  # type: ignore[assignment]
    if OAuthError and isinstance(exc, OAuthError):
        code = getattr(exc, "error", "") or ""
        if code in ("temporarily_unavailable", "server_error"):
            return "transient", f"{name}:{code}"
        return "auth", f"{name}:{code} {str(getattr(exc, 'description', '') or '')[:80]}".strip()
    from_http_library = type(exc).__module__.split(".")[0] in ("httpx", "httpx2", "httpcore")     # authlib may switch to httpx2
    if from_http_library or isinstance(exc, (OSError, json.JSONDecodeError)):   # timeouts, resets, 5xx, a non-JSON 4xx/429 page
        return "transient", name
    return "file", f"{name}: {str(exc)[:80]}"


def refresh_schwab_token_now(path: str | None = None) -> dict:
    """ONE forced refresh: client from the file -> POST grant_type=refresh_token -> atomic write. Never raises.
    Returns {"ok", "kind": ok|auth|transient|replaced|file, "detail", "expires_at", "seconds"}."""
    path = path or _path()
    t0 = time.perf_counter()

    def result(ok, kind, detail="", expires_at=None):
        return {"ok": ok, "kind": kind, "detail": detail, "expires_at": expires_at, "seconds": time.perf_counter() - t0}

    try:
        try:
            read_token_file(path)                        # file problems are not network problems
        except Exception as e:                           # noqa: BLE001
            return result(False, "file", f"token file unusable ({type(e).__name__})")
        client = _build_client(path)
        try:
            client.session.refresh_token(TOKEN_ENDPOINT)
            expires_at = float(client.session.token["expires_at"])
        finally:
            try:
                client.session.close()
            except Exception:                            # noqa: BLE001
                pass
        return result(True, "ok", "", expires_at)
    except Exception as e:                               # noqa: BLE001 -- the keeper must never raise into the bot
        kind, why = _classify(e)
        return result(False, kind, why)


# ------------------------------------------------------------------------------------------------ the cycle

def _next_action(now: float, expires_at, st: dict) -> tuple[bool, float]:
    """(refresh now?, seconds to sleep otherwise) -- pure, so tests drive it with a fake clock."""
    poll = float(_cfg("SCHWAB_KEEPER_POLL_SEC", 20))
    if now < st["next_try"]:
        return False, min(poll, st["next_try"] - now)
    margin = float(_cfg("SCHWAB_REFRESH_MARGIN_SEC", 600))
    if expires_at is None or expires_at - now <= margin:
        return True, 0.0
    return False, min(poll, max(1.0, expires_at - margin - now))


def _say(key: str, every: float, level: int, now: float, message: str, *args) -> None:
    """Log (keep=True: it reaches the console/file log) at most once per `every` seconds for this key."""
    last = _KEEPER["logged"].get(key)
    if last is not None and now - last < every:
        return
    _KEEPER["logged"][key] = now
    log.log(level, message, *args, extra={"keep": True})


def _age_check(now: float, path: str) -> None:
    """Warn about the 7-day login clock: a refresh never extends it, and a login mid-session dies mid-session a week later."""
    st = _KEEPER
    try:
        health = schwab_health.assess(path, datetime.fromtimestamp(now, schwab_health.ET), hard_stop=_cfg("FORCE_CLOSE_TIME", None),
                                      margin_min=float(_cfg("TOKEN_ENTRY_MARGIN_MIN", 30)), warn_days=float(_cfg("TOKEN_WARN_DAYS", 5.0)),
                                      relogin=_relogin_command())
    except Exception:                                    # noqa: BLE001
        return
    previous = (st["health"] or {}).get("state")
    st["health"] = health
    state = health["state"]
    if state != previous and state != "OK":
        st["events"].append({"event": "TOKEN_STATUS", **{k: v for k, v in health.items() if k in ("state", "age_days", "expires_at", "message")}})
    if state == "WARN":
        _say("age_warn", 3600, logging.WARNING, now, "SCHWAB LOGIN AGING: %s -- re-login before it expires: %s", health["message"], health["relogin"])
    elif state == "EXPIRING":
        _say("age_expiring", 900, logging.CRITICAL, now, "SCHWAB LOGIN EXPIRES TODAY: %s -- live ES stops then; re-login: %s",
             health["message"], health["relogin"])
    elif state == "DEAD" and st["kind"] != "auth":       # a rejected refresh already says it, louder
        _say("age_dead", 900, logging.CRITICAL, now, "SCHWAB LOGIN EXPIRED: %s -- live ES is off; re-login: %s", health["message"],
             health["relogin"])


def _cycle(now: float | None = None) -> None:
    now = time.time() if now is None else now
    st = _KEEPER
    path = _path()
    read_ok = True
    try:
        raw = read_token_file(path)
        expires_at = float(raw["token"]["expires_at"])
    except FileNotFoundError:
        st["kind"], st["detail"] = "file", "token file not found"
        _say("missing", 900, logging.CRITICAL, now, "SCHWAB TOKEN FILE NOT FOUND at %s: live ES is off until you log in: %s", path,
             _relogin_command())
        return
    except Exception as e:                               # noqa: BLE001
        # Unreadable for the moment (a login being written, an antivirus scan): not a new login, not a failed refresh -- look again on
        # the next poll. Only a file that STAYS unreadable is reported (and a re-login rewrites it).
        st["unreadable"] += 1
        if st["unreadable"] >= 3:
            st["kind"], st["detail"] = "file", f"token file unreadable ({type(e).__name__})"
            _say("unreadable", 900, logging.CRITICAL, now, "SCHWAB TOKEN FILE UNREADABLE (%s) for %d checks: live ES is off; a "
                 "re-login rewrites it: %s", type(e).__name__, st["unreadable"], _relogin_command())
        return
    st["unreadable"] = 0
    sig = raw.get("creation_timestamp") if isinstance(raw, dict) else None
    if sig != st["sig"]:
        if st["sig"] is not None:
            st["events"].append({"event": "SCHWAB_LOGIN_CHANGED", "creation_timestamp": sig})
            st.update(fails=0, next_try=0.0)
            log.info("Schwab login changed on disk: the keeper picked up the new token.", extra={"keep": True})
        st["sig"] = sig
    st["expires_at"] = expires_at
    _age_check(now, path)
    if st["pending"] is not None:                        # a granted token still waiting to be saved: save it, never grant again first
        if now < st["next_try"]:
            return
        wrapped, created = st["pending"]
        try:
            _write_token_file(path, wrapped, created)
        except _LoginReplaced:
            st["pending"] = None
            st.update(fails=0, next_try=0.0)
            log.warning("A newer Schwab login was written while a refreshed token waited to be saved: the waiting token was dropped.",
                        extra={"keep": True})
            return
        except Exception as e:                           # noqa: BLE001
            st["fails"] += 1
            steps = tuple(_cfg("SCHWAB_REFRESH_BACKOFF_SEC", (15, 30, 60, 120)))
            wait = float(steps[min(st["fails"], len(steps)) - 1])
            st["next_try"] = now + wait
            _say("write", 300, logging.WARNING, now, "Schwab token: the refreshed token still cannot be saved (%s: %s); keeping it, "
                 "retrying the save in %ds.", type(e).__name__, str(e)[:80], int(wait))
            return
        st["pending"] = None
        expires_at = float(wrapped["token"]["expires_at"])
        st.update(fails=0, next_try=0.0, kind="ok", detail="", last_ok=now, expires_at=expires_at)
        st["events"].append({"event": "SCHWAB_TOKEN_REFRESHED", "expires_at": expires_at, "seconds": 0.0})
        log.info("Schwab access token saved on retry, valid until %s ET.",
                 datetime.fromtimestamp(expires_at, schwab_health.ET).strftime("%H:%M"), extra={"keep": True})
        return
    go, _ = _next_action(now, expires_at, st)
    if not go:
        return
    result = refresh_schwab_token_now(path)
    if result["ok"]:
        if st["fails"]:
            st["events"].append({"event": "SCHWAB_TOKEN_REFRESH_RECOVERED", "after_failures": st["fails"]})
            log.warning("Schwab token refresh RECOVERED after %d failure(s).", st["fails"], extra={"keep": True})
        st.update(fails=0, next_try=0.0, kind="ok", detail="", last_ok=now, expires_at=result["expires_at"])
        st["events"].append({"event": "SCHWAB_TOKEN_REFRESHED", "expires_at": result["expires_at"], "seconds": round(result["seconds"], 3)})
        log.info("Schwab access token refreshed, valid until %s ET.",
                 datetime.fromtimestamp(result["expires_at"], schwab_health.ET).strftime("%H:%M"), extra={"keep": True})
        return
    st["fails"] += 1
    st["kind"], st["detail"] = result["kind"], result["detail"]
    kind = result["kind"]
    if kind == "auth":
        wait = float(_cfg("SCHWAB_AUTH_RETRY_SEC", 900))
        health = st["health"] or {}
        age = f"{health['age_days']:.1f} days old" if health.get("age_days") is not None else "of unknown age"
        hint = (" Also check SCHWAB_APP_KEY / SCHWAB_APP_SECRET in this folder's .env."
                if "invalid_client" in result["detail"] and (health.get("age_days") or 0) < 7 else "")
        _say("auth", wait, logging.CRITICAL, now,
             "SCHWAB REFRESH TOKEN REJECTED (%s): the login is %s. LIVE ES IS OFF until you re-login: %s (the keeper picks the new "
             "token up within %d s, no restart).%s", result["detail"], age, _relogin_command(),
             int(_cfg("SCHWAB_KEEPER_POLL_SEC", 20)), hint)
    elif kind == "replaced":
        wait = 0.0                                       # a re-login just landed: rebuild from the new file right away
    else:
        steps = tuple(_cfg("SCHWAB_REFRESH_BACKOFF_SEC", (15, 30, 60, 120)))
        wait = float(steps[min(st["fails"], len(steps)) - 1])
        if kind == "write":
            _say("write", 300 if st["fails"] > 1 else 0, logging.WARNING, now,
                 "Schwab token refresh succeeded but the new token could not be SAVED (%s); keeping it and retrying the save in %ds.",
                 result["detail"], int(wait))
        else:
            _say("transient" if kind == "transient" else "file", 300 if st["fails"] > 1 else 0, logging.WARNING, now,
                 "Schwab token refresh failed (%s); retrying in %ds. Live ES stays on until the access token expires, then falls back "
                 "to SPX candles.", result["detail"], int(wait))
    st["next_try"] = now + wait
    st["events"].append({"event": "SCHWAB_TOKEN_REFRESH_FAILED", "failure": kind, "detail": result["detail"], "retry_in_sec": wait,
                         "fails": st["fails"]})       # not "kind": Journal.event() takes that name itself


def _loop() -> None:
    st = _KEEPER
    while not st["stop"].is_set():
        try:
            _cycle()
        except Exception as e:                           # noqa: BLE001 -- nothing may end the keeper
            log.error("token keeper cycle crashed (%s: %s); continuing", type(e).__name__, e)
        st["first"].set()
        st["stop"].wait(float(_cfg("SCHWAB_KEEPER_POLL_SEC", 20)))


# ------------------------------------------------------------------------------------------------ control

def disabled_reason() -> str | None:
    """Why the keeper cannot run in this process (None = it can)."""
    if not _cfg("SCHWAB_TOKEN_KEEPER", False):
        return "this process does not own the Schwab token (SCHWAB_TOKEN_KEEPER is off)"
    if not str(_cfg("SCHWAB_APP_KEY", "") or "").strip():
        return "SCHWAB_APP_KEY is not set in .env"
    if not str(_cfg("SCHWAB_APP_SECRET", "") or "").strip():
        return "SCHWAB_APP_SECRET is not set in .env (the refresh needs it)"
    if not _path():
        return "SCHWAB_TOKEN_PATH is not set"
    return None


def running() -> bool:
    t = _KEEPER["thread"]
    return t is not None and t.is_alive()


def start(wait_first_sec: float = 0.0) -> bool:
    """Start the keeper thread (once) when this process owns the token and its credentials are configured. True if running."""
    reason = disabled_reason()
    if reason:
        if _cfg("SCHWAB_TOKEN_KEEPER", False):           # it was meant to run: say why it does not
            log.warning("Schwab token keeper NOT started: %s. Live ES will only work while a Schwab bot or a manual login keeps "
                        "the token fresh.", reason, extra={"keep": True})
        return False
    if running():
        return True
    try:                                                 # imported ONCE, here on the main thread: the keeper thread must not swap the
        with warnings.catch_warnings():                  # global warning filters (authlib warns about httpx on import)
            warnings.simplefilter("ignore")
            import schwab.auth  # noqa: F401
    except Exception as e:                               # noqa: BLE001
        log.warning("Schwab token keeper NOT started: schwab-py cannot be imported (%s: %s)", type(e).__name__, e, extra={"keep": True})
        return False
    _KEEPER["stop"].clear()
    _KEEPER["first"].clear()
    t = threading.Thread(target=_loop, name="schwab-token-keeper", daemon=True)
    _KEEPER["thread"] = t
    t.start()
    log.info("Schwab token keeper started: refreshing %s when the access token has <= %ds left.", _path(),
             int(_cfg("SCHWAB_REFRESH_MARGIN_SEC", 600)), extra={"keep": True})
    if wait_first_sec:
        _KEEPER["first"].wait(wait_first_sec)
    return True


def wait_first(timeout: float) -> bool:
    """Block until the keeper's first cycle has finished (so the start-of-day report sees the refreshed token). False if it is not running."""
    if not running():
        return False
    return _KEEPER["first"].wait(timeout)


def stop(timeout: float = 3.0) -> None:
    _KEEPER["stop"].set()
    t = _KEEPER["thread"]
    if t is not None and t.is_alive():
        t.join(timeout)


def reset_state() -> None:
    """For tests: forget everything the keeper remembered."""
    stop(1.0)
    _KEEPER.update(thread=None, fails=0, next_try=0.0, kind=None, detail="", last_ok=None, sig=None, expires_at=None, health=None,
                   client_factory=None, pending=None, unreadable=0)
    _KEEPER["logged"].clear()
    _KEEPER["events"].clear()
    _KEEPER["first"].clear()


def drain_events() -> list[dict]:
    """Queued keeper events, for the MAIN thread to journal (the journal file has no lock)."""
    out = []
    while _KEEPER["events"]:
        try:
            out.append(_KEEPER["events"].popleft())
        except IndexError:
            break
    return out


def status() -> dict:
    return {"running": running(), "kind": _KEEPER["kind"], "detail": _KEEPER["detail"], "last_ok": _KEEPER["last_ok"],
            "expires_at": _KEEPER["expires_at"], "health": _KEEPER["health"], "fails": _KEEPER["fails"]}


def expired_reason() -> str:
    """Why market_data finds the access token expired, for its 'live ES off' line."""
    if not running():
        return "access token expired (no token keeper runs in this process; only a running Schwab bot or a manual login refreshes it)"
    detail = _KEEPER["detail"]
    return "access token expired (token keeper: " + (f"last refresh failed: {detail}" if detail else "refresh pending") + ")"


def report_line() -> str:
    """One line for the start-of-day report."""
    reason = disabled_reason()
    if reason:
        return f"Schwab token keeper: OFF -- {reason}"
    if not running():
        return "Schwab token keeper: not running"
    health = _KEEPER["health"]
    if health is None:
        try:
            health = schwab_health.assess(_path(), hard_stop=_cfg("FORCE_CLOSE_TIME", None), relogin=_relogin_command())
        except Exception:                                # noqa: BLE001
            health = {"message": "login age unknown"}
    expires_at = _KEEPER["expires_at"]
    valid = (f"; access token valid until {datetime.fromtimestamp(expires_at, schwab_health.ET).strftime('%H:%M')} ET"
             if expires_at and expires_at > time.time() else "; access token not valid right now")
    problem = f" -- LAST REFRESH FAILED ({_KEEPER['kind']}: {_KEEPER['detail']})" if _KEEPER["kind"] not in (None, "ok") else ""
    return f"Schwab token keeper: ON -- {health.get('message', '')}{valid}{problem}"

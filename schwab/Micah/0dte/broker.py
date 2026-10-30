"""Charles Schwab implementation of the Broker contract (docs/BROKER_INTERFACE.md).

Schwab has no paper-trading API: every submitted order is real money. `Broker(...)`
is a factory that picks the implementation from `config.SCHWAB_MODE`:

    sim      (default) PaperBroker from paper_sim.py: a simulated account filled
             against live Schwab quotes. The wrapped SchwabBroker is dry-run and only
             supplies market data.
    dry_run  SchwabBroker that logs every order payload and never sends it.
    live     SchwabBroker sending real orders, but only when SCHWAB_LIVE_ORDERS=true
             is also set; otherwise it is forced to dry-run with a loud warning.

Passing `dry_run=True` (the schedulers' --dry-run flag) always yields the log-only
SchwabBroker, whatever the mode.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import time
import uuid
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

with warnings.catch_warnings():
    # authlib.deprecate installs an "always" filter for its own warning class at
    # import, so the ignore must be added after it to take precedence.
    import authlib.deprecate
    warnings.simplefilter("ignore", authlib.deprecate.AuthlibDeprecationWarning)
    import httpx
    from schwab.auth import client_from_token_file
    from schwab.client import Client as SchwabClient

try:
    from . import options_math
    from . import schwab_accounts
    from . import schwab_health
except ImportError:  # running as a plain script from the folder
    import options_math  # type: ignore
    import schwab_accounts  # type: ignore
    import schwab_health  # type: ignore

if TYPE_CHECKING:
    from paper_sim import PaperBroker

ET = ZoneInfo("US/Eastern")
HERE = Path(__file__).resolve().parent
FOLDER_HINT = str(Path(*HERE.parts[-2:]))     # e.g. Micah\0dte: the folder to cd into for a re-login (two folders are named 0dte now)


def _schwab_root(here: Path) -> Path | None:
    """The schwab folder this bot lives under: schwab/<bot> or schwab/<Person>/<bot>; None for any other layout (tests, copies elsewhere)."""
    if here.parent.name.lower() == "schwab":
        return here.parent
    if len(here.parents) > 1 and here.parents[1].name.lower() == "schwab":
        return here.parents[1]
    return None


def _env_app_key(folder: Path) -> str:
    """The SCHWAB_APP_KEY in a bot folder's .env ("" when there is none): bots with the same key share one Schwab login."""
    try:
        from dotenv import dotenv_values
        return str(dotenv_values(folder / ".env").get("SCHWAB_APP_KEY") or "").strip()
    except Exception:       # noqa: BLE001 -- an unreadable .env just means "unknown app": not a sibling
        return ""
load_dotenv(HERE / ".env")

TERMINAL_STATUSES = {"filled", "canceled", "rejected", "expired", "replaced", "dry_run"}
_STATUS_MAP = {
    "NEW": "new",
    "WORKING": "new",
    "ACCEPTED": "accepted",
    "QUEUED": "pending",
    "PENDING_ACTIVATION": "pending",
    "PENDING_ACKNOWLEDGEMENT": "pending",
    "PENDING_CANCEL": "pending",
    "PENDING_REPLACE": "pending",
    "PENDING_RECALL": "pending",
    "AWAITING_PARENT_ORDER": "pending",
    "AWAITING_CONDITION": "pending",
    "AWAITING_STOP_CONDITION": "pending",
    "AWAITING_MANUAL_REVIEW": "pending",
    "AWAITING_UR_OUT": "pending",
    "AWAITING_RELEASE_TIME": "pending",
    "FILLED": "filled",
    "CANCELED": "canceled",
    "REJECTED": "rejected",
    "EXPIRED": "expired",
    "REPLACED": "replaced",
}
_INDEX_SYMBOLS = {"SPX": "$SPX", "SPXW": "$SPX", "^GSPC": "$SPX", "$SPX": "$SPX",
                  "VIX": "$VIX", "^VIX": "$VIX", "NDX": "$NDX", "RUT": "$RUT", "DJX": "$DJX"}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_OCC_RE = re.compile(r"([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})")


class BrokerError(Exception):
    pass


class SchwabAuthError(BrokerError):
    """The Schwab token is invalid or expired (HTTP 401, or the refresh failed). It is a BrokerError, so every existing
    handler treats it as one -- but it carries the command that fixes it, and the broker stops calling Schwab for
    AUTH_PROBE_SEC after one so a dead token costs microseconds per call instead of three retries with sleeps."""
    reauth_command = "python schwab_login.py --force"


def _looks_like_auth_failure(exc: Exception) -> bool:
    """The token refresh runs inside the client call: authlib raises OAuthError (a dead refresh token) and a non-JSON token
    response raises a ValueError (JSONDecodeError)."""
    return (isinstance(exc, ValueError) or type(exc).__module__.startswith("authlib")
            or "token" in str(exc).lower() or "oauth" in type(exc).__name__.lower())


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root.upper()}{expiration.strftime('%y%m%d')}{right.upper()[0]}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> dict:
    m = _OCC_RE.fullmatch(symbol.strip().upper())
    if not m:
        raise BrokerError(f"not an OCC option symbol: {symbol!r}")
    root, ymd, right, strike = m.groups()
    return {
        "root": root,
        "expiration": datetime.strptime(ymd, "%y%m%d").date(),
        "right": right,
        "strike": int(strike) / 1000.0,
    }


def schwab_option_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root.upper():<6}{expiration.strftime('%y%m%d')}{right.upper()[0]}{int(round(strike * 1000)):08d}"


def to_schwab_symbol(occ: str) -> str:
    p = parse_occ(occ)
    return schwab_option_symbol(p["root"], p["expiration"], p["right"], p["strike"])


def to_occ(schwab_symbol: str) -> str:
    p = parse_occ(schwab_symbol)
    return occ_symbol(p["root"], p["expiration"], p["right"], p["strike"])


def _to_et(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(ET)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def _greek(value: Any) -> float | None:
    # Schwab reports -999 (or NaN) for greeks/IV it cannot compute.
    v = _f(value)
    return None if v is None or v <= -999.0 else v


def _strip_index(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    return symbol[1:] if symbol.startswith("$") else symbol


class _CallableLog:
    """Adapts the plain `log(message)` callable the strategies pass to the Logger calls used here."""

    def __init__(self, fn: Callable[[str], Any]):
        self._fn = fn

    def _emit(self, msg: str, *args: Any) -> None:
        self._fn(msg % args if args else msg)

    info = warning = error = critical = _emit


class SchwabBroker:
    name = "schwab"
    is_paper = False
    _forced_dry_run_warned = False

    occ_symbol = staticmethod(occ_symbol)
    parse_occ = staticmethod(parse_occ)

    def __init__(self, config_module, dry_run: bool | None = None, log=None):
        self.config = config_module
        if log is None:
            self.log: Any = logging.getLogger("broker.schwab")
        else:
            self.log = log if hasattr(log, "info") else _CallableLog(log)
        self._sleep = time.sleep
        self._client: SchwabClient | None = None
        self._account_hash: str | None = None
        self._account_number: str | None = None
        self._dry_orders: dict[str, dict] = {}
        self._fallback: Any = None
        self._fallback_warned = False
        self._auth_dead_until = 0.0            # monotonic time before which Schwab is not called again (see SchwabAuthError)
        self._token_created: datetime | None = None
        self._client_token_path: Path | None = None      # the token file the current client was built from

        mode = str(self._cfg("SCHWAB_MODE", "live")).strip().lower()
        if mode not in ("sim", "dry_run", "live"):
            raise BrokerError(f"SCHWAB_MODE={mode!r} is not one of sim, dry_run, live")
        requested = dry_run if dry_run is not None else bool(self._cfg("DRY_RUN", True))
        if mode in ("sim", "dry_run"):
            requested = True
        live_env = os.getenv("SCHWAB_LIVE_ORDERS", "").strip().lower() == "true"
        cls = type(self)
        if not requested and not live_env:
            requested = True
            if not cls._forced_dry_run_warned:
                self.log.warning(
                    "SCHWAB: dry_run=False requested but SCHWAB_LIVE_ORDERS is not 'true'; "
                    "FORCING dry_run=True. Schwab has no paper trading - set SCHWAB_LIVE_ORDERS=true "
                    "only when you intend to trade real money."
                )
                cls._forced_dry_run_warned = True
        self.dry_run = bool(requested)
        if not self.dry_run:
            self.log.warning("SCHWAB: LIVE ORDERS ENABLED. Every order placed is REAL MONEY.")

        self.risk_free_rate = float(self._cfg("RISK_FREE_RATE", 0.04))
        self.close_slippage = float(self._cfg("CLOSE_SLIPPAGE", 0.05))
        self.close_retry_sec = float(self._cfg("CLOSE_RETRY_SEC", 10.0))
        self.close_max_retries = int(self._cfg("CLOSE_MAX_RETRIES", 5))
        self.account_index = int(self._cfg("SCHWAB_ACCOUNT_INDEX", os.getenv("SCHWAB_ACCOUNT_INDEX", 0)))
        if self.account_index < 0:                      # 2026-10-26: a negative index would silently pick an account from the END of Schwab's list
            raise BrokerError(f"SCHWAB_ACCOUNT_INDEX={self.account_index} is invalid: it counts from 0")
        self.log_dir = self._resolve_path(self._cfg("LOG_DIR", None) or "logs")
        token = self._cfg("SCHWAB_TOKEN_PATH", None) or os.getenv("SCHWAB_TOKEN_PATH") \
            or self._cfg("TOKEN_PATH", None) or "token.json"
        self.token_path = self._resolve_path(token)
        if self.token_path.is_dir():                     # a FOLDER means the token.json inside it (opening a folder is a PermissionError on Windows)
            self.token_path = self.token_path / "token.json"
        # 2026-10-14: with no explicit SCHWAB_TOKEN_PATH this bot and its sibling Schwab bot (schwab/<Person>/0dte <-> schwab/<Person>/45DTE: same app, same
        # account) share whichever login is NEWEST -- see _select_token_path
        self._explicit_token_path = self.token_path != HERE / "token.json"
        # 2026-10-15 go-live safety. Active in the live and dry_run modes (never in the simulator): the account must be SELECTED (see
        # schwab_accounts.py) before live orders, the capital the bot sees is capped (_with_guards) and opening orders are clamped to
        # LIVE_MAX_CONTRACTS_PER_TRADE (place_credit_spread). The strategies' own sizing is capped by config.HARD_MAX_CONTRACTS too.
        self._guards_active = mode != "sim"
        self.selection_path = self._resolve_path(self._cfg("SCHWAB_ACCOUNT_FILE", None) or schwab_accounts.FILE_NAME)
        self._selected_stamp: int | None = None            # mtime of the selection file the cached account hash was resolved from
        self._shared_with: list[Path] = []                  # other person folders whose selection names this same account (new orders refused)
        self._account_entry: dict | None = None            # the linked account in use: {"last4", "type", "selected"}
        self._capital: dict | None = None                  # today's capital state: {"date", "raw_start", "cap", "floor", "pct"}
        self._last_account: dict | None = None             # the last get_account() result, for the start-of-day report
        self._contract_cap()                                # an invalid cap stops the bot at start-up, not at the first order

    # ------------------------------------------------------------------ plumbing

    def _cfg(self, key: str, default: Any) -> Any:
        return getattr(self.config, key, default)

    @staticmethod
    def _resolve_path(p: str | os.PathLike) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (HERE / path).resolve()

    AUTH_PROBE_SEC = 60.0

    def _sibling_token_paths(self) -> list[Path]:
        """token.json of the other Schwab bot folders this bot may share a login with: the folders next to it (a folder with its own
        schwab_login.py -- the original rule), and, since the bots moved into person folders (schwab/<Person>/<bot>), every other bot
        folder under the schwab root whose .env uses the SAME Schwab app key (2026-10-30). One app key has one valid login, the newest, so
        the paper schwab/45DTE finds schwab/Micah/0dte's token by itself and a bot on another app (another login) is never mixed in."""
        found: list[Path] = []
        try:
            for folder in sorted(HERE.parent.iterdir()):
                if folder != HERE and folder.is_dir() and (folder / "schwab_login.py").is_file() and (folder / "token.json").is_file():
                    found.append(folder / "token.json")
        except OSError:
            pass
        root = _schwab_root(HERE)
        own_key = str(self._cfg("SCHWAB_APP_KEY", None) or os.getenv("SCHWAB_APP_KEY") or "").strip()
        if root is None or not own_key:
            return found
        try:
            scripts = sorted(set(root.glob("*/schwab_login.py")) | set(root.glob("*/*/schwab_login.py")))
        except OSError:
            scripts = []
        for script in scripts:
            folder = script.parent
            token = folder / "token.json"
            if folder == HERE or token in found or not token.is_file():
                continue
            if _env_app_key(folder) == own_key:
                found.append(token)
        return found

    def _select_token_path(self) -> Path:
        """The token file to use right now. Logging in again appears to cancel the older login for the same Schwab app, so the bots must not
        each cling to a file of their own: with no explicit SCHWAB_TOKEN_PATH this bot uses the NEWEST login among its own token.json and its
        sibling bots' (a tie keeps its own). That is the valid one whether or not an older login is cancelled, and re-logging in from either
        bot's folder keeps both working without restarting. An explicit SCHWAB_TOKEN_PATH, or a path set by hand, is used exactly as given."""
        own = HERE / "token.json"
        siblings = self._sibling_token_paths()
        if self._explicit_token_path or self.token_path not in [own, *siblings]:
            return self.token_path
        best = own
        best_created = schwab_health.token_created_at(own) if own.is_file() else None
        if own.is_file() and best_created is None:
            return own                   # it exists but cannot be read right now (another bot is rewriting it): never swap to a sibling's older copy
        for candidate in siblings:
            created = schwab_health.token_created_at(candidate)
            if created is not None and (best_created is None or created > best_created):
                best, best_created = candidate, created
        return best

    def token_status(self, now: datetime | None = None) -> dict:
        """schwab_health.assess for this bot's token file (state OK / WARN / EXPIRING / DEAD / MISSING)."""
        stop = self._cfg("FORCE_CLOSE_TIME", None) or schwab_health.DEFAULT_HARD_STOP
        return schwab_health.assess(self._select_token_path(), now, hard_stop=stop,
                                    margin_min=float(self._cfg("TOKEN_ENTRY_MARGIN_MIN", 30)),
                                    warn_days=float(self._cfg("TOKEN_WARN_DAYS", 5.0)),
                                    relogin=f"cd {FOLDER_HINT} && {SchwabAuthError.reauth_command}")

    def _auth_error(self, name: str, detail: str) -> SchwabAuthError:
        self._auth_dead_until = time.monotonic() + self.AUTH_PROBE_SEC
        return SchwabAuthError(f"Schwab {name} failed: {detail}. The token is invalid or expired [token file: {self.token_path}] -- "
                               f"re-login: cd {FOLDER_HINT} && {SchwabAuthError.reauth_command}")

    def _get_client(self) -> SchwabClient:
        chosen = self._select_token_path()
        if chosen != self.token_path:
            self.log.warning("Schwab: using the NEWER login in %s (this bot's own token file is older or missing)", chosen)
            self.token_path = chosen
        created = schwab_health.token_created_at(self.token_path) if self.token_path.is_file() else None
        if self._client is not None and (self._token_created is None
                                         or (created == self._token_created and self.token_path == self._client_token_path)):
            return self._client        # (an injected client, as the tests use, has no recorded token and is never replaced)
        if self._client is not None and created is None and self.token_path == self._client_token_path and self.token_path.is_file():
            # the file exists but cannot be read right now: another bot on this login is rewriting it (a token refresh). Keep the client
            # we have -- it holds its own copy of the token -- rather than drop it and report a re-login that is not needed (2026-10-26)
            self.log.debug("Schwab token file is unreadable at the moment (being rewritten?): keeping the current client")
            return self._client
        if self._client is not None:
            self.log.warning("Schwab token file was replaced (new login): reloading the client")
            self._client = None
        app_key = self._cfg("SCHWAB_APP_KEY", None) or os.getenv("SCHWAB_APP_KEY")
        app_secret = self._cfg("SCHWAB_APP_SECRET", None) or os.getenv("SCHWAB_APP_SECRET")
        if not app_key or not app_secret:
            raise BrokerError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing (set them in the folder .env)")
        if not self.token_path.is_file():
            raise BrokerError(f"Schwab token file not found at {self.token_path}; run schwab_login.py first")
        try:
            self._client = client_from_token_file(str(self.token_path), app_key, app_secret)
        except Exception as exc:
            raise SchwabAuthError(f"could not load Schwab token from {self.token_path}: {exc} -- re-login: "
                                  f"cd {FOLDER_HINT} && {SchwabAuthError.reauth_command}") from exc
        if created is None:              # the pre-read was torn but the loader got a whole file: stamp it, or a later new login is never noticed
            created = schwab_health.token_created_at(self.token_path)
        self._token_created = created
        self._client_token_path = self.token_path
        self._auth_dead_until = 0.0
        return self._client

    def _request(self, fn, *args, allow: tuple[int, ...] = (), attempts: int = 3, **kwargs) -> httpx.Response:
        name = getattr(fn, "__name__", str(fn))
        wait = self._auth_dead_until - time.monotonic()
        if wait > 0:
            raise SchwabAuthError(f"Schwab {name} skipped: the token was rejected a moment ago (next attempt in {wait:.0f}s) "
                                  f"[token file: {self.token_path}] -- re-login: cd {FOLDER_HINT} && {SchwabAuthError.reauth_command}")
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = fn(*args, **kwargs)
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                self.log.warning("Schwab %s transport error (attempt %d/%d): %s", name, attempt + 1, attempts, exc)
                self._sleep(0.5 * (2 ** attempt))
                continue
            except Exception as exc:     # 2026-10-08: the token refresh happens INSIDE the call; these used to kill the process
                detail = f"{type(exc).__name__}: {str(exc)[:200]}"
                if _looks_like_auth_failure(exc):
                    raise self._auth_error(name, f"token refresh failed ({detail})") from exc
                raise BrokerError(f"Schwab {name} failed: {detail}") from exc
            if resp.status_code == 401 and 401 not in allow:
                raise self._auth_error(name, f"HTTP 401 {resp.text[:200]}")
            if resp.status_code in _RETRY_STATUS and attempt < attempts - 1:
                self.log.warning("Schwab %s HTTP %s (attempt %d/%d)", name, resp.status_code, attempt + 1, attempts)
                self._sleep(0.5 * (2 ** attempt))
                continue
            if resp.is_error and resp.status_code not in allow:
                raise BrokerError(f"Schwab {name} failed: HTTP {resp.status_code} {resp.text[:400]}")
            self._auth_dead_until = 0.0
            return resp
        raise BrokerError(f"Schwab {name} failed after {attempts} attempt{'s' if attempts != 1 else ''}: {last_exc}")

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(f"Schwab returned non-JSON body: {resp.text[:200]}") from exc

    def _selection_stamp(self) -> int | None:
        try:
            return self.selection_path.stat().st_mtime_ns
        except OSError:
            return None

    def _assert_selection_unchanged(self) -> None:
        """Opening orders only (live). The owner may choose another account, or remove the choice, after this bot resolved its account. The bot
        keeps managing what it holds (reads, cancels and closes stay on the account it started with, so nothing is orphaned) but opens nothing
        new until it is restarted: a bot must never open on one account while its positions sit on another."""
        if self.dry_run or not self._guards_active:
            return
        if self._account_hash is None:
            self._hash()                                 # the first order of a process: resolve (and validate) the account first
        in_use = f"...{(self._account_number or '')[-4:]}"
        if os.getenv("SCHWAB_ALLOW_SHARED_ACCOUNT", "").strip().lower() != "true":
            self._shared_with = schwab_accounts.other_folders_using(self._account_hash, [self.selection_path.parent])
            if self._shared_with:
                names = ", ".join(str(Path(*c.parts[-2:])) for c in self._shared_with)
                raise BrokerError(f"NO new orders: account {in_use} is also the selected account of {names}. One bot per account -- open "
                                  f"positions are still managed here. Change one folder's choice (python schwab_select_account.py), or set "
                                  f"SCHWAB_ALLOW_SHARED_ACCOUNT=true in .env and restart to allow two bots on one account.")
        stamp = self._selection_stamp()
        if stamp == self._selected_stamp:
            return
        selection = schwab_accounts.read_selection(self.selection_path)
        if selection is None:
            raise BrokerError(f"The Schwab account selection was removed or is unreadable while the bot runs: NO new orders until it is restarted "
                              f"(open positions are still managed on {in_use}). Run python schwab_select_account.py, then restart the bot.")
        if selection["hash_value"] != self._account_hash:
            raise BrokerError(f"The selected Schwab account is now {schwab_accounts.describe(selection)} but this bot trades {in_use}: NO new orders "
                              f"until it is restarted (open positions are still managed on {in_use}).")
        self._selected_stamp = stamp                 # the same account was saved again

    def _hash(self) -> str:
        """Schwab's hash of the account the bot trades. LIVE orders need an account the owner SELECTED after the last login (selected_account.json:
        schwab_login.py offers the choice, `python schwab_select_account.py` repeats it); it must still be linked to this login. Without
        a saved selection a dry-run/sim bot falls back to SCHWAB_ACCOUNT_INDEX (as before), a live bot refuses."""
        if self._account_hash is not None:
            return self._account_hash                # resolved once per process: reads, cancels and closes stay on this account (see _assert_selection_unchanged)
        client = self._get_client()
        numbers = self._json(self._request(client.get_account_numbers))
        if not numbers:
            raise BrokerError("Schwab returned no linked accounts for this token")
        self._selected_stamp = self._selection_stamp()
        selection = schwab_accounts.read_selection(self.selection_path) if self._guards_active else None
        if selection is not None:
            entry = next((n for n in numbers if n.get("hashValue") == selection["hash_value"]), None)
            if entry is None or str(entry.get("accountNumber", ""))[-4:] != str(selection["last4"]):
                raise BrokerError(f"The selected Schwab account ({schwab_accounts.describe(selection)}) is not linked to this login any more: "
                                  f"run python schwab_select_account.py to choose the account again")
        elif not self.dry_run:
            raise BrokerError("No Schwab account has been selected for LIVE orders: run python schwab_select_account.py "
                              "(schwab_login.py also offers the choice right after a login). Nothing was sent.")
        else:
            if self.account_index >= len(numbers):
                raise BrokerError(f"SCHWAB_ACCOUNT_INDEX={self.account_index} but only {len(numbers)} account(s) linked")
            entry = numbers[self.account_index]
        self._account_hash = entry["hashValue"]
        self._account_number = str(entry.get("accountNumber", ""))
        self._account_entry = {"last4": self._account_number[-4:], "type": str((selection or {}).get("type") or ""),
                               "selected": selection is not None}
        # 2026-10-26: one bot per account. Another person folder (schwab/<Person>/0dte) selecting this same account does not stop reads,
        # cancels or closes -- a bot must always be able to manage what it holds -- only NEW orders (see _assert_selection_unchanged)
        self._shared_with = schwab_accounts.other_folders_using(self._account_hash, [self.selection_path.parent]) if selection else []
        return self._account_hash

    @staticmethod
    def _quote_symbol(underlying: str) -> str:
        u = underlying.strip().upper()
        return _INDEX_SYMBOLS.get(u, u)

    def _securities_account(self, with_positions: bool) -> dict:
        client = self._get_client()
        fields = SchwabClient.Account.Fields.POSITIONS if with_positions else None
        data = self._json(self._request(client.get_account, self._hash(), fields=fields))
        acct = data.get("securitiesAccount", data)
        if not isinstance(acct, dict):
            raise BrokerError(f"unexpected account payload: {str(data)[:200]}")
        return acct

    # ------------------------------------------------------------------ account

    def get_account(self) -> dict:
        acct = self._securities_account(with_positions=False)
        bal = acct.get("currentBalances") or {}
        buying_power = _f(bal.get("buyingPower"))
        if buying_power is None:
            buying_power = _f(bal.get("availableFunds"), _f(bal.get("cashAvailableForTrading"), 0.0))
        obp = None
        for key in ("optionBuyingPower", "buyingPowerNonMarginableTrade",
                    "availableFundsNonMarginableTrade", "cashAvailableForTrading"):
            obp = _f(bal.get(key))
            if obp is not None:
                break
        raw = {
            "equity": _f(bal.get("liquidationValue"), _f(bal.get("equity"), 0.0)),
            "cash": _f(bal.get("cashBalance"), 0.0),
            "buying_power": buying_power,
            "options_buying_power": obp if obp is not None else buying_power,
            "account_id": str(acct.get("accountNumber") or self._account_number or ""),
        }
        self._last_account = self._with_guards(raw)
        return self._last_account

    # ------------------------------------------------------------------ go-live capital cap

    def _capital_cap_active(self) -> bool:
        return self._guards_active and bool(self._cfg("LIVE_CAPITAL_CAP_ENABLED", False))

    def _capital_file(self) -> Path:
        return self.log_dir / "live_capital.json"

    def _capital_state(self, raw_equity: float) -> dict:
        """Today's capital base, fixed at the first account read of the ET day and kept in logs/live_capital.json so a restart the same
        day continues it: cap = the GREATER of LIVE_CAPITAL_FLOOR_USD and LIVE_CAPITAL_PCT of the account's total value, never more than the
        account actually holds."""
        today = datetime.now(ET).date().isoformat()
        state = self._capital
        if state is None:
            try:
                state = json.loads(self._capital_file().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        floor = float(self._cfg("LIVE_CAPITAL_FLOOR_USD", 1000.0))
        pct = float(self._cfg("LIVE_CAPITAL_PCT", 0.25))
        account = hashlib.sha256((self._account_hash or "").encode()).hexdigest()[:12]       # a digest: the account hash itself is not written to logs
        if not isinstance(state, dict) or state.get("date") != today or state.get("account") != account \
                or state.get("floor") != floor or state.get("pct") != pct \
                or not isinstance(state.get("raw_start"), (int, float)) or not isinstance(state.get("cap"), (int, float)):
            state = {"date": today, "account": account, "raw_start": raw_equity,
                     "cap": round(min(raw_equity, max(floor, pct * raw_equity)), 2), "floor": floor, "pct": pct}
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._capital_file().write_text(json.dumps(state), encoding="utf-8")
            except OSError as exc:
                self.log.warning("could not save %s (%s): the capital base is kept in memory only", self._capital_file(), exc)
        self._capital = state
        return state

    def _with_guards(self, raw: dict) -> dict:
        """The account as the strategies must see it. With the cap on, the bot works with a virtual account: today's capital cap less the
        day's real loss (so a $1 loss in the account is a $1 loss to the bot's daily-loss limit), never MORE than the cap (a profit, a deposit
        or a hand trade cannot raise it), and cash / buying power never exceed it. The real figures stay in raw_equity / raw_cash / raw_buying_power."""
        if not self._capital_cap_active():
            return raw
        equity = raw.get("equity") or 0.0
        if equity <= 0:                                  # total value unknown: the cap cannot be sized, so nothing is usable (fail closed)
            return {**raw, "equity": 0.0, "cash": 0.0, "buying_power": 0.0, "options_buying_power": 0.0,
                    "raw_equity": equity, "raw_cash": raw.get("cash"), "raw_buying_power": raw.get("buying_power"), "capital_cap": 0.0}
        state = self._capital_state(float(equity))
        virtual = max(min(state["cap"] + (float(equity) - state["raw_start"]), state["cap"]), 0.0)       # a ceiling: profits and deposits never raise it
        capped = dict(raw)
        capped["equity"] = round(virtual, 2)
        for key in ("cash", "buying_power", "options_buying_power"):
            value = raw.get(key)
            capped[key] = round(min(value, virtual), 2) if isinstance(value, (int, float)) else value
        capped.update(raw_equity=equity, raw_cash=raw.get("cash"), raw_buying_power=raw.get("buying_power"), capital_cap=state["cap"])
        return capped

    def _contract_cap(self) -> int | None:
        value = self._cfg("LIVE_MAX_CONTRACTS_PER_TRADE", None)
        if not self._guards_active or value is None:
            return None
        try:
            cap = int(value)
        except (TypeError, ValueError):
            cap = 0
        if cap < 1:                                      # 0, negative or garbage must never mean "no cap"
            raise BrokerError(f"LIVE_MAX_CONTRACTS_PER_TRADE={value!r} is invalid: it must be a whole number of at least 1")
        return cap

    def account_report_lines(self) -> list[str]:
        """Lines for the start-of-day report: which account, how much capital, how many contracts, live or dry-run (nothing in the simulator)."""
        if not self._guards_active:
            return []
        try:
            self._hash()
        except BrokerError as exc:
            return [f"Account: NOT SELECTED -- {exc}"]
        entry = self._account_entry or {}
        kind = f" ({entry['type']})" if entry.get("type") else ""
        how = "selected" if entry.get("selected") else f"by SCHWAB_ACCOUNT_INDEX={self.account_index}, NO account selected yet"
        lines = [f"Account: ...{entry.get('last4', '?')}{kind} [{how}] -- "
                 + ("DRY-RUN: orders are logged, not sent" if self.dry_run else "LIVE ORDERS: REAL MONEY")]
        if self._shared_with:
            names = ", ".join(str(Path(*c.parts[-2:])) for c in self._shared_with)
            if os.getenv("SCHWAB_ALLOW_SHARED_ACCOUNT", "").strip().lower() == "true":
                lines.append(f"WARNING: this account is ALSO traded by {names}: SCHWAB_ALLOW_SHARED_ACCOUNT=true -- two bots on one account")
            elif self.dry_run:
                lines.append(f"WARNING: this account is also the selected account of {names}: once LIVE, new orders would be refused here "
                             f"(dry-run logs them)")
            else:
                lines.append(f"WARNING: this account is also the selected account of {names}: NEW orders are refused here "
                             f"(SCHWAB_ALLOW_SHARED_ACCOUNT=true in .env, then a restart, overrides)")
        acct, state = self._last_account, self._capital
        if self._capital_cap_active() and acct and state and acct.get("raw_equity") is not None:
            lines.append(f"Capital: ${acct['equity']:,.2f} usable of ${acct['raw_equity']:,.2f} in the account (cap = the greater of "
                         f"${state['floor']:,.0f} or {state['pct']:.0%} of the account, never more than it holds; this bot process only)")
        cap = self._contract_cap()
        if cap:
            lines.append(f"Contracts: at most {cap} per opening order (LIVE_MAX_CONTRACTS_PER_TRADE)")
        return lines

    def _equity_history_path(self) -> Path:
        return self.log_dir / "equity_history.csv"

    def _read_equity_history(self) -> list[tuple[date, float]]:
        path = self._equity_history_path()
        if not path.is_file():
            return []
        rows: list[tuple[date, float]] = []
        with path.open(newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or row[0] == "date":
                    continue
                try:
                    rows.append((date.fromisoformat(row[0]), float(row[1])))
                except ValueError:
                    continue
        rows.sort(key=lambda r: r[0])
        return rows

    def record_daily_equity(self) -> None:
        acct = self.get_account()
        equity = acct.get("raw_equity", acct["equity"])          # the real account value, not the capped one: get_pnl_summary compares it with the real value
        today = datetime.now(ET).date()
        rows = [r for r in self._read_equity_history() if r[0] != today]
        rows.append((today, equity))
        rows.sort(key=lambda r: r[0])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self._equity_history_path().open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "equity"])
            for d, e in rows:
                writer.writerow([d.isoformat(), f"{e:.2f}"])

    def get_pnl_summary(self) -> dict:
        now = datetime.now(ET)
        today = now.date()
        result: dict[str, Any] = {"ytd": None, "mtd": None, "today": None, "as_of": now}
        try:
            acct = self._securities_account(with_positions=True)
            bal = acct.get("currentBalances") or {}
            equity_now = _f(bal.get("liquidationValue"), _f(bal.get("equity")))
            positions = acct.get("positions") or []
            day_pl = [_f(p.get("currentDayProfitLoss")) for p in positions]
            if positions and any(v is not None for v in day_pl):
                result["today"] = round(sum(v for v in day_pl if v is not None), 2)
        except Exception as exc:
            self.log.warning("get_pnl_summary: account fetch failed: %s", exc)
            equity_now = None
        try:
            history = self._read_equity_history()
            if equity_now is None and history:
                equity_now = history[-1][1]
            if equity_now is not None:
                year_rows = [r for r in history if r[0].year == today.year and r[0] < today]
                month_rows = [r for r in year_rows if r[0].month == today.month]
                if year_rows:
                    result["ytd"] = round(equity_now - year_rows[0][1], 2)
                if month_rows:
                    result["mtd"] = round(equity_now - month_rows[0][1], 2)
        except Exception as exc:
            self.log.warning("get_pnl_summary: history read failed: %s", exc)
        return result

    # ------------------------------------------------------------------ positions

    def _normalize_position(self, raw: dict) -> dict | None:
        inst = raw.get("instrument") or {}
        asset_type = str(inst.get("assetType", "")).upper()
        symbol = str(inst.get("symbol", ""))
        qty = int(round(_f(raw.get("longQuantity"), 0.0) - _f(raw.get("shortQuantity"), 0.0)))
        market_value = _f(raw.get("marketValue"))
        long_pl = _f(raw.get("longOpenProfitLoss"))
        short_pl = _f(raw.get("shortOpenProfitLoss"))
        unreal = None if long_pl is None and short_pl is None else (long_pl or 0.0) + (short_pl or 0.0)
        pos = {
            "symbol": symbol,
            "underlying": _strip_index(inst.get("underlyingSymbol")) or symbol,
            "qty": qty,
            "avg_price": _f(raw.get("averagePrice"), 0.0),
            "current_price": None,
            "market_value": market_value,
            "unrealized_pl": unreal,
            "asset_class": "stock",
            "expiration": None,
            "strike": None,
            "right": None,
        }
        multiplier = 1.0
        if asset_type == "OPTION":
            try:
                p = parse_occ(symbol)
            except BrokerError:
                self.log.warning("unparseable option symbol in positions: %r", symbol)
                return None
            pos.update({
                "symbol": occ_symbol(p["root"], p["expiration"], p["right"], p["strike"]),
                "asset_class": "option",
                "expiration": p["expiration"],
                "strike": p["strike"],
                "right": p["right"],
            })
            if not inst.get("underlyingSymbol"):
                pos["underlying"] = p["root"]
            multiplier = 100.0
        if market_value is not None and qty != 0:
            pos["current_price"] = round(abs(market_value) / (abs(qty) * multiplier), 4)
        return pos

    def get_positions(self) -> list[dict]:
        acct = self._securities_account(with_positions=True)
        out = []
        for raw in acct.get("positions") or []:
            pos = self._normalize_position(raw)
            if pos is not None and pos["qty"] != 0:
                out.append(pos)
        return out

    # ------------------------------------------------------------------ orders (read)

    @staticmethod
    def _leg_side(instruction: str) -> str:
        return "buy" if instruction.upper().startswith("BUY") else "sell"

    def _normalize_order(self, raw: dict) -> dict:
        raw_status = str(raw.get("status", "")).upper()
        status = _STATUS_MAP.get(raw_status, "unknown")
        qty = int(round(_f(raw.get("quantity"), 0.0)))
        filled_qty = int(round(_f(raw.get("filledQuantity"), 0.0)))
        if status in ("new", "accepted", "pending") and 0 < filled_qty < qty:
            status = "partially_filled"

        fills: dict[Any, list[tuple[float, float]]] = {}
        for activity in raw.get("orderActivityCollection") or []:
            for ex in activity.get("executionLegs") or []:
                fills.setdefault(ex.get("legId"), []).append(
                    (_f(ex.get("quantity"), 0.0), _f(ex.get("price"), 0.0)))

        legs = []
        net = 0.0
        all_filled = True
        underlying = None
        for leg in raw.get("orderLegCollection") or []:
            inst = leg.get("instrument") or {}
            sym = str(inst.get("symbol", ""))
            if str(inst.get("assetType", "")).upper() == "OPTION":
                try:
                    sym = to_occ(sym)
                except BrokerError:
                    pass
                underlying = underlying or _strip_index(inst.get("underlyingSymbol"))
            else:
                underlying = underlying or sym
            instruction = str(leg.get("instruction", ""))
            leg_qty = int(round(_f(leg.get("quantity"), 0.0)))
            leg_fills = fills.get(leg.get("legId"), [])
            fill_qty = sum(q for q, _ in leg_fills)
            avg = (sum(q * p for q, p in leg_fills) / fill_qty) if fill_qty > 0 else None
            if avg is None:
                all_filled = False
            else:
                net += avg if self._leg_side(instruction) == "sell" else -avg
            legs.append({
                "symbol": sym,
                "side": self._leg_side(instruction),
                "position_intent": instruction.lower(),
                "ratio_qty": max(1, leg_qty // qty) if qty else leg_qty,
                "status": status,
                "filled_avg_price": avg,
            })

        limit_price = _f(raw.get("price"))
        if legs and all_filled:
            filled_avg = round(abs(net), 4) if len(legs) > 1 else legs[0]["filled_avg_price"]
        elif status == "filled" and limit_price is not None:
            filled_avg = limit_price
        else:
            filled_avg = None

        duration = str(raw.get("duration", "")).upper()
        tif = {"GOOD_TILL_CANCEL": "gtc", "DAY": "day", "FILL_OR_KILL": "fok",
               "IMMEDIATE_OR_CANCEL": "ioc"}.get(duration, duration.lower() or "gtc")
        if len(legs) > 1:
            symbol = underlying or (parse_occ(legs[0]["symbol"])["root"] if legs else "")
            side = None
        else:
            symbol = legs[0]["symbol"] if legs else ""
            side = legs[0]["side"] if legs else None
        return {
            "id": str(raw.get("orderId", "")),
            "status": status,
            "symbol": symbol,
            "order_class": "mleg" if len(legs) > 1 else "simple",
            "side": side,
            "qty": qty,
            "filled_qty": filled_qty,
            "limit_price": limit_price,
            "filled_avg_price": filled_avg,
            "time_in_force": tif,
            "legs": legs,
            "submitted_at": _to_et(raw.get("enteredTime")) or datetime.now(ET),
            "filled_at": _to_et(raw.get("closeTime")) if status == "filled" else None,
            "raw": raw,
        }

    def get_open_orders(self) -> list[dict]:
        client = self._get_client()
        now_utc = datetime.now(timezone.utc)
        resp = self._request(client.get_orders_for_account, self._hash(),
                             from_entered_datetime=now_utc - timedelta(days=59),
                             to_entered_datetime=now_utc)
        orders = [self._normalize_order(o) for o in self._json(resp) or []]
        return [o for o in orders if o["status"] not in TERMINAL_STATUSES and o["status"] != "unknown"]

    def get_order(self, order_id: str) -> dict | None:
        order_id = str(order_id)
        if order_id.startswith("DRY-"):
            return self._dry_orders.get(order_id)
        client = self._get_client()
        resp = self._request(client.get_order, order_id, self._hash(), allow=(404,))
        if resp.status_code == 404:
            return None
        return self._normalize_order(self._json(resp))

    def wait_for_fill(self, order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout_sec
        order = self.get_order(order_id)
        while order is None or order["status"] not in TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                break
            self._sleep(poll_sec)
            order = self.get_order(order_id)
        if order is None:
            raise BrokerError(f"order {order_id} not found while waiting for fill")
        return order

    # ------------------------------------------------------------------ orders (write)

    @staticmethod
    def _duration(time_in_force: str) -> str:
        tif = time_in_force.lower()
        if tif in ("gtc", "good_till_cancel"):
            return "GOOD_TILL_CANCEL"
        if tif == "day":
            return "DAY"
        raise BrokerError(f"unsupported time_in_force for Schwab spreads: {time_in_force!r}")

    def _vertical_json(self, order_type: str, price: float, time_in_force: str, qty: int,
                       legs: list[tuple[str, str]]) -> dict:
        if qty <= 0:
            raise BrokerError("qty must be positive")
        if price <= 0:
            raise BrokerError("limit price must be positive")
        return {
            "orderType": order_type,
            "session": "NORMAL",
            "price": f"{price:.2f}",
            "duration": self._duration(time_in_force),
            "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": "VERTICAL",
            "orderLegCollection": [
                {"instruction": instruction, "quantity": qty,
                 "instrument": {"symbol": to_schwab_symbol(symbol), "assetType": "OPTION"}}
                for instruction, symbol in legs
            ],
        }

    def _synthetic_order(self, order_json: dict, underlying: str, time_in_force: str,
                         order_id: str, status: str) -> dict:
        leg_specs = order_json.get("orderLegCollection") or []
        qty = int(leg_specs[0]["quantity"]) if leg_specs else 0
        legs = [{
            "symbol": to_occ(leg["instrument"]["symbol"]) if leg["instrument"].get("assetType") == "OPTION"
            else leg["instrument"]["symbol"],
            "side": self._leg_side(leg["instruction"]),
            "position_intent": leg["instruction"].lower(),
            "ratio_qty": 1,
            "status": status,
            "filled_avg_price": None,
        } for leg in leg_specs]
        return {
            "id": order_id,
            "status": status,
            "symbol": underlying,
            "order_class": "mleg" if len(legs) > 1 else "simple",
            "side": None if len(legs) > 1 else (legs[0]["side"] if legs else None),
            "qty": qty,
            "filled_qty": 0,
            "limit_price": _f(order_json.get("price")),
            "filled_avg_price": None,
            "time_in_force": time_in_force.lower(),
            "legs": legs,
            "submitted_at": datetime.now(ET),
            "filled_at": None,
            "raw": order_json,
        }

    def _dry_order(self, order_json: dict, underlying: str, time_in_force: str, label: str) -> dict:
        self.log.info("[DRY-RUN] %s would send: %s", label, json.dumps(order_json))
        order = self._synthetic_order(order_json, underlying, time_in_force, f"DRY-{uuid.uuid4()}", "dry_run")
        self._dry_orders[order["id"]] = order
        return order

    @staticmethod
    def _order_id_from_response(resp: httpx.Response) -> str | None:
        location = resp.headers.get("Location", "")
        m = re.search(r"/orders/(\d+)", location)
        return m.group(1) if m else None

    def _submit(self, order_json: dict, underlying: str, time_in_force: str, label: str) -> dict:
        if self.dry_run:
            return self._dry_order(order_json, underlying, time_in_force, label)
        client = self._get_client()
        account_hash = self._hash()                      # refuses (BrokerError) unless a selected, still-linked account: nothing is sent then
        self.log.warning("[LIVE] %s sending: %s", label, json.dumps(order_json))
        try:
            # never blind-retry an order: a timeout or 5xx can follow an order Schwab DID accept, and a second POST would open a second position
            resp = self._request(client.place_order, account_hash, order_json, attempts=1)
        except SchwabAuthError:
            raise
        except BrokerError as exc:
            try:
                found = self._matching_working_order(order_json)
            except BrokerError as exc2:
                raise BrokerError(f"{label} may or may not have reached Schwab and was NOT re-sent ({exc}); looking for the resting order failed "
                                  f"too ({exc2}): check the account before restarting") from exc
            if found is None:
                raise BrokerError(f"{label} was NOT re-sent (a second POST could open a duplicate): {exc}") from exc
            self.log.warning("%s: no reply, but Schwab holds the order: using %s", label, found["id"])
            return found
        order_id = self._order_id_from_response(resp)
        if order_id is None:
            raise BrokerError(f"Schwab accepted {label} (HTTP {resp.status_code}) but returned no order id")
        try:
            order = self.get_order(order_id)
        except BrokerError as exc:
            self.log.warning("placed %s but could not fetch order %s: %s", label, order_id, exc)
            order = None
        if order is None:
            order = self._synthetic_order(order_json, underlying, time_in_force, order_id, "accepted")
        return order

    def _matching_working_order(self, order_json: dict) -> dict | None:
        """A resting order with exactly the legs, instructions, quantity and price of order_json (after place_order failed ambiguously), else None."""
        wanted = sorted((leg["instruction"].lower(), to_occ(leg["instrument"]["symbol"])) for leg in order_json["orderLegCollection"])
        qty = int(order_json["orderLegCollection"][0]["quantity"])
        price = float(order_json["price"])
        for order in self.get_open_orders():
            got = sorted((str(leg.get("position_intent", "")).lower(), leg["symbol"]) for leg in order.get("legs") or [])
            if got == wanted and int(order.get("qty") or 0) == qty and abs(float(order.get("limit_price") or 0.0) - price) < 0.005:
                return order
        return None

    def _spread_legs(self, underlying: str, expiration: date, right: str, short_strike: float,
                     long_strike: float, root: str | None) -> tuple[str, str, str]:
        r = (root or _strip_index(underlying) or underlying).upper()
        short_sym = occ_symbol(r, expiration, right, short_strike)
        long_sym = occ_symbol(r, expiration, right, long_strike)
        return _strip_index(underlying.upper()) or underlying.upper(), short_sym, long_sym

    def place_credit_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                            long_strike: float, qty: int, limit_credit: float, time_in_force: str = "gtc",
                            root: str | None = None, client_tag: str | None = None) -> dict:
        und, short_sym, long_sym = self._spread_legs(underlying, expiration, right, short_strike, long_strike, root)
        self._assert_selection_unchanged()
        cap = self._contract_cap()
        if cap is not None and qty > cap:                # the last line of defence behind the strategies' own sizing; closes are never clamped
            self.log.warning("LIVE CAP: %s %s/%s asked for %d contracts: opening %d (LIVE_MAX_CONTRACTS_PER_TRADE)",
                             client_tag or und, short_sym, long_sym, qty, cap)
            qty = cap
        order_json = self._vertical_json("NET_CREDIT", limit_credit, time_in_force, qty,
                                         [("SELL_TO_OPEN", short_sym), ("BUY_TO_OPEN", long_sym)])
        label = f"place_credit_spread({client_tag or und} {short_sym}/{long_sym} x{qty} @ {limit_credit:.2f})"
        return self._submit(order_json, und, time_in_force, label)

    def place_close_spread(self, underlying: str, expiration: date, right: str, short_strike: float,
                           long_strike: float, qty: int, limit_debit: float, time_in_force: str = "gtc",
                           root: str | None = None, client_tag: str | None = None) -> dict:
        und, short_sym, long_sym = self._spread_legs(underlying, expiration, right, short_strike, long_strike, root)
        order_json = self._vertical_json("NET_DEBIT", limit_debit, time_in_force, qty,
                                         [("BUY_TO_CLOSE", short_sym), ("SELL_TO_CLOSE", long_sym)])
        label = f"place_close_spread({client_tag or und} {short_sym}/{long_sym} x{qty} @ {limit_debit:.2f})"
        return self._submit(order_json, und, time_in_force, label)

    def _spread_close_ask(self, underlying: str, expiration: date, right: str, short_strike: float,
                          long_strike: float, root: str | None) -> float:
        lo, hi = sorted((short_strike, long_strike))
        chain = self.get_option_chain(underlying, expiration, right, strike_min=lo - 1e-6, strike_max=hi + 1e-6)
        r = (root or _strip_index(underlying) or underlying).upper()
        by_strike = {(q["strike"], q["root"]): q for q in chain}
        short_q = by_strike.get((short_strike, r)) or next((q for q in chain if q["strike"] == short_strike), None)
        long_q = by_strike.get((long_strike, r)) or next((q for q in chain if q["strike"] == long_strike), None)
        if short_q is None or long_q is None:
            raise BrokerError(f"could not quote spread {short_strike}/{long_strike} {right} {expiration}")
        return max(short_q["ask"] - long_q["bid"], 0.01)

    def close_spread_at_market(self, underlying: str, expiration: date, right: str, short_strike: float,
                               long_strike: float, qty: int, time_in_force: str = "gtc",
                               root: str | None = None, client_tag: str | None = None) -> dict:
        price = round(self._spread_close_ask(underlying, expiration, right, short_strike, long_strike, root)
                      + self.close_slippage, 2)
        tag = client_tag or "close_at_market"
        order = self.place_close_spread(underlying, expiration, right, short_strike, long_strike, qty, price,
                                        time_in_force=time_in_force, root=root, client_tag=tag)
        if self.dry_run:
            return order
        for attempt in range(self.close_max_retries + 1):
            order = self.wait_for_fill(order["id"], self.close_retry_sec, poll_sec=min(2.0, self.close_retry_sec))
            if order["status"] == "filled":
                return order
            if order["status"] in TERMINAL_STATUSES:
                raise BrokerError(f"close order {order['id']} ended {order['status']} without fill")
            if attempt >= self.close_max_retries:
                break
            self.cancel_order(order["id"])
            latest = self.wait_for_fill(order["id"], 5.0, poll_sec=1.0)
            if latest["status"] == "filled":
                return latest
            remaining = qty - latest["filled_qty"]
            if remaining <= 0:
                return latest
            price = round(price + self.close_slippage, 2)
            self.log.warning("close_spread_at_market retry %d/%d at %.2f", attempt + 1,
                             self.close_max_retries, price)
            order = self.place_close_spread(underlying, expiration, right, short_strike, long_strike,
                                            remaining, price, time_in_force=time_in_force, root=root,
                                            client_tag=tag)
        raise BrokerError(f"close_spread_at_market exhausted {self.close_max_retries} retries; "
                          f"last order {order['id']} status {order['status']}")

    def replace_order_price(self, order_id: str, new_limit: float) -> dict:
        order_id = str(order_id)
        if self.dry_run or order_id.startswith("DRY-"):
            existing = self._dry_orders.get(order_id)
            if existing is not None:
                new_json = dict(existing["raw"], price=f"{new_limit:.2f}")
                existing["status"] = "replaced"
                return self._dry_order(new_json, existing["symbol"], existing["time_in_force"],
                                       f"replace_order_price({order_id} -> {new_limit:.2f})")
            return self._dry_order({"replaceOrderId": order_id, "price": f"{new_limit:.2f}", "orderLegCollection": []},
                                   "", "gtc", f"replace_order_price({order_id} -> {new_limit:.2f})")
        client = self._get_client()
        raw = self._json(self._request(client.get_order, order_id, self._hash()))
        new_json = {
            "orderType": raw.get("orderType"),
            "session": raw.get("session", "NORMAL"),
            "price": f"{new_limit:.2f}",
            "duration": raw.get("duration", "GOOD_TILL_CANCEL"),
            "orderStrategyType": raw.get("orderStrategyType", "SINGLE"),
            "orderLegCollection": [
                {"instruction": leg["instruction"], "quantity": leg["quantity"],
                 "instrument": {"symbol": leg["instrument"]["symbol"], "assetType": leg["instrument"]["assetType"]}}
                for leg in raw.get("orderLegCollection", [])
            ],
        }
        if raw.get("complexOrderStrategyType"):
            new_json["complexOrderStrategyType"] = raw["complexOrderStrategyType"]
        self.log.warning("[LIVE] replace_order %s sending: %s", order_id, json.dumps(new_json))
        resp = self._request(client.replace_order, self._hash(), order_id, new_json)
        new_id = self._order_id_from_response(resp) or order_id
        order = self.get_order(new_id)
        if order is None:
            raise BrokerError(f"replaced order {order_id} but new order {new_id} not found")
        return order

    def cancel_order(self, order_id: str) -> bool:
        order_id = str(order_id)
        if order_id.startswith("DRY-"):
            existing = self._dry_orders.get(order_id)
            if existing is not None:
                existing["status"] = "canceled"
            self.log.info("[DRY-RUN] cancel_order(%s)", order_id)
            return existing is not None
        if self.dry_run:
            self.log.info("[DRY-RUN] would cancel Schwab order %s; nothing sent", order_id)
            return True
        client = self._get_client()
        resp = self._request(client.cancel_order, order_id, self._hash(), allow=(404,))
        return resp.status_code != 404

    def cancel_all_orders(self) -> int:
        count = 0
        for order in self.get_open_orders():
            if self.cancel_order(order["id"]):
                count += 1
        if self.dry_run:
            for order in self._dry_orders.values():
                if order["status"] == "dry_run":
                    order["status"] = "canceled"
                    count += 1
        return count

    # ------------------------------------------------------------------ market data

    def get_spot(self, symbol: str) -> float:
        client = self._get_client()
        sym = self._quote_symbol(symbol)
        data = self._json(self._request(client.get_quotes, [sym]))
        entry = None
        if isinstance(data, dict) and data:
            entry = data.get(sym) or next(iter(data.values()))
        if not entry:
            raise BrokerError(f"no quote returned for {symbol!r}")
        q = entry.get("quote") or {}
        for key in ("lastPrice", "mark", "closePrice"):
            v = _f(q.get(key))
            if v:
                return v
        bid, ask = _f(q.get("bidPrice")), _f(q.get("askPrice"))
        if bid and ask:
            return (bid + ask) / 2.0
        raise BrokerError(f"quote for {symbol!r} has no usable price: {q}")

    def _index_data_fallback(self):
        """Alpaca indicative option data for indexes Schwab returns no chain for (e.g. $SPX)."""
        if self._fallback is not None:
            return self._fallback or None
        key = self._cfg("ALPACA_API_KEY", None) or os.getenv("ALPACA_API_KEY")
        secret = self._cfg("ALPACA_SECRET_KEY", None) or os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            self._fallback = False
            return None
        try:
            from .alpaca_data import AlpacaOptionData
        except ImportError:
            from alpaca_data import AlpacaOptionData  # type: ignore
        self._fallback = AlpacaOptionData(
            key, secret,
            trading_url=self._cfg("ALPACA_BASE_URL", None) or "https://paper-api.alpaca.markets",
            data_url=self._cfg("ALPACA_DATA_URL", None) or "https://data.alpaca.markets",
            risk_free_rate=self.risk_free_rate, log=self.log.info)
        return self._fallback

    def _fallback_or_raise(self, underlying: str, what: str, detail: str):
        fb = self._index_data_fallback()
        if fb is None:
            raise BrokerError(
                f"Schwab returned no {what} for {underlying} ({detail}). Schwab does not serve option "
                f"chains for this index on this account; set ALPACA_API_KEY/ALPACA_SECRET_KEY in .env to "
                f"use Alpaca's indicative quotes as the data source.")
        if not self._fallback_warned:
            self.log.warning("Schwab returned no %s for %s (%s); using Alpaca indicative option data instead.",
                             what, underlying, detail)
            self._fallback_warned = True
        return fb

    def get_expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]:
        client = self._get_client()
        sym = self._quote_symbol(underlying)
        is_index = sym.startswith("$")
        today = datetime.now(ET).date()
        found: set[date] = set()
        if hasattr(client, "get_option_expiration_chain"):
            resp = self._request(client.get_option_expiration_chain, sym, allow=(400, 404))
            if not resp.is_error:
                for item in (self._json(resp) or {}).get("expirationList") or []:
                    try:
                        found.add(date.fromisoformat(str(item.get("expirationDate"))[:10]))
                    except ValueError:
                        continue
        if not found:
            resp = self._request(client.get_option_chain, sym,
                                 contract_type=SchwabClient.Options.ContractType.ALL,
                                 strike_count=1, strategy=SchwabClient.Options.Strategy.SINGLE,
                                 from_date=today + timedelta(days=min_dte),
                                 to_date=today + timedelta(days=max_dte),
                                 allow=(400, 404) if is_index else ())
            data = {} if resp.is_error else (self._json(resp) or {})
            for key in ("putExpDateMap", "callExpDateMap"):
                for exp_key in (data.get(key) or {}):
                    try:
                        found.add(date.fromisoformat(exp_key.split(":")[0]))
                    except ValueError:
                        continue
        if not found and is_index:
            fb = self._fallback_or_raise(underlying, "expirations", f"HTTP {resp.status_code}")
            found = set(fb.expirations(_strip_index(sym) or underlying, min_dte, max_dte))
        return sorted(d for d in found if min_dte <= (d - today).days <= max_dte)

    def _normalize_chain_row(self, row: dict, right: str, spot: float | None) -> dict | None:
        bid, ask = _f(row.get("bid"), 0.0), _f(row.get("ask"), 0.0)
        if bid == 0 and ask == 0:
            return None
        try:
            p = parse_occ(str(row.get("symbol", "")))
        except BrokerError:
            return None
        mid = round((bid + ask) / 2.0, 4)
        delta = _greek(row.get("delta"))
        iv = _greek(row.get("volatility"))
        iv = round(iv / 100.0, 6) if iv is not None and iv > 3.0 else iv
        if delta is None and spot:
            delta, iv_calc = options_math.delta_from_quote(mid, spot, p["strike"], p["expiration"], right,
                                                           r=self.risk_free_rate)
            iv = iv if iv is not None else iv_calc
        return {
            "symbol": occ_symbol(p["root"], p["expiration"], p["right"], p["strike"]),
            "underlying": None,
            "root": p["root"],
            "expiration": p["expiration"],
            "strike": _f(row.get("strikePrice"), p["strike"]),
            "right": p["right"],
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": _f(row.get("last")),
            "delta": delta,
            "iv": iv,
            "quote_time": _to_et(row.get("quoteTimeInLong")),
        }

    def get_option_chain(self, underlying: str, expiration: date, right: str,
                         strike_min: float | None = None, strike_max: float | None = None,
                         spot: float | None = None) -> list[dict]:
        client = self._get_client()
        right = right.upper()[0]
        if right not in ("P", "C"):
            raise BrokerError(f"right must be 'P' or 'C', got {right!r}")
        sym = self._quote_symbol(underlying)
        is_index = sym.startswith("$")
        contract = SchwabClient.Options.ContractType.PUT if right == "P" else SchwabClient.Options.ContractType.CALL
        resp = self._request(client.get_option_chain, sym, contract_type=contract,
                             strategy=SchwabClient.Options.Strategy.SINGLE,
                             from_date=expiration, to_date=expiration, include_underlying_quote=True,
                             allow=(400, 404) if is_index else ())
        if resp.is_error:
            fb = self._fallback_or_raise(underlying, "option chain", f"HTTP {resp.status_code}")
            return fb.chain(_strip_index(sym) or underlying, expiration, right, strike_min, strike_max, spot)
        data = self._json(resp) or {}
        if str(data.get("status", "SUCCESS")).upper() not in ("SUCCESS", ""):
            raise BrokerError(f"Schwab chain status {data.get('status')} for {underlying} {expiration}")
        spot = spot or _f(data.get("underlyingPrice")) or _f((data.get("underlying") or {}).get("last"))
        exp_map = data.get("putExpDateMap" if right == "P" else "callExpDateMap") or {}
        und_name = _strip_index(underlying.upper()) or underlying.upper()
        rows: list[dict] = []
        for exp_key, strikes in exp_map.items():
            if exp_key.split(":")[0] != expiration.isoformat():
                continue
            for contracts in strikes.values():
                for raw in contracts:
                    q = self._normalize_chain_row(raw, right, spot)
                    if q is None:
                        continue
                    if strike_min is not None and q["strike"] < strike_min:
                        continue
                    if strike_max is not None and q["strike"] > strike_max:
                        continue
                    q["underlying"] = und_name
                    rows.append(q)
        rows.sort(key=lambda q: (q["strike"], q["root"]))
        return rows


class Broker(SchwabBroker):
    """Factory the strategies construct: `Broker(config, dry_run=..., log=...)`.

    Returns a PaperBroker (wrapping a dry-run SchwabBroker for data) when
    `config.SCHWAB_MODE == "sim"` and dry_run was not requested; otherwise a
    SchwabBroker whose dry-run state follows the mode (see module docstring).
    """

    def __new__(cls, config_module, dry_run: bool | None = None, log=None) -> "SchwabBroker | PaperBroker":
        mode = str(getattr(config_module, "SCHWAB_MODE", "live")).strip().lower()
        if mode == "sim" and not dry_run:
            try:
                from .paper_sim import PaperBroker
            except ImportError:
                from paper_sim import PaperBroker  # type: ignore
            data = SchwabBroker(config_module, dry_run=True, log=log)
            return PaperBroker(data, config_module, log=log)
        return super().__new__(cls)

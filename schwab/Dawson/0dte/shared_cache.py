"""A snapshot cache shared by every bot process in this folder (the Alpaca personas, one process each).

Why: each persona used to download its own copy of the same market data on its own schedule (20 s cache phases, hourly SPX/SPY
ratio calibrations, a basis latched at whichever minute that process happened to ask), so identically configured personas
saw slightly different candles and fired on different candles (10/5 ORB 10:05: 4 of 5 personas fired, one did not). Now ONE
process fetches a dataset, writes it as a snapshot file, and every other process reads that same file until it is stale, so
all personas decide on the same bytes, and Yahoo is asked once per refresh instead of once per persona.

How: shared_get(name, fresh, fetch)
  * a snapshot file shared_data/<name>.pkl holds {"fetched_at": epoch seconds, "df": DataFrame};
  * if the file is `fresh` (the caller's rule) it is used as it is;
  * otherwise the first process to create <name>.lock (O_CREAT|O_EXCL) fetches and replaces the file atomically; the others
    wait up to SHARED_WAIT_SEC for the new file, then fall back to the stale snapshot (or, with none, a private fetch);
  * a lock older than SHARED_LOCK_STALE_SEC (its owner died) is broken;
  * with SHARED_DATA_ENABLED false (tests, a single process) there is no disk at all: fetch() is called directly.
Nothing here ever raises into the caller: every disk problem degrades to "fetch it yourself".
"""

from __future__ import annotations

import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any, Callable

import config

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent


def _directory() -> Path:
    raw = Path(getattr(config, "SHARED_DATA_DIR", "shared_data"))
    return raw if raw.is_absolute() else HERE / raw


def _enabled() -> bool:
    return bool(getattr(config, "SHARED_DATA_ENABLED", False))


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _read(path: Path) -> dict | None:
    for attempt in range(4):                      # Windows: a file being replaced can be momentarily unreadable
        try:
            with open(path, "rb") as fh:
                payload = pickle.load(fh)
            if isinstance(payload, dict) and "fetched_at" in payload and "df" in payload:
                return payload
            return None
        except FileNotFoundError:
            return None
        except (PermissionError, OSError):
            time.sleep(0.05)
        except Exception as exc:                 # noqa: BLE001 -- a corrupt/half-written file is simply refetched
            log.warning("shared snapshot %s unreadable (%s): refetching", path.name, exc)
            return None
    return None


def _write(path: Path, payload: dict) -> bool:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:               # a reader holds it open: try again in a moment
                time.sleep(0.05)
        return False
    except Exception as exc:                      # noqa: BLE001
        log.warning("could not write shared snapshot %s: %s", path.name, exc)
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _try_lock(lock: Path) -> bool:
    stale = float(getattr(config, "SHARED_LOCK_STALE_SEC", 30))
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink()                 # its owner died mid-fetch
                    continue
            except OSError:
                pass
            return False
        except OSError:
            return False
    return False


def _unlock(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def shared_get(name: str, fresh: Callable[[dict], bool], fetch: Callable[[], Any]) -> dict | None:
    """The shared snapshot called `name` ({"fetched_at", "df"}), refreshed with fetch() when `fresh(snapshot)` is false.
    fetch() returns a DataFrame, or None / an empty frame when the feed is down (nothing is shared then, so the next caller
    retries). Returns None when there is neither a usable snapshot nor a fetch result."""
    def fetched() -> dict | None:
        df = fetch()
        if df is None or getattr(df, "empty", True):
            return None
        return {"fetched_at": time.time(), "df": df}

    if not _enabled():
        return fetched()
    directory = _directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return fetched()
    path = directory / f"{_safe(name)}.pkl"
    lock = directory / f"{_safe(name)}.lock"
    snap = _read(path)
    if snap is not None and fresh(snap):
        return snap
    wait = float(getattr(config, "SHARED_WAIT_SEC", 6))
    deadline = time.monotonic() + wait
    while True:
        if _try_lock(lock):
            try:
                snap2 = _read(path)               # someone may have refreshed it while we waited for the lock
                if snap2 is not None and fresh(snap2):
                    return snap2
                new = fetched()
                if new is None:
                    return snap2 or snap          # feed down: serve whatever we had, share nothing
                _write(path, new)
                return new
            finally:
                _unlock(lock)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
        snap2 = _read(path)
        if snap2 is not None and fresh(snap2):
            return snap2
    # the owner of the lock is slow or dead: use the stale snapshot, or fetch privately (nothing written)
    return snap if snap is not None else fetched()

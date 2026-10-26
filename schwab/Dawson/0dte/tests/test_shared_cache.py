"""shared_cache.shared_get: one process fetches, every process reads the same snapshot (2026-10-08)."""

import os
import pickle
import threading
import time

import pandas as pd
import pytest

import config
import shared_cache


def frame(value=1.0):
    return pd.DataFrame({"close": [value]}, index=pd.DatetimeIndex([pd.Timestamp("2026-10-08 10:00", tz="US/Eastern")]))


@pytest.fixture(autouse=True)
def shared_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DATA_ENABLED", True)
    monkeypatch.setattr(config, "SHARED_DATA_DIR", str(tmp_path / "shared"))
    monkeypatch.setattr(config, "SHARED_WAIT_SEC", 0.5)
    monkeypatch.setattr(config, "SHARED_LOCK_STALE_SEC", 30)
    return tmp_path / "shared"


def always_fresh(snap):
    return True


def never_fresh(snap):
    return False


def test_the_first_caller_fetches_and_the_second_reads_the_same_snapshot(shared_dir):
    calls = []

    def fetch():
        calls.append(1)
        return frame(7.0)
    first = shared_cache.shared_get("SPX 1m", never_fresh if not calls else always_fresh, fetch)
    second = shared_cache.shared_get("SPX 1m", always_fresh, fetch)
    assert len(calls) == 1 and second["fetched_at"] == first["fetched_at"]
    assert float(second["df"]["close"].iloc[0]) == 7.0 and (shared_dir / "SPX_1m.pkl").exists()


def test_a_stale_snapshot_is_refetched_and_replaced(shared_dir):
    shared_cache.shared_get("a", never_fresh, lambda: frame(1.0))
    snap = shared_cache.shared_get("a", never_fresh, lambda: frame(2.0))
    assert float(snap["df"]["close"].iloc[0]) == 2.0
    assert float(shared_cache.shared_get("a", always_fresh, lambda: frame(3.0))["df"]["close"].iloc[0]) == 2.0
    assert not list(shared_dir.glob("*.tmp")) and not list(shared_dir.glob("*.lock"))      # nothing left behind


def test_a_dead_feed_shares_nothing_and_serves_the_previous_snapshot(shared_dir):
    good = shared_cache.shared_get("a", never_fresh, lambda: frame(1.0))
    out = shared_cache.shared_get("a", never_fresh, lambda: None)
    assert out["fetched_at"] == good["fetched_at"]                               # the last good one, not an empty frame
    assert shared_cache.shared_get("never-fetched", never_fresh, lambda: pd.DataFrame()) is None
    assert not (shared_dir / "never-fetched.pkl").exists()


def test_a_corrupt_snapshot_is_simply_refetched(shared_dir):
    shared_dir.mkdir(parents=True)
    (shared_dir / "a.pkl").write_bytes(b"not a pickle")
    snap = shared_cache.shared_get("a", always_fresh, lambda: frame(5.0))
    assert float(snap["df"]["close"].iloc[0]) == 5.0
    (shared_dir / "a.pkl").write_bytes(pickle.dumps({"something": "else"}))
    assert float(shared_cache.shared_get("a", always_fresh, lambda: frame(6.0))["df"]["close"].iloc[0]) == 6.0


def test_a_lock_left_by_a_dead_process_is_broken(shared_dir):
    shared_dir.mkdir(parents=True)
    lock = shared_dir / "a.lock"
    lock.write_text("999999")
    old = time.time() - 120
    os.utime(lock, (old, old))
    snap = shared_cache.shared_get("a", never_fresh, lambda: frame(4.0))
    assert float(snap["df"]["close"].iloc[0]) == 4.0 and not lock.exists()


def test_a_busy_lock_makes_a_caller_wait_then_use_the_stale_snapshot(shared_dir):
    stale = shared_cache.shared_get("a", never_fresh, lambda: frame(1.0))
    (shared_dir / "a.lock").write_text("123")                                    # another process is fetching right now
    started = time.monotonic()
    out = shared_cache.shared_get("a", never_fresh, lambda: frame(9.0))
    assert out["fetched_at"] == stale["fetched_at"] and 0.4 <= time.monotonic() - started < 3.0


def test_a_busy_lock_with_no_snapshot_falls_back_to_a_private_fetch(shared_dir):
    shared_dir.mkdir(parents=True)
    (shared_dir / "b.lock").write_text("123")
    out = shared_cache.shared_get("b", never_fresh, lambda: frame(8.0))
    assert float(out["df"]["close"].iloc[0]) == 8.0 and not (shared_dir / "b.pkl").exists()   # nothing written without the lock


def test_a_caller_waiting_for_the_lock_picks_up_the_new_snapshot(shared_dir, monkeypatch):
    monkeypatch.setattr(config, "SHARED_WAIT_SEC", 10)       # generous: a loaded machine must not let the wait run out before the write
    shared_dir.mkdir(parents=True)
    (shared_dir / "a.lock").write_text("123")
    results = []

    def waiter():
        results.append(shared_cache.shared_get("a", lambda s: s["df"]["close"].iloc[0] == 2.0, lambda: frame(99.0)))
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)
    shared_cache._write(shared_dir / "a.pkl", {"fetched_at": time.time(), "df": frame(2.0)})   # the owner finishes
    (shared_dir / "a.lock").unlink()
    t.join(5)
    assert results and float(results[0]["df"]["close"].iloc[0]) == 2.0


def test_concurrent_callers_cause_exactly_one_fetch(shared_dir, monkeypatch):
    monkeypatch.setattr(config, "SHARED_WAIT_SEC", 5)
    calls, gate = [], threading.Barrier(6)

    def slow_fetch():
        calls.append(1)
        time.sleep(0.4)
        return frame(3.0)
    out = []

    def worker():
        gate.wait()
        out.append(shared_cache.shared_get("race", lambda s: True if (shared_dir / "race.pkl").exists() and s else False, slow_fetch))
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(calls) == 1 and len(out) == 6 and {o["fetched_at"] for o in out} == {out[0]["fetched_at"]}


def test_disabled_means_a_plain_fetch_and_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DATA_ENABLED", False)
    monkeypatch.setattr(config, "SHARED_DATA_DIR", str(tmp_path / "nope"))
    calls = []
    snap = shared_cache.shared_get("a", always_fresh, lambda: calls.append(1) or frame(1.0))
    assert len(calls) == 1 and float(snap["df"]["close"].iloc[0]) == 1.0 and not (tmp_path / "nope").exists()


def test_an_unwritable_directory_degrades_to_a_private_fetch(tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setattr(config, "SHARED_DATA_DIR", str(blocker / "sub"))
    assert float(shared_cache.shared_get("a", never_fresh, lambda: frame(1.5))["df"]["close"].iloc[0]) == 1.5


def test_a_snapshot_refreshed_while_we_waited_for_the_lock_is_used_not_refetched(shared_dir, monkeypatch):
    shared_cache.shared_get("a", never_fresh, lambda: frame(1.0))                      # a stale snapshot on disk
    real_try_lock = shared_cache._try_lock

    def another_process_refreshes_first(lock):
        shared_cache._write(shared_dir / "a.pkl", {"fetched_at": time.time(), "df": frame(2.0)})
        return real_try_lock(lock)
    monkeypatch.setattr(shared_cache, "_try_lock", another_process_refreshes_first)
    calls = []
    out = shared_cache.shared_get("a", lambda s: float(s["df"]["close"].iloc[0]) == 2.0, lambda: calls.append(1) or frame(99.0))
    assert not calls and float(out["df"]["close"].iloc[0]) == 2.0                      # no second Yahoo request

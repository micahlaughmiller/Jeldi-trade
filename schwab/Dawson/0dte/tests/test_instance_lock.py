"""One process per persona: the lock file keeps a second copy off the same account (2026-10-08)."""

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

import instance_lock

HERE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    while instance_lock._HELD:
        instance_lock._HELD.pop().close()


def _hold_in_a_child(directory, name):
    """Start a process that takes the lock and waits; returns (child, its real pid). A venv python.exe is a launcher, so the
    child reports its own pid."""
    code = ("import os, sys; sys.path.insert(0, %r); import instance_lock; "
            "ok, _ = instance_lock.acquire(%r, %r); print('held' if ok else 'failed', os.getpid(), flush=True); sys.stdin.readline()"
            % (str(HERE), str(directory), name))
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(HERE))
    state, pid = child.stdout.readline().split()
    assert state == "held"
    return child, pid


def _release_child(child):
    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(timeout=20)


def test_the_first_copy_gets_the_lock_and_a_second_in_the_same_process_does_not(tmp_path):
    assert instance_lock.acquire(tmp_path, "0dte_astra") == (True, "")
    ok, holder = instance_lock.acquire(tmp_path, "0dte_astra")
    assert ok is False and holder == f"pid {os.getpid()}"


def test_different_personas_do_not_block_each_other(tmp_path):
    assert instance_lock.acquire(tmp_path, "0dte_astra")[0] is True
    assert instance_lock.acquire(tmp_path, "0dte_aria")[0] is True


def test_the_lock_dies_with_its_process_and_names_its_holder_while_alive(tmp_path):
    child, pid = _hold_in_a_child(tmp_path, "x")
    try:
        ok, holder = instance_lock.acquire(tmp_path, "x")
        assert ok is False and holder == f"pid {pid}"                  # another process holds it, and says who
    finally:
        _release_child(child)
    assert instance_lock.acquire(tmp_path, "x") == (True, "")          # the holder is gone: free again, no cleanup needed


def test_a_stale_lock_file_left_behind_neither_blocks_nor_garbles_the_holder_note(tmp_path):
    (tmp_path / "x.lock").write_bytes(b"L99999-leftover-bytes-from-a-crashed-copy")
    child, pid = _hold_in_a_child(tmp_path, "x")
    try:
        assert (tmp_path / "x.lock").read_bytes() == pid.encode()      # the leftover was replaced, not appended to
        assert instance_lock.acquire(tmp_path, "x") == (False, f"pid {pid}")
    finally:
        _release_child(child)


def test_the_lock_file_stays_readable_by_everyone_while_it_is_held(tmp_path):
    child, pid = _hold_in_a_child(tmp_path, "x")
    try:
        assert (tmp_path / "x.lock").read_bytes() == pid.encode()      # a backup, a zip or an antivirus scan can read it
    finally:
        _release_child(child)


def _break_locking(monkeypatch, error):
    def boom(*args, **kwargs):
        raise error
    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", boom)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", boom)


def test_a_filesystem_that_cannot_lock_runs_the_bot_unguarded_and_says_so(tmp_path, monkeypatch):
    _break_locking(monkeypatch, OSError(errno.EINVAL, "locking not supported here"))
    ok, note = instance_lock.acquire(tmp_path, "0dte_astra")
    assert ok is True and "not enforced" in note and "locking not supported" in note


def test_only_real_contention_refuses_the_start(tmp_path, monkeypatch):
    for error in (PermissionError(errno.EACCES, "held"), BlockingIOError(errno.EAGAIN, "held"), OSError(errno.EDEADLK, "held")):
        with monkeypatch.context() as m:
            _break_locking(m, error)
            assert instance_lock.acquire(tmp_path, "0dte_astra")[0] is False


def test_failing_to_write_the_pid_note_keeps_the_lock(tmp_path, monkeypatch):
    def full_disk():
        raise OSError(errno.ENOSPC, "disk full")
    with monkeypatch.context() as m:
        m.setattr(instance_lock.os, "getpid", full_disk)
        assert instance_lock.acquire(tmp_path, "0dte_astra") == (True, "")
    assert instance_lock.acquire(tmp_path, "0dte_astra")[0] is False   # still held, only the note is missing


def test_an_unusable_lock_directory_never_keeps_the_bot_from_trading(tmp_path):
    blocker = tmp_path / "file-not-a-dir"
    blocker.write_text("x")
    ok, note = instance_lock.acquire(blocker / "logs", "0dte_astra")
    assert ok is True and "not enforced" in note

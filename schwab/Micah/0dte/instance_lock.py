"""One bot process per persona.

Two copies of the same persona (a double launch, a window left open) trade one account with independent in-memory counters:
the 2-open cap, the daily trade limits, the same-contract guard and the loss limit are each per process, so both would
enter and neither would know. acquire() takes an exclusive OS lock on logs/<name>.lock for the life of the process (the OS drops
it when the process dies, however it dies), and a second copy is refused with the first one's pid.

Only a genuine "somebody else holds it" refuses the start; any other problem with the lock (a filesystem without byte-range locks,
a full disk, a read-only folder) lets the bot run unguarded, and says so.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

_HELD: list = []        # open handles, kept alive for the life of the process
_LOCK_OFFSET = 1 << 30  # Windows: the locked byte lies far beyond the end of the file, so the pid note stays readable by anyone


def _contended(exc: OSError) -> bool:
    """True when the lock call failed because another process holds the lock (not because locking itself is unavailable)."""
    busy = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
    return isinstance(exc, (PermissionError, BlockingIOError)) or exc.errno in busy


def acquire(directory: Path, name: str) -> tuple[bool, str]:
    """(True, "") when this process now holds the lock for `name`; (False, "pid N") when another process already does;
    (True, note) when the lock could not be used at all and the bot runs without the guard."""
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = Path(directory) / f"{name}.lock"
        fh = open(path, "a+b")
    except OSError as exc:
        return True, f"lock unavailable ({exc}); not enforced"
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if _contended(exc):
            return False, _holder(path)
        return True, f"lock unavailable ({exc}); not enforced"
    try:
        fh.seek(0)
        fh.truncate(0)
        fh.write(str(os.getpid()).encode())
        fh.flush()
    except OSError:
        pass            # the lock is held; only the note of who holds it is missing
    _HELD.append(fh)
    return True, ""


def _holder(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            pid = fh.read(16).decode(errors="ignore").strip()
        return f"pid {pid}" if pid else "another process"
    except OSError:
        return "another process"

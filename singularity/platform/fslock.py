"""Directory locks: the engine's one `mkdir` idiom, and the Windows moment it
used to miss.

Every writer lock in the engine is a directory created with
`mkdir(exist_ok=False)`: creation is atomic, so exactly one caller wins, and
the others see `FileExistsError`. On Windows there is a second answer. A lock
directory its holder is removing stays "delete pending" while any handle to it
is open -- a waiter's `stat` of the lock is one -- and creating it again in that
window is refused with ERROR_ACCESS_DENIED, which Python raises as
`PermissionError`. The lock is contended, not forbidden: the holder was just
there. Measured on the GitHub Windows runners (build 26100), 10 threads marking
one ingest registry: 10-18 PermissionErrors per 3000 acquisitions, every one
from the lock's `mkdir`; a desktop Windows 11 (26200) never showed it, because
there the directory goes at once.

`make_lock_dir` answers the same question on every platform: True when this
call created the lock, False when someone else holds it or is releasing it.
A `PermissionError` that outlasts `PENDING_SECONDS` is a real one and is
raised: delete-pending ends when the last handle closes, milliseconds later.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from . import current_os

PENDING_SECONDS = 0.5          # longest a released lock stays delete-pending, generously
_POLL_SECONDS = 0.01


def make_lock_dir(lock: Path) -> bool:
    """Create `lock`; True when this call did, False when it is taken."""
    deadline = time.monotonic() + PENDING_SECONDS
    while True:
        try:
            lock.mkdir(exist_ok=False)
            return True
        except FileExistsError:
            return False
        except PermissionError:
            if current_os() != "windows" or time.monotonic() >= deadline:
                raise
            time.sleep(_POLL_SECONDS)


def break_stale_lock(lock: Path, max_age: float) -> bool:
    """Remove `lock` if it is older than `max_age` seconds; True when this call did.

    The naive `stat` then `rmdir` lets two waiters both judge one stale lock:
    the first removes it and a new holder takes the name, then the second
    removes that FRESH lock. So the break is serialized by a second lock dir,
    the age is re-checked inside it, and the lock is renamed aside (atomic)
    and verified to be the very directory that was judged stale (same inode and
    mtime) before it is deleted; a fresh one grabbed by mistake is put back."""
    breaker = lock.with_name(lock.name + ".break")
    if not make_lock_dir(breaker):
        try:                            # a breaker that died; it holds for milliseconds
            if time.time() - breaker.stat().st_mtime > max_age:
                breaker.rmdir()
        except OSError:
            pass
        return False
    try:
        try:
            seen = lock.stat()
        except OSError:
            return False
        if time.time() - seen.st_mtime <= max_age:
            return False
        grave = lock.with_name(f"{lock.name}.gone-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(lock, grave)
        except OSError:
            return False
        got = grave.stat()
        if (got.st_ino, got.st_mtime_ns) != (seen.st_ino, seen.st_mtime_ns):
            try:                        # took a lock somebody just made: give it back
                os.rename(grave, lock)
            except OSError:
                shutil.rmtree(grave, ignore_errors=True)
            return False
        shutil.rmtree(grave, ignore_errors=True)
        return True
    finally:
        try:
            breaker.rmdir()
        except OSError:
            pass

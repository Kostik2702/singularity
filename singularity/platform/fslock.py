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

import time
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

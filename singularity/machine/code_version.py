"""Fingerprint of the engine code currently on disk.

The daemon loads the embedding model once and stays up for days. That is the
whole point of it — and it means the process goes on serving the code it was
started with, while the files underneath it change. `mb daemon reload` does not
help: it re-reads the config and the indexes, never the modules.

This was not a theory. On 2026-08-05 the first run of `tools/search_eval.py`
reported a dead-flat zero delta between hybrid on and hybrid off, on every
class, to three decimals. Nothing was wrong with the fusion; the daemon predated
it. Had that run been recorded as the baseline, the file under `docs/` would
have said "hybrid does not help" — with numbers, reproducibly, and wrongly.

So the fingerprint is deliberately about staleness and nothing else: the newest
mtime among the package's own `.py` files. It does not identify a version, it
answers one question — is the process serving what is on disk right now.
"""
from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def code_fingerprint(root: Path | None = None) -> int:
    """Newest source mtime in the package, in whole nanoseconds; 0 if unreadable.

    Whole-number nanoseconds rather than a hash: hashing every module on each
    `mb status` would cost more than the check is worth, and an mtime answers
    the actual question. A filesystem with coarse timestamps can only make this
    miss a change made within the same tick, never invent one.
    """
    base = root or _PACKAGE_ROOT
    newest = 0
    try:
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    except OSError:
        return 0
    return newest


def is_stale(serving: int | None, current: int | None = None) -> bool:
    """True when the running process started before the newest source change.

    Unknown on either side is NOT stale: a daemon from before this check existed
    reports nothing, and answering "stale" for it would train the reader to
    ignore the warning — which is how a real staleness would then get missed.
    """
    if not serving:
        return False
    now = current if current is not None else code_fingerprint()
    if not now:
        return False
    return now > serving

"""`graph-last.json`: all a hook may know about the code graph (CODE-GRAPH D1).

The daemon never opens a graph, and neither does a hook: a read-only SQLite
open of a WAL database still creates its `-shm`/`-wal` files, and a hook's
contract is to read files and nothing else. So every writer of a graph -- the
end of a full build, the end of an in-place update that wrote -- leaves one
entry per unit in `<code_graph_dir>/graph-last.json`, and a hook reads only
that file (the pattern of `doctor-last.json`, with the same 48-hour rule for
calling it stale).

One file for every unit, rewritten whole (tmp + os.replace), so a reader sees
the old file or the new one and never half of either. Two writers -- two units
built at once, or a build racing an update of the same unit -- serialize on an
OS lock around the read-modify-write, so neither drops the other's entry. The
OS releases that lock when its holder dies, so a writer killed mid-write
leaves nothing to take over. And an entry is only ever replaced by a LATER
state of its unit (generation first, then the update sequence within it): an
update of the generation a build has just replaced finishes after the build
more often than not, and must not put the old generation back.

The reader half (`read`, `entry_for`) is stdlib-only and imports neither
sqlite3 nor the rest of the package: it runs inside SessionStart, whose budget
is 100 ms end to end (B8), and the hook-path probe in the tests pins that.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..log import log

SIDECAR_NAME = "graph-last.json"
_LOCK_NAME = "graph-last.lock"
# The lock guards a read and a write of a few kilobytes; a holder that keeps it
# this long is stuck, and the entry is better recorded by the unit's next write
# than waited for on the query path.
_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.005
# On Windows `os.replace` onto a file another process has open fails (WinError
# 5), and a hook reading this file holds it open for well under a millisecond.
_REPLACE_TRIES = 20
_REPLACE_PAUSE_SECONDS = 0.01


class SidecarBusy(RuntimeError):
    """Another writer held the sidecar lock for longer than _LOCK_WAIT_SECONDS."""


# --- the reader: what a hook runs ---------------------------------------------------

def path(graph_dir: Path) -> Path:
    return Path(graph_dir) / SIDECAR_NAME


def graph_dir(vault: Path) -> Path:
    """`Config(vault).code_graph_dir`, at a price SessionStart can pay.

    The machine id there comes from `platform.node()`, which on Windows builds
    the whole `platform.uname()` -- and `win32_ver` runs `cmd /c ver` in a
    subprocess for it: 12-18 ms measured, most of what this line added to
    SessionStart (p95 84 -> 105 ms against a budget of 100). The node name
    itself is `socket.gethostname()` there (CPython's `platform._node`), so the
    id comes out the same; a test pins this function to `Config`'s answer."""
    if os.name == "nt":
        import socket
        try:
            node = socket.gethostname()
        except OSError:
            node = ""
    else:
        import platform
        node = platform.node()         # os.uname() here: no subprocess
    machine = re.sub(r"[^A-Za-z0-9_.-]", "_", node or "unknown")[:64] or "unknown"
    return Path(vault) / ".mb" / "machines" / machine / "code_graph"


def read(graph_dir: Path) -> dict[str, dict[str, Any]]:
    """{unit key: entry}; {} for a missing, unreadable or malformed file. Never raises."""
    try:
        data = json.loads(path(graph_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, dict):
        return {}
    return {k: v for k, v in units.items() if isinstance(v, dict) and isinstance(v.get("real_root"), str)}


def norm(p: str | Path) -> str:
    """The one spelling a root and a directory are compared in -- the same as
    `query.locate` uses, so a hook and `mb code` pick the same unit."""
    return os.path.normcase(os.path.realpath(str(p)))


def entry_for(graph_dir: Path, where: str | Path) -> dict[str, Any] | None:
    """The entry of the deepest built unit whose root contains `where`, or None.

    Roots are stored already resolved (`real_root`), so a lookup resolves one
    path -- `where` -- however many units the machine has."""
    entries = read(graph_dir)
    if not entries:
        return None
    target = norm(where)
    best: dict[str, Any] | None = None
    for entry in entries.values():
        root = entry["real_root"]
        inside = target == root or target.startswith(root.rstrip(os.sep) + os.sep)
        if inside and (best is None or len(root) > len(best["real_root"])):
            best = entry
    return best


# --- the writers: the end of a build, the end of an update that wrote ---------------

def record(db: Path) -> bool:
    """Describe the graph in `db` -- as committed -- in the sidecar. True if
    the entry was written, False if it was not (a later state was already
    recorded, or recording failed and was logged).

    Never raises: the graph was written, and a sidecar that lags one write
    behind is corrected by the unit's next write; failing the build or the
    query over it would lose more than it protects.

    The directories come from the store's layout, `<code_graph_dir>/<unit
    dir>/<generation>/graph.db` (see store.unit_dir), so an in-place update --
    which knows only its database -- records itself the same way a build does."""
    db = Path(db)
    graph_dir = db.parent.parent.parent
    try:
        entry = _describe(db)
        with _locked(graph_dir):
            units = read(graph_dir)
            units = {k: v for k, v in units.items() if _still_built(graph_dir, v)}
            old = units.get(entry["key"])
            if old is not None and _order(old) > _order(entry):
                return False
            units[entry["key"]] = entry
            _write(graph_dir, units)
        return True
    except Exception as exc:  # noqa: BLE001 -- see the docstring: never fail the writer
        log.warning("[mb] code graph: %s not recorded for hooks: %s: %s",
                    db, type(exc).__name__, exc)
        return False


def _describe(db: Path) -> dict[str, Any]:
    """One entry, read in one read transaction so the counts and the meta
    describe the same commit."""
    import sqlite3

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0, isolation_level=None)
    try:
        con.execute("BEGIN")
        meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}
        files, nodes, edges, stale = (con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                      for t in ("files", "nodes", "edges", "stale"))
        con.execute("COMMIT")
    finally:
        con.close()
    root = str(meta["root"])
    return {"key": str(meta["unit"]), "root": root, "real_root": norm(root),
            "dir": db.parent.parent.name, "generation": db.parent.name,
            "built_at": meta.get("built_at"), "updated_at": meta.get("updated_at"),
            "seq": int(meta.get("seq") or 0), "files": files, "nodes": nodes, "edges": edges,
            "stale_count": stale, "stale_all": meta.get("stale_all"),
            "schema": meta.get("schema_version"), "recorded_at": time.time()}


def _order(entry: dict[str, Any]) -> tuple[int, int]:
    """Later states sort higher: the generation (`gen_<time_ns>_<pid>`, named
    by the store when it is written), then the update sequence inside it."""
    try:
        gen_ns = int(str(entry.get("generation", "")).split("_")[1])
    except (IndexError, ValueError):
        gen_ns = 0
    return gen_ns, int(entry.get("seq") or 0)


def _still_built(graph_dir: Path, entry: dict[str, Any]) -> bool:
    """A unit whose store was deleted (the rollback of D1 is deleting it) must
    not go on being announced to every session."""
    return (graph_dir / str(entry.get("dir", "")) / "CURRENT").is_file()


def _write(graph_dir: Path, units: dict[str, dict[str, Any]]) -> None:
    target = path(graph_dir)
    tmp = target.with_name(f"{SIDECAR_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"units": dict(sorted(units.items()))}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(_REPLACE_PAUSE_SECONDS)


@contextmanager
def _locked(graph_dir: Path) -> Iterator[None]:
    """Exclusive, cross-process, polled. A byte-range lock (Windows) or flock
    (POSIX) on a lock file that is never deleted: the OS drops it with the
    handle, so a dead holder needs no takeover rule -- the race every
    age-based takeover has between two waiters does not exist."""
    graph_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(graph_dir / _LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise SidecarBusy(f"{graph_dir / _LOCK_NAME} held for {_LOCK_WAIT_SECONDS:.0f}s")
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)

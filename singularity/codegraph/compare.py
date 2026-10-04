"""What "the same graph" means, and where two graphs differ (CODE-GRAPH P3, P6).

A graph maintained in place by updates must equal a full build of the same
tree outside the stale set the updates declared (UNMET j). File ids differ
between generations, so every row is compared by the PATH of its file. Not
compared: `meta` and the snapshot's bookkeeping (`dirty`, `stale`, the update
sequence), which legitimately differ, and the file communities, which only a
build computes (an update leaves them as they were and `arch` says how far
behind they are).

Two readers of that one definition (`rows`):
* `dump` / `diff` -- whole tables in memory: the equivalence oracle and the
  tests, whose graphs hold a few dozen files;
* `diff_graphs` -- one table at a time, each row reduced to a 16-byte digest,
  the examples fetched in a second streaming pass: the nightly comparison of a
  real unit. The 100k-file proxy holds ~1.3M nodes and ~1.5M edges, and both
  graphs' rows held as tuples at once would take gigabytes.
The oracle compares with `diff_graphs`, so its 0-divergence result vouches
for the comparator nightly relies on.

`rebuild_and_compare` is the nightly half: bring the incremental graph up to
date, snapshot it, rebuild the unit from scratch in a subprocess -- outside
the daemon and outside the nightly process, which never imports the parser --
and compare the snapshot with the rebuild.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import Config
from . import store

# Each table as the rows an answer or a later update reads: (sql, per file). A
# per-file table's first column is a file id, compared as its path, and the
# rows of a stale file are left out.
TABLES: dict[str, tuple[str, bool]] = {
    "files": ("SELECT path, lang, content_hash, parse_status, is_test, package FROM files", False),
    "nodes": ("SELECT file_id, qname, name, kind, parent, spans, signature, doc1, decorators,"
              " exported, local, bases FROM nodes", True),
    "name_refs": ("SELECT lang, name, files FROM name_refs", False),
    "edges": ("SELECT file_id, src, dst, kind, tier, callee, line FROM edges", True),
    "unresolved": ("SELECT file_id, src, name, receiver, line FROM unresolved", True),
    "deps": ("SELECT d.file_id, p.path FROM deps d JOIN dep_paths p ON p.id = d.path_id", True),
    "lookups": ("SELECT file_id, name FROM lookups", True),
}
EXAMPLES = 5


def stale_paths(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT path FROM stale")}


def rows(con: sqlite3.Connection, table: str, exclude: set[str]) -> Iterator[tuple[str, ...]]:
    """The rows of `table`, every value as text, file ids as paths; rows of a
    file in `exclude` are left out (the files table itself always counts)."""
    sql, per_file = TABLES[table]
    if not per_file:
        for r in con.execute(sql):
            yield tuple(map(str, r))
        return
    path = dict(con.execute("SELECT id, path FROM files").fetchall())
    for r in con.execute(sql):
        p = path[r[0]]
        if p not in exclude:
            yield tuple(map(str, (p, *r[1:])))


def dump(db: Path) -> dict[str, list[tuple[str, ...]]]:
    """Every compared table of one graph, sorted, outside the graph's own
    stale set."""
    con = store.connect_ro(db)
    try:
        own = stale_paths(con)
        return {t: sorted(rows(con, t, own)) for t in TABLES}
    finally:
        con.close()


def diff(a: dict[str, list], b: dict[str, list], examples: int = EXAMPLES) -> dict[str, dict]:
    """{table: {"update_only", "build_only", "counts"}} for every table that
    differs between two dumps; {} when they are equal."""
    out = {}
    for k in a:
        if a[k] != b[k]:
            sa, sb = set(a[k]), set(b[k])
            out[k] = {"update_only": sorted(sa - sb)[:examples], "build_only": sorted(sb - sa)[:examples],
                      "counts": [len(a[k]), len(b[k])]}
    return out


def _digest(row: tuple[str, ...]) -> bytes:
    """`repr` of a tuple of str is unambiguous, and 2-3x cheaper than JSON on
    the proxy's 5M rows per graph; both sides are digested by one process, so
    it need not be stable across Python versions."""
    return hashlib.blake2b(repr(row).encode(), digest_size=16).digest()


def _examples(found: Iterator[tuple[str, ...]], wanted: Counter[bytes], n: int) -> list[tuple[str, ...]]:
    """The first `n` rows, in sorted order, of the ones `wanted` counts -- a
    row one graph holds twice and the other once is named once, not twice."""
    left = Counter(wanted)

    def extra() -> Iterator[tuple[str, ...]]:
        for r in found:
            d = _digest(r)
            if left[d] > 0:
                left[d] -= 1
                yield r
    return heapq.nsmallest(n, extra())


def diff_graphs(a_db: Path, b_db: Path, examples: int = EXAMPLES) -> dict[str, dict]:
    """`diff` of two graphs on disk, a table at a time, as multisets: a row
    one graph holds twice and the other once is a difference. Rows of a file
    either graph declares stale are left out of both -- equality is claimed
    only outside the declared stale set. Beyond `diff`'s keys, `differing`
    counts the rows only `a` / only `b` holds."""
    con_a, con_b = store.connect_ro(a_db), store.connect_ro(b_db)
    try:
        exclude = stale_paths(con_a) | stale_paths(con_b)
        out = {}
        for t in TABLES:
            ca = Counter(_digest(r) for r in rows(con_a, t, exclude))
            cb = Counter(_digest(r) for r in rows(con_b, t, exclude))
            if ca == cb:
                continue
            only_a, only_b = ca - cb, cb - ca
            out[t] = {"update_only": _examples(rows(con_a, t, exclude), only_a, examples),
                      "build_only": _examples(rows(con_b, t, exclude), only_b, examples),
                      "counts": [sum(ca.values()), sum(cb.values())],
                      "differing": [sum(only_a.values()), sum(only_b.values())]}
        return out
    finally:
        con_a.close()
        con_b.close()


# --- nightly: the incremental graph against a full build ------------------------------

SNAPSHOT_NAME = "nightly-before.db"
SUBPROCESS_TIMEOUT_SECONDS = 3600      # B7 allows 10 minutes for 100k files; a hang is not a build
_TAIL = 800


def rebuild_and_compare(cfg: Config, card: dict[str, Any],
                        timeout: float = SUBPROCESS_TIMEOUT_SECONDS) -> dict[str, Any]:
    """One unit: `mb code update`, snapshot, `mb code build`, compare.

    The update first, because a graph behind its tree differs from a rebuild
    by every edit nobody queried since -- that difference is expected, and
    reporting it would bury the one that is not. What is left after it is a
    divergence of the incremental path (UNMET j): logged, and a WARN, never an
    error -- the rebuild has just replaced the graph that diverged.

    status: equal | differs | skipped | error."""
    from . import update as update_mod

    key, root = card["key"], Path(card["root"])
    if not root.is_dir():
        return {"status": "skipped", "reason": f"the unit's root is not reachable: {root}"}
    foreign = _foreign_package(root)
    if foreign:
        return {"status": "skipped", "reason": foreign}
    if update_mod._lock_alive(cfg, key):
        return {"status": "skipped", "reason": "a build or update of this unit is running"}
    started, started_ns = time.perf_counter(), time.time_ns()
    upd = _mb_code(cfg, root, "update", timeout)
    if "error" in upd:
        return {"status": "error", "stage": "update", **upd}
    db = store.current_db(cfg, key)
    if db is None:
        return {"status": "error", "stage": "update", "error": "no current generation after the update"}
    result: dict[str, Any] = {"incremental": upd["out"].get("mode"), "generation_before": db.parent.name}
    if upd["out"].get("mode") != "update":
        # the "incremental" graph is itself a fresh build: the comparison below
        # then checks that two builds agree, which is still worth knowing
        result["incremental_note"] = upd["out"].get("why")
    snapshot = store.unit_dir(cfg, key) / SNAPSHOT_NAME
    try:
        _snapshot(db, snapshot)
        built = _mb_code(cfg, root, "build", timeout)
        if "error" in built:
            return {"status": "error", "stage": "build", **result, **built}
        new_db = store.current_db(cfg, key)
        if new_db is None or new_db.parent.name == db.parent.name:
            return {"status": "error", "stage": "build", **result,
                    "error": "the build did not produce a new generation of this unit"}
        differences = diff_graphs(snapshot, new_db)
        result["generation"] = new_db.parent.name
        summary = next(iter(built["out"].get("built") or []), {})
        result["build"] = {k: summary.get(k) for k in ("files", "nodes", "unresolved", "seconds")}
        if differences:
            result["diff"] = differences
            moved = _changed_since(root, snapshot, new_db, started_ns)
            if moved:
                result["tree_changed_during_check"] = moved
        result["status"] = "differs" if differences else "equal"
    finally:
        _remove_db(snapshot)
    result["seconds"] = round(time.perf_counter() - started, 1)
    return result


def _foreign_package(root: Path) -> str | None:
    """`-m singularity` puts the working directory first on sys.path, so a unit
    that holds a `singularity` package of its own would run THAT code as the
    engine. A namespace-only folder is harmless (PYTHONPATH below wins over it);
    the engine's own checkout is harmless (it is the same code)."""
    here = Path(__file__).resolve().parents[2]
    if not (root / "singularity" / "__init__.py").is_file():
        return None
    if os.path.normcase(os.path.realpath(root)) == os.path.normcase(os.path.realpath(here)):
        return None
    return (f"{root} holds its own `singularity` package; `python -m singularity` run there would "
            "import it instead of this engine, so the unit is not rebuilt from nightly")


def _mb_code(cfg: Config, root: Path, verb: str, timeout: float) -> dict[str, Any]:
    """`mb code <verb> --json` in the unit's root, in a child process of this
    interpreter. {"out": parsed JSON} or {"error": ..., "stderr": tail}."""
    from ..platform.proc import NO_WINDOW, module_search_root

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (module_search_root(), env.get("PYTHONPATH")) if p)
    env["MB_CODE_NO_BACKGROUND"] = "1"          # nightly waits for its own work; nothing detaches
    argv = [sys.executable, "-m", "singularity", "--vault", str(cfg.vault), "code", verb, "--json"]
    try:
        proc = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return {"error": f"`mb code {verb}` did not finish in {timeout:.0f}s and was stopped",
                "stderr": str(exc.stderr or "")[-_TAIL:]}
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return {"error": f"`mb code {verb}` exited with {proc.returncode}",
                "stderr": (proc.stderr or "")[-_TAIL:]}
    try:
        return {"out": json.loads(lines[-1])}
    except ValueError:
        return {"error": f"`mb code {verb}` printed no JSON", "stderr": (proc.stderr or "")[-_TAIL:]}


def _snapshot(db: Path, dest: Path) -> None:
    """A consistent copy of a live WAL database: SQLite's backup, not a file
    copy, which could miss what sits in the WAL."""
    _remove_db(dest)
    src = store.connect_ro(db)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _remove_db(db: Path) -> None:
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _changed_since(root: Path, a_db: Path, b_db: Path, since_ns: int, cap: int = 20) -> list[str]:
    """Files of either graph that were modified or removed after `since_ns`:
    an edit during the check shows up as a difference that is not a bug."""
    paths: set[str] = set()
    for db in (a_db, b_db):
        con = store.connect_ro(db)
        try:
            paths |= {r[0] for r in con.execute("SELECT path FROM files")}
        finally:
            con.close()
    moved = []
    for p in sorted(paths):
        try:
            if (root / p).stat().st_mtime_ns >= since_ns:
                moved.append(p)
        except OSError:
            moved.append(p)
        if len(moved) >= cap:
            break
    return moved

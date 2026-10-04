"""Refresh-before-query, the writing half (CODE-GRAPH P3).

`refresh` runs before every `mb code` answer unless `--no-refresh`:
detection (see fresh) says which files differ from the graph's snapshot;
up to MAX_SYNC_FILES of them are applied in place, in one SQLite
transaction on the current generation, before the answer is read. More than
that, or a change to what defines the unit (see fresh), and the answer is
given from the graph as it is, with the changed files named in
`stale_files`, while a background `mb code update` (a subprocess -- there is
no watcher) catches up.

What an update re-resolves, so that its graph equals a full build's outside
the declared stale set (UNMET j):
* the changed and added files themselves;
* every file whose last resolution READ a changed, added or deleted path --
  its definitions, bindings, hints or exports, or merely whether a module
  existed there (`deps`, recorded by the resolvers; see tables);
* every file that looked up a unique name whose answer moved: the name's
  unique-name target before and after the change is compared, so a common
  name defined once more changes nothing, while the second definition of a
  once-unique name demotes every guess on it and the removal of one of two
  promotes (`lookups`).
Past MAX_SYNC_DEPENDENTS such dependents, only the changed files are
re-resolved now; the dependents keep their old edges, are named in the
stale set and are the background update's work. A stale set larger than
MAX_STALE is declared whole (`stale_all`).

Writers take SQLite's own write lock, polled (see store.write_transaction),
and re-check under it that nobody wrote the graph since they looked
(`seq`): a caller that lost the race detects again and, as a rule, finds
the work done -- 16 callers, one update, no stale answer (B5).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from . import fresh, languages, scope, sidecar, store, versions
from .model import FileFacts, decode_facts


# `rows` and `tables` pull in the resolvers: imported where a write needs
# them, so the check of an unchanged tree costs git status and nothing more (B3).

MAX_SYNC_FILES = 200            # owner decision 2026-09-26: more is a background update
MAX_SYNC_DEPENDENTS = 3000
MAX_STALE = 5000                # N, the cap on the stale set
STALE_LISTED = 200              # an answer names at most this many stale files, and says so
QUERY_WAIT_SECONDS = 10.0       # how long a query waits for another writer before answering as is
_RETRIES = 5
_CHUNK = 500                    # SQLite's host-parameter limit is 999 on old builds


class DbSource:
    """A resolver's view of the stored graph (tables.MemorySource is the
    in-memory one): one language's files, their stored facts, and the
    unique-name candidates from `nodes`."""

    def __init__(self, con: sqlite3.Connection, lang: str, preloaded: dict[str, FileFacts] | None = None):
        self._con, self._lang = con, lang
        self._ids: dict[str, int] = {}
        self._tests: set[str] = set()
        for fid, path, is_test in con.execute("SELECT id, path, is_test FROM files WHERE lang = ?", (lang,)):
            self._ids[path] = fid
            if is_test:
                self._tests.add(path)
        self._facts: dict[str, FileFacts] = dict(preloaded or {})
        self._guesses: dict[tuple[str, bool], str | None] = {}

    def paths(self) -> list[str]:
        return list(self._ids)

    def exists(self, path: str) -> bool:
        return path in self._ids

    def facts(self, path: str) -> FileFacts:
        f = self._facts.get(path)
        if f is None:
            row = self._con.execute("SELECT blob FROM facts WHERE file_id = ?", (self._ids[path],)).fetchone()
            f = self._facts[path] = decode_facts(row[0])
        return f

    def is_test(self, path: str) -> bool:
        return path in self._tests

    def package_files(self, package: str) -> tuple[str, ...]:
        """tables.MemorySource.package_files over `files.package`."""
        rows = self._con.execute("SELECT path FROM files WHERE lang = ? AND package IS ?",
                                 (self._lang, package or None))
        return tuple(r[0] for r in rows)

    def guess(self, name: str, from_test: bool, never: tuple[str, ...]) -> str | None:
        key = (name, from_test)
        if key not in self._guesses:
            self._guesses[key] = guess_sql(self._con, name, self._lang, from_test, never)
        return self._guesses[key]


def guess_sql(con: sqlite3.Connection, name: str, lang: str, from_test: bool,
              never: tuple[str, ...]) -> str | None:
    """tables.guess_target in SQL. Two eligible definitions are as good as
    two thousand -- `__init__` and `forward` have thousands -- so at most two
    are fetched."""
    sql = ("SELECT n.qname FROM nodes n JOIN files f ON f.id = n.file_id"
           " WHERE n.name = ? AND f.lang = ? AND n.local = 0")
    params: list = [name, lang]
    if not from_test:
        sql += " AND f.is_test = 0"
    if never:
        sql += f" AND n.kind NOT IN ({','.join('?' * len(never))})"
        params += list(never)
    rows = con.execute(sql + " LIMIT 2", params).fetchall()
    return rows[0][0] if len(rows) == 1 else None


@dataclass
class Freshness:
    """What an answer can say about its own currency. `checked`: the tree was
    compared with the graph for this answer."""
    checked: bool
    updated: list[str] = field(default_factory=list)       # applied before this answer
    stale_files: list[str] = field(default_factory=list)   # known to differ; the answer may be off there
    stale_all: str | None = None                           # the whole graph may be off, and why
    head_unreachable: bool = False
    background: bool = False                               # a background update runs or was started
    note: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"checked": self.checked, "updated": self.updated[:STALE_LISTED],
                "updated_count": len(self.updated), "stale_files": self.stale_files[:STALE_LISTED],
                "stale_count": len(self.stale_files), "stale_all": self.stale_all,
                "head_unreachable": self.head_unreachable, "background": self.background,
                "note": self.note}


@dataclass
class _Snapshot:
    meta: dict[str, Any]
    dirty: set[str]
    stale: dict[str, str]


def _snapshot(con: sqlite3.Connection) -> _Snapshot | None:
    """The graph's bookkeeping; None for a graph of another schema, which is
    rebuilt, never read for freshness."""
    meta = store.read_meta(con)
    if meta.get("schema_version") != store.SCHEMA_VERSION:
        return None
    return _Snapshot(meta, {r[0] for r in con.execute("SELECT path FROM dirty")},
                     {r[0]: r[1] for r in con.execute("SELECT path, reason FROM stale")})


def _old_schema(con: sqlite3.Connection) -> str:
    return (f"the graph has schema {store.read_meta(con).get('schema_version')} and this engine "
            f"{store.SCHEMA_VERSION}: rebuild it with `mb code build`")


def known_stale(con: sqlite3.Connection) -> Freshness:
    """`--no-refresh`: nothing compared, but what the graph already knows is
    stale is still said."""
    snap = _snapshot(con)
    if snap is None:
        return Freshness(checked=False, stale_all=_old_schema(con))
    return Freshness(checked=False, stale_files=sorted(snap.stale), stale_all=snap.meta.get("stale_all"),
                     note="--no-refresh: answers reflect the graph as last written")


def refresh(cfg: Config, key: str, *, background_ok: bool = True) -> Freshness:
    """Bring the unit's graph up to date with its tree if that is cheap and
    safe, and say exactly how current the answer about to be read is."""
    for _ in range(_RETRIES):
        db = store.current_db(cfg, key)
        if db is None:
            return Freshness(checked=False, note="no current generation")
        con = store.connect_ro(db)
        try:
            snap = _snapshot(con)
            if snap is None:
                # a graph of an older engine: rebuilt in the background (update falls back to it)
                root = Path(store.read_meta(con)["root"])
                started = background_ok and spawn(cfg, key, root, "update")
                return Freshness(checked=False, stale_all=_old_schema(con), background=started)
            try:
                det = fresh.detect(cfg, snap.meta, con, snap.dirty)
            except (fresh.GitFailed, scope.GraphError) as exc:
                # a scan that failed says nothing about the tree: nothing is written (UNMET k)
                return Freshness(checked=False, stale_files=sorted(snap.stale), note=f"not checked: {exc}")
        finally:
            con.close()
        root = Path(snap.meta["root"])
        known = set(snap.stale)
        if det.rebuild or det.overflow:
            why = det.rebuild or f"{len(det.overflow or [])} files changed since the graph was built"
            started = background_ok and spawn(cfg, key, root, "update")
            return Freshness(checked=True, stale_files=sorted(known | set(det.overflow or [])), stale_all=why,
                             head_unreachable=det.head_unreachable, background=started)
        code = det.code_changes
        if not det.changes and not det.moves_snapshot(snap.meta, snap.dirty):
            return Freshness(checked=True, stale_files=sorted(known), stale_all=snap.meta.get("stale_all"),
                             head_unreachable=det.head_unreachable)
        blocked = why_not_here(snap.meta) if code else None
        if blocked or len(code) > MAX_SYNC_FILES:
            started = background_ok and spawn(cfg, key, root, "update")
            return Freshness(checked=True, stale_files=sorted(known | {c.path for c in code}),
                             stale_all=snap.meta.get("stale_all"), head_unreachable=det.head_unreachable,
                             background=started,
                             note=blocked or f"{len(code)} changed files: more than {MAX_SYNC_FILES} "
                                             "are updated in the background")
        try:
            done = apply(db, det, snap.meta.get("seq", 0), cap_dependents=True, wait=QUERY_WAIT_SECONDS)
        except store.GraphLocked:
            # a long writer (a background update of thousands of files): a query
            # does not wait it out -- it answers now and names what it could not apply
            return Freshness(checked=True, stale_files=sorted(known | {c.path for c in code}),
                             stale_all=snap.meta.get("stale_all"), head_unreachable=det.head_unreachable,
                             background=True, note="another update is writing this graph; answered as it stands")
        if done is None:
            continue                                  # another writer got there first: look again
        started = bool(done.stale) and background_ok and spawn(cfg, key, root, "update")
        return Freshness(checked=True, updated=done.updated, stale_files=done.stale, stale_all=done.stale_all,
                         head_unreachable=det.head_unreachable, background=started)
    return Freshness(checked=False, note="the graph kept changing under this query; answered as it stands")


def why_not_here(meta: dict[str, Any]) -> str | None:
    """Why this interpreter may not write changed files into this graph."""
    if not versions.parser_available():
        return (f"the parser is not installed in {sys.executable}; changed files are updated by "
                "`mb code update` from an interpreter that has it")
    mismatch = versions.version_mismatch(meta)
    if mismatch:
        return ("the graph was written by other parser/resolver versions "
                f"({', '.join(f'{k} {a}->{b}' for k, (a, b) in sorted(mismatch.items()))}): rebuild it")
    return None


@dataclass
class Applied:
    updated: list[str]
    resolved: int
    stale: list[str]
    stale_all: str | None


def _chunks(values: list) -> list[list]:
    return [values[i:i + _CHUNK] for i in range(0, len(values), _CHUNK)]


def _marks(chunk: list) -> str:
    return ",".join("?" * len(chunk))


def _ids(con: sqlite3.Connection, paths) -> dict[str, tuple[int, str, str, str | None]]:
    """{path: (id, lang, content_hash, package)} of the stored files among `paths`."""
    out: dict[str, tuple[int, str, str, str | None]] = {}
    for chunk in _chunks(sorted(paths)):
        out.update({r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
            f"SELECT path, id, lang, content_hash, package FROM files WHERE path IN ({_marks(chunk)})", chunk)})
    return out


def _delete_rows(con: sqlite3.Connection, tables: tuple[str, ...], file_ids: list[int]) -> None:
    for chunk in _chunks(file_ids):
        for table in tables:
            con.execute(f"DELETE FROM {table} WHERE file_id IN ({_marks(chunk)})", chunk)


def _looked_up(con: sqlite3.Connection, names: dict[str, set[str]]) -> dict[str, set[str]]:
    """The names some file of the language asked the unique-name tier about:
    an answer nobody asked for moves nothing."""
    out: dict[str, set[str]] = {}
    for lang, ns in names.items():
        found: set[str] = set()
        for chunk in _chunks(sorted(ns)):
            found |= {r[0] for r in con.execute(
                "SELECT DISTINCT l.name FROM lookups l JOIN files f ON f.id = l.file_id"
                f" WHERE f.lang = ? AND l.name IN ({_marks(chunk)})", [lang, *chunk])}
        out[lang] = found
    return out


def _outcomes(con: sqlite3.Connection, names: dict[str, set[str]]) -> dict[tuple[str, str], tuple]:
    """The unique-name tier's answer for each name, from a production and
    from a test caller -- what a guess on the name would draw right now."""
    out = {}
    for lang, ns in names.items():
        never = languages.family(lang).never_guessed
        for n in ns:
            out[(lang, n)] = (guess_sql(con, n, lang, False, never), guess_sql(con, n, lang, True, never))
    return out


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def apply(db: Path, det: fresh.Detection, seq_seen: int, *, cap_dependents: bool,
          also_resolve: set[str] = frozenset(), wait: float | None = None) -> Applied | None:
    """Apply `det` to the graph in one transaction. None: another writer
    changed the graph since `det` was taken -- detect again. `also_resolve`:
    paths to re-resolve regardless (the stale set, in the background run).
    `wait`: how long to wait for another writer (store default if None)."""
    from .rows import dep_rows, name_ref_counts, node_rows, workspace_from_meta

    with store.write_transaction(db, **({} if wait is None else {"wait": wait})) as con:
        meta = store.read_meta(con)
        if meta.get("seq", 0) != seq_seen:
            return None
        root = Path(meta["root"])
        rows = _ids(con, det.changes)
        modified: dict[str, tuple] = {}
        added: dict[str, tuple] = {}
        deleted: set[str] = set()
        touched: dict[str, os.stat_result] = {}
        flux: set[str] = set()                     # moved between detection and now: keep watching
        to_parse = [c for c in det.changes.values() if c.kind in ("added", "modified")]
        build = None
        if to_parse:
            from . import build                    # the parser, only when there is a file to parse
        for p, ch in sorted(det.changes.items()):
            if ch.kind == "deleted":
                if p in rows:
                    deleted.add(p)
                continue
            try:
                st = (root / p).stat()                          # before the read
            except OSError:
                if p in rows:
                    deleted.add(p)
                continue
            if ch.kind == "touched":
                if _sha(root / p) == rows[p][2]:
                    touched[p] = st
                else:
                    flux.add(p)                                 # changed since: the next query sees it
                continue
            try:
                lang, facts, digest, blob = build.extract_file(root, p)
            except OSError:
                if p in rows:
                    deleted.add(p)
                continue
            if digest != ch.content_hash:
                flux.add(p)
            if p in rows and digest == rows[p][2]:
                touched[p] = st
            elif p in rows:
                modified[p] = (st, lang, facts, digest, blob)
            else:
                added[p] = (st, lang, facts, digest, blob)

        read_done = time.time_ns()

        def mtime(st: os.stat_result) -> int:
            return store.recorded_mtime(st.st_mtime_ns, read_done)

        # the unique-name answers before the change, for every name it touches
        names: dict[str, set[str]] = {lang: set() for lang in languages.LANGS}
        gone_ids = [rows[p][0] for p in (*modified, *deleted)]
        for chunk in _chunks(gone_ids):
            for name, lang in con.execute(
                    "SELECT n.name, f.lang FROM nodes n JOIN files f ON f.id = n.file_id"
                    f" WHERE n.file_id IN ({_marks(chunk)})", chunk):
                names[lang].add(name)
        for _st, lang, facts, _digest, _blob in (*modified.values(), *added.values()):
            names[lang].update(d.name for d in facts.definitions)
        names = _looked_up(con, names)
        before = _outcomes(con, names)

        # name mentions: the old versions' out, the new versions' in (deadcode reads the counts)
        mentions: Counter[tuple[str, str]] = Counter()
        for chunk in _chunks(gone_ids):
            for (blob,) in con.execute(f"SELECT blob FROM facts WHERE file_id IN ({_marks(chunk)})", chunk):
                mentions.subtract(name_ref_counts([decode_facts(blob)]))
        mentions.update(name_ref_counts([v[2] for v in (*modified.values(), *added.values())]))
        con.executemany("INSERT INTO name_refs(lang, name, files) VALUES (?, ?, ?) ON CONFLICT(lang, name)"
                        " DO UPDATE SET files = files + excluded.files",
                        [(lang, n, d) for (lang, n), d in mentions.items() if d])
        con.execute("DELETE FROM name_refs WHERE files <= 0")

        # the files themselves
        _delete_rows(con, ("edges", "unresolved", "deps", "lookups", "facts", "nodes"), gone_ids)
        _delete_rows(con, ("communities",), [rows[p][0] for p in deleted])
        for chunk in _chunks([rows[p][0] for p in deleted]):
            con.execute(f"DELETE FROM files WHERE id IN ({_marks(chunk)})", chunk)
        for p, st in touched.items():
            con.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE id = ?",
                        (st.st_size, mtime(st), rows[p][0]))
        for p, (st, _lang, facts, digest, _blob) in modified.items():
            con.execute("UPDATE files SET size = ?, mtime_ns = ?, content_hash = ?, parse_status = ?, package = ?"
                        " WHERE id = ?", (st.st_size, mtime(st), digest,
                                          "partial" if facts.degraded else "ok", facts.package, rows[p][0]))
        for p, (st, lang, facts, digest, _blob) in added.items():
            cur = con.execute(
                "INSERT INTO files(path, lang, size, mtime_ns, content_hash, parse_status, is_test, package)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (p, lang, st.st_size, mtime(st), digest, "partial" if facts.degraded else "ok",
                 int(scope.is_test_path(p)), facts.package))
            rows[p] = (cur.lastrowid, lang, digest, facts.package)
        parsed: dict[str, FileFacts] = {}
        for p, (_st, _lang, facts, _digest, blob) in (*modified.items(), *added.items()):
            store.insert_nodes(con, node_rows(facts, rows[p][0]))
            con.execute("INSERT INTO facts(file_id, blob) VALUES (?, ?)", (rows[p][0], blob))
            parsed[p] = facts

        # who has to be resolved again
        after = _outcomes(con, names)
        moved: dict[str, list[str]] = {lang: [] for lang in languages.LANGS}
        for (lang, n), was in before.items():
            if after[(lang, n)] != was:
                moved[lang].append(n)
        changed = sorted({*modified, *added, *deleted})
        # A file entering or leaving a JVM package changes what listing that
        # package answers: its readers depend on the package's pseudo-path.
        moved_members: set[str] = set()
        for p in deleted:
            moved_members |= languages.membership(rows[p][1], rows[p][3])
        for _st, lang, facts, _digest, _blob in added.values():
            moved_members |= languages.membership(lang, facts.package)
        for p, (_st, lang, facts, _digest, _blob) in modified.items():
            if facts.package != rows[p][3]:
                moved_members |= languages.membership(lang, rows[p][3]) | languages.membership(lang, facts.package)
        dependents: set[str] = set()
        for chunk in _chunks(changed + sorted(moved_members)):
            dependents |= {r[0] for r in con.execute(
                "SELECT DISTINCT f.path FROM dep_paths dp JOIN deps d ON d.path_id = dp.id"
                f" JOIN files f ON f.id = d.file_id WHERE dp.path IN ({_marks(chunk)})", chunk)}
        for lang, ns in moved.items():
            for chunk in _chunks(ns):
                dependents |= {r[0] for r in con.execute(
                    "SELECT DISTINCT f.path FROM lookups l JOIN files f ON f.id = l.file_id"
                    f" WHERE f.lang = ? AND l.name IN ({_marks(chunk)})", [lang, *chunk])}
        dependents |= set(also_resolve)
        dependents -= {*modified, *added, *deleted}
        rows.update(_ids(con, dependents))
        dependents = {p for p in dependents if p in rows}
        deferred: set[str] = set()
        if cap_dependents and len(dependents) > MAX_SYNC_DEPENDENTS:
            deferred, dependents = dependents, set()
        _delete_rows(con, ("edges", "unresolved", "deps", "lookups"), [rows[p][0] for p in dependents])

        targets = sorted({*modified, *added, *dependents})
        edges: list = []
        unresolved: list = []
        file_deps: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        for lang in languages.LANGS:
            lang_targets = [p for p in targets if rows[p][1] == lang]
            if not lang_targets:
                continue
            resolver = languages.resolver(lang, source=DbSource(con, lang, parsed),
                                          workspace=workspace_from_meta(meta))
            e, u = resolver.resolve(lang_targets)
            edges, unresolved = edges + e, unresolved + u
            file_deps.update(resolver.file_deps)
        fid = {p: rows[p][0] for p in targets}
        con.executemany("INSERT INTO edges(src, dst, kind, tier, callee, file_id, line)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(e.src, e.dst, e.kind, e.tier, e.callee, fid[e.file], e.line) for e in edges])
        con.executemany("INSERT INTO unresolved(src, name, receiver, file_id, line) VALUES (?, ?, ?, ?, ?)",
                        [(u.src, u.name, u.receiver, fid[u.file], u.line) for u in unresolved])
        deps, lookups = dep_rows(file_deps, fid)
        store.insert_deps(con, deps)
        con.executemany("INSERT INTO lookups(file_id, name) VALUES (?, ?)", lookups)

        # the stale set and the snapshot's bookkeeping
        for chunk in _chunks([*targets, *deleted]):
            con.execute(f"DELETE FROM stale WHERE path IN ({_marks(chunk)})", chunk)
        con.executemany("INSERT OR REPLACE INTO stale(path, reason) VALUES (?, ?)",
                        [(p, "depends on a change applied without it") for p in sorted(deferred)])
        stale = sorted(r[0] for r in con.execute("SELECT path FROM stale"))
        stale_all = (f"{len(stale)} files wait to be resolved again (more than {MAX_STALE})"
                     if len(stale) > MAX_STALE else None)
        con.execute("DELETE FROM dirty")
        con.executemany("INSERT INTO dirty(path) VALUES (?)", [(p,) for p in sorted(det.dirty | flux)])
        con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
                        [(k, json.dumps(v)) for k, v in {"indexed_head": det.head, "seq": seq_seen + 1,
                                                         "updated_at": time.time(),
                                                         "stale_all": stale_all}.items()])
    sidecar.record(db)
    return Applied(updated=changed, resolved=len(targets), stale=stale, stale_all=stale_all)


# --- the background half -------------------------------------------------------

def _lock_alive(cfg: Config, key: str) -> bool:
    lock = store.unit_dir(cfg, key) / ".build.lock"
    return lock.exists() and not store._owner_is_dead(lock)


def spawn(cfg: Config, key: str, root: Path, verb: str) -> bool:
    """Start `mb code <verb>` for the unit in a detached subprocess -- the
    decision's replacement for a watcher. True if one runs now."""
    if _lock_alive(cfg, key):
        return True
    if not versions.parser_available() or os.environ.get("MB_CODE_NO_BACKGROUND"):
        return False
    from ..platform.proc import NO_WINDOW

    log = store.unit_dir(cfg, key) / "background.log"
    # A hidden console of its own, not DETACHED_PROCESS: a detached child has NO
    # console, so each console program it starts -- git, the build's pool
    # workers -- would flash a window of its own; these inherit the hidden one.
    flags = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, "-m", "singularity", "code", verb, "--json"], cwd=str(root),
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)
    return True


def update_unit(cfg: Config, key: str) -> dict[str, Any]:
    """`mb code update`: everything detection finds, however much, and the
    whole stale set, under the build lock so one runs per unit. What an
    in-place update cannot do correctly (see fresh) is a full build."""
    from . import build
    with store.build_lock(cfg, key):
        for _ in range(_RETRIES):
            db = store.current_db(cfg, key)
            if db is None:
                return {"mode": "rebuild", "why": "no current generation", **_rebuild(cfg, key, build)}
            con = store.connect_ro(db)
            try:
                snap = _snapshot(con)
                det = fresh.detect(cfg, snap.meta, con, snap.dirty) if snap else None
            finally:
                con.close()
            why = ("the graph has an old schema" if snap is None else
                   det.rebuild or (f"{len(det.overflow)} changed files" if det.overflow else None)
                   or why_not_here(snap.meta))
            if why:
                return {"mode": "rebuild", "why": why, **_rebuild(cfg, key, build)}
            done = apply(db, det, snap.meta.get("seq", 0), cap_dependents=False, also_resolve=set(snap.stale))
            if done is not None:
                return {"mode": "update", "updated": done.updated, "resolved": done.resolved,
                        "stale": done.stale}
    return {"mode": "none", "why": "the graph kept changing"}


def _rebuild(cfg: Config, key: str, build) -> dict[str, Any]:
    unit = next((u for u in scope.discover_units(cfg) if u.key == key), None)
    if unit is None:
        raise scope.GraphError(f"{key!r} is no longer a graph unit of this configuration")
    return build.build_unit(cfg, unit, locked=True)

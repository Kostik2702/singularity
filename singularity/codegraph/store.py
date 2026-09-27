"""One SQLite graph per unit, in generations (CODE-GRAPH D-7).

A full build writes a NEW generation directory and then moves the `CURRENT`
pointer -- a small text file, never the database: on Windows `os.replace`
over a database another process holds open fails (WinError 5), and the
pointer is what readers follow anyway. A build that dies half way leaves
`CURRENT` on the previous generation, which stays readable.

Old generations are removed after the swap. A reader can still hold one open
(WinError 32 on delete), so removal is retried on every later build and
whatever survives is reported, never silently ignored: a forgotten
generation of a 100k-file repository is gigabytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import Config
from ..platform import proc

# 2: index on unresolved(src) -- `callees` scanned the table whole, 1.8 ms on
# 25k rows and seconds on the millions of a 100k-file unit.
# 3: what an in-place update needs (P3): each file's facts, what its
# resolution read (`deps`: paths, misses included; `lookups`: unique-name
# lookups), the snapshot's dirty set, the stale set, `nodes.local`.
# 4: what P4 reads -- per-language counts of files naming each name (`deadcode`
# must not call a function handed over as a value dead), file communities with
# stable labels (`arch`), `nodes.bases`.
SCHEMA_VERSION = 4
_LOCK_WAIT_SECONDS = 5.0
# A writer that finds another writer busy polls instead of sleeping in
# SQLite's busy handler: 16 contending callers took 601-1784 ms with the
# handler and 75-81 ms polling (council measurement, CODE-GRAPH D1).
_WRITE_POLL_SECONDS = 0.01
_WRITE_WAIT_SECONDS = 30.0

DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE files(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, lang TEXT NOT NULL,
    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content_hash TEXT NOT NULL,
    parse_status TEXT NOT NULL, is_test INTEGER NOT NULL);
CREATE TABLE nodes(
    id INTEGER PRIMARY KEY, qname TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    kind TEXT NOT NULL, file_id INTEGER NOT NULL, parent TEXT, spans TEXT NOT NULL,
    signature TEXT NOT NULL, doc1 TEXT, decorators TEXT NOT NULL, exported INTEGER NOT NULL,
    local INTEGER NOT NULL DEFAULT 0, bases TEXT NOT NULL DEFAULT '[]');
CREATE TABLE name_refs(lang TEXT NOT NULL, name TEXT NOT NULL, files INTEGER NOT NULL,
    PRIMARY KEY(lang, name));
CREATE TABLE communities(file_id INTEGER PRIMARY KEY, community INTEGER NOT NULL);
CREATE TABLE community_labels(community INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE facts(file_id INTEGER PRIMARY KEY, blob BLOB NOT NULL);
CREATE TABLE dep_paths(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
CREATE TABLE deps(file_id INTEGER NOT NULL, path_id INTEGER NOT NULL);
CREATE TABLE lookups(file_id INTEGER NOT NULL, name TEXT NOT NULL);
CREATE TABLE dirty(path TEXT PRIMARY KEY);
CREATE TABLE stale(path TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE edges(
    src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT NOT NULL,
    callee TEXT, file_id INTEGER NOT NULL, line INTEGER NOT NULL);
CREATE TABLE unresolved(
    src TEXT NOT NULL, name TEXT NOT NULL, receiver TEXT,
    file_id INTEGER NOT NULL, line INTEGER NOT NULL);
"""
# Built after the bulk insert of a generation: filling indexed tables row by
# row cost the proxy build ~30 s of its write phase.
INDEXES = """
CREATE INDEX nodes_name ON nodes(name);
CREATE INDEX nodes_file ON nodes(file_id);
CREATE INDEX edges_src ON edges(src, kind);
CREATE INDEX edges_dst ON edges(dst, kind);
CREATE INDEX edges_site ON edges(file_id, line);
CREATE INDEX unresolved_name ON unresolved(name);
CREATE INDEX unresolved_src ON unresolved(src);
CREATE INDEX unresolved_site ON unresolved(file_id, line);
CREATE INDEX deps_path ON deps(path_id);
CREATE INDEX deps_file ON deps(file_id);
CREATE INDEX lookups_name ON lookups(name);
CREATE INDEX lookups_file ON lookups(file_id);
CREATE INDEX communities_by ON communities(community);
"""


# "Racy git": a file modified this close to the moment its content was read
# can be modified again within the same clock tick at the same size, and its
# stat would then vouch for content the graph does not hold. Such a stat is
# recorded as 0, so the next comparison hashes the file (see fresh).
RACY_NS = 2_000_000_000


def recorded_mtime(mtime_ns: int, read_done_ns: int) -> int:
    return 0 if read_done_ns - mtime_ns < RACY_NS else mtime_ns


class GraphLocked(RuntimeError):
    """Another live process is building this unit's graph."""


def unit_dir(cfg: Config, unit_key: str) -> Path:
    """A path-safe directory per unit: readable prefix plus a hash of the
    exact key, so `a b` and `a_b` cannot share a store."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", unit_key).strip("_")[:60] or "unit"
    return cfg.code_graph_dir / f"{slug}-{hashlib.sha256(unit_key.encode()).hexdigest()[:8]}"


def current_db(cfg: Config, unit_key: str) -> Path | None:
    pointer = unit_dir(cfg, unit_key) / "CURRENT"
    try:
        gen = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    db = pointer.parent / gen / "graph.db"
    return db if db.is_file() else None


@contextmanager
def build_lock(cfg: Config, unit_key: str) -> Iterator[None]:
    """One full build per unit at a time. The owner's pid and start time are
    written INTO the lock, so a lock whose owner died is taken over at once
    instead of after hours; a live owner's lock is never removed by anyone
    else (the losing contender only waits, then gives up)."""
    lock = unit_dir(cfg, unit_key) / ".build.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "create_time": proc.pid_create_time(os.getpid())}
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps(me), encoding="utf-8")
            break
        except FileExistsError:
            if _owner_is_dead(lock):
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise GraphLocked(f"a build of {unit_key!r} is running (lock {lock})") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _owner_is_dead(lock: Path) -> bool:
    try:
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # a lock without an owner file is either being created this instant or
        # was left by a crash between mkdir and write: judge it by age
        try:
            return time.time() - lock.stat().st_mtime > 30
        except OSError:
            return True
    pid = int(owner.get("pid", 0))
    if not pid or not proc.is_running(pid):
        return True
    recorded, actual = owner.get("create_time"), proc.pid_create_time(pid)
    return bool(recorded and actual and abs(recorded - actual) > 1.0)    # the pid was reused


def write_generation(cfg: Config, unit_key: str, *, meta: dict[str, Any],
                     files: list[dict[str, Any]], nodes: list[dict[str, Any]],
                     edges: list[tuple], unresolved: list[tuple],
                     facts: list[tuple] = (), deps: list[tuple] = (), lookups: list[tuple] = (),
                     dirty: list[str] = (), name_refs: list[tuple] = (), communities: list[tuple] = (),
                     community_labels: list[tuple] = ()) -> dict[str, Any]:
    """Write a complete new generation, then point CURRENT at it."""
    base = unit_dir(cfg, unit_key)
    gen = f"gen_{time.time_ns()}_{os.getpid()}"
    gen_dir = base / gen
    gen_dir.mkdir(parents=True)
    db = gen_dir / "graph.db"
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(DDL)
        with con:
            con.executemany("INSERT INTO meta(k, v) VALUES (?, ?)",
                            [(k, json.dumps(v)) for k, v in {**meta, "schema_version": SCHEMA_VERSION}.items()])
            con.executemany(
                "INSERT INTO files(id, path, lang, size, mtime_ns, content_hash, parse_status, is_test)"
                " VALUES (:id, :path, :lang, :size, :mtime_ns, :content_hash, :parse_status, :is_test)", files)
            insert_nodes(con, nodes)
            con.executemany("INSERT INTO edges(src, dst, kind, tier, callee, file_id, line)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)", edges)
            con.executemany("INSERT INTO unresolved(src, name, receiver, file_id, line)"
                            " VALUES (?, ?, ?, ?, ?)", unresolved)
            con.executemany("INSERT INTO facts(file_id, blob) VALUES (?, ?)", facts)
            insert_deps(con, deps)
            con.executemany("INSERT INTO lookups(file_id, name) VALUES (?, ?)", lookups)
            con.executemany("INSERT INTO dirty(path) VALUES (?)", [(p,) for p in dirty])
            con.executemany("INSERT INTO name_refs(lang, name, files) VALUES (?, ?, ?)", name_refs)
            con.executemany("INSERT INTO communities(file_id, community) VALUES (?, ?)", communities)
            con.executemany("INSERT INTO community_labels(community, label) VALUES (?, ?)", community_labels)
        con.executescript(INDEXES)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    pointer_tmp = base / f"CURRENT.{os.getpid()}.tmp"
    pointer_tmp.write_text(gen, encoding="utf-8")
    os.replace(pointer_tmp, base / "CURRENT")
    # What a reader needs to pick this unit for a path, without opening any
    # database or asking git: key and root. Hooks and `mb code` read only this.
    card_tmp = base / f"unit.json.{os.getpid()}.tmp"
    card_tmp.write_text(json.dumps({"key": unit_key, "root": meta.get("root"),
                                    "generation": gen, "built_at": meta.get("built_at")}),
                        encoding="utf-8")
    os.replace(card_tmp, base / "unit.json")
    return {"generation": gen, "gc_pending": collect_garbage(base, keep=gen)}


def built_units(cfg: Config) -> list[dict[str, Any]]:
    """Every unit with a graph on this machine, from its `unit.json` card."""
    out = []
    root = cfg.code_graph_dir
    if not root.is_dir():
        return out
    for card in sorted(root.glob("*/unit.json")):
        try:
            data = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("key") and data.get("root"):
            out.append(data)
    return out


def collect_garbage(base: Path, keep: str) -> list[str]:
    """Remove every generation but `keep`; name the ones that could not go."""
    pending = []
    for d in sorted(base.glob("gen_*")):
        if d.name == keep or not d.is_dir():
            continue
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            pending.append(d.name)
    return pending


def insert_nodes(con: sqlite3.Connection, nodes: list[dict[str, Any]]) -> None:
    con.executemany(
        "INSERT INTO nodes(qname, name, kind, file_id, parent, spans, signature, doc1, decorators, exported,"
        " local, bases) VALUES (:qname, :name, :kind, :file_id, :parent, :spans, :signature, :doc1,"
        " :decorators, :exported, :local, :bases)", nodes)


def insert_deps(con: sqlite3.Connection, deps: list[tuple[int, str]]) -> None:
    """(file_id, path) rows. A path is stored once in `dep_paths`: most of
    them are module probes that missed (695k distinct paths, 2.6M rows on
    the 44k-file proxy), and a text column with its index held them twice."""
    if not deps:
        return
    wanted = sorted({p for _, p in deps})
    if con.execute("SELECT 1 FROM dep_paths LIMIT 1").fetchone() is None:     # a build: number them here
        ids = {p: i for i, p in enumerate(wanted, 1)}
        con.executemany("INSERT INTO dep_paths(id, path) VALUES (?, ?)", [(i, p) for p, i in ids.items()])
    else:                                                                      # an update: a few, looked up
        con.executemany("INSERT OR IGNORE INTO dep_paths(path) VALUES (?)", [(p,) for p in wanted])
        ids = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            ids.update(con.execute(f"SELECT path, id FROM dep_paths WHERE path IN "
                                   f"({','.join('?' * len(chunk))})", chunk).fetchall())
    con.executemany("INSERT INTO deps(file_id, path_id) VALUES (?, ?)", [(f, ids[p]) for f, p in deps])


@contextmanager
def write_transaction(db: Path, wait: float = _WRITE_WAIT_SECONDS) -> Iterator[sqlite3.Connection]:
    """The in-place writer of an update: `BEGIN IMMEDIATE`, polled. A caller
    that finds another writer at work waits for it without holding anything,
    then gets the lock and sees that writer's result -- so it re-checks what
    is left to do inside the transaction, usually nothing. Commits on a clean
    exit; any exception, a kill included, leaves the database as it was.
    GraphLocked after `wait` seconds."""
    con = sqlite3.connect(db, timeout=0, isolation_level=None)
    con.row_factory = sqlite3.Row
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                con.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise GraphLocked(f"another update of {db} held the write lock for {wait:.0f}s") from None
                time.sleep(_WRITE_POLL_SECONDS)
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def connect_ro(db: Path) -> sqlite3.Connection:
    """A reader: read-only, and patient with a writer's lock for a second."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    return con


def read_meta(con: sqlite3.Connection) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}

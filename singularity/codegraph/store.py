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
SCHEMA_VERSION = 2
_LOCK_WAIT_SECONDS = 5.0

DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE files(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, lang TEXT NOT NULL,
    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content_hash TEXT NOT NULL,
    parse_status TEXT NOT NULL, is_test INTEGER NOT NULL);
CREATE TABLE nodes(
    id INTEGER PRIMARY KEY, qname TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    kind TEXT NOT NULL, file_id INTEGER NOT NULL, parent TEXT, spans TEXT NOT NULL,
    signature TEXT NOT NULL, doc1 TEXT, decorators TEXT NOT NULL, exported INTEGER NOT NULL);
CREATE TABLE edges(
    src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT NOT NULL,
    callee TEXT, file_id INTEGER NOT NULL, line INTEGER NOT NULL);
CREATE TABLE unresolved(
    src TEXT NOT NULL, name TEXT NOT NULL, receiver TEXT,
    file_id INTEGER NOT NULL, line INTEGER NOT NULL);
CREATE INDEX nodes_name ON nodes(name);
CREATE INDEX nodes_file ON nodes(file_id);
CREATE INDEX edges_src ON edges(src, kind);
CREATE INDEX edges_dst ON edges(dst, kind);
CREATE INDEX edges_site ON edges(file_id, line);
CREATE INDEX unresolved_name ON unresolved(name);
CREATE INDEX unresolved_src ON unresolved(src);
CREATE INDEX unresolved_site ON unresolved(file_id, line);
"""


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
                     edges: list[tuple], unresolved: list[tuple]) -> dict[str, Any]:
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
            con.executemany(
                "INSERT INTO nodes(qname, name, kind, file_id, parent, spans, signature, doc1, decorators, exported)"
                " VALUES (:qname, :name, :kind, :file_id, :parent, :spans, :signature, :doc1, :decorators, :exported)",
                nodes)
            con.executemany("INSERT INTO edges(src, dst, kind, tier, callee, file_id, line)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)", edges)
            con.executemany("INSERT INTO unresolved(src, name, receiver, file_id, line)"
                            " VALUES (?, ?, ?, ?, ?)", unresolved)
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


def connect_ro(db: Path) -> sqlite3.Connection:
    """A reader: read-only, and patient with a writer's lock for a second."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    return con


def read_meta(con: sqlite3.Connection) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}

"""Answers from a built graph (CODE-GRAPH P2).

Read-only by construction: every function takes a read-only connection and
returns data; nothing here parses code, so the query path never imports a
parser (the import-floor guard pins that) and runs on any interpreter that
has sqlite3.

Two honesty rules shape every answer:
* an answer about callers carries `unresolved_candidates` -- call sites of
  the same NAME the resolver could not place. A caller missing from the
  edges is recoverable from there; presenting absence of an edge as absence
  of a relation is UNMET (h).
* a truncated list says so (`has_more`, `next_offset`), never silently.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from . import store
from .scope import GraphError

ENVELOPE_SCHEMA = 1
MAX_DEPTH = 5


class GraphNotBuilt(GraphError):
    """No graph covers the requested place; the message says how to build one."""


class SymbolNotFound(GraphError):
    pass


class Ambiguous(GraphError):
    def __init__(self, token: str, candidates: list[str]):
        super().__init__(f"{token!r} names {len(candidates)} definitions; pass one qname")
        self.candidates = candidates


class StaleSource(GraphError):
    """The file changed since the graph was built; its stored spans no longer
    point at the same text, so a snippet would be cut from the wrong lines."""


@dataclass(frozen=True)
class OpenGraph:
    key: str
    root: Path
    con: sqlite3.Connection
    meta: dict[str, Any]


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def locate(cfg: Config, where: Path) -> dict[str, Any]:
    """The built unit whose root is the deepest one containing `where`."""
    target = _norm(where)
    best: dict[str, Any] | None = None
    for card in store.built_units(cfg):
        root = _norm(card["root"])
        if (target == root or target.startswith(root + os.sep)) and (
                best is None or len(root) > len(_norm(best["root"]))):
            best = card
    if best is None:
        raise GraphNotBuilt(f"no code graph covers {where}; build one with `mb code build` "
                            "from inside the repository (code_graph_enabled must be true)")
    return best


def open_graph(cfg: Config, where: Path) -> OpenGraph:
    card = locate(cfg, where)
    db = store.current_db(cfg, card["key"])
    if db is None:
        raise GraphNotBuilt(f"the graph of {card['key']!r} has no current generation; rebuild it")
    con = store.connect_ro(db)
    return OpenGraph(card["key"], Path(card["root"]), con, store.read_meta(con))


def page(items: list[Any], total: int, offset: int, limit: int) -> dict[str, Any]:
    has_more = offset + len(items) < total
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "has_more": has_more, "next_offset": offset + len(items) if has_more else None,
            "truncation_reason": "limit" if has_more else None}


def envelope(command: str, g: OpenGraph | None, data: dict[str, Any]) -> dict[str, Any]:
    graph = None
    if g is not None:
        graph = {"unit": g.key, "root": str(g.root), "indexed_head": g.meta.get("indexed_head"),
                 "built_at": g.meta.get("built_at"), "versions": g.meta.get("versions"),
                 "schema_version": g.meta.get("schema_version")}
    return {"schema_version": ENVELOPE_SCHEMA, "command": command, "graph": graph,
            # Pre-query refresh arrives in P3. Until then an answer reflects the
            # last build, and says so instead of implying it is current.
            "freshness": {"checked": False, "stale_files": None,
                          "note": "answers reflect the last build; refresh-before-query is not implemented yet"},
            **data}


def _node(row: sqlite3.Row) -> dict[str, Any]:
    return {"qname": row["qname"], "kind": row["kind"], "name": row["name"], "file": row["path"],
            "spans": json.loads(row["spans"]), "signature": row["signature"], "doc1": row["doc1"],
            "exported": bool(row["exported"])}


_NODE_SQL = ("SELECT n.qname, n.kind, n.name, f.path, n.spans, n.signature, n.doc1, n.exported "
             "FROM nodes n JOIN files f ON f.id = n.file_id")


def resolve_symbol(con: sqlite3.Connection, token: str) -> str:
    """A qname, `Class.method`, `path::name` or a bare name -> one qname."""
    row = con.execute("SELECT qname FROM nodes WHERE qname = ?", (token,)).fetchone()
    if row:
        return row[0]
    if "::" in token or "." in token:
        rows = con.execute("SELECT qname FROM nodes WHERE qname LIKE ? ESCAPE '\\' OR qname LIKE ? ESCAPE '\\'",
                           ("%::" + _like(token), "%." + _like(token))).fetchall()
    else:
        rows = con.execute("SELECT qname FROM nodes WHERE name = ?", (token,)).fetchall()
    found = sorted({r[0] for r in rows})
    if not found:
        raise SymbolNotFound(f"no definition named {token!r} in this graph")
    if len(found) > 1:
        raise Ambiguous(token, found)
    return found[0]


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def outline(con: sqlite3.Connection, file: str) -> list[dict[str, Any]]:
    rows = con.execute(_NODE_SQL + " WHERE f.path = ?", (file.replace("\\", "/"),)).fetchall()
    if not rows and not con.execute("SELECT 1 FROM files WHERE path = ?", (file,)).fetchone():
        raise SymbolNotFound(f"{file!r} is not a file of this graph")
    return sorted((_node(r) for r in rows), key=lambda n: (n["spans"][0][0], n["qname"]))


def snippet(g: OpenGraph, qname: str) -> dict[str, Any]:
    row = g.con.execute(_NODE_SQL + " WHERE n.qname = ?", (qname,)).fetchone()
    if row is None:
        raise SymbolNotFound(qname)
    stored = g.con.execute("SELECT content_hash FROM files WHERE path = ?", (row["path"],)).fetchone()[0]
    data = (g.root / row["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != stored:
        raise StaleSource(f"{row['path']} changed since the graph was built; rebuild before reading spans")
    lines = data.decode("utf-8", "replace").splitlines()
    node = _node(row)
    node["code"] = ["\n".join(lines[a - 1:b]) for a, b in node["spans"]]
    return node


def _short_name(qname: str) -> str:
    return qname.split("::", 1)[-1].rsplit(".", 1)[-1]


def callers(con: sqlite3.Connection, qname: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    total = con.execute("SELECT count(*) FROM edges WHERE dst = ? AND kind = 'CALLS'", (qname,)).fetchone()[0]
    rows = con.execute(
        "SELECT e.src, e.tier, f.path, e.line FROM edges e JOIN files f ON f.id = e.file_id "
        "WHERE e.dst = ? AND e.kind = 'CALLS' ORDER BY f.path, e.line LIMIT ? OFFSET ?",
        (qname, limit, offset)).fetchall()
    items = [{"caller": r[0], "tier": r[1], "file": r[2], "line": r[3]} for r in rows]
    name = _short_name(qname)
    cand_total = con.execute("SELECT count(*) FROM unresolved WHERE name = ?", (name,)).fetchone()[0]
    cand = con.execute(
        "SELECT u.src, u.receiver, f.path, u.line FROM unresolved u JOIN files f ON f.id = u.file_id "
        "WHERE u.name = ? ORDER BY f.path, u.line LIMIT ?", (name, limit)).fetchall()
    return {"symbol": qname, "callers": page(items, total, offset, limit),
            "unresolved_candidates": page([{"caller": r[0], "receiver": r[1], "file": r[2], "line": r[3]}
                                           for r in cand], cand_total, 0, limit)}


def callees(con: sqlite3.Connection, qname: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    total = con.execute("SELECT count(*) FROM edges WHERE src = ? AND kind = 'CALLS'", (qname,)).fetchone()[0]
    rows = con.execute(
        "SELECT e.dst, e.tier, e.callee, f.path, e.line FROM edges e JOIN files f ON f.id = e.file_id "
        "WHERE e.src = ? AND e.kind = 'CALLS' ORDER BY e.line LIMIT ? OFFSET ?",
        (qname, limit, offset)).fetchall()
    unresolved = con.execute(
        "SELECT u.name, u.receiver, f.path, u.line FROM unresolved u JOIN files f ON f.id = u.file_id "
        "WHERE u.src = ? ORDER BY u.line LIMIT ?", (qname, limit)).fetchall()
    return {"symbol": qname,
            "callees": page([{"callee": r[0], "tier": r[1], "name": r[2], "file": r[3], "line": r[4]}
                             for r in rows], total, offset, limit),
            "unresolved_calls": [{"name": r[0], "receiver": r[1], "file": r[2], "line": r[3]}
                                 for r in unresolved]}


def trace(con: sqlite3.Connection, qname: str, direction: str = "callers", depth: int = 3,
          limit: int = 500) -> dict[str, Any]:
    """Breadth-first over CALLS, each symbol once at its shallowest depth."""
    depth = max(1, min(depth, MAX_DEPTH))
    near, far = ("dst", "src") if direction == "callers" else ("src", "dst")
    rows = con.execute(
        f"WITH RECURSIVE walk(sym, d) AS ("
        f"  SELECT ?, 0"
        f"  UNION"
        f"  SELECT e.{far}, w.d + 1 FROM edges e JOIN walk w ON e.{near} = w.sym"
        f"  WHERE e.kind = 'CALLS' AND w.d < ?"
        f") SELECT sym, min(d) FROM walk WHERE sym != ? GROUP BY sym ORDER BY min(d), sym LIMIT ?",
        (qname, depth, qname, limit + 1)).fetchall()
    items = [{"symbol": r[0], "depth": r[1]} for r in rows[:limit]]
    result = page(items, len(rows), 0, limit)
    if len(rows) > limit:
        result.update(total=None, truncation_reason="limit")
    return {"symbol": qname, "direction": direction, "depth": depth, "reached": result}


def find(con: sqlite3.Connection, pattern: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    like = "%" + _like(pattern) + "%"
    where = "n.name LIKE ? ESCAPE '\\' OR n.qname LIKE ? ESCAPE '\\'"
    total = con.execute(f"SELECT count(*) FROM nodes n WHERE {where}", (like, like)).fetchone()[0]
    rows = con.execute(_NODE_SQL + f" WHERE {where} ORDER BY (n.name = ?) DESC, n.qname LIMIT ? OFFSET ?",
                       (like, like, pattern, limit, offset)).fetchall()
    return {"pattern": pattern, "matches": page([_node(r) for r in rows], total, offset, limit)}


def schema(g: OpenGraph) -> dict[str, Any]:
    con = g.con
    return {"meta": g.meta,
            "files": dict(con.execute("SELECT lang, count(*) FROM files GROUP BY lang").fetchall()),
            "parse_status": dict(con.execute("SELECT parse_status, count(*) FROM files GROUP BY parse_status")),
            "nodes": dict(con.execute("SELECT kind, count(*) FROM nodes GROUP BY kind").fetchall()),
            "edges": dict(con.execute("SELECT kind, count(*) FROM edges GROUP BY kind").fetchall()),
            "call_tiers": dict(con.execute(
                "SELECT tier, count(*) FROM edges WHERE kind = 'CALLS' GROUP BY tier").fetchall()),
            "unresolved": con.execute("SELECT count(*) FROM unresolved").fetchone()[0]}

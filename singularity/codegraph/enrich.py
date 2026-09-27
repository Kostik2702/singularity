"""What the code graph knows about a code search hit (CODE-GRAPH P7, decision
D1 row "Search").

`mb search --with-code --graph` takes the daemon's answer as it is and then,
in the CLI process, attaches to every code hit the definition it falls in and
that definition's callers, callees and unresolved same-name call sites. The
search itself is not touched: without `--graph` this module is never
imported, and with it the hit list, its order and its scores are exactly what
the daemon returned (D2).

Read-only and cheap by construction:
* the graph is read as last written. There is no refresh before the answer --
  that is `mb code`'s job, and a search must not write -- but what the graph
  itself knows to be stale is still said (`update.known_stale`);
* no parser and no numpy: the standard library, sqlite3 and the graph's own
  readers. The import-floor test runs this path and pins it;
* a hit and a graph row are joined only when they describe the same version
  of the file. The code index remembers each file the way
  `indexer._file_stat` recorded it, `[int(st_mtime * 1000), size]` in its
  `file_meta`; the graph remembers `(size, mtime_ns)`. The graph's nanoseconds
  are rounded the way `_file_stat` rounds (`stat_ms`). When the two disagree,
  or the graph recorded no mtime (a racy stat, see store.RACY_NS), the file on
  disk arbitrates: if the index's stat is the disk's and the graph's content
  hash is the disk's, both hold the disk's version. Otherwise the hit's graph
  data is marked stale -- two versions are never silently mixed.

Honesty (UNMET h): a hit whose file is in no built graph, or not among its
unit's files, says OUTSIDE; "0 callers" would claim a fact the graph does not
hold. A hit that falls in no definition (module-level code) carries no counts
at all rather than zeros.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from . import query, update
from .scope import GraphError

OUTSIDE = "outside graph universe"


def stat_ms(mtime_ns: int) -> int:
    """`indexer._file_stat`'s mtime from nanoseconds: `int(st_mtime * 1000)`,
    where CPython builds `st_mtime` as `sec + nsec * 1e-9` in double precision.
    A double near 1.8e9 s resolves ~240 ns, so an mtime 100-200 ns short of a
    millisecond lands on it: truncating the nanoseconds instead (`// 10**6`)
    would call such a file different from itself. Tests compare the two on
    real stats."""
    sec, nsec = divmod(mtime_ns, 1_000_000_000)
    return int((sec + nsec * 1e-9) * 1000)


def committed_metadata(index_dir: Path) -> Path | None:
    """The `metadata.json` a reader of the index uses: CURRENT's generation,
    else the newest whole one, else the legacy flat layout. This is
    `indexer.index_files` re-stated, because the indexer imports numpy and
    this path must not; a test pins the two to the same file."""
    def whole(d: Path) -> bool:
        return (d / "embeddings.npy").exists() and (d / "metadata.json").exists()

    try:
        name = (index_dir / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    if name and whole(index_dir / name):
        return index_dir / name / "metadata.json"
    for gen in sorted(index_dir.glob("gen_*"), reverse=True):
        if whole(gen):
            return gen / "metadata.json"
    if whole(index_dir):
        return index_dir / "metadata.json"
    return None


_HEAD = '{"model": '
_FILE_META_KEY = ', "file_meta": '
_FIRST_READ = 1 << 17            # 128 KiB: the live index's file_meta is ~90 KiB


def _head_file_meta(text: str) -> dict[str, list[int]] | None:
    """`file_meta` when `text` starts with the writer's shape -- `model`, then
    `file_meta` (indexer._rebuild) -- else None. Raises ValueError when the
    text ends before `file_meta` does."""
    if not text.startswith(_HEAD):
        if _HEAD.startswith(text):
            raise ValueError("the head ends before `model`")
        return None
    decoder = json.JSONDecoder()
    _, end = decoder.raw_decode(text, len(_HEAD))
    if not text.startswith(_FILE_META_KEY, end):
        if end + len(_FILE_META_KEY) > len(text):
            raise ValueError("the head ends before file_meta")
        return None
    value, _ = decoder.raw_decode(text, end + len(_FILE_META_KEY))
    return value if isinstance(value, dict) else None


def parse_file_meta(text: str) -> dict[str, list[int]]:
    """`file_meta` out of a whole metadata.json text: from its head when it
    has the writer's shape, else by a full parse."""
    try:
        found = _head_file_meta(text)
        if found is not None:
            return found
    except ValueError:
        pass
    data = json.loads(text)
    found = data.get("file_meta") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def index_file_meta(index_dir: Path) -> dict[str, list[int]]:
    """The committed code index's `file_meta`. The file is megabytes of
    chunks (16 MB live) behind a head of ~90 KiB that holds `file_meta`, so
    only the head is read, doubled until `file_meta` decodes; reading and
    decoding the whole file was 49 of the 63 ms the enrichment took on the
    live vault. Any other shape is parsed whole."""
    meta = committed_metadata(index_dir)
    if meta is None:
        return {}
    try:
        with open(meta, "rb") as fh:
            raw, eof = fh.read(_FIRST_READ), False
            while True:
                # a multi-byte character cut by the window's edge waits for the next read
                text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=eof)
                try:
                    found = _head_file_meta(text)
                except ValueError:                  # the window ends inside the head
                    if not eof:
                        more = fh.read(len(raw))    # double it
                        eof = not more
                        raw += more
                        continue
                    found = None
                if found is not None:
                    return found
                if not eof:
                    raw += fh.read()
                return parse_file_meta(raw.decode("utf-8"))
    except (OSError, ValueError):                   # UnicodeDecodeError included
        return {}


@dataclass
class _Span:
    qname: str
    name: str
    kind: str
    start: int
    end: int


def definition_for(con: sqlite3.Connection, file_id: int, line_start: Any, line_end: Any,
                   name: Any) -> _Span | None:
    """The definition a hit's line range is about.

    The chunker starts a chunk on a `def`/`class` line pulled up over its
    decorators, and runs it on to just before the next symbol, trailing blank
    lines and module-level code included; the graph's span starts at the
    `def` itself. So a strict "covers the whole range" test would hand a
    decorated method's chunk to its class. First, then, the definition OF THE
    CHUNK'S NAME that starts inside the range (the outermost, if several do);
    failing that, the innermost definition covering the whole range; failing
    that, none."""
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        return None
    spans = [_Span(q, n, k, a, b)
             for q, n, k, raw in con.execute("SELECT qname, name, kind, spans FROM nodes WHERE file_id = ?",
                                             (file_id,))
             for a, b in json.loads(raw)]
    named = [s for s in spans if name and s.name == name and line_start <= s.start <= line_end]
    if named:
        return min(named, key=lambda s: (s.start, s.start - s.end))
    covering = [s for s in spans if s.start <= line_start and line_end <= s.end]
    if covering:
        return min(covering, key=lambda s: (s.end - s.start, -s.start))
    return None


@dataclass
class _Unit:
    key: str
    graph: query.OpenGraph | None
    real_root: str
    stale: set[str] = field(default_factory=set)
    stale_all: str | None = None
    unavailable: str | None = None


class _Session:
    """One search's worth of graph reads: each unit opened once, the index's
    file_meta read once, everything closed at the end."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._units: dict[str, _Unit] = {}
        self._cards: dict[str, dict[str, Any] | None] = {}
        self._file_meta: dict[str, list[int]] | None = None

    def card(self, disk: Path) -> dict[str, Any] | None:
        """The built unit a file belongs to, or None. `query.locate` reads
        every unit's card on each call; a unit root is a directory, so the
        files of one directory share the answer and it is asked once."""
        where = str(disk.parent)
        if where not in self._cards:
            try:
                self._cards[where] = query.locate(self.cfg, disk.parent)
            except query.GraphNotBuilt:
                self._cards[where] = None
        return self._cards[where]

    def close(self) -> None:
        for unit in self._units.values():
            if unit.graph is not None:
                unit.graph.con.close()

    def file_meta(self) -> dict[str, list[int]]:
        if self._file_meta is None:
            self._file_meta = index_file_meta(self.cfg.code_index_dir)
        return self._file_meta

    def unit(self, card: dict[str, Any]) -> _Unit:
        key = card["key"]
        if key not in self._units:
            real_root = os.path.realpath(str(card["root"]))
            try:
                g = query.open_card(self.cfg, card)
            except (GraphError, sqlite3.Error) as exc:
                self._units[key] = _Unit(key, None, real_root, unavailable=str(exc))
            else:
                try:
                    known = update.known_stale(g.con)
                except sqlite3.Error as exc:
                    g.con.close()
                    self._units[key] = _Unit(key, None, real_root, unavailable=str(exc))
                else:
                    self._units[key] = _Unit(key, g, real_root, set(known.stale_files), known.stale_all)
        return self._units[key]

    def versions_differ(self, index_rel: str, disk: Path, row: sqlite3.Row) -> str | None:
        """Why the index's and the graph's versions of the file may differ, or
        None when they are the same (see the module docstring)."""
        entry = self.file_meta().get(index_rel)
        if not (isinstance(entry, list) and len(entry) == 2):
            return "the code index does not list this file"
        index_ms, index_size = entry
        if row["mtime_ns"] and row["size"] == index_size and stat_ms(row["mtime_ns"]) == index_ms:
            return None
        try:
            st = disk.stat()
            if [stat_ms(st.st_mtime_ns), st.st_size] == entry and \
                    hashlib.sha256(disk.read_bytes()).hexdigest() == row["content_hash"]:
                return None
        except OSError:
            pass
        if not row["mtime_ns"] and row["size"] == index_size:
            return "cannot tell whether the code index and the graph hold the same version of this file"
        return "the code index and the graph hold different versions of this file"

    def describe(self, meta: dict[str, Any]) -> dict[str, Any] | str:
        index_rel = meta.get("file")
        base = self.cfg.code_repos_dir
        if not isinstance(index_rel, str) or not index_rel or base is None:
            return OUTSIDE
        disk = base / index_rel
        card = self.card(disk)
        if card is None:
            return OUTSIDE
        unit = self.unit(card)
        if unit.graph is None:
            return {"unit": unit.key, "unavailable": unit.unavailable}
        try:
            rel = os.path.relpath(os.path.realpath(str(disk)), unit.real_root).replace(os.sep, "/")
        except ValueError:                     # another drive
            return OUTSIDE
        if rel.startswith("../"):
            return OUTSIDE
        con = unit.graph.con
        try:
            row = con.execute("SELECT id, size, mtime_ns, content_hash FROM files WHERE path = ?",
                              (rel,)).fetchone()
            if row is None:
                return OUTSIDE
            reasons = []
            if unit.stale_all:
                reasons.append(f"the graph may be stale: {unit.stale_all}")
            if rel in unit.stale:
                reasons.append("the graph marks this file stale")
            differ = self.versions_differ(index_rel, disk, row)
            if differ:
                reasons.append(differ)
            a, b = meta.get("line_start"), meta.get("line_end")
            found = definition_for(con, row["id"], a, b, meta.get("name"))
            out: dict[str, Any] = {"unit": unit.key, "file": rel}
            if found is None:
                out.update(qname=None, kind=None, callers=None, unresolved_candidates=None,
                           callees=None, unresolved_calls=None,
                           note=f"no definition covers lines {a}-{b} (module-level code)")
            else:
                # counts only (limit 0), with `mb code callers`'s own meaning of
                # a candidate; `unresolved_calls` is the callee side of the same
                # honesty -- `callees 0` beside 3 unplaced calls is not "calls nothing"
                up = query.callers(con, found.qname, limit=0)
                down = query.callees(con, found.qname, limit=0)
                unplaced = con.execute("SELECT count(*) FROM unresolved WHERE src = ?",
                                       (found.qname,)).fetchone()[0]
                out.update(qname=found.qname, kind=found.kind, callers=up["callers"]["total"],
                           unresolved_candidates=up["unresolved_candidates"]["total"],
                           callees=down["callees"]["total"], unresolved_calls=unplaced)
        except sqlite3.Error as exc:
            return {"unit": unit.key, "unavailable": str(exc)}
        out["stale"] = bool(reasons)
        if reasons:
            out["stale_reason"] = "; ".join(reasons)
        return out


def enrich(cfg: Config, results: list[dict[str, Any]]) -> None:
    """Add `graph` to every code hit of the daemon's `--json` results, in
    place: a dict of what the graph says, or OUTSIDE."""
    hits = [r for r in results if r.get("source") == "code"]
    if not hits:
        return
    session = _Session(cfg)
    try:
        for hit in hits:
            hit["graph"] = session.describe(hit.get("meta") or {})
    finally:
        session.close()

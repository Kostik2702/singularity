"""`mb search --graph-boost`: re-rank code hits by how much the code calls them
(CODE-GRAPH D1, search row: "boost later: its own flag, a new compare mode,
the owner's signature"). OFF unless the flag is given; the owner decides from
`tools/code_index_baseline.py compare-boost` whether it ever becomes a default.

The signal is the number of DISTINCT resolved callers of the hit's innermost
symbol: a function the rest of the code calls from many places is more often
the answer to "where is X done" than a one-off helper with the same words.
Distinct callers, not call edges: one caller looping over a helper is one
reason to read it, not ten.

The formula, fixed before any measurement:

    score_i = (1 + ALPHA * s_i) / (RRF_K + rank_i)
    s_i     = min(1, log2(1 + callers_i) / log2(1 + SATURATION))

* `rank_i` is the hit's position in the answer as the engine returned it, over
  all corpora. Positions, not scores: cosine and fused scores live on scales
  that would each need their own constant (the reason the engine fuses by
  rank, `lexical.rrf`), and `RRF_K` is that fusion's own constant.
* `s_i` is 0 for a note, for code outside every graph, for a file that changed
  since its graph was built, and for module-level lines. Unknown is NEUTRAL,
  never "0 callers" (D1: absence of an edge is not absence of a relation) --
  such a hit keeps its place and is only passed by a boosted neighbour.
* log2 with saturation at 16 callers: a hub called from hundreds of places
  (a logger, a config getter) must not outrank every specific answer, so the
  signal flattens where it stops meaning "central" and starts meaning
  "plumbing".
* ALPHA = 0.05 bounds the move. A hit at rank r with the full boost passes the
  hit at rank q < r only if r - q < ALPHA * (RRF_K + q): at most 3 places for
  any q up to 19. The boost reorders near neighbours; it cannot lift a weak
  hit from the bottom, and it never adds or removes a hit -- the answer holds
  exactly the engine's hits, so with every signal 0 it IS the engine's answer.

Read-only and `--no-refresh` by construction: nothing here updates a graph,
imports a parser, or writes a file. A graph is opened `mode=ro`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..config import Config

ALPHA = 0.05
SATURATION = 16
RRF_K = 60            # `lexical.rrf`'s k: the engine's own rank-fusion constant

OK = "ok"
OUTSIDE = "outside graph universe"
STALE = "stale"
MODULE = "module level"


def strength(callers: int | None) -> float:
    """0..1, saturating at SATURATION distinct callers."""
    if not callers or callers <= 0:
        return 0.0
    return min(1.0, math.log2(1 + callers) / math.log2(1 + SATURATION))


def boosted_order(strengths: list[float]) -> list[int]:
    """Indices of the hits in their boosted order. Ties keep the engine's order."""
    keyed = [((1 + ALPHA * s) / (RRF_K + rank), -rank, i)
             for i, (rank, s) in enumerate(zip(range(1, len(strengths) + 1), strengths,
                                               strict=True))]
    return [i for _, _, i in sorted(keyed, reverse=True)]


class GraphIndex:
    """The built graphs of this machine, opened lazily and read-only."""

    def __init__(self, cfg: Config):
        from ..codegraph import store

        self.cfg = cfg
        self._store = store
        cards = store.built_units(cfg)
        # deepest root first: a nested unit owns its files, not its parent
        self.units = sorted(((os.path.realpath(c["root"]), c) for c in cards),
                            key=lambda rc: len(rc[0]), reverse=True)
        self._cons: dict[str, sqlite3.Connection | None] = {}

    def close(self) -> None:
        for con in self._cons.values():
            if con is not None:
                con.close()

    def _con(self, key: str) -> sqlite3.Connection | None:
        if key not in self._cons:
            db = self._store.current_db(self.cfg, key)
            self._cons[key] = self._store.connect_ro(db) if db is not None else None
        return self._cons[key]

    def locate(self, path: Path) -> tuple[sqlite3.Connection, str] | None:
        """(connection, unit-relative path) of the unit holding `path`."""
        real = os.path.realpath(str(path))
        for root, card in self.units:
            if os.path.normcase(real).startswith(os.path.normcase(root) + os.sep):
                con = self._con(card["key"])
                if con is None:
                    return None
                return con, real[len(root) + 1:].replace(os.sep, "/")
        return None

    def signal(self, meta: dict[str, Any]) -> tuple[int | None, str, str | None]:
        """(distinct callers, state, symbol) for one code hit."""
        root = self.cfg.code_repos_dir
        file = meta.get("file")
        if root is None or not file:
            return None, OUTSIDE, None
        path = root / file
        found = self.locate(path)
        if found is None:
            return None, OUTSIDE, None
        con, rel = found
        row = con.execute("SELECT id, size, mtime_ns, content_hash FROM files WHERE path = ?",
                          (rel,)).fetchone()
        if row is None:
            return None, OUTSIDE, None
        if (con.execute("SELECT 1 FROM stale WHERE path = ?", (rel,)).fetchone()
                or not _unchanged(path, row["size"], row["mtime_ns"], row["content_hash"])):
            return None, STALE, None
        symbol = _symbol(con, row["id"], rel, meta)
        if symbol is None:
            return None, MODULE, None
        callers = con.execute("SELECT count(DISTINCT src) FROM edges WHERE dst = ? AND kind = 'CALLS'",
                              (symbol,)).fetchone()[0]
        return int(callers), OK, symbol


def _unchanged(path: Path, size: int, mtime_ns: int, digest: str) -> bool:
    """The graph's own freshness rule: stat, then content hash."""
    try:
        st = path.stat()
        if st.st_size == size and st.st_mtime_ns == mtime_ns:
            return True
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def _symbol(con: sqlite3.Connection, file_id: int, rel: str, meta: dict[str, Any]) -> str | None:
    """The innermost definition a chunk is about.

    A v2 chunk names it (`qname`, whose path is corpus-relative; the graph's
    is unit-relative, so only the part after `::` is carried over). A v1
    chunk has only its lines: the first definition that STARTS inside them is
    the chunk's own symbol (its window opens on the decorators or the
    signature); failing that, the innermost definition containing its first
    line (a window deep inside a long body)."""
    qname = meta.get("qname")
    if isinstance(qname, str) and "::" in qname:
        row = con.execute("SELECT qname FROM nodes WHERE qname = ?",
                          (f"{rel}::{qname.split('::', 1)[1]}",)).fetchone()
        if row is not None:
            return row[0]
    first, last = meta.get("line_start"), meta.get("line_end")
    if not isinstance(first, int) or not isinstance(last, int):
        return None
    spans = [(q, a, b) for q, raw in con.execute("SELECT qname, spans FROM nodes WHERE file_id = ?",
                                                 (file_id,))
             for a, b in json.loads(raw)]
    starting = sorted((a, q) for q, a, _ in spans if first <= a <= last)
    if starting:
        return starting[0][1]
    containing = sorted((b - a, q) for q, a, b in spans if a <= first <= b)
    return containing[0][1] if containing else None


def apply(cfg: Config, resp: dict[str, Any], *, explain: bool = False,
          as_lines: bool = False) -> dict[str, Any]:
    """Re-rank a `format: json` search answer in place; optionally render it
    as the compact lines the daemon would have returned."""
    results = list(resp.get("results") or [])
    graphs = GraphIndex(cfg)
    try:
        facts = [graphs.signal(r.get("meta") or {}) if r.get("source") == "code"
                 else (None, None, None) for r in results]
    finally:
        graphs.close()
    strengths = [strength(callers) if state == OK else 0.0 for callers, state, _ in facts]
    order = boosted_order(strengths)
    if explain:
        for r, (callers, state, symbol), s in zip(results, facts, strengths, strict=True):
            if state is not None:
                r.setdefault("explain", {})["graph"] = {
                    "state": state, "symbol": symbol, "callers": callers,
                    "factor": round(1 + ALPHA * s, 4)}
    ranked = [results[i] for i in order]
    counts: dict[str, int] = {}
    for _, state, _ in facts:
        if state is not None:
            counts[state] = counts.get(state, 0) + 1
    resp["graph_boost"] = {"alpha": ALPHA, "saturation": SATURATION, "k": RRF_K,
                           "code_hits": sum(counts.values()), **counts,
                           "moved": sum(1 for pos, i in enumerate(order) if pos != i)}
    unknown = counts.get(OUTSIDE, 0) + counts.get(STALE, 0)
    if unknown:
        resp["graph_boost_warning"] = (
            f"{unknown} code hit(s) have no current graph ({OUTSIDE} or {STALE}); they keep "
            f"their place -- `mb code build` / `mb code update` in their repository")
    if as_lines:
        from .engine import SearchEngine

        resp.pop("results", None)
        resp["lines"] = [SearchEngine._compact_line(r) for r in ranked]
    else:
        resp["results"] = ranked
    return resp

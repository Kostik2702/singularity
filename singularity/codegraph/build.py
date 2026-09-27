"""Full build of one unit's graph (CODE-GRAPH P1).

scope -> read + hash -> extract -> resolve -> a new store generation. Nothing
is written until everything is known: a scan that could not read part of the
unit, or a failure anywhere before the final transaction, leaves the current
generation exactly as it was.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from itertools import repeat
from pathlib import Path
from typing import Any

from ..config import Config
from . import arch, audit, extract_py, extract_ts, fresh, scope, sidecar, store
from .model import FileFacts, encode_facts
from .resolve_py import PyResolver
from .resolve_ts import TsResolver, load_workspace
from .rows import dep_rows, name_ref_counts, node_rows, workspace_meta
from .versions import RESOLVER_VERSION, parser_versions, version_mismatch  # noqa: F401  (re-exported)

# Extraction is per file and CPU-bound, so a large unit is parsed in a process
# pool (B7: 222 s -> 19 s on the 44k-file proxy with 16 workers). Below
# _POOL_MIN_FILES the pool costs more than it saves: every worker is spawned
# and re-imports tree-sitter and the grammars.
_POOL_MIN_FILES = 2000
_MAX_WORKERS = 16
_MAX_BATCH = 250


def _read_text(unit: scope.Unit):
    def read(rel: str) -> str | None:
        try:
            return (unit.root / rel).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None
    return read


def extract_file(root: Path, path: str) -> tuple[str, FileFacts, str, bytes]:
    """(lang, facts, content hash, stored facts) of one file."""
    data = (root / path).read_bytes()
    if path.endswith(".py"):
        lang, facts = "python", extract_py.extract(path, data)
    else:
        lang, facts = "ts", extract_ts.extract(path, data)
    return lang, facts, hashlib.sha256(data).hexdigest(), encode_facts(facts)


def _extract_batch(root: str, paths: list[str]) -> list[tuple[str, FileFacts, str, bytes]]:
    """`extract_file` for each path, in order. Module level: a pool worker
    finds it by name."""
    return [extract_file(Path(root), path) for path in paths]


def watched(unit: scope.Unit, cfg: Config) -> dict[str, list]:
    """{path: [size, mtime_ns, sha256]} of the files that shape the unit's
    universe or its TypeScript resolution; a change to one is a rebuild
    (see fresh). Stat before read, like every file of the graph."""
    out: dict[str, list] = {}
    for rel in scope.list_manifests(unit, cfg, include_gitignore=unit.is_git):
        try:
            st = (unit.root / rel).stat()
            digest = hashlib.sha256((unit.root / rel).read_bytes()).hexdigest()
        except OSError:
            continue
        out[rel] = [st.st_size, store.recorded_mtime(st.st_mtime_ns, time.time_ns()), digest]
    return out


def _communities(cfg: Config, key: str, paths: list[str], edges) -> tuple[dict[str, int], dict[int, str], str | None]:
    """Louvain over the file graph, numbered against the current generation
    (see arch). Without networkx the build goes on and `arch` says why it is empty."""
    previous: dict[str, int] = {}
    db = store.current_db(cfg, key)
    if db is not None:
        con = store.connect_ro(db)
        try:
            previous = dict(con.execute(
                "SELECT f.path, c.community FROM communities c JOIN files f ON f.id = c.file_id").fetchall())
        except sqlite3.OperationalError:          # a generation from before communities existed
            pass
        finally:
            con.close()
    try:
        assign, names = arch.communities(paths, arch.file_weights((e.src, e.dst) for e in edges), previous)
    except ImportError:
        return {}, {}, "networkx is not installed in the interpreter that built this graph"
    return assign, names, None


def _dirty(unit: scope.Unit, cfg: Config, *states: fresh.GitState) -> list[str]:
    skip, vendored = scope._skip_dirs(cfg), scope._vendored(cfg)
    listed = set().union(*(s.listed for s in states))
    return sorted(p for p in listed if scope._wanted(p, skip, vendored, unit.nested)
                  or fresh.watchable_path(p, True, skip, vendored, unit.nested))


def _worker_count(n_files: int, workers: int | None) -> int:
    if workers is not None:
        return max(1, workers)
    if n_files < _POOL_MIN_FILES:
        return 1
    return max(1, min(_MAX_WORKERS, (os.cpu_count() or 2) - 1))


def _extract_all(root: Path, paths: list[str], workers: int) -> list[tuple[str, FileFacts, str, bytes]]:
    """Same result, same order, whatever `workers` is. A worker that fails
    (a file deleted since the scan, a killed process) fails the build before
    anything is written, exactly as the serial loop does."""
    if workers == 1:
        return _extract_batch(str(root), paths)
    size = max(1, min(_MAX_BATCH, -(-len(paths) // (workers * 4))))
    batches = [paths[i:i + size] for i in range(0, len(paths), size)]
    with ProcessPoolExecutor(workers) as pool:
        return [r for part in pool.map(_extract_batch, repeat(str(root)), batches) for r in part]


def build_unit(cfg: Config, unit: scope.Unit, workers: int | None = None,
               locked: bool = False) -> dict[str, Any]:
    """`workers`: None sizes the pool by the unit (serial below
    _POOL_MIN_FILES); a number forces it -- tests force 2 on a small unit.
    `locked`: the caller already holds the unit's build lock (`mb code update`
    falling back to a rebuild)."""
    started = time.perf_counter()
    timings: dict[str, float] = {}
    with nullcontext() if locked else store.build_lock(cfg, unit.key):
        # Git's view before the scan and after the reads: HEAD from the first
        # (a commit during the build is caught by the next query's diff), the
        # dirty set from both (a file dirtied while the build ran is listed).
        before = fresh.git_state(unit.root) if unit.is_git else None
        t = time.perf_counter()
        entries = scope.list_files(unit, cfg)
        watch = watched(unit, cfg)
        timings["scan"] = time.perf_counter() - t
        t = time.perf_counter()
        file_rows: list[dict[str, Any]] = []
        facts_rows: list[tuple[int, bytes]] = []
        py_facts: dict[str, FileFacts] = {}
        ts_facts: dict[str, FileFacts] = {}
        langs: Counter[str] = Counter()
        n_workers = _worker_count(len(entries), workers)
        extracted = _extract_all(unit.root, [e.path for e in entries], n_workers)
        read_done = time.time_ns()
        for fid, (entry, (lang, facts, digest, blob)) in enumerate(zip(entries, extracted, strict=True), 1):
            (py_facts if lang == "python" else ts_facts)[entry.path] = facts
            langs[lang] += 1
            file_rows.append({"id": fid, "path": entry.path, "lang": lang, "size": entry.size,
                              "mtime_ns": store.recorded_mtime(entry.mtime_ns, read_done),
                              "content_hash": digest,
                              "parse_status": "partial" if facts.degraded else "ok",
                              "is_test": int(entry.is_test)})
            facts_rows.append((fid, blob))
        after = fresh.git_state(unit.root) if unit.is_git else None
        timings["extract"] = time.perf_counter() - t
        t, cpu = time.perf_counter(), time.process_time()
        test_paths = {e.path for e in entries if e.is_test}
        py = PyResolver(py_facts, test_paths=test_paths)
        edges, unresolved = py.resolve()
        file_deps = dict(py.file_deps)
        packages, aliases = load_workspace(_read_text(unit), [p for p in watch if fresh.WATCHED.match(
            p.rsplit("/", 1)[-1])])
        if ts_facts:
            ts = TsResolver(ts_facts, test_paths, packages, aliases)
            ts_edges, ts_unresolved = ts.resolve()
            edges += ts_edges
            unresolved += ts_unresolved
            file_deps.update(ts.file_deps)
        timings["resolve"] = time.perf_counter() - t
        # Resolve is one CPU-bound thread in this process: well below 1 means the
        # process was kept waiting and the timings measure the machine.
        resolve_cpu_over_wall = (time.process_time() - cpu) / max(timings["resolve"], 1e-9)
        t = time.perf_counter()
        comm, comm_labels, arch_note = _communities(cfg, unit.key, [r["path"] for r in file_rows], edges)
        timings["arch"] = time.perf_counter() - t
        t = time.perf_counter()
        file_id = {r["path"]: r["id"] for r in file_rows}
        nodes = [n for facts_by_path in (py_facts, ts_facts) for f in facts_by_path.values()
                 for n in node_rows(f, file_id[f.path])]
        edge_rows = [(e.src, e.dst, e.kind, e.tier, e.callee, file_id[e.file], e.line) for e in edges]
        unresolved_rows = [(u.src, u.name, u.receiver, file_id[u.file], u.line) for u in unresolved]
        deps, lookups = dep_rows(file_deps, file_id)
        statuses = Counter(r["parse_status"] for r in file_rows)
        meta = {"unit": unit.key, "root": str(unit.root), "is_git": unit.is_git,
                "nested": list(unit.nested), "indexed_head": before.head if before else None,
                "versions": parser_versions(), "scope": fresh.scope_fingerprint(cfg),
                "watched": watch, "workspace": workspace_meta(packages, aliases),
                "built_at": time.time(), "files": len(file_rows),
                "arch": {"seed": arch.SEED, "computed_at_seq": 0, "note": arch_note}}
        refs = name_ref_counts(list(py_facts.values()) + list(ts_facts.values()))
        written = store.write_generation(
            cfg, unit.key, meta=meta, files=file_rows, nodes=nodes, edges=edge_rows,
            unresolved=unresolved_rows, facts=facts_rows, deps=deps, lookups=lookups,
            dirty=_dirty(unit, cfg, before, after) if before and after else [],
            name_refs=[(lang, name, n) for (lang, name), n in sorted(refs.items())],
            communities=[(file_id[p], c) for p, c in sorted(comm.items())],
            community_labels=sorted(comm_labels.items()))
        timings["write"] = time.perf_counter() - t
        # The secret audit is part of every full build (CODE-GRAPH D1): it reads what was
        # just written and keeps the count in meta for doctor K2 (see audit).
        t = time.perf_counter()
        audited = audit.audit_db(store.unit_dir(cfg, unit.key) / written["generation"] / "graph.db")
        timings["audit"] = time.perf_counter() - t
        # last, so the hooks' sidecar describes the generation as it finally stands
        sidecar.record(store.unit_dir(cfg, unit.key) / written["generation"] / "graph.db")
    tiers = Counter(e.tier for e in edges if e.kind == "CALLS")
    return {"unit": unit.key, "files": len(file_rows), "by_lang": dict(langs),
            "parse_status": dict(statuses), "nodes": len(nodes),
            "deps": len(deps), "lookups": len(lookups),
            "edges": dict(Counter(e.kind for e in edges)), "call_tiers": dict(tiers),
            "unresolved": len(unresolved_rows), "workers": n_workers,
            "audit": {"findings": audited["count"], "by_kind": audited["by_kind"], "recorded": audited["recorded"]},
            "resolve_cpu_over_wall": round(resolve_cpu_over_wall, 2),
            "seconds": round(time.perf_counter() - started, 2),
            "timings": {k: round(v, 2) for k, v in timings.items()}, **written}

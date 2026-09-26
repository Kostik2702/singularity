"""Full build of one unit's graph (CODE-GRAPH P1).

scope -> read + hash -> extract -> resolve -> a new store generation. Nothing
is written until everything is known: a scan that could not read part of the
unit, or a failure anywhere before the final transaction, leaves the current
generation exactly as it was.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from ..config import Config
from . import extract_py, scope, store
from .model import FileFacts
from .resolve_py import PyResolver

# Bumped on any change that can move an edge: a graph built by resolver N is
# never extended by resolver N+1 (UNMET o). 2: name-unique skips test files and
# function-local definitions (independent audit of P1a output, 2026-09-26).
RESOLVER_VERSION = 2
_PARSER_PACKAGES = ("tree-sitter", "tree-sitter-python", "tree-sitter-typescript",
                    "tree-sitter-javascript")


def parser_versions() -> dict[str, str]:
    """Recorded in every graph. A writer whose versions differ is not allowed
    to mix its output into this graph (UNMET o) -- see `version_mismatch`."""
    out = {}
    for pkg in _PARSER_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "absent"
    out["resolver"] = str(RESOLVER_VERSION)
    return out


def version_mismatch(meta: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """{package: (recorded, running)} for every version that differs."""
    recorded = meta.get("versions") or {}
    running = parser_versions()
    return {k: (recorded.get(k, "absent"), v) for k, v in running.items() if recorded.get(k) != v}


def _git_head(unit: scope.Unit) -> str | None:
    if not unit.is_git:
        return None
    done = subprocess.run(["git", "--no-optional-locks", "-C", str(unit.root), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if done.returncode != 0:          # a repository with no commit yet
        return None
    return done.stdout.strip() or None


def build_unit(cfg: Config, unit: scope.Unit) -> dict[str, Any]:
    started = time.perf_counter()
    with store.build_lock(cfg, unit.key):
        head = _git_head(unit)                         # before the scan: a commit during it re-detects
        entries = scope.list_files(unit, cfg)
        file_rows: list[dict[str, Any]] = []
        facts: dict[str, FileFacts] = {}
        langs: Counter[str] = Counter()
        for fid, entry in enumerate(entries, 1):
            data = (unit.root / entry.path).read_bytes()
            lang = "python" if entry.path.endswith(".py") else "ts"
            status = "skipped"                          # TS arrives in P1b
            if lang == "python":
                f = extract_py.extract(entry.path, data)
                facts[entry.path] = f
                status = "partial" if f.degraded else "ok"
            langs[lang] += 1
            file_rows.append({"id": fid, "path": entry.path, "lang": lang, "size": entry.size,
                              "mtime_ns": entry.mtime_ns,
                              "content_hash": hashlib.sha256(data).hexdigest(),
                              "parse_status": status, "is_test": int(entry.is_test)})
        test_paths = {e.path for e in entries if e.is_test}
        edges, unresolved = PyResolver(facts, test_paths=test_paths).resolve()
        file_id = {r["path"]: r["id"] for r in file_rows}
        node_rows = [{"qname": d.qname, "name": d.name, "kind": d.kind, "file_id": file_id[f.path],
                      "parent": d.parent, "spans": json.dumps(d.spans), "signature": d.signature,
                      "doc1": d.doc1, "decorators": json.dumps(d.decorators), "exported": int(d.exported)}
                     for f in facts.values() for d in f.definitions]
        edge_rows = [(e.src, e.dst, e.kind, e.tier, e.callee, file_id[e.file], e.line) for e in edges]
        unresolved_rows = [(u.src, u.name, u.receiver, file_id[u.file], u.line) for u in unresolved]
        statuses = Counter(r["parse_status"] for r in file_rows)
        meta = {"unit": unit.key, "root": str(unit.root), "is_git": unit.is_git,
                "indexed_head": head, "versions": parser_versions(),
                "built_at": time.time(), "files": len(file_rows)}
        written = store.write_generation(cfg, unit.key, meta=meta, files=file_rows, nodes=node_rows,
                                         edges=edge_rows, unresolved=unresolved_rows)
    tiers = Counter(e.tier for e in edges if e.kind == "CALLS")
    return {"unit": unit.key, "files": len(file_rows), "by_lang": dict(langs),
            "parse_status": dict(statuses), "nodes": len(node_rows),
            "edges": dict(Counter(e.kind for e in edges)), "call_tiers": dict(tiers),
            "unresolved": len(unresolved_rows), "seconds": round(time.perf_counter() - started, 2),
            **written}

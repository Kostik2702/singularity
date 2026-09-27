"""The stored form of what the build and an update write, shared by both.
Parser-free: an update that only deletes files or moves the snapshot's
bookkeeping runs without tree-sitter in the interpreter.
"""
from __future__ import annotations

import json
from typing import Any

from .model import FileFacts
from .resolve_py import CONTAINERS
from .resolve_ts import _TYPELIKE as TS_CONTAINERS
from .resolve_ts import Package
from .tables import is_local


def node_rows(f: FileFacts, file_id: int) -> list[dict[str, Any]]:
    containers = CONTAINERS if f.lang == "python" else TS_CONTAINERS
    defs = {d.qname: d for d in f.definitions}
    return [{"qname": d.qname, "name": d.name, "kind": d.kind, "file_id": file_id, "parent": d.parent,
             "spans": json.dumps(d.spans), "signature": d.signature, "doc1": d.doc1,
             "decorators": json.dumps(d.decorators), "exported": int(d.exported),
             "local": int(is_local(defs, d.qname, containers))} for d in f.definitions]


def dep_rows(file_deps: dict[str, tuple[frozenset[str], frozenset[str]]],
             file_id: dict[str, int]) -> tuple[list[tuple], list[tuple]]:
    """(deps, lookups) rows from a resolver's `file_deps`."""
    deps = [(file_id[p], d) for p, (ds, _) in file_deps.items() for d in sorted(ds)]
    lookups = [(file_id[p], n) for p, (_, ns) in file_deps.items() for n in sorted(ns)]
    return deps, lookups


def workspace_meta(packages: list[Package], aliases: list[tuple[str, list[str]]]) -> dict[str, Any]:
    return {"packages": [{"name": p.name, "dir": p.dir, "exports": p.exports, "fallback": p.fallback}
                         for p in packages],
            "aliases": [[pattern, targets] for pattern, targets in aliases]}


def workspace_from_meta(meta: dict[str, Any]) -> tuple[list[Package], list[tuple[str, list[str]]]]:
    ws = meta.get("workspace") or {}
    return ([Package(**p) for p in ws.get("packages", [])],
            [(pattern, targets) for pattern, targets in ws.get("aliases", [])])

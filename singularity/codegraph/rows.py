"""The stored form of what the build and an update write, shared by both.
Parser-free: an update that only deletes files or moves the snapshot's
bookkeeping runs without tree-sitter in the interpreter.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import languages
from .model import FileFacts
from .resolve_ts import Package
from .tables import is_local


def node_rows(f: FileFacts, file_id: int) -> list[dict[str, Any]]:
    containers = languages.family(f.lang).containers
    defs = {d.qname: d for d in f.definitions}
    return [{"qname": d.qname, "name": d.name, "kind": d.kind, "file_id": file_id, "parent": d.parent,
             "spans": json.dumps(d.spans), "signature": d.signature, "doc1": d.doc1,
             "decorators": json.dumps(d.decorators), "exported": int(d.exported),
             "local": int(is_local(defs, d.qname, containers)), "bases": json.dumps(d.bases)}
            for d in f.definitions]


def name_ref_counts(facts: dict[str, FileFacts] | list[FileFacts]) -> Counter[tuple[str, str]]:
    """(lang, name) -> number of files that mention the name (FileFacts.refs)."""
    out: Counter[tuple[str, str]] = Counter()
    for f in (facts.values() if isinstance(facts, dict) else facts):
        out.update((f.lang, n) for n in f.refs)
    return out


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

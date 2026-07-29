"""SUI-005: sanctioned, versioned, read-only export of the vault link graph.

This is the ONLY supported way for a program outside Singularity to read the
graph. Contract: `[[Singularity graph export contract v1]]` in the vault,
written before this module.

Why it exists rather than letting a consumer call `curator.link_graph()`:
that function is private, returns Python `set` objects no other runtime can
consume, exposes internals (`mtime`, `headings`, raw frontmatter) that were
never promised, and pinning an external consumer to it would make every
refactor a cross-project break.

Read-only by construction: nothing here writes to the vault, and no write verb
will be added -- the consumer (Singularity UI) has no write path into memory by
design.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import curator
from .config import Config

SCHEMA_VERSION = 1

# Generated, chronological and scaffolding content. Their links are noise in a
# knowledge graph, and the exclusion is part of the contract so a consumer can
# rely on never seeing them.
EXCLUDED_PREFIXES = curator.EXCLUDED_PREFIXES


def _clean(value: Any) -> Optional[str]:
    """Frontmatter scalar -> trimmed string, or None. Never invents a value."""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in value.strip("[]").split(",")]
    out = []
    for item in value:
        text = _clean(item)
        if text:
            out.append(text)
    return out


def build_snapshot(cfg: Config, include_broken_links: bool = False) -> dict[str, Any]:
    """Build the version-1 graph snapshot.

    `include_broken_links` controls only the diagnostics LIST. The broken-link
    COUNT is always reported: suppressing a long list is a bandwidth choice,
    but hiding the fact that links are broken would misrepresent the graph.
    """
    unreadable: list[str] = []
    notes = curator._scan_notes(cfg, unreadable=unreadable)

    # Excluded prefixes are dropped before anything else, so they can neither
    # become nodes nor contribute edges nor resolve someone else's link.
    notes = {rel: info for rel, info in notes.items()
             if not rel.startswith(EXCLUDED_PREFIXES)}

    names = curator._name_index(notes)

    nodes: list[dict[str, Any]] = []
    for rel, info in notes.items():
        fm = info.get("fm") or {}
        nodes.append({
            "id": rel,                       # vault-relative POSIX path: the identity
            # `.strip()` matters: a CRLF vault yields a trailing \r in the
            # heading-derived title, which would change the content hash
            # between a Windows and a Linux checkout of identical content.
            "title": str(info.get("title") or Path(rel).stem).strip(),
            "type": _clean(fm.get("type")),
            "status": _clean(fm.get("status")),
            "project": _clean(fm.get("project")),
            "tags": _string_list(fm.get("tags")),
            "aliases": _string_list(info.get("aliases")),
        })
    nodes.sort(key=lambda n: n["id"])

    edge_keys: set[tuple[str, str, str]] = set()
    broken: list[dict[str, str]] = []
    for rel, info in notes.items():
        for link in info.get("outlinks", []):
            target = names.get(str(link).lower())
            if not target:
                broken.append({"source": rel, "target": str(link)})
                continue
            if target == rel:
                continue                     # a note linking to itself is not an edge
            edge_keys.add((rel, target, "wikilink"))

    edges = [{"source": s, "target": t, "kind": k} for s, t, k in sorted(edge_keys)]
    broken.sort(key=lambda b: (b["source"], b["target"]))

    diagnostics: dict[str, Any] = {
        "brokenLinks": broken if include_broken_links else [],
    }
    if unreadable:
        # Present only when it happened: an always-empty key invites consumers
        # to stop checking it.
        diagnostics["unreadable"] = sorted(unreadable)

    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "snapshotId": _snapshot_id(nodes, edges),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {"nodes": len(nodes), "edges": len(edges), "brokenLinks": len(broken)},
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }
    return snapshot


def _snapshot_id(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Content address over nodes and edges only.

    `generatedAt` is deliberately excluded: a consumer caches by snapshot id,
    and an id that changed every run would make the cache useless.
    """
    canonical = json.dumps({"schemaVersion": SCHEMA_VERSION, "nodes": nodes, "edges": edges},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(snapshot: dict[str, Any], pretty: bool = False) -> str:
    if pretty:
        return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written export is worse than no export: the consumer would parse a
    truncated snapshot as if it were the graph.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".graph-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

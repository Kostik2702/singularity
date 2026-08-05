"""mb stats — memory analytics from index metadata + a vault scan (AUD-21).

Pure metadata: never loads the ML model and never emits note/query CONTENT —
only counts and structure. Covers notes/chunks/types, indexed-text coverage,
duplicate chunks, and how many vault notes are not yet indexed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..config import Config


def compute(cfg: Config) -> dict[str, Any]:
    from ..search import indexer

    files = indexer.index_files(cfg.memory_index_dir)
    if files is None:
        indexed_meta: dict[str, Any] = {"present": False, "notes": 0, "chunks": 0}
    else:
        _, meta_path = files
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        chunks = meta.get("chunks", [])
        by_type: dict[str, int] = {}
        note_files: set[str] = set()
        chars = 0
        seen_hashes: set[str] = set()
        duplicates = 0
        for c in chunks:
            by_type[c.get("type", "?")] = by_type.get(c.get("type", "?"), 0) + 1
            note_files.add(c.get("file", ""))
            content = c.get("content", "")
            chars += len(content)
            h = hashlib.sha1(content.encode("utf-8")).hexdigest()
            if h in seen_hashes:
                duplicates += 1
            else:
                seen_hashes.add(h)
        indexed_meta = {
            "present": True,
            "notes": len(note_files),
            "chunks": len(chunks),
            "by_type": dict(sorted(by_type.items())),
            "chars_indexed": chars,
            "avg_chunks_per_note": round(len(chunks) / max(len(note_files), 1), 2),
            "duplicate_chunks": duplicates,
            "chunk_schema": meta.get("chunk_schema"),
        }
        indexed_files = note_files

    # vault scan (no ML): how many indexable notes are not yet in the index
    scanned = indexer._scan_vault(cfg)
    if files is not None:
        unindexed = sorted(set(scanned) - indexed_files)
    else:
        unindexed = sorted(scanned)
    indexed_meta["vault_notes"] = len(scanned)
    indexed_meta["unindexed"] = len(unindexed)
    indexed_meta["sources"] = _sources_stats(cfg)
    indexed_meta["code"] = _code_stats(cfg)
    return indexed_meta


def _code_stats(cfg: Config) -> dict[str, Any]:
    """Code corpus counts (C05), read from the cheap `summary.json` sidecar
    plus one pass over the chunk metadata for the per-repo breakdown.

    `configured` is reported separately from `present`: "you never turned this
    on" and "you turned it on but never built it" are different situations and
    only the second one is a problem."""
    from ..search import indexer

    configured = cfg.code_repos_dir is not None
    out: dict[str, Any] = {"configured": configured, "present": False}
    if not configured:
        return out
    out["root"] = str(cfg.code_repos_dir)
    summary = indexer.index_summary(cfg.code_index_dir)
    if summary is None:
        return out
    files = indexer.index_files(cfg.code_index_dir)
    repos: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    meta: dict[str, Any] = {}      # read below only when `files` is present
    if files is not None:
        try:
            meta = json.loads(files[1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        for c in meta.get("chunks", []):
            repo = str(c.get("repo") or "-")
            repos[repo] = repos.get(repo, 0) + 1
            kind = str(c.get("kind") or "?")
            by_kind[kind] = by_kind.get(kind, 0) + 1
    out.update({
        "present": True,
        "files": summary.get("file_count", 0),
        "chunks": summary.get("chunk_count", 0),
        "chunk_schema": summary.get("chunk_schema"),
        "indexed_at": summary.get("indexed_at"),
        "repos": dict(sorted(repos.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "excluded": [e.get("path") for e in (meta.get("excluded") or [])
                     if isinstance(e, dict)],
    })
    return out


def _sources_stats(cfg: Config) -> dict[str, Any]:
    """Ingest layer counts (W11). Metadata only, like the rest of this module:
    never note content, never a model load."""
    from . import sources as sources_mod
    from ..search import indexer

    records = [r for r in sources_mod.read_registry(cfg)["sources"].values()
               if isinstance(r, dict)]
    by_status: dict[str, int] = {}
    for record in records:
        by_status[str(record.get("status", "?"))] = \
            by_status.get(str(record.get("status", "?")), 0) + 1
    summary = indexer.index_summary(cfg.sources_index_dir) or {}
    return {
        "documents": len(records),
        "stored": sum(1 for r in records if r.get("stored")),
        "by_status": dict(sorted(by_status.items())),
        "derived_notes": sum(len(r.get("derived_notes") or []) for r in records),
        "chunks": summary.get("chunk_count", 0),
        "indexed_at": summary.get("indexed_at"),
    }



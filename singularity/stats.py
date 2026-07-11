"""mb stats — memory analytics from index metadata + a vault scan (AUD-21).

Pure metadata: never loads the ML model and never emits note/query CONTENT —
only counts and structure. Covers notes/chunks/types, indexed-text coverage,
duplicate chunks, and how many vault notes are not yet indexed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import Config


def compute(cfg: Config) -> dict[str, Any]:
    from .search import indexer

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
    return indexed_meta


def run(cfg: Config, as_json: bool = False) -> int:
    s = compute(cfg)
    if as_json:
        print(json.dumps(s, ensure_ascii=False))
        return 0
    if not s.get("present"):
        print(f"index: absent — {s.get('vault_notes', 0)} vault notes, "
              f"{s.get('unindexed', 0)} unindexed")
        return 0
    print(f"notes indexed : {s['notes']} / {s['vault_notes']} vault "
          f"({s['unindexed']} unindexed)")
    print(f"chunks        : {s['chunks']} ({s['avg_chunks_per_note']}/note, "
          f"{s['duplicate_chunks']} duplicate)")
    print(f"text indexed  : {s['chars_indexed']} chars")
    print(f"by type       : " + ", ".join(f"{k}={v}" for k, v in s["by_type"].items()))
    print(f"chunk schema  : {s['chunk_schema']}")
    return 0

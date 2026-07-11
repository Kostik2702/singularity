"""Vault indexer: full rebuild + incremental update.

Index format (ported from Genevra, no unsafe deserialization):
  memory_index/embeddings.npy   float32 (chunk_count, dim), allow_pickle=False
  memory_index/metadata.json    {indexed_at, model, chunk_count,
                                 file_meta {rel: [mtime_ms, size]}, chunks[]}

Incremental diff by (mtime_ms int, size) — int avoids float precision loss in
JSON round-trip (Genevra), size guards against cloud-sync mtime jitter (ours).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from . import chunking
from .chunking import extract_chunks, should_skip
from .encoder import shared_encoder


def _file_stat(path: Path) -> list[int]:
    st = path.stat()
    return [int(st.st_mtime * 1000), st.st_size]


def _scan_vault(cfg: Config) -> dict[str, Path]:
    """rel_path (forward slashes) -> absolute Path for every indexable .md file."""
    skip_dirs = cfg.get("skip_dirs")
    skip_parts = cfg.get("skip_path_parts")
    files: dict[str, Path] = {}
    for path in cfg.vault.rglob("*.md"):
        rel = path.relative_to(cfg.vault).as_posix()
        if should_skip(rel, skip_dirs, skip_parts):
            continue
        files[rel] = path
    return files


def _load_index(index_dir: Path) -> tuple[np.ndarray, dict[str, Any]] | None:
    emb_path = index_dir / "embeddings.npy"
    meta_path = index_dir / "metadata.json"
    if not emb_path.exists() or not meta_path.exists():
        return None
    try:
        embs = np.load(emb_path, allow_pickle=False)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if embs.shape[0] != len(meta.get("chunks", [])):
            return None  # corrupted / out of sync -> caller rebuilds
        return embs, meta
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_index(index_dir: Path, embs: np.ndarray, meta: dict[str, Any]) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    np.save(index_dir / "embeddings.npy", embs.astype(np.float32), allow_pickle=False)
    meta["indexed_at"] = datetime.now().isoformat(timespec="seconds")
    meta["chunk_count"] = len(meta["chunks"])
    meta["chunk_schema"] = chunking.CHUNK_SCHEMA_VERSION
    (index_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def rebuild(cfg: Config, progress: bool = True) -> dict[str, int]:
    """Full rebuild of the vault index."""
    files = _scan_vault(cfg)
    chunks: list[dict[str, Any]] = []
    file_meta: dict[str, list[int]] = {}
    for rel, path in sorted(files.items()):
        file_chunks = extract_chunks(path, rel)
        if file_chunks:
            chunks.extend(file_chunks)
        file_meta[rel] = _file_stat(path)

    encoder = shared_encoder(cfg.model, cfg.get("batch_size"))
    if chunks:
        embs = encoder.encode([c["text"] for c in chunks], progress=progress)
    else:
        embs = np.zeros((0, 384), dtype=np.float32)

    _save_index(cfg.memory_index_dir, embs,
                {"model": cfg.model, "file_meta": file_meta, "chunks": chunks})
    size_mb = embs.nbytes / 1e6
    return {"files": len(files), "chunks": len(chunks), "size_mb": round(size_mb, 1)}


def update(cfg: Config, only_files: list[str] | None = None) -> dict[str, int]:
    """Incremental update by (mtime, size) diff. Falls back to rebuild when
    the index is missing/corrupted or was built with another model."""
    loaded = _load_index(cfg.memory_index_dir)
    if loaded is None:
        return rebuild(cfg, progress=False)
    embs, meta = loaded
    if meta.get("model") != cfg.model:
        return rebuild(cfg, progress=False)
    if meta.get("chunk_schema") != chunking.CHUNK_SCHEMA_VERSION:
        return rebuild(cfg, progress=False)   # old chunk format -> full rebuild

    files = _scan_vault(cfg)
    old_meta: dict[str, list[int]] = meta.get("file_meta", {})

    if only_files is not None:
        candidates = set(only_files)
    else:
        candidates = set(files) | set(old_meta)

    added_or_changed: list[str] = []
    deleted: list[str] = []
    for rel in candidates:
        if rel not in files:
            if rel in old_meta:
                deleted.append(rel)
            continue
        if old_meta.get(rel) != _file_stat(files[rel]):
            added_or_changed.append(rel)

    if not added_or_changed and not deleted:
        return {"added": 0, "deleted": 0, "chunks": embs.shape[0]}

    # drop chunks of deleted+changed files (Genevra keep-mask)
    dirty = set(deleted) | set(added_or_changed)
    keep = np.array([c["file"] not in dirty for c in meta["chunks"]], dtype=bool)
    embs = embs[keep]
    meta["chunks"] = [c for c, k in zip(meta["chunks"], keep) if k]
    for rel in deleted:
        old_meta.pop(rel, None)

    # re-chunk changed/new files, vstack
    new_chunks: list[dict[str, Any]] = []
    for rel in sorted(added_or_changed):
        new_chunks.extend(extract_chunks(files[rel], rel))
        old_meta[rel] = _file_stat(files[rel])
    if new_chunks:
        encoder = shared_encoder(cfg.model, cfg.get("batch_size"))
        new_embs = encoder.encode([c["text"] for c in new_chunks])
        embs = np.vstack([embs, new_embs]) if embs.size else new_embs
        meta["chunks"].extend(new_chunks)

    meta["file_meta"] = old_meta
    _save_index(cfg.memory_index_dir, embs, meta)
    return {"added": len(added_or_changed), "deleted": len(deleted),
            "chunks": len(meta["chunks"])}

"""Vault indexer: full rebuild + incremental update.

Index format (ported from Genevra, no unsafe deserialization):
  memory_index/embeddings.npy   float32 (chunk_count, dim), allow_pickle=False
  memory_index/metadata.json    {indexed_at, model, chunk_count,
                                 file_meta {rel: [mtime_ms, size]}, chunks[]}

Incremental diff by (mtime_ms int, size) — int avoids float precision loss in
JSON round-trip (Genevra), size guards against cloud-sync mtime jitter (ours).
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from . import chunking
from .chunking import extract_chunks, should_skip
from .encoder import shared_encoder

CURRENT_FILE = "CURRENT"          # names the committed generation dir
# Monotonic per-process sequence: two commits within the same millisecond (fast
# CI, back-to-back rebuilds) would otherwise mint an identical gen token and
# collide onto ONE directory — silently overwriting the "immutable" last-good
# generation and defeating crash safety. The zero-padded counter disambiguates
# them while preserving lexical (chronological) sort order.
_COMMIT_SEQ = itertools.count()
STALE_LOCK_SECONDS = 6 * 3600     # Genevra 6h rule: take over an abandoned lock


class IndexLockedError(RuntimeError):
    """Raised when another writer already holds the index lock."""


# --------------------------------------------------------------------------
# Transactional index storage (AUD-04)
#
# The index lives as immutable generations `memory_index/gen_<token>/` each
# holding a whole {embeddings.npy, metadata.json} pair. A single-file atomic
# rename of `CURRENT` is the commit point, so a reader only ever sees a fully
# written generation and a crash mid-commit leaves the previous generation as
# last-good. A legacy flat layout is still read until the next rebuild migrates.
# --------------------------------------------------------------------------

def _gen_is_whole(gen_dir: Path) -> bool:
    return (gen_dir / "embeddings.npy").exists() and (gen_dir / "metadata.json").exists()


def _current_gen_dir(index_dir: Path) -> Path | None:
    cur = index_dir / CURRENT_FILE
    if cur.exists():
        name = cur.read_text(encoding="utf-8").strip()
        gd = index_dir / name
        if name and _gen_is_whole(gd):
            return gd
    # CURRENT missing/torn -> newest whole generation is the last-good
    for gd in sorted(index_dir.glob("gen_*"), reverse=True):
        if _gen_is_whole(gd):
            return gd
    return None


def index_files(index_dir: Path) -> tuple[Path, Path] | None:
    """(embeddings.npy, metadata.json) of the committed generation, else the
    legacy flat layout, else None. Used by every index reader."""
    gd = _current_gen_dir(index_dir)
    if gd is not None:
        return gd / "embeddings.npy", gd / "metadata.json"
    flat_emb, flat_meta = index_dir / "embeddings.npy", index_dir / "metadata.json"
    if flat_emb.exists() and flat_meta.exists():
        return flat_emb, flat_meta
    return None


def load_committed_index(index_dir: Path) -> tuple[np.ndarray | None, list[dict[str, Any]], str]:
    """(row-normalized embeddings, chunks, status). status: "ok" | "missing" | "torn".
    Reads the CURRENT committed generation via index_files() -- never a torn
    partial write, never a legacy flat leftover -- and never imports the
    encoder (no model load). Single source for every reader that needs the
    live committed index without triggering a rebuild: digest.plan() and
    curator's semantic layer both use this instead of re-deriving their own
    path resolution (T13: curator._note_embeddings() used to read
    memory_index/embeddings.npy directly, bypassing the CURRENT-generation
    pointer -- silently stale on any vault using the transactional layout)."""
    files = index_files(index_dir)
    if files is None:
        return None, [], "missing"
    emb_path, meta_path = files
    try:
        embs = np.load(emb_path, allow_pickle=False).astype(np.float32)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        chunks = meta.get("chunks", [])
        if embs.shape[0] != len(chunks):
            return None, [], "torn"
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return embs / norms, chunks, "ok"
    except (OSError, ValueError, json.JSONDecodeError):
        return None, [], "torn"


def _prune_generations(index_dir: Path, keep_current: str) -> None:
    """Keep the current generation and the single most-recent previous one
    (last-good); remove older generations."""
    gens = sorted((p for p in index_dir.glob("gen_*") if p.is_dir()),
                  key=lambda p: p.name, reverse=True)
    keep = {keep_current}
    for gd in gens:
        if gd.name in keep:
            continue
        if len(keep) < 2:
            keep.add(gd.name)
            continue
        shutil.rmtree(gd, ignore_errors=True)


def _commit_generation(index_dir: Path, embs: np.ndarray, meta: dict[str, Any]) -> None:
    """Write a new generation and atomically make it current. Refuses to commit
    a torn pair (embeddings rows != chunk count)."""
    meta["indexed_at"] = datetime.now().isoformat(timespec="seconds")
    meta["chunk_count"] = len(meta["chunks"])
    meta["chunk_schema"] = chunking.CHUNK_SCHEMA_VERSION
    if embs.shape[0] != len(meta["chunks"]):
        raise ValueError(
            f"refusing to commit torn index: {embs.shape[0]} embeddings vs "
            f"{len(meta['chunks'])} chunks")

    index_dir.mkdir(parents=True, exist_ok=True)
    token = f"{int(time.time() * 1000):013d}_{os.getpid()}_{next(_COMMIT_SEQ):06d}"
    gen = f"gen_{token}"
    gen_dir = index_dir / gen
    gen_dir.mkdir(parents=True, exist_ok=True)
    np.save(gen_dir / "embeddings.npy", embs.astype(np.float32), allow_pickle=False)
    (gen_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # commit: atomic single-file pointer swap
    tmp = index_dir / (CURRENT_FILE + ".tmp")
    tmp.write_text(gen, encoding="utf-8")
    os.replace(tmp, index_dir / CURRENT_FILE)
    _prune_generations(index_dir, keep_current=gen)


@contextlib.contextmanager
def _writer_lock(cfg: Config):
    """Serialize every index writer (rebuild/update/watcher/nightly) with one
    cross-process mkdir lock. Takes over a lock older than STALE_LOCK_SECONDS.

    A losing (contended) acquisition attempt must NEVER remove the lock: the
    unconditional `finally: shutil.rmtree(...)` this used to have deleted the
    OTHER writer's still-active lock the moment a second writer's `mkdir`
    failed and IndexLockedError was raised -- confirmed live (T12) with a
    manual repro -- which let a third writer race in while the real holder
    was still mid-write. Cleanup now only runs when this call actually owns
    the lock (freshly acquired, or a stale takeover)."""
    lock = cfg.lock_dir
    lock.parent.mkdir(parents=True, exist_ok=True)
    owns_lock = False
    try:
        lock.mkdir(exist_ok=False)
        owns_lock = True
    except FileExistsError:
        age = time.time() - lock.stat().st_mtime
        if age < STALE_LOCK_SECONDS:
            raise IndexLockedError("another index writer holds the lock")
        owns_lock = True   # stale -> take over; we now own cleanup
    try:
        yield
    finally:
        if owns_lock:
            shutil.rmtree(lock, ignore_errors=True)


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
    files = index_files(index_dir)
    if files is None:
        return None
    emb_path, meta_path = files
    try:
        embs = np.load(emb_path, allow_pickle=False)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if embs.shape[0] != len(meta.get("chunks", [])):
            return None  # corrupted / out of sync -> caller rebuilds
        return embs, meta
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_index(index_dir: Path, embs: np.ndarray, meta: dict[str, Any]) -> None:
    _commit_generation(index_dir, embs, meta)


def rebuild(cfg: Config, progress: bool = True) -> dict[str, int]:
    """Full rebuild of the vault index (serialized against other writers)."""
    with _writer_lock(cfg):
        return _rebuild(cfg, progress)


def _rebuild(cfg: Config, progress: bool = True) -> dict[str, int]:
    files = _scan_vault(cfg)
    chunks: list[dict[str, Any]] = []
    file_meta: dict[str, list[int]] = {}
    personal_roots = cfg.get("personal_roots") or []
    for rel, path in sorted(files.items()):
        file_chunks = extract_chunks(path, rel, personal_roots)
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


def inspect(cfg: Config, rel: str) -> list[dict[str, Any]]:
    """Chunk-level view of one file in the current index (no content), for
    `mb index inspect` and search debugging (AUD-17)."""
    loaded = _load_index(cfg.memory_index_dir)
    if loaded is None:
        return []
    _, meta = loaded
    return [{"chunk_id": c.get("chunk_id"), "section": c.get("section"),
             "chunk_index": c.get("chunk_index"),
             "line_start": c.get("line_start"), "line_end": c.get("line_end"),
             "chars": len(c.get("content", "")),
             "project": c.get("project")}
            for c in meta.get("chunks", []) if c.get("file") == rel]


def update(cfg: Config, only_files: list[str] | None = None) -> dict[str, int]:
    """Incremental update by (mtime, size) diff (serialized against other
    writers). Falls back to rebuild when the index is missing/corrupted or was
    built with another model/chunk schema."""
    with _writer_lock(cfg):
        return _update(cfg, only_files)


def _update(cfg: Config, only_files: list[str] | None = None) -> dict[str, int]:
    loaded = _load_index(cfg.memory_index_dir)
    if loaded is None:
        return _rebuild(cfg, progress=False)
    embs, meta = loaded
    if meta.get("model") != cfg.model:
        return _rebuild(cfg, progress=False)
    if meta.get("chunk_schema") != chunking.CHUNK_SCHEMA_VERSION:
        return _rebuild(cfg, progress=False)   # old chunk format -> full rebuild

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
    personal_roots = cfg.get("personal_roots") or []
    new_chunks: list[dict[str, Any]] = []
    for rel in sorted(added_or_changed):
        new_chunks.extend(extract_chunks(files[rel], rel, personal_roots))
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

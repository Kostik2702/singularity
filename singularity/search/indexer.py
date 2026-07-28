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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..config import Config
from . import chunking
from .chunking import extract_chunks, extract_source_chunks, should_skip
from .encoder import shared_encoder

CURRENT_FILE = "CURRENT"          # names the committed generation dir
SUMMARY_FILE = "summary.json"     # tiny sidecar: model/counts without parsing metadata.json
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
# Corpora (W03)
#
# The indexer used to hardcode ONE (root, index_dir) pair: cfg.vault ->
# cfg.memory_index_dir. WIKI-LOOP D2 needs a second, physically separate
# corpus over `.mb/sources/`, so scanning, chunking, locking and committing
# are parameterized by a Corpus instead. `SearchEngine.reload()` was already
# multi-corpus on the READ side (engine.py:53) -- only the writer was not.
#
# Every corpus gets its OWN writer lock: indexing sources must never block
# indexing the vault (and vice versa), otherwise a nightly source rebuild
# would stall the watcher's on-save updates.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Corpus:
    """One indexable body of markdown: where to scan, where to commit, how to
    chunk, and what to skip.

    `rel_prefix` is prepended to every scanned path so a chunk's `file` stays
    VAULT-relative (`.mb/sources/2026/x.md`) even though scanning is rooted at
    the corpus root. Skip rules are evaluated on the UN-prefixed sub-path:
    the sources prefix itself starts with `.mb`, which the vault's own
    `skip_dirs` would otherwise reject wholesale."""
    name: str
    root: Path
    index_dir: Path
    skip_dirs: tuple[str, ...] = ()
    skip_parts: tuple[str, ...] = ()
    rel_prefix: str = ""
    chunker: Callable[..., list[dict[str, Any]]] = field(default=extract_chunks)


def memory_corpus(cfg: Config) -> Corpus:
    return Corpus(name="memory", root=cfg.vault, index_dir=cfg.memory_index_dir,
                  skip_dirs=tuple(cfg.get("skip_dirs") or ()),
                  skip_parts=tuple(cfg.get("skip_path_parts") or ()),
                  chunker=extract_chunks)


def sources_corpus(cfg: Config) -> Corpus:
    return Corpus(name="sources", root=cfg.sources_dir,
                  index_dir=cfg.sources_index_dir,
                  rel_prefix=".mb/sources/", chunker=extract_source_chunks)


CORPUS_BUILDERS = {"memory": memory_corpus, "sources": sources_corpus}


def resolve_corpus(cfg: Config, corpus: "str | Corpus | None") -> Corpus:
    """`None` -> the memory corpus, preserving every pre-W03 call site."""
    if corpus is None:
        return memory_corpus(cfg)
    if isinstance(corpus, Corpus):
        return corpus
    builder = CORPUS_BUILDERS.get(str(corpus))
    if builder is None:
        raise ValueError(f"unknown corpus: {corpus!r} "
                         f"(known: {', '.join(sorted(CORPUS_BUILDERS))})")
    return builder(cfg)


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


def index_summary(index_dir: Path) -> dict[str, Any] | None:
    """Model + counts of the committed generation WITHOUT parsing the whole
    metadata.json (which carries every chunk and is megabytes on a real vault).

    W05 needs this on every `mb ingest plan` to prove both corpora were built
    by the same model — reading two multi-megabyte files for four fields would
    have made the command as slow as a rebuild. Generations written before the
    sidecar existed fall back to the full parse, so old indexes keep working."""
    files = index_files(index_dir)
    if files is None:
        return None
    _, meta_path = files
    sidecar = meta_path.with_name(SUMMARY_FILE)
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass    # fall through to the authoritative file
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {"model": meta.get("model"), "chunk_count": meta.get("chunk_count"),
            "chunk_schema": meta.get("chunk_schema"),
            "indexed_at": meta.get("indexed_at"),
            "file_count": len(meta.get("file_meta") or {})}


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
    # cheap sidecar, written BEFORE the pointer swap so a committed generation
    # always has one (readers still fall back to metadata.json if it is absent)
    (gen_dir / SUMMARY_FILE).write_text(json.dumps(
        {"model": meta.get("model"), "chunk_count": meta.get("chunk_count"),
         "chunk_schema": meta.get("chunk_schema"),
         "indexed_at": meta.get("indexed_at"),
         "file_count": len(meta.get("file_meta") or {})},
        ensure_ascii=False), encoding="utf-8")

    # commit: atomic single-file pointer swap
    tmp = index_dir / (CURRENT_FILE + ".tmp")
    tmp.write_text(gen, encoding="utf-8")
    os.replace(tmp, index_dir / CURRENT_FILE)
    _prune_generations(index_dir, keep_current=gen)


@contextlib.contextmanager
def _writer_lock(cfg: Config, corpus_name: str = "memory"):
    """Serialize every index writer (rebuild/update/watcher/nightly) of ONE
    corpus with a cross-process mkdir lock. Takes over a lock older than
    STALE_LOCK_SECONDS.

    W03: the lock is per-corpus (`cfg.corpus_lock_dir`), so `mb ingest index`
    and `mb index update` never block each other. The memory corpus keeps the
    historic `.mb/rebuild.lock` path verbatim, so a lock held by an older
    build is still honored across the upgrade.

    A losing (contended) acquisition attempt must NEVER remove the lock: the
    unconditional `finally: shutil.rmtree(...)` this used to have deleted the
    OTHER writer's still-active lock the moment a second writer's `mkdir`
    failed and IndexLockedError was raised -- confirmed live (T12) with a
    manual repro -- which let a third writer race in while the real holder
    was still mid-write. Cleanup now only runs when this call actually owns
    the lock (freshly acquired, or a stale takeover)."""
    lock = cfg.corpus_lock_dir(corpus_name)
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


def _scan_corpus(corpus: Corpus) -> dict[str, Path]:
    """rel_path (forward slashes, `rel_prefix` applied) -> absolute Path for
    every indexable .md file of this corpus."""
    files: dict[str, Path] = {}
    if not corpus.root.exists():
        return files
    skip_dirs = list(corpus.skip_dirs)
    skip_parts = list(corpus.skip_parts)
    for path in corpus.root.rglob("*.md"):
        sub = path.relative_to(corpus.root).as_posix()
        if should_skip(sub, skip_dirs, skip_parts):
            continue
        files[corpus.rel_prefix + sub] = path
    return files


def _scan_vault(cfg: Config) -> dict[str, Path]:
    """rel_path (forward slashes) -> absolute Path for every indexable .md file
    of the VAULT corpus. Kept as the pre-W03 name/signature because it is a
    public read path used outside the indexer (`stats.compute`)."""
    return _scan_corpus(memory_corpus(cfg))


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


def rebuild(cfg: Config, progress: bool = True,
            corpus: "str | Corpus | None" = None) -> dict[str, int]:
    """Full rebuild of one corpus (serialized against other writers of the
    SAME corpus only). `corpus=None` == the vault corpus, as before W03."""
    corp = resolve_corpus(cfg, corpus)
    with _writer_lock(cfg, corp.name):
        return _rebuild(cfg, progress, corp)


def _rebuild(cfg: Config, progress: bool = True,
             corpus: "str | Corpus | None" = None) -> dict[str, int]:
    corp = resolve_corpus(cfg, corpus)
    files = _scan_corpus(corp)
    chunks: list[dict[str, Any]] = []
    file_meta: dict[str, list[int]] = {}
    personal_roots = cfg.get("personal_roots") or []
    for rel, path in sorted(files.items()):
        file_chunks = corp.chunker(path, rel, personal_roots)
        if file_chunks:
            chunks.extend(file_chunks)
        file_meta[rel] = _file_stat(path)

    encoder = shared_encoder(cfg.model, cfg.get("batch_size"), cfg=cfg)
    if chunks:
        embs = encoder.encode([c["text"] for c in chunks], progress=progress)
    else:
        embs = np.zeros((0, 384), dtype=np.float32)

    _save_index(corp.index_dir, embs,
                {"model": cfg.model, "file_meta": file_meta, "chunks": chunks})
    size_mb = embs.nbytes / 1e6
    return {"files": len(files), "chunks": len(chunks), "size_mb": round(size_mb, 1)}


def inspect(cfg: Config, rel: str,
            corpus: "str | Corpus | None" = None) -> list[dict[str, Any]]:
    """Chunk-level view of one file in the current index (no content), for
    `mb index inspect` and search debugging (AUD-17)."""
    loaded = _load_index(resolve_corpus(cfg, corpus).index_dir)
    if loaded is None:
        return []
    _, meta = loaded
    return [{"chunk_id": c.get("chunk_id"), "section": c.get("section"),
             "chunk_index": c.get("chunk_index"),
             "line_start": c.get("line_start"), "line_end": c.get("line_end"),
             "chars": len(c.get("content", "")),
             "project": c.get("project")}
            for c in meta.get("chunks", []) if c.get("file") == rel]


def update(cfg: Config, only_files: list[str] | None = None,
           corpus: "str | Corpus | None" = None) -> dict[str, int]:
    """Incremental update by (mtime, size) diff (serialized against other
    writers of the SAME corpus). Falls back to rebuild when the index is
    missing/corrupted or was built with another model/chunk schema."""
    corp = resolve_corpus(cfg, corpus)
    with _writer_lock(cfg, corp.name):
        return _update(cfg, only_files, corp)


def _update(cfg: Config, only_files: list[str] | None = None,
            corpus: "str | Corpus | None" = None) -> dict[str, int]:
    corp = resolve_corpus(cfg, corpus)
    loaded = _load_index(corp.index_dir)
    if loaded is None:
        return _rebuild(cfg, progress=False, corpus=corp)
    embs, meta = loaded
    if meta.get("model") != cfg.model:
        return _rebuild(cfg, progress=False, corpus=corp)
    if meta.get("chunk_schema") != chunking.CHUNK_SCHEMA_VERSION:
        return _rebuild(cfg, progress=False, corpus=corp)  # old chunk format -> full rebuild

    files = _scan_corpus(corp)
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
        new_chunks.extend(corp.chunker(files[rel], rel, personal_roots))
        old_meta[rel] = _file_stat(files[rel])
    if new_chunks:
        encoder = shared_encoder(cfg.model, cfg.get("batch_size"), cfg=cfg)
        new_embs = encoder.encode([c["text"] for c in new_chunks])
        embs = np.vstack([embs, new_embs]) if embs.size else new_embs
        meta["chunks"].extend(new_chunks)

    meta["file_meta"] = old_meta
    _save_index(corp.index_dir, embs, meta)
    return {"added": len(added_or_changed), "deleted": len(deleted),
            "chunks": len(meta["chunks"])}

"""Vault indexer: full rebuild + incremental update.

Index format (no unsafe deserialization):
  memory_index/embeddings.npy   float32 (chunk_count, dim), allow_pickle=False
  memory_index/metadata.json    {indexed_at, model, chunk_count,
                                 file_meta {rel: [mtime_ms, size]}, chunks[]}

Incremental diff by (mtime_ms int, size) — int avoids float precision loss in
JSON round-trip, size guards against cloud-sync mtime jitter.
"""
from __future__ import annotations

import contextlib
import fnmatch
import functools
import itertools
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np

from ..config import INBOX_DIR, Config
from ..platform.fslock import make_lock_dir
from . import chunking, code_chunking, code_chunking_v2
from .chunking import extract_chunks, extract_source_chunks, should_skip
from .code_chunking_v2 import GrammarPinRefused  # noqa: F401  (the daemon names it here)
from .encoder import shared_encoder

CURRENT_FILE = "CURRENT"          # names the committed generation dir
SUMMARY_FILE = "summary.json"     # tiny sidecar: model/counts without parsing metadata.json
# Monotonic per-process sequence: two commits within the same millisecond (fast
# CI, back-to-back rebuilds) would otherwise mint an identical gen token and
# collide onto ONE directory — silently overwriting the "immutable" last-good
# generation and defeating crash safety. The zero-padded counter disambiguates
# them while preserving lexical (chronological) sort order.
_COMMIT_SEQ = itertools.count()
STALE_LOCK_SECONDS = 6 * 3600     # 6h rule: take over an abandoned lock


class IndexLockedError(RuntimeError):
    """Raised when another writer already holds the index lock."""


class CorpusRootUnavailable(RuntimeError):
    """The corpus root (or a subtree of it) could not be read, while an index
    with content already exists.

    Refusing beats proceeding: a scan that returns nothing is byte-identical to
    "every file was deleted", and the incremental writer would faithfully purge
    the whole index. An unmounted drive, a synced folder mid-reconnect or a
    denied ACL would silently destroy a corpus that takes minutes to rebuild.
    Bootstrapping an EMPTY corpus over a missing root is still allowed — there
    is nothing to lose there."""


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
    """One indexable body of files: where to scan, where to commit, how to
    chunk, and what to skip.

    `rel_prefix` is prepended to every scanned path so a chunk's `file` stays
    VAULT-relative (`.mb/sources/2026/x.md`) even though scanning is rooted at
    the corpus root. Skip rules are evaluated on the UN-prefixed sub-path:
    the sources prefix itself starts with `.mb`, which the vault's own
    `skip_dirs` would otherwise reject wholesale.

    C01 added the four fields below so a NON-markdown body (the code corpus)
    can reuse this writer instead of growing a parallel indexer. Every default
    reproduces the pre-C01 markdown behavior exactly, so the two live corpora
    are untouched:

    * `patterns`      — filename globs to collect (was a hardcoded `*.md`).
    * `prune_dirs`    — directory NAMES cut at ANY depth while walking. Distinct
      from `skip_dirs`, which (per `chunking.should_skip`) only ever matched the
      FIRST path segment: correct for a vault rooted at its own notes, useless
      for a root holding many repositories where `node_modules` sits three
      levels down. Pruning happens during traversal, so the subtree is never
      descended at all — measured on a real repositories root, that is the
      difference between visiting ~500 files and ~56 000 (epic §2 recon).
    * `skip_file_patterns` — globs matched against the whole rel-path, applied
      AFTER the cheap directory cuts and BEFORE any `stat()`.
    * `max_file_bytes`— size ceiling; `None` disables the check entirely (and
      with it the only `stat()` the scan would otherwise perform).
    * `chunk_schema`  — the chunk-format version of THIS corpus. Used to be a
      single global constant stamped onto every corpus (D3): bumping the
      markdown chunker forced a needless rebuild of the code index, and bumping
      the code chunker forced nothing at all.
    """
    name: str
    root: Path
    index_dir: Path
    skip_dirs: tuple[str, ...] = ()
    skip_parts: tuple[str, ...] = ()
    rel_prefix: str = ""
    chunker: Callable[..., list[dict[str, Any]]] = field(default=extract_chunks)
    patterns: tuple[str, ...] = ("*.md",)
    prune_dirs: tuple[str, ...] = ()
    skip_file_patterns: tuple[str, ...] = ()
    max_file_bytes: int | None = None
    chunk_schema: int = chunking.CHUNK_SCHEMA_VERSION
    # C09: directory-name regex for vendored third-party distributions, pruned
    # at any depth like `prune_dirs` but by shape rather than by an enumerated
    # name — a bundled SDK carries its version in the directory name, and no
    # fixed list can enumerate versions. Empty = the check is skipped entirely.
    vendored_dir_pattern: str = ""
    # C10: follow directory symlinks while scanning. Windows junctions are
    # traversed by `os.walk` regardless (Python reports them islink=False), but
    # a POSIX symlink is NOT — so "link a repository in from outside the root"
    # worked on Windows and silently indexed nothing on macOS/Linux. Kept off
    # for the vault: a note tree is authored in place, and quietly following a
    # link out of it would index files the user never put in their vault.
    follow_links: bool = False
    # issue #12: for a corpus whose root the USER named explicitly, "configured
    # but unreachable" is a different state from "empty", and returning
    # `files: 0` for it is what made a stale-config bug take a day to find. The
    # vault and `.mb/sources/` keep the permissive default: a vault that has
    # never ingested anything legitimately has no sources dir yet.
    root_must_exist: bool = False
    # CODE-GRAPH P7: a corpus whose chunk boundaries come from a parser names
    # the check a writer must pass before touching it. Called first thing by
    # every write; returns the grammar pins to stamp on the generation, or
    # raises `GrammarPinRefused` with nothing scanned and nothing written.
    # None (every corpus but code under schema v2) = no parser, no stamp, and
    # metadata exactly as before this field existed.
    pin_guard: Callable[[], dict[str, str]] | None = None


def memory_corpus(cfg: Config) -> Corpus:
    return Corpus(name="memory", root=cfg.vault, index_dir=cfg.memory_index_dir,
                  # INBOX_DIR is not configurable away: a capture is a draft,
                  # and ranking drafts against knowledge is the failure this
                  # staging area exists to prevent (AUD-21).
                  skip_dirs=(*(cfg.get("skip_dirs") or ()), INBOX_DIR),
                  skip_parts=tuple(cfg.get("skip_path_parts") or ()),
                  chunker=extract_chunks,
                  chunk_schema=chunking.CHUNK_SCHEMA_VERSION)


def sources_corpus(cfg: Config) -> Corpus:
    return Corpus(name="sources", root=cfg.sources_dir,
                  index_dir=cfg.sources_index_dir,
                  rel_prefix=".mb/sources/", chunker=extract_source_chunks,
                  chunk_schema=chunking.CHUNK_SCHEMA_VERSION)


class CodeCorpusNotConfigured(RuntimeError):
    """`code_repos_dir` is unset, so there is no code corpus to act on.

    Raised instead of returning a corpus rooted at nothing: an empty index is
    indistinguishable from "your code has no matches", and the user would have
    to guess which of the two they are looking at."""


def _code_chunker(cfg: Config) -> tuple[Callable[..., list[dict[str, Any]]], int,
                                          Callable[[], dict[str, str]] | None]:
    """(chunker, chunk schema, pin guard) named by `code_chunk_schema`.

    By CONFIG only (CODE-GRAPH D1, UNMET r): whether the parser is installed
    decides whether a v2 write may proceed, never which schema is written. A
    v2 config on an interpreter without the grammars refuses; it does not
    quietly become v1."""
    schema = cfg.get("code_chunk_schema") or "v1"
    if schema == "v1":
        return (code_chunking.extract_code_chunks,
                code_chunking.CODE_CHUNK_SCHEMA_VERSION, None)
    if schema == "v2":
        return (code_chunking_v2.extract_code_chunks,
                code_chunking_v2.CODE_CHUNK_SCHEMA_VERSION, code_chunking_v2.writer_guard)
    raise ValueError(f"code_chunk_schema must be 'v1' or 'v2', not {schema!r}")


def code_corpus(cfg: Config) -> Corpus:
    """Third corpus (C03): source files under `cfg.code_repos_dir`.

    Scanning, generations, locking, atomic commit and the incremental
    (mtime, size) diff are all inherited from the shared writer — the ONLY
    things specific to code are the policy constants and the chunker."""
    root = cfg.code_repos_dir
    if root is None:
        raise CodeCorpusNotConfigured(
            "code_repos_dir is not set — the code corpus is off. Point it at "
            "the directory that HOLDS your repositories: "
            '`mb config set code_repos_dir "<path/to/your/repos>"`')
    chunker, chunk_schema, pin_guard = _code_chunker(cfg)
    return Corpus(
        name="code", root=root, index_dir=cfg.code_index_dir,
        # first-segment match == repository name, which is exactly what
        # `should_skip` already does; no new mechanism needed for D1
        skip_dirs=code_chunking.SKIP_REPOS,
        prune_dirs=(code_chunking.SKIP_DIRS
                    + tuple(cfg.get("code_skip_dirs") or ())),
        vendored_dir_pattern=(code_chunking.VENDORED_DIR_PATTERN
                              if cfg.get("code_vendored_pattern") is None
                              else str(cfg.get("code_vendored_pattern"))),
        patterns=code_chunking.CODE_PATTERNS,
        skip_file_patterns=code_chunking.SKIP_FILE_PATTERNS,
        max_file_bytes=code_chunking.MAX_FILE_BYTES,
        chunker=chunker,
        chunk_schema=chunk_schema,
        pin_guard=pin_guard,
        # a repository living outside the root is linked in — and a symlink is
        # the only way to do that on macOS/Linux
        follow_links=True,
        # the user typed this path; if it is not there, say so (issue #12)
        root_must_exist=True)


CORPUS_BUILDERS = {"memory": memory_corpus, "sources": sources_corpus,
                   "code": code_corpus}


def resolve_corpus(cfg: Config, corpus: str | Corpus | None) -> Corpus:
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


def _commit_generation(index_dir: Path, embs: np.ndarray, meta: dict[str, Any],
                       *, chunk_schema: int) -> None:
    """Write a new generation and atomically make it current. Refuses to commit
    a torn pair (embeddings rows != chunk count).

    `chunk_schema` is keyword-only and REQUIRED (D3). It used to be read from
    the global `chunking.CHUNK_SCHEMA_VERSION` right here, which silently
    stamped the markdown chunk format onto every corpus; a writer must now
    state which format it just produced instead of letting this function
    guess."""
    meta["indexed_at"] = datetime.now().isoformat(timespec="seconds")
    meta["chunk_count"] = len(meta["chunks"])
    meta["chunk_schema"] = chunk_schema
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
    owns_lock = make_lock_dir(lock)
    if not owns_lock:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:    # released between the two calls: it was held a moment ago
            age = 0.0
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


@functools.lru_cache(maxsize=64)
def _glob_matcher(globs: tuple[str, ...]) -> Callable[[str], Any] | None:
    """One anchored, case-insensitive regex for a whole glob set, or None when
    the set is empty (so the caller can skip the check outright).

    Case-insensitive on every platform on purpose: `fnmatch` folds case only on
    Windows, and a skip rule that silently stops working when the same vault is
    opened on Linux is the kind of divergence nobody notices until a `.MIN.JS`
    shows up in search results."""
    if not globs:
        return None
    return re.compile(
        "|".join(f"(?:{fnmatch.translate(g)})" for g in globs),
        re.IGNORECASE).match


@functools.lru_cache(maxsize=8)
def _regex_matcher(pattern: str) -> Callable[[str], Any] | None:
    """Compiled `.match` for a raw regex, or None when the pattern is empty.
    Kept as a string on `Corpus` rather than a compiled object so the dataclass
    stays trivially comparable and picklable."""
    if not pattern:
        return None
    return re.compile(pattern, re.IGNORECASE).match


def _scan_corpus(corpus: Corpus, report: dict[str, Any] | None = None
                 ) -> dict[str, Path]:
    """rel_path (forward slashes, `rel_prefix` applied) -> absolute Path for
    every indexable file of this corpus.

    Walked with `os.walk` rather than `rglob` because only `os.walk` lets us
    PRUNE (`dirnames[:] = ...`): `rglob` descends into `node_modules` and
    discards the results afterwards, which on a repositories root is the whole
    cost of the scan. Filter order is load-bearing, cheapest first (R4):

        1. directory prune   — during traversal, subtree never entered
        2. filename patterns — string match, no syscall
        3. skip_dirs/parts   — rel-path match, no syscall
        4. skip_file_patterns— rel-path regex, no syscall
        5. stat()            — the only syscall, and only for survivors

    Reversing 4 and 5 would cost one `stat()` per candidate — on a measured
    repositories root, ~56 000 syscalls to keep ~500 files.

    `report` is an optional out-param (kept out of the return type so
    `_scan_vault` and every existing call site are untouched). It collects:

      * `walk_errors` — directories `os.walk` could not read. Without an
        `onerror` handler `os.walk` swallows them and simply yields nothing for
        that subtree, which is INDISTINGUISHABLE from the files having been
        deleted — and the caller would then purge their chunks. A denied ACL or
        a file lock must never look like a deletion.
      * `excluded`  — subtrees deliberately dropped, by NAME not by count. A
        count cannot tell "pruned a vendored engine" from "pruned your own
        snapshot", which is the whole point of reporting an exclusion at all.
    """
    files: dict[str, Path] = {}
    if report is not None:
        report.setdefault("walk_errors", [])
        report.setdefault("excluded", [])
    if not corpus.root.exists():
        if report is not None:
            report["root_missing"] = str(corpus.root)
        return files
    root = corpus.root
    skip_dirs = list(corpus.skip_dirs)
    skip_parts = list(corpus.skip_parts)
    prune = set(corpus.prune_dirs)
    match_vendored = _regex_matcher(corpus.vendored_dir_pattern)
    match_name = _glob_matcher(tuple(corpus.patterns))
    match_skip = _glob_matcher(tuple(corpus.skip_file_patterns))
    max_bytes = corpus.max_file_bytes

    def _on_walk_error(exc: OSError) -> None:
        """Record only errors that could actually have HIDDEN indexable content.

        An unreadable directory is treated as possible data loss, so it blocks
        the write. That must not extend to directories the corpus already
        declares uninteresting: the vault walks into `.git/`, `.obsidian/` and
        `.mb/` (they are filtered per-file by `should_skip`, not pruned), so one
        mode-700 directory inside `.git` used to freeze the vault index — both
        `update` AND `rebuild`, i.e. including the documented escape hatch —
        over a subtree guaranteed to contribute zero chunks."""
        if report is None:
            return
        path = str(getattr(exc, "filename", "") or root)
        try:
            rel = Path(path).relative_to(root).as_posix()
        except ValueError:
            rel = ""
        if rel:
            if any(part in prune for part in rel.split("/")):
                return
            if should_skip(rel, skip_dirs, skip_parts):
                return
        report["walk_errors"].append(
            {"path": path, "error": f"{type(exc).__name__}: {exc}"})

    # Every visited directory is keyed by its REAL path. Following links makes
    # a cycle reachable (a link pointing at an ancestor), and `os.walk` has no
    # loop detection of its own — on Windows that is already reachable today
    # via a junction, without any opt-in at all.
    seen_real: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error,
                                                followlinks=corpus.follow_links):
        try:
            real = os.path.realpath(dirpath)
        except OSError:
            real = dirpath
        if real in seen_real:
            dirnames[:] = []
            continue
        seen_real.add(real)
        if prune or match_vendored is not None:
            keep = []
            for d in dirnames:
                if d in prune:
                    continue
                if match_vendored is not None and match_vendored(d):
                    if report is not None:
                        rel = (Path(dirpath) / d).relative_to(root).as_posix()
                        report["excluded"].append(
                            {"kind": "vendored", "path": rel})
                    continue
                keep.append(d)
            dirnames[:] = keep
        base = Path(dirpath)
        for name in filenames:
            if match_name is not None and not match_name(name):
                continue
            path = base / name
            sub = path.relative_to(root).as_posix()
            if should_skip(sub, skip_dirs, skip_parts):
                continue
            if match_skip is not None and match_skip(sub):
                continue
            if max_bytes is not None:
                try:
                    if path.stat().st_size > max_bytes:
                        continue
                except OSError:
                    continue      # vanished/unreadable between walk and stat
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


GRAMMAR_PINS_KEY = "grammar_pins"


def _grammar_pins(corp: Corpus) -> dict[str, str] | None:
    """The pins this write may stamp, or None for a corpus without a parser.
    Raises `GrammarPinRefused` BEFORE the caller has scanned or written."""
    return corp.pin_guard() if corp.pin_guard is not None else None


def _save_index(corpus: Corpus, embs: np.ndarray, meta: dict[str, Any],
                pins: dict[str, str] | None = None) -> None:
    if pins is not None:
        meta[GRAMMAR_PINS_KEY] = pins
    _commit_generation(corpus.index_dir, embs, meta,
                       chunk_schema=corpus.chunk_schema)


def rebuild(cfg: Config, progress: bool = True,
            corpus: str | Corpus | None = None) -> dict[str, int]:
    """Full rebuild of one corpus (serialized against other writers of the
    SAME corpus only). `corpus=None` == the vault corpus, as before W03."""
    corp = resolve_corpus(cfg, corpus)
    with _writer_lock(cfg, corp.name):
        return _rebuild(cfg, progress, corp)


def _guard_scan(corp: Corpus, report: dict[str, Any],
                existing_chunks: int) -> None:
    """Refuse to replace a populated index using a scan we know was incomplete.

    Only fires when there is something to lose: bootstrapping an empty corpus
    over a missing root stays legal, which is what `mb ingest index` does on a
    vault that has never ingested anything.

    The exception is a corpus whose root the user named explicitly
    (`root_must_exist`, issue #12). There, an unreachable root and a genuinely
    empty one are not interchangeable: reporting `{"files": 0}` for a
    `code_repos_dir` that does not exist on this machine is indistinguishable
    from a successful scan of an empty tree, and `CodeCorpusNotConfigured`
    already models "off" — "configured but unreachable" deserves the same
    treatment."""
    if corp.root_must_exist and report.get("root_missing"):
        raise CorpusRootUnavailable(
            f"{corp.name} corpus root does not exist on this machine: "
            f"{report['root_missing']} — configured, but unreachable; refusing "
            f"to report that as an empty tree. The index is untouched. Fix the "
            f"path with `mb config set code_repos_dir \"<path>\"`, then "
            f"`mb daemon restart` so the daemon stops serving the old value.")
    if existing_chunks <= 0:
        return
    if report.get("root_missing"):
        raise CorpusRootUnavailable(
            f"{corp.name} corpus root is unavailable ({report['root_missing']}) "
            f"— refusing to drop {existing_chunks} existing chunk(s); the index "
            f"is untouched. Reconnect the path, or rebuild deliberately once it "
            f"is back.")
    errs = report.get("walk_errors") or []
    if errs:
        first = errs[0]
        raise CorpusRootUnavailable(
            f"{corp.name} corpus: {len(errs)} directory(ies) could not be read "
            f"(first: {first['path']} — {first['error']}) — refusing to treat "
            f"unreadable as deleted; the index is untouched.")


def _existing_chunk_count(corp: Corpus) -> int:
    summary = index_summary(corp.index_dir) or {}
    try:
        return int(summary.get("chunk_count") or 0)
    except (TypeError, ValueError):
        return 0


def _rebuild(cfg: Config, progress: bool = True,
             corpus: str | Corpus | None = None) -> dict[str, int]:
    corp = resolve_corpus(cfg, corpus)
    pins = _grammar_pins(corp)
    report: dict[str, Any] = {}
    files = _scan_corpus(corp, report)
    _guard_scan(corp, report, _existing_chunk_count(corp))
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

    excluded = report.get("excluded") or []
    _save_index(corp, embs,
                {"model": cfg.model, "file_meta": file_meta, "chunks": chunks,
                 "excluded": excluded}, pins)
    size_mb = embs.nbytes / 1e6
    out: dict[str, Any] = {"files": len(files), "chunks": len(chunks),
                           "size_mb": round(size_mb, 1)}
    if excluded:
        out["excluded"] = [e["path"] for e in excluded]
    return out


def inspect(cfg: Config, rel: str,
            corpus: str | Corpus | None = None) -> list[dict[str, Any]]:
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
           corpus: str | Corpus | None = None) -> dict[str, int]:
    """Incremental update by (mtime, size) diff (serialized against other
    writers of the SAME corpus). Falls back to rebuild when the index is
    missing/corrupted or was built with another model/chunk schema."""
    corp = resolve_corpus(cfg, corpus)
    with _writer_lock(cfg, corp.name):
        return _update(cfg, only_files, corp)


def _update(cfg: Config, only_files: list[str] | None = None,
            corpus: str | Corpus | None = None) -> dict[str, int]:
    corp = resolve_corpus(cfg, corpus)
    # First, before the index is even read: a writer that may not write this
    # corpus must not rebuild it either.
    pins = _grammar_pins(corp)
    loaded = _load_index(corp.index_dir)
    if loaded is None:
        return _rebuild(cfg, progress=False, corpus=corp)
    embs, meta = loaded
    if meta.get("model") != cfg.model:
        return _rebuild(cfg, progress=False, corpus=corp)
    if meta.get("chunk_schema") != corp.chunk_schema:
        # D3: compared against THIS corpus's format, not a global constant.
        # Zero-downtime migration falls out of the numbers rather than a special
        # case: every generation already on disk was stamped with the old global
        # `chunking.CHUNK_SCHEMA_VERSION`, and that is exactly what the memory
        # and sources corpora now declare — so the upgrade is a no-op for them.
        # A genuinely stale generation (older markdown format, or an absent
        # stamp) still forces the full rebuild it always did.
        return _rebuild(cfg, progress=False, corpus=corp)  # old chunk format -> full rebuild
    if pins is not None and meta.get(GRAMMAR_PINS_KEY) != pins:
        # CODE-GRAPH D1: the lock moved, so every boundary may have moved with
        # it. Compared with the PINS, never with what this interpreter loaded
        # -- the guard above already refused a writer whose wheels differ, so
        # two interpreters taking turns cannot ping-pong the corpus.
        return _rebuild(cfg, progress=False, corpus=corp)

    report: dict[str, Any] = {}
    files = _scan_corpus(corp, report)
    # An incomplete scan here is the dangerous one: everything it failed to see
    # is about to be classified as `deleted` and purged a few lines below.
    _guard_scan(corp, report, len(meta.get("chunks") or []))
    # `_update` re-commits the LOADED meta, so a field written only by
    # `_rebuild` would be carried forward stale forever — refresh it here.
    meta["excluded"] = report.get("excluded") or []
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

    excluded = [e["path"] for e in (report.get("excluded") or [])]
    if not added_or_changed and not deleted:
        out: dict[str, Any] = {"added": 0, "deleted": 0, "chunks": embs.shape[0]}
        if excluded:
            out["excluded"] = excluded
        return out

    # drop chunks of deleted+changed files (keep-mask)
    dirty = set(deleted) | set(added_or_changed)
    keep = np.array([c["file"] not in dirty for c in meta["chunks"]], dtype=bool)
    embs = embs[keep]
    meta["chunks"] = [c for c, k in zip(meta["chunks"], keep, strict=False) if k]
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
    _save_index(corp, embs, meta, pins)
    out = {"added": len(added_or_changed), "deleted": len(deleted),
           "chunks": len(meta["chunks"])}
    if excluded:
        out["excluded"] = excluded
    return out

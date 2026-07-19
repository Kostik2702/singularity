"""Search cache with stale marker (CONTRACT.md §5). Cache is evidence, not
truth: served-stale candidates keep their original retrieved_at and are
explicitly marked health=cached, stale=true."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def cache_dir() -> Path:
    root = os.environ.get("PSCOUT_CACHE_DIR")
    return Path(root) if root else Path.home() / ".singularity" / "provider-cache"


def cache_key(provider: str, queries: list[str], capabilities: list[str]) -> str:
    normalized = json.dumps(
        {"p": provider,
         "q": sorted(q.strip().lower() for q in queries),
         "c": sorted(capabilities)},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _path(key: str) -> Path:
    return cache_dir() / f"{key}.json"


CACHE_SCHEMA = 1  # bump when the candidate contract / gate logic changes


def store(key: str, payload: dict) -> None:
    payload = {"cache_schema": CACHE_SCHEMA, **payload}
    dest = _path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{key}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(key: str) -> dict | None:
    """None for unreadable OR different-schema entries — a record written by
    older gate logic must never be served as current (review E)."""
    try:
        entry = json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if entry.get("cache_schema") != CACHE_SCHEMA:
        return None
    return entry


def serve_stale(entry: dict) -> list[dict]:
    """Mark cached candidates as stale evidence (§5). The import gate is
    RE-COMPUTED and records failing validation are dropped — the cache is a
    convenience layer, never a trust boundary (review E: a tampered/stale
    entry with importable=true must not reach consumers)."""
    from .candidate import CandidateError, import_gate, validate
    out = []
    for rec in entry.get("candidates", []):
        rec = dict(rec)
        rec["health"] = "cached"
        rec["stale"] = True
        rec["importable"], rec["import_block_reason"] = import_gate(rec)
        try:
            validate(rec)
        except CandidateError:
            continue
        out.append(rec)
    return out

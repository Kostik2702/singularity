"""Digest planner: learnings/active sections -> reference/rule/howto/decision
merge candidates (T11, spec FR-4).

Report-only: `plan()` never mutates notes. It reads ONLY the committed index
generation (embeddings.npy + metadata.json via search.indexer.index_files) --
no encoder/daemon/model load, so `mb digest plan` stays fast and offline.
Once a human/agent has merged a section into a target note, `mark()` records
its (file, section) identity in an atomic `.mb/digest.json` journal so it
never resurfaces in a later plan.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config

SOURCE_PREFIX = "learnings/active/"
TARGET_TYPES = {"reference", "rule", "howto", "decision"}
JOURNAL_NAME = "digest.json"


# ---------------------------------------------------------------------------
# journal (.mb/digest.json) — atomic tmp+replace, same convention as
# config.Config.save()/backup.py
# ---------------------------------------------------------------------------

def _journal_path(cfg: Config) -> Path:
    return cfg.mb_dir / JOURNAL_NAME


def _read_journal(cfg: Config) -> dict[str, Any]:
    path = _journal_path(cfg)
    if not path.exists():
        return {"marked": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"marked": {}}
    if not isinstance(data, dict) or not isinstance(data.get("marked"), dict):
        return {"marked": {}}
    return data


def _write_journal(cfg: Config, data: dict[str, Any]) -> None:
    from ..config import atomic_write_json
    atomic_write_json(_journal_path(cfg), data)


def identity_key(file: str, section: str) -> str:
    return f"{file}#{section}"


def mark(cfg: Config, identities: list[tuple[str, str]]) -> int:
    """Atomically record (file, section) identities as processed. Tolerant of
    identities no longer present in the current index (renamed/deleted
    source) -- marking always succeeds, it never looks the identity up
    against the live index. Returns the count marked."""
    data = _read_journal(cfg)
    now = datetime.now().isoformat(timespec="seconds")
    for file, section in identities:
        data["marked"][identity_key(file, section)] = {"marked_at": now}
    _write_journal(cfg, data)
    return len(identities)


def _pool(embs: np.ndarray, idxs: list[int]) -> np.ndarray | None:
    """Mean-pooled, re-normalized vector over the given chunk rows.

    W05: the implementation moved to `search.similarity.mean_pool`, shared with
    curator's note vectors and `ingest_plan` — `mb ingest plan` scores source
    chunks against exactly these vectors, so two copies of the aggregation
    would mean two different notions of 'similar'."""
    from ..search.similarity import mean_pool
    return mean_pool(embs, idxs)


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def _group_sources(chunks: list[dict[str, Any]],
                   project_filter: set[str] | None
                   ) -> dict[tuple[str, str], dict[str, Any]]:
    """(file, section) -> {project, idxs}. Source = active-learning chunks,
    personal excluded (spec: 'Personal notes are never digest targets or
    auto-edited' extends to never being a source either -- personal insights
    are never proposed for promotion into shared notes). project_filter, if
    given, is an already-resolved concrete id set (FR-1 resolver) restricting
    which source projects are considered; "general" is a literal member for
    an unprojected chunk, matching engine._match_filters' convention."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for i, c in enumerate(chunks):
        file = str(c.get("file", ""))
        if not file.startswith(SOURCE_PREFIX):
            continue
        project = c.get("project")
        if project == "personal":
            continue
        if project_filter is not None and (project or "general") not in project_filter:
            continue
        key = (file, str(c.get("section", "")))
        g = groups.setdefault(key, {"project": project, "idxs": []})
        g["idxs"].append(i)
    return groups


def _group_targets(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """file -> {project, type, idxs}. Target = reference/rule/howto/decision
    chunks, pooled at whole-note granularity (learnings merge INTO an
    existing note, not into one of its sections); personal excluded. A file
    under learnings/ is never a target even if its frontmatter mistakenly
    overrides `type` to one of TARGET_TYPES -- learnings are structurally the
    input side of digestion, never the output side."""
    groups: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(chunks):
        if c.get("type") not in TARGET_TYPES:
            continue
        file = str(c.get("file", ""))
        if file.startswith("learnings/"):
            continue
        project = c.get("project")
        if project == "personal":
            continue
        g = groups.setdefault(file, {"project": project, "type": c.get("type"), "idxs": []})
        g["idxs"].append(i)
    return groups


def _compatible(source_project: str | None, target_project: str | None) -> bool:
    """A target is compatible when it is general (project-agnostic knowledge
    base) or shares the source's exact project -- this is what gives
    'project isolation' (a proj-A source never matches a proj-B-only target)
    together with 'general fallback' (any source can still match a general
    target)."""
    return target_project is None or target_project == source_project


# ---------------------------------------------------------------------------

def plan(cfg: Config, project_filter: set[str] | None = None) -> dict[str, Any]:
    """Report-only merge candidates for every un-marked active-learning
    section. Deterministic and repeat-stable: candidates are score-sorted
    with a file-name tiebreak, and entries themselves are ordered by their
    best candidate score (most actionable first) then (file, section) for
    entries with no candidate at all."""
    from ..search import indexer
    embs, chunks, status = indexer.load_committed_index(cfg.memory_index_dir)
    if status != "ok":
        return {"entries": [], "index_status": status, "source_count": 0, "target_count": 0}

    marked = set(_read_journal(cfg)["marked"])
    threshold = float(cfg.get("digest_score_threshold"))
    top_k = int(cfg.get("digest_top_k"))

    sources = _group_sources(chunks, project_filter)
    targets = _group_targets(chunks)

    target_vecs: list[tuple[str, str, str | None, np.ndarray]] = []
    for file, info in targets.items():
        v = _pool(embs, info["idxs"])
        if v is not None:
            target_vecs.append((file, info["type"], info["project"], v))

    entries: list[dict[str, Any]] = []
    for (file, section), info in sources.items():
        if identity_key(file, section) in marked:
            continue
        source_project = info["project"]
        candidates: list[dict[str, Any]] = []
        v = _pool(embs, info["idxs"])
        if v is not None:
            scored = []
            for t_file, t_type, t_project, t_vec in target_vecs:
                if not _compatible(source_project, t_project):
                    continue
                score = float(np.dot(v, t_vec))
                if score < threshold:
                    continue
                scored.append((score, t_file, t_type, t_project))
            scored.sort(key=lambda x: (-x[0], x[1]))
            candidates = [{"file": f, "type": t, "project": p, "score": round(s, 4)}
                         for s, f, t, p in scored[:top_k]]
        entries.append({"file": file, "section": section,
                        "project": source_project, "candidates": candidates})

    entries.sort(key=lambda e: (
        -(e["candidates"][0]["score"] if e["candidates"] else -1.0),
        e["file"], e["section"]))
    return {"entries": entries, "index_status": status,
            "source_count": len(sources), "target_count": len(targets)}

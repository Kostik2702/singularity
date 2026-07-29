"""Memory curator (MB-10): vault health + semantic link suggestions.

Two layers (port of Genevra memory-validation + memory-doctor):
  1. Mechanics (zero tokens, no model): broken wikilinks, mandatory
     frontmatter, decisions without owner/source, duplicate titles.
  2. Semantics (reuses the EXISTING index, no re-encoding): note-level
     embeddings = mean of chunk embeddings -> cosine pairs above threshold
     that have no wikilink in either direction -> link SUGGESTIONS.
     Plus orphans (no in/out links) and stale active notes.

The curator NEVER writes to the vault — it only reports/suggests
(Never Decide Alone for structure; a human or the assistant applies
approved suggestions).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import Config
from .search.chunking import parse_frontmatter, _WIKILINK_RE

# type is enforced only where the note contract applies (MB-08 folders);
# project/analysis docs need just `created`; title may come from H1
CONTRACT_FOLDERS = ("learnings", "decisions", "howto", "incidents",
                    "rules", "reference", "daily")
SUGGEST_THRESHOLD = 0.65
SUGGEST_LIMIT = 15
STALE_DAYS = 60
# housekeeping notes: excluded from orphan/suggestion analysis (not from validation)
EXCLUDED_PREFIXES = ("_moc/", "changelog/", "daily/", "templates/")

# T13: chunk-level semantic lint (near-duplicate / supersession detection).
# Higher bar than SUGGEST_THRESHOLD -- that one flags "topically related, no
# link yet" at whole-note granularity; this flags "near-identical content" at
# chunk granularity, a much stronger claim that needs a much higher score.
DUPLICATE_THRESHOLD = 0.90
DUPLICATE_LIMIT = 15
SUPERSESSION_GAP_DAYS = 60   # same magnitude as STALE_DAYS, distinct concept -- kept separate
SUPERSESSION_LIMIT = 15
_BLOCK_SIZE = 512            # tile size for blockwise cosine -- bounds peak memory to O(block^2)

# W04 provenance contract: a note derived from an ingested source carries
# `sources: [<source_id>]`. Two failure modes are worth reporting (never
# fatal -- the exit code still depends on broken_links alone):
#   * a note cites an id the registry does not have (renamed/lost provenance);
#   * a source sits at status "new" long enough that nobody is going to
#     process it without a nudge.
ORPHAN_SOURCE_DAYS = 30
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _scan_notes(cfg: Config) -> dict[str, dict[str, Any]]:
    """rel_path -> {frontmatter, title, aliases, outlinks, mtime} for all vault notes."""
    skip_dirs = set(cfg.get("skip_dirs")) - {"templates"}  # validate templates too? no:
    skip_dirs.add("templates")
    notes: dict[str, dict[str, Any]] = {}
    for path in cfg.vault.rglob("*.md"):
        rel = path.relative_to(cfg.vault).as_posix()
        first = rel.split("/", 1)[0]
        if first in skip_dirs:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm, body = parse_frontmatter(raw)
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = (fm.get("title") or (m.group(1).strip() if m else path.stem))
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.strip("[]").split(",") if a.strip()]
        # wikilinks only OUTSIDE code: [[..]] inside fenced blocks / `inline code`
        # is syntax examples (bash [[ tests ]]), not links
        linkable = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
        linkable = re.sub(r"`[^`\n]*`", " ", linkable)
        linkable = linkable.replace("\\|", "|")  # escaped pipe in md tables
        outlinks = [mt.group(1).strip() for mt in _WIKILINK_RE.finditer(linkable)]
        outlinks = [l for l in outlinks if l and l != "..."]
        notes[rel] = {
            "fm": fm, "title": str(title).strip('"'), "aliases": aliases,
            "outlinks": outlinks, "mtime": path.stat().st_mtime,
            # W08 needs section headings (open questions, un-digested learning
            # sections). Collected here because the body is already in memory —
            # a second full pass over the vault for this would double the cost
            # of every `mb curator gaps` run.
            "headings": _SECTION_RE.findall(body),
        }
    return notes


def _name_index(notes: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Resolvable link name (lowercase) -> rel_path. Stems, titles, aliases."""
    idx: dict[str, str] = {}
    for rel, info in notes.items():
        idx[Path(rel).stem.lower()] = rel
        idx[str(info["title"]).lower()] = rel
        for a in info["aliases"]:
            idx[str(a).lower()] = rel
    return idx


# ---------------------------------------------------------------------------
# Layer 1 — mechanics
# ---------------------------------------------------------------------------

def note_source_ids(frontmatter: dict[str, Any]) -> list[str]:
    """`sources:` frontmatter as a list of ids. Accepts a YAML list, a bare
    string, or an inline `[a, b]` written as text by a template."""
    value = frontmatter.get("sources")
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return [v.strip().strip("'\"") for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip().strip("'\"") for v in value if str(v).strip()]
    return [str(value)]


def _provenance_issues(cfg: Config, notes: dict[str, dict[str, Any]]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(source_not_in_registry, orphan_source). Tolerant: no registry at all
    means no provenance issues, not an error -- ingest is opt-in."""
    from . import sources as sources_mod

    registry = sources_mod.read_registry(cfg)["sources"]
    dangling: list[dict[str, Any]] = []
    for rel, info in sorted(notes.items()):
        for sid in note_source_ids(info["fm"]):
            if sid not in registry:
                dangling.append({"file": rel, "source_id": sid})

    orphans: list[dict[str, Any]] = []
    cutoff = time.time() - ORPHAN_SOURCE_DAYS * 86400
    for sid, record in sorted(registry.items()):
        if not isinstance(record, dict) or record.get("status") != "new":
            continue
        ingested = _frontmatter_date(str(record.get("ingested_at", ""))[:10])
        if ingested is not None and ingested < cutoff:
            orphans.append({"source_id": sid, "title": record.get("title"),
                            "ingested_at": record.get("ingested_at")})
    return dangling, orphans


def validate(cfg: Config) -> dict[str, list]:
    notes = _scan_notes(cfg)
    names = _name_index(notes)
    issues: dict[str, list] = {"broken_links": [], "missing_frontmatter": [],
                               "decision_no_owner": [], "duplicate_titles": [],
                               "source_not_in_registry": [], "orphan_source": []}

    titles: dict[str, list[str]] = defaultdict(list)
    for rel, info in notes.items():
        fm = info["fm"]
        missing = []
        if not fm.get("created"):
            missing.append("created")
        if not fm.get("title") and info["title"] == Path(rel).stem:
            missing.append("title|H1")
        if rel.split("/", 1)[0] in CONTRACT_FOLDERS and not fm.get("type"):
            missing.append("type")
        if missing:
            issues["missing_frontmatter"].append({"file": rel, "missing": missing})
        if fm.get("type") == "decision" and not (fm.get("owner") and fm.get("source")):
            issues["decision_no_owner"].append({"file": rel})
        titles[str(info["title"]).lower()].append(rel)
        for link in info["outlinks"]:
            if link.lower() not in names:
                issues["broken_links"].append({"file": rel, "link": link})
    for title, files in titles.items():
        if len(files) > 1:
            issues["duplicate_titles"].append({"title": title, "files": files})

    dangling, orphans = _provenance_issues(cfg, notes)
    issues["source_not_in_registry"] = dangling
    issues["orphan_source"] = orphans
    return issues


# ---------------------------------------------------------------------------
# Layer 2 — semantics (from the existing index, no model needed)
# ---------------------------------------------------------------------------

def _note_embeddings(cfg: Config) -> tuple[list[str], np.ndarray]:
    # T13: was reading memory_index/embeddings.npy directly -- a legacy flat
    # path that a vault on the transactional generation layout (AUD-04)
    # leaves behind stale (it's whatever the last flat-layout write happened
    # to be, not the current generation). load_committed_index() resolves
    # the actual CURRENT generation the same way digest.plan() already does.
    from .search import indexer
    from .search.similarity import group_indices, pooled_vectors

    embs, chunks, status = indexer.load_committed_index(cfg.memory_index_dir)
    if status != "ok":
        return [], np.zeros((0, 1))
    # W05: pooling now lives in search.similarity (one implementation shared
    # with digest and ingest_plan). Grouping order is unchanged -- first
    # appearance in the chunk list -- because the caller's output ordering
    # depends on it.
    groups = group_indices(
        chunks, key=lambda c: c["file"],
        predicate=lambda c: not str(c["file"]).startswith(EXCLUDED_PREFIXES))
    return pooled_vectors(embs, groups)


def link_graph(cfg: Config) -> tuple[dict[str, dict[str, Any]], dict[str, str],
                                     dict[str, set[str]], dict[str, set[str]]]:
    """(notes, name_index, outgoing, incoming) with links RESOLVED to real
    files. Factored out of `analyze()` so `gaps.py` reaches the same graph
    instead of rebuilding it (W08) — one vault scan, two consumers."""
    notes = _scan_notes(cfg)
    names = _name_index(notes)
    outgoing: dict[str, set[str]] = {rel: set() for rel in notes}
    incoming: dict[str, set[str]] = {rel: set() for rel in notes}
    for rel, info in notes.items():
        for link in info["outlinks"]:
            target = names.get(link.lower())
            if target and target != rel:
                outgoing[rel].add(target)
                incoming[target].add(rel)
    return notes, names, outgoing, incoming


def analyze(cfg: Config) -> dict[str, Any]:
    """Orphans, stale notes, and wikilink suggestions."""
    notes, names, outgoing, incoming = link_graph(cfg)

    orphans = [rel for rel in notes
               if not outgoing[rel] and not incoming[rel]
               and not rel.startswith(EXCLUDED_PREFIXES)]

    now = time.time()
    stale = [rel for rel, info in notes.items()
             if info["fm"].get("status") == "active"
             and (now - info["mtime"]) > STALE_DAYS * 86400]

    suggestions: list[dict[str, Any]] = []
    try:
        files, vecs = _note_embeddings(cfg)
    except (OSError, ValueError, json.JSONDecodeError):
        files, vecs = [], np.zeros((0, 1))
    if len(files) >= 2:
        sim = vecs @ vecs.T
        np.fill_diagonal(sim, 0.0)
        pairs = np.argwhere(sim > SUGGEST_THRESHOLD)
        scored = sorted(((float(sim[i, j]), i, j) for i, j in pairs if i < j),
                        reverse=True)
        for score, i, j in scored:
            a, b = files[i], files[j]
            if a in notes and b in notes:
                if b in outgoing.get(a, set()) or a in outgoing.get(b, set()):
                    continue
            suggestions.append({"a": a, "b": b, "score": round(score, 3),
                                "suggest": f"[[{Path(b).stem}]] <-> [[{Path(a).stem}]]"})
            if len(suggestions) >= SUGGEST_LIMIT:
                break

    return {"notes": len(notes), "orphans": sorted(orphans), "stale": sorted(stale),
            "link_suggestions": suggestions}


# ---------------------------------------------------------------------------
# Layer 3 — T13 semantic lint: chunk-level near-duplicates, supersession
# candidates, context.json budget/reference issues. Report-only, same
# invariant as layers 1/2 -- never writes, never mutates note status.
# ---------------------------------------------------------------------------

def _eligible_chunk_rows(chunks: list[dict[str, Any]],
                         embs: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Same exclusions as digest.py's source/target grouping: personal
    project chunks and housekeeping-prefixed files never enter lint."""
    keep = [i for i, c in enumerate(chunks)
           if c.get("project") != "personal"
           and not str(c.get("file", "")).startswith(EXCLUDED_PREFIXES)]
    if not keep:
        return np.zeros((0, embs.shape[1] if embs.size else 1)), []
    return embs[keep], [chunks[i] for i in keep]


def _blockwise_pairs(embs: np.ndarray, threshold: float,
                     block_size: int = _BLOCK_SIZE) -> list[tuple[int, int, float]]:
    """(i, j, score) for every i<j pair with cosine similarity > threshold.

    W05: the implementation moved to `search.similarity.blockwise_pairs`,
    shared with the contradiction detector — both need the same O(block^2)
    memory bound and the same i<j-by-index rule."""
    from .search.similarity import blockwise_pairs
    return blockwise_pairs(embs, threshold, block_size)


def _duplicate_pairs(cfg: Config) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """(same_file pairs, cross_file pairs, index_status) -- UNCAPPED and
    score-sorted descending; the caller caps same/cross_file for the report
    but supersession derivation needs the full sorted cross_file list first
    (see _supersession_candidates)."""
    from .search import indexer

    embs, chunks, status = indexer.load_committed_index(cfg.memory_index_dir)
    if status != "ok":
        return [], [], status
    sub_embs, sub_chunks = _eligible_chunk_rows(chunks, embs)
    if sub_embs.shape[0] < 2:
        return [], [], status

    same_file: list[dict[str, Any]] = []
    cross_file: list[dict[str, Any]] = []
    for i, j, score in _blockwise_pairs(sub_embs, DUPLICATE_THRESHOLD):
        a, b = sub_chunks[i], sub_chunks[j]
        a_file, b_file = a.get("file"), b.get("file")
        a_section, b_section = a.get("section"), b.get("section")
        if a_file == b_file and a_section == b_section:
            # sequential sub-chunks of ONE long section (the chunker splits
            # long sections into several chunk_index parts sharing the same
            # section/line range) -- naturally near-identical by construction,
            # not a genuine duplicate. Confirmed live: real vault top hits
            # were exactly this (e.g. a 58-line section split into 6 parts).
            continue
        entry = {"a": {"file": a_file, "section": a_section},
                "b": {"file": b_file, "section": b_section},
                "score": round(score, 4)}
        (same_file if a_file == b_file else cross_file).append(entry)
    same_file.sort(key=lambda e: (-e["score"], e["a"]["file"], e["a"]["section"] or ""))
    cross_file.sort(key=lambda e: (-e["score"], e["a"]["file"], e["b"]["file"]))
    return same_file, cross_file, status


def _frontmatter_date(value: Any) -> Optional[float]:
    if isinstance(value, _dt.datetime):
        return value.timestamp()
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min).timestamp()
    if isinstance(value, str):
        try:
            return _dt.datetime.strptime(value.strip(), "%Y-%m-%d").timestamp()
        except ValueError:
            return None
    return None


def _note_recency(info: dict[str, Any]) -> float:
    """Frontmatter `created` date if present/parseable, else file mtime --
    spec: 'supersession candidates by frontmatter date then mtime'."""
    created = _frontmatter_date(info["fm"].get("created"))
    return created if created is not None else info["mtime"]


def _supersession_candidates(notes: dict[str, dict[str, Any]],
                             cross_file_pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per FILE pair (not per section pair) -- cross_file_pairs
    is already score-sorted descending, so the first pair seen for a given
    (a_file, b_file) key is its highest-scoring section pair, the best
    representative. Only a >=60d recency gap makes a near-duplicate pair a
    supersession candidate; same-day/close-together near-duplicates are just
    duplicates, not one superseding the other."""
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []
    for pair in cross_file_pairs:
        a_file, b_file = pair["a"]["file"], pair["b"]["file"]
        key = tuple(sorted((a_file, b_file)))
        if key in seen:
            continue
        seen.add(key)
        a_info, b_info = notes.get(a_file), notes.get(b_file)
        if a_info is None or b_info is None:
            continue
        a_recency, b_recency = _note_recency(a_info), _note_recency(b_info)
        gap_days = abs(a_recency - b_recency) / 86400
        if gap_days < SUPERSESSION_GAP_DAYS:
            continue
        older, newer = (a_file, b_file) if a_recency < b_recency else (b_file, a_file)
        candidates.append({"older": older, "newer": newer,
                           "gap_days": round(gap_days, 1), "score": pair["score"]})
    candidates.sort(key=lambda c: (-c["gap_days"], c["older"], c["newer"]))
    return candidates


def _check_note_budget(project: str, module: Optional[str], vault: Path,
                       rel_path: str, max_lines: int) -> list[dict[str, Any]]:
    from .context_map import _resolve_within_vault

    resolved = _resolve_within_vault(vault, rel_path)
    if resolved is None or not resolved.is_file():
        return [{"project": project, "module": module, "note": rel_path,
                 "kind": "missing_note"}]
    try:
        raw = resolved.read_text(encoding="utf-8-sig")
    except OSError:
        return [{"project": project, "module": module, "note": rel_path,
                 "kind": "missing_note"}]
    _, body = parse_frontmatter(raw)
    n = len(body.strip("\n").splitlines())
    if n > max_lines:
        return [{"project": project, "module": module, "note": rel_path,
                 "kind": "over_budget", "lines": n, "budget": max_lines}]
    return []


def _context_issues(cfg: Config) -> list[dict[str, Any]]:
    """context.json missing-note and over-budget issues, checked against the
    SAME config keys runtime truncation already uses (spec FR-3: 'Budget
    config is single source for runtime truncation and curator warnings') --
    never a second, possibly-drifted budget definition."""
    from .context_map import DEFAULT_BRAIN_MAX_LINES, load_context_map
    from .module_context import DEFAULT_MODULE_NOTE_MAX_LINES

    ctx = load_context_map(cfg.vault)
    if not isinstance(ctx, dict):
        return []
    brain_max = int(cfg.get("brain_max_lines", DEFAULT_BRAIN_MAX_LINES))
    module_max = int(cfg.get("module_note_max_lines", DEFAULT_MODULE_NOTE_MAX_LINES))
    issues: list[dict[str, Any]] = []
    for project, entry in ctx.items():
        if not isinstance(entry, dict):
            continue
        brain_note = entry.get("brain_note")
        if isinstance(brain_note, str) and brain_note.strip():
            issues.extend(_check_note_budget(project, None, cfg.vault, brain_note, brain_max))
        context_map = entry.get("context_map")
        if isinstance(context_map, dict):
            for module, mentry in context_map.items():
                if not isinstance(mentry, dict):
                    continue
                note = mentry.get("note")
                if isinstance(note, str) and note.strip():
                    issues.extend(_check_note_budget(project, module, cfg.vault, note, module_max))
    return issues


def lint(cfg: Config) -> dict[str, Any]:
    """T13: chunk-level near-duplicates (same-file/cross-file), supersession
    candidates, and context.json reference/budget issues. Pure report --
    never writes, never mutates note status/frontmatter."""
    same_file, cross_file, index_status = _duplicate_pairs(cfg)
    supersession = _supersession_candidates(_scan_notes(cfg), cross_file)
    return {
        "index_status": index_status,
        "duplicates": {"same_file": same_file[:DUPLICATE_LIMIT],
                       "cross_file": cross_file[:DUPLICATE_LIMIT]},
        "supersession": supersession[:SUPERSESSION_LIMIT],
        "context_issues": _context_issues(cfg),
    }


# ---------------------------------------------------------------------------

def run(cfg: Config, as_json: bool = False) -> int:
    issues = validate(cfg)
    report = analyze(cfg)
    lint_report = lint(cfg)
    hard = sum(len(v) for v in issues.values())

    if as_json:
        print(json.dumps({"issues": issues, **report, "lint": lint_report},
                         ensure_ascii=False, indent=1))
        return 1 if issues["broken_links"] else 0

    print(f"== mb curator — {report['notes']} notes ==")
    for key, label in (("broken_links", "Broken wikilinks"),
                       ("missing_frontmatter", "Missing mandatory frontmatter"),
                       ("decision_no_owner", "Decisions without owner/source"),
                       ("duplicate_titles", "Duplicate titles"),
                       ("source_not_in_registry", "Notes citing an unknown source_id"),
                       ("orphan_source", f"Sources unprocessed >{ORPHAN_SOURCE_DAYS}d")):
        items = issues[key]
        print(f"\n[{'FAIL' if items and key == 'broken_links' else 'WARN' if items else 'PASS'}] "
              f"{label}: {len(items)}")
        for it in items[:10]:
            print(f"   - {it}")

    print(f"\nOrphans (no links in/out): {len(report['orphans'])}")
    for rel in report["orphans"][:10]:
        print(f"   - {rel}")
    print(f"Stale active notes (>{STALE_DAYS}d): {len(report['stale'])}")
    for rel in report["stale"][:10]:
        print(f"   - {rel}")

    print(f"\nLink suggestions (cosine>{SUGGEST_THRESHOLD}, no existing link) — "
          f"NOT applied, approve first:")
    if not report["link_suggestions"]:
        print("   (none)")
    for s in report["link_suggestions"]:
        print(f"   {s['score']:.2f}  {s['a']}  <->  {s['b']}")

    dup = lint_report["duplicates"]
    print(f"\nDuplicate sections (same file, cosine>{DUPLICATE_THRESHOLD}): {len(dup['same_file'])}")
    for d in dup["same_file"][:10]:
        print(f"   {d['score']:.2f}  {d['a']['file']} :: {d['a']['section']}  <->  {d['b']['section']}")
    print(f"Duplicate sections (cross file, cosine>{DUPLICATE_THRESHOLD}): {len(dup['cross_file'])}")
    for d in dup["cross_file"][:10]:
        print(f"   {d['score']:.2f}  {d['a']['file']}::{d['a']['section']}  <->  "
              f"{d['b']['file']}::{d['b']['section']}")
    print(f"Supersession candidates (gap>={SUPERSESSION_GAP_DAYS}d): {len(lint_report['supersession'])}")
    for s in lint_report["supersession"][:10]:
        print(f"   {s['older']} -> possibly superseded by {s['newer']} (gap {s['gap_days']}d)")
    print(f"Context budget/reference issues: {len(lint_report['context_issues'])}")
    for c in lint_report["context_issues"][:10]:
        where = f"{c['project']}/{c['module']}" if c.get("module") else c["project"]
        print(f"   [{c['kind']}] {where}: {c['note']}")
    return 1 if issues["broken_links"] else 0

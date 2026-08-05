"""Deterministic ingest fan-out (WIKI-LOOP W05).

Answers, with numbers rather than a guess: *which existing wiki pages does this
source oblige us to touch?* The method's core claim is that one document
should ripple into 10-15 related pages — done by hand that degrades into
"append something somewhere", which is how knowledge stops compounding.

Contract, identical to `digest.plan()` (digest.py:144):
  * **report-only** — this module has no write path at all (D5, enforced by
    `tests/test_wiki_loop_report_only.py`);
  * reads ONLY committed index generations via `indexer.load_committed_index`
    — no encoder, no daemon, no model load, fully offline;
  * a missing/torn index is REPORTED as a status, never raised.

Cross-corpus by construction (D1/D2): source chunks come from
`.mb/sources_index/`, targets from `memory_index`. Both must have been built
by the same model — cosine between two embedding spaces is noise, and quietly
returning noise here is worse than refusing, because the caller cannot tell.
"""
from __future__ import annotations

import re
from typing import Any


from ..config import Config

# Housekeeping roots that are never wiki pages (same set curator excludes from
# orphan/suggestion analysis) plus `learnings/`: active learnings are the INPUT
# side of the knowledge pipeline (digest promotes them into the canon), never
# the page a source should be merged into.
EXCLUDED_TARGET_PREFIXES = ("_moc/", "changelog/", "daily/", "templates/",
                            "learnings/", ".mb/")
DEFAULT_SUMMARY_TYPE = "reference"
SUMMARY_FOLDER_BY_TYPE = {"learning": "learnings/active", "decision": "decisions",
                          "howto": "howto", "incident": "incidents", "rule": "rules",
                          "reference": "reference", "daily": "daily"}
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|([^\]]+))?\]\]")
_MIN_NEW_PAGE_CHARS = 3
_MAX_NEW_PAGE_CHARS = 60
# "1. ", "4) ", "II. " — an enumerator marks DOCUMENT STRUCTURE, not an entity.
# Verified on the real vault: without this filter every numbered section of an
# ingested design doc ("4. Карта интеграции...") was proposed as a wiki page.
_ENUMERATOR_RE = re.compile(r"^\s*(?:\d+|[ivxlcIVXLC]+)[.)]\s+")
# Headings that are a document's skeleton in this vault's note templates.
_STRUCTURAL_HEADINGS = {
    "контекст", "решение", "решения", "ссылки", "кто и когда", "суть", "находки",
    "итог", "итоги", "выводы", "результат", "результаты", "риски", "шаги",
    "пример", "примеры", "приложение", "открытые вопросы", "что дальше",
    "бизнес-цель", "технический scope", "acceptance", "размер", "происхождение",
    "summary", "overview", "context", "decision", "links", "notes", "todo",
    "open questions", "appendix", "example", "examples", "lesson", "steps",
    "corrective actions", "diagnostic questions", "what was expected",
}


def _section_label(chunk: dict[str, Any]) -> str:
    """Never empty: `why` without both sides is unverifiable, and an
    unverifiable reason is exactly what the fan-out gate rejects."""
    for key in ("section", "title"):
        value = str(chunk.get(key) or "").strip()
        if value:
            return value
    return "(preamble)"


def _is_target(chunk: dict[str, Any]) -> bool:
    file = str(chunk.get("file", ""))
    if not file or file.startswith(EXCLUDED_TARGET_PREFIXES):
        return False
    return chunk.get("project") != "personal"


def _summary_target(cfg: Config, title: str, source_id: str,
                    note_type: str) -> dict[str, Any]:
    """Where the summary note would live. The stem is sanitized EXACTLY like
    `cmd_note` does (cli.py), so the proposed path is the one the suggested
    command actually produces — a mismatch here would send the skill chasing a
    file that never appears."""
    folder = SUMMARY_FOLDER_BY_TYPE.get(note_type, "reference")
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    rel = f"{folder}/{safe or 'source'}.md"
    return {"file": rel, "type": note_type,
            "exists": (cfg.vault / rel).exists(),
            "command": f'mb note new {note_type} "{title}" --from-source {source_id}'}


def _known_names(chunks: list[dict[str, Any]]) -> set[str]:
    """Lowercased names an existing note answers to — stems, titles, aliases.

    Built from index chunks rather than a vault scan on purpose: `mb ingest
    plan` must stay in the same latency class as `mb digest plan` (~0.3s), and
    a full `_scan_notes` would triple that for a name set the index already
    carries."""
    names: set[str] = set()
    for chunk in chunks:
        file = str(chunk.get("file", ""))
        if file:
            stem = file.rsplit("/", 1)[-1]
            names.add(stem[:-3].lower() if stem.endswith(".md") else stem.lower())
        title = str(chunk.get("title") or "").strip()
        if title:
            names.add(title.lower())
        for alias in chunk.get("aliases") or []:
            names.add(str(alias).strip().lower())
    return names


def _looks_like_page_name(text: str) -> bool:
    """Would this heading make sense as the TITLE of a wiki page?

    Deterministic rejects only — anything statistical belongs behind
    `mb curator gaps --heuristic`, where it is labelled as a guess."""
    value = str(text or "").strip()
    if not (_MIN_NEW_PAGE_CHARS <= len(value) <= _MAX_NEW_PAGE_CHARS):
        return False
    if value.isdigit() or _ENUMERATOR_RE.match(value):
        return False
    if "`" in value or "|" in value:            # code span / table fragment
        return False
    if value.endswith((".", "!", "?", ":")):    # a sentence, not a name
        return False
    return value.lower() not in _STRUCTURAL_HEADINGS


def _new_pages(src_chunks: list[dict[str, Any]], rows: list[int],
               known: set[str], limit: int) -> list[dict[str, Any]]:
    """Entities the source talks about that have no page of their own.

    Two deterministic signals only:
      * `unresolved_wikilink` — the source links `[[X]]` and no note answers
        to X (the same evidence `curator.validate().broken_links` uses);
      * `source_section` — a `##` heading of the source with no matching note.

    Frequency-based term extraction is deliberately NOT here; it belongs to
    `mb curator gaps --heuristic`, where it is opt-in and labelled as a guess.
    """
    found: dict[str, str] = {}
    for row in rows:
        chunk = src_chunks[row]
        for link in chunk.get("wikilinks") or []:
            name = str(link).strip()
            if name and name.lower() not in known:
                found.setdefault(name, "unresolved_wikilink")
        for match in _WIKILINK_RE.finditer(str(chunk.get("content") or "")):
            name = match.group(1).strip()
            if name and name.lower() not in known:
                found.setdefault(name, "unresolved_wikilink")
        section = str(chunk.get("section") or "").strip()
        if _looks_like_page_name(section) and section.lower() not in known:
            found.setdefault(section, "source_section")

    # unresolved links outrank headings: a link is an explicit claim that the
    # page should exist, a heading is only a topic the source happens to cover
    order = {"unresolved_wikilink": 0, "source_section": 1}
    rows_out = sorted(({"name": n, "reason": r} for n, r in found.items()),
                      key=lambda e: (order[e["reason"]], e["name"].lower(), e["name"]))
    return rows_out[:limit]


def plan(cfg: Config, source_id: str, top_k: int | None = None,
         threshold: float | None = None,
         summary_type: str = DEFAULT_SUMMARY_TYPE) -> dict[str, Any]:
    """Fan-out candidates for one ingested source. Deterministic: a repeated
    run against an unchanged pair of index generations returns byte-identical
    JSON (scores sorted descending, ties broken on file name)."""
    from . import digest
    from . import sources as sources_mod
    from ..search import indexer
    from ..search.similarity import (cross_best_pair, group_indices, mean_pool,
                                    pooled_vectors)

    top_k = int(top_k if top_k is not None else cfg.get("ingest_fanout_top_k"))
    threshold = float(threshold if threshold is not None
                      else cfg.get("ingest_fanout_threshold"))
    out: dict[str, Any] = {
        "source_id": str(source_id), "title": None,
        "index_status": "missing", "sources_index_status": "missing",
        "error": None, "skipped": None,
        "top_k": top_k, "threshold": threshold,
        "summary_target": None, "touch": [], "new_pages": [], "conflicts": [],
        "source_chunks": 0, "target_count": 0, "suppressed": 0,
    }

    record = sources_mod.get(cfg, source_id)
    if record is None:
        out["error"] = f"unknown source_id: {source_id} (see `mb ingest list`)"
        return out
    out["title"] = record.get("title")
    out["summary_target"] = _summary_target(cfg, str(record.get("title") or source_id),
                                            str(source_id), summary_type)

    if not cfg.get("ingest_index_sources"):
        out["error"] = ("the sources corpus is disabled (ingest_index_sources=false); "
                        "`mb ingest plan` needs it — enable it with "
                        "`mb config set ingest_index_sources true`, then "
                        "`mb ingest index --rebuild`")
        return out

    mem_embs, mem_chunks, mem_status = indexer.load_committed_index(cfg.memory_index_dir)
    src_embs, src_chunks, src_status = indexer.load_committed_index(cfg.sources_index_dir)
    out["index_status"] = mem_status
    out["sources_index_status"] = src_status
    if mem_status != "ok" or src_status != "ok":
        return out          # status, not an exception — digest.plan's contract

    mem_model = (indexer.index_summary(cfg.memory_index_dir) or {}).get("model")
    src_model = (indexer.index_summary(cfg.sources_index_dir) or {}).get("model")
    if mem_model and src_model and mem_model != src_model:
        out["error"] = (
            f"index model mismatch: vault index was built with {mem_model!r}, "
            f"sources index with {src_model!r}. Cosine across two embedding "
            f"spaces is meaningless, so no plan is produced — rebuild with "
            f"`mb ingest index --rebuild` (and `mb index rebuild` if the vault "
            f"index is the stale one).")
        return out

    src_rows = [i for i, c in enumerate(src_chunks) if c.get("source_id") == str(source_id)]
    out["source_chunks"] = len(src_rows)
    if not src_rows:
        out["error"] = (f"source {source_id} has no chunks in the sources index — "
                        f"run `mb ingest index` (the watcher does not see .mb/)")
        return out

    source_project = record.get("project")
    if source_project == "personal":
        # Same invariant as digest: personal knowledge is never promoted into
        # shared notes, so a personal source produces no fan-out at all.
        out["skipped"] = ("personal source: personal material is never fanned out "
                          "into shared notes")
        return out

    target_groups = group_indices(mem_chunks, key=lambda c: c["file"],
                                  predicate=_is_target)
    out["target_count"] = len(target_groups)
    meta_by_file = {file: mem_chunks[idxs[0]] for file, idxs in target_groups.items()}
    files, vecs = pooled_vectors(mem_embs, target_groups)

    src_vec = mean_pool(src_embs, src_rows)
    if src_vec is None or not files:
        return out

    # Already-handled pages drop out so the top-k window is spent on work that
    # is still open. The suppression is version-scoped: it lifts automatically
    # if the source is re-ingested under a new hash (sources.marked_notes).
    handled = sources_mod.marked_notes(record)
    scores = vecs @ src_vec
    scored: list[tuple[float, str]] = []
    suppressed = 0
    for file, score in zip(files, scores, strict=False):
        target_project = meta_by_file[file].get("project")
        if not digest._compatible(source_project, target_project):
            continue
        if float(score) < threshold:
            continue
        if file in handled:
            suppressed += 1
            continue
        scored.append((float(score), file))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    out["suppressed"] = suppressed

    touch: list[dict[str, Any]] = []
    for score, file in scored[:top_k]:
        a_row, b_row, pair_score = cross_best_pair(
            src_embs, src_rows, mem_embs, target_groups[file])
        if a_row is None or b_row is None:
            continue        # no evidence -> not a candidate (the `why` gate)
        touch.append({
            "file": file,
            "type": meta_by_file[file].get("type"),
            "project": meta_by_file[file].get("project"),
            "score": round(score, 4),
            "why": {"source_section": _section_label(src_chunks[a_row]),
                    "note_section": _section_label(mem_chunks[b_row]),
                    "pair_score": round(float(pair_score), 4)},
        })

    out["touch"] = touch
    out["new_pages"] = _new_pages(src_chunks, src_rows, _known_names(mem_chunks), top_k)

    # Cross-corpus contradiction candidates: does this source dispute something
    # the wiki already claims? Same band and evidence contract as
    # `mb curator contradictions`, restricted to this source's chunks.
    from . import contradictions
    out["conflicts"] = contradictions.for_source(
        src_embs, src_chunks, src_rows,
        mem_embs, mem_chunks, [i for idxs in target_groups.values() for i in idxs],
        judged=set(contradictions.read_journal(cfg)["verdicts"]), limit=top_k)
    return out

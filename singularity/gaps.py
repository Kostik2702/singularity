"""Research agenda (WIKI-LOOP W08): what is MISSING from the vault.

`curator.analyze()` reports hygiene — orphans, stale notes, link suggestions.
That answers "is the wiki tidy?". This module answers a different and, per the
method, equally valuable question: **what should be looked into next?**

Six deterministic signals, no model, no network, no writes (D5):

1. `unprocessed_sources`  — ingested documents nobody turned into notes yet
2. `missing_pages`        — entities that are linked to but have no page
3. `open_questions`       — questions the vault itself already wrote down
4. `undigested_learnings` — session insights that never became durable notes
5. `unlinked_from_index`  — notes outside the map (NOT the same as orphans)
6. `decisions_without_review` — old decisions nobody re-confirmed

Everything statistical stays behind `--heuristic`, labelled as a guess. The
default report contains only things that are true by construction.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime
from typing import Any

from .config import Config

# Roots that are not knowledge and must never appear as a gap.
EXCLUDED_PREFIXES = ("_moc/", "changelog/", "daily/", "templates/",
                     "compaction-plans/", ".mb/")
INDEX_ROOTS = ("00_INDEX.md",)
INDEX_ROOT_PREFIXES = ("_moc/",)
DECISION_REVIEW_DAYS = 180        # decisions age slower than learnings
# `learnings/` is the INPUT side of the pipeline (digest promotes it into the
# canon), not a wiki page — being off the map is its normal state, so it never
# counts as an unlinked gap.
UNLINKED_EXTRA_EXCLUDED = ("learnings/",)
# Per-signal ceiling in the ranked list. `counts` always reports the true
# totals; without this one broad signal (measured: 154 unlinked notes against
# 2 open questions) fills the whole agenda and hides everything else.
DEFAULT_PER_SIGNAL = 10
OPEN_QUESTION_HEADINGS = ("open questions", "открытые вопросы", "todo",
                          "open:", "открыто:", "вопросы")
LEARNING_PREFIX = "learnings/active/"
_HEADING_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TERM_RE = re.compile(r"[A-ZА-ЯЁ][\w\-]{3,}(?:\s+[A-ZА-ЯЁ][\w\-]{3,})?")

# Weight per signal — how strongly a hit of this kind deserves attention.
# Ranking is weight x age, so a stale gap of a weak kind can still outrank a
# fresh gap of a strong one, which is the intent: old unanswered questions rot.
SIGNAL_WEIGHTS = {
    "unprocessed_sources": 1.0,
    "open_questions": 0.9,
    "missing_pages": 0.8,
    "undigested_learnings": 0.7,
    "decisions_without_review": 0.6,
    "unlinked_from_index": 0.4,
}


def _age_days(timestamp: float | None, now: float) -> float:
    if timestamp is None:
        return 0.0
    return max(0.0, (now - timestamp) / 86400)


def _parse_date(value: Any) -> float | None:
    """`2026-07-28` (or a heading containing it) -> epoch seconds."""
    if value is None:
        return None
    match = _HEADING_DATE_RE.search(str(value))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).timestamp()
    except ValueError:
        return None


def _item(signal: str, key: str, age_days: float, detail: dict[str, Any]
          ) -> dict[str, Any]:
    score = SIGNAL_WEIGHTS[signal] * (1.0 + age_days / 30.0)
    return {"signal": signal, "key": key, "age_days": round(age_days, 1),
            "score": round(score, 4), "detail": detail}


# --- signals ----------------------------------------------------------------

def _unprocessed_sources(cfg: Config, now: float) -> list[dict[str, Any]]:
    """`status: new`, or a source that changed after the notes derived from it.

    The second half matters: a source marked `processed` whose notes are all
    older than it means the notes describe a previous version."""
    from . import sources as sources_mod

    items: list[dict[str, Any]] = []
    for sid, record in sorted(sources_mod.read_registry(cfg)["sources"].items()):
        if not isinstance(record, dict):
            continue
        ingested = _parse_date(record.get("ingested_at")) or now
        derived = record.get("derived_notes") or []
        reason = None
        if record.get("status") == "new" or not derived:
            reason = "never processed"
        else:
            newest_note = 0.0
            for rel in derived:
                path = cfg.vault / rel
                if path.is_file():
                    newest_note = max(newest_note, path.stat().st_mtime)
            if newest_note and ingested > newest_note:
                reason = "source is newer than every note derived from it"
        if reason:
            items.append(_item("unprocessed_sources", sid,
                               _age_days(ingested, now),
                               {"title": record.get("title"), "reason": reason,
                                "status": record.get("status"),
                                "derived_notes": derived}))
    return items


def _missing_pages(notes: dict[str, dict[str, Any]], names: dict[str, str],
                   min_inbound: int, now: float) -> list[dict[str, Any]]:
    """Link targets that several notes reference and none provides.

    One dangling link is a typo; several independent notes pointing at the same
    absent page is the vault telling you what to write next."""
    inbound: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for rel, info in notes.items():
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        for link in info["outlinks"]:
            key = str(link).strip().lower()
            if not key or key in names:
                continue
            inbound.setdefault(key, set()).add(rel)
            display.setdefault(key, str(link).strip())

    items: list[dict[str, Any]] = []
    for key, sources_set in sorted(inbound.items()):
        if len(sources_set) < min_inbound:
            continue
        oldest = min((notes[rel]["mtime"] for rel in sources_set), default=now)
        items.append(_item("missing_pages", display[key], _age_days(oldest, now),
                           {"inbound": len(sources_set),
                            "linked_from": sorted(sources_set)[:10]}))
    return items


def _open_questions(notes: dict[str, dict[str, Any]], now: float
                    ) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rel, info in sorted(notes.items()):
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        age = _age_days(_parse_date(info["fm"].get("created")) or info["mtime"], now)
        if str(info["fm"].get("status", "")).strip().lower() == "open":
            items.append(_item("open_questions", rel, age,
                               {"where": "frontmatter status: open"}))
        for heading in info["headings"]:
            low = heading.strip().lower()
            if any(low.startswith(marker) for marker in OPEN_QUESTION_HEADINGS):
                items.append(_item("open_questions", f"{rel}#{heading}", age,
                                   {"where": "section", "heading": heading}))
    return items


def _undigested_learnings(cfg: Config, notes: dict[str, dict[str, Any]],
                          min_age_days: float, now: float) -> list[dict[str, Any]]:
    """Active-learning sections old enough to have been promoted, that the
    digest journal has no record of."""
    from . import digest

    marked = set(digest._read_journal(cfg)["marked"])
    items: list[dict[str, Any]] = []
    for rel, info in sorted(notes.items()):
        if not rel.startswith(LEARNING_PREFIX):
            continue
        for heading in info["headings"]:
            if digest.identity_key(rel, heading) in marked:
                continue
            age = _age_days(_parse_date(heading) or info["mtime"], now)
            if age < min_age_days:
                continue
            items.append(_item("undigested_learnings", f"{rel}#{heading}", age,
                               {"file": rel, "section": heading}))
    return items


def _unlinked_from_index(notes: dict[str, dict[str, Any]],
                         outgoing: dict[str, set[str]],
                         incoming: dict[str, set[str]], now: float
                         ) -> list[dict[str, Any]]:
    """Notes unreachable from the map. Deliberately NOT orphans: a note can be
    densely linked to its neighbours and still sit in a corner nobody navigates
    to from `00_INDEX` or a MOC."""
    roots = [rel for rel in notes
             if rel in INDEX_ROOTS or rel.startswith(INDEX_ROOT_PREFIXES)]
    reachable: set[str] = set()
    queue = list(roots)
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(outgoing.get(current, ()))

    items: list[dict[str, Any]] = []
    for rel, info in sorted(notes.items()):
        if rel in reachable or rel.startswith(EXCLUDED_PREFIXES + UNLINKED_EXTRA_EXCLUDED):
            continue
        if not outgoing.get(rel) and not incoming.get(rel):
            continue          # that is an orphan; curator.analyze already says so
        items.append(_item("unlinked_from_index", rel,
                           _age_days(info["mtime"], now),
                           {"outlinks": len(outgoing.get(rel, ())),
                            "inlinks": len(incoming.get(rel, ()))}))
    return items


def _decisions_without_review(notes: dict[str, dict[str, Any]], now: float
                              ) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rel, info in sorted(notes.items()):
        fm = info["fm"]
        if str(fm.get("type", "")).strip().lower() != "decision":
            continue
        if fm.get("reviewed") or fm.get("superseded") or fm.get("superseded_by"):
            continue
        if str(fm.get("status", "")).strip().lower() in ("superseded", "archived"):
            continue
        age = _age_days(_parse_date(fm.get("created")) or info["mtime"], now)
        if age < DECISION_REVIEW_DAYS:
            continue
        items.append(_item("decisions_without_review", rel, age,
                           {"title": info["title"], "created": fm.get("created")}))
    return items


def _heuristic_terms(notes: dict[str, dict[str, Any]], names: dict[str, str],
                     min_documents: int, now: float) -> list[dict[str, Any]]:
    """Capitalized multi-word terms that appear across several notes and have
    no page. A GUESS: capitalization is not entity detection, so this stays
    behind `--heuristic` and is labelled as such in every item."""
    document_frequency: Counter = Counter()
    for rel, info in notes.items():
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        seen = {t.strip() for t in _TERM_RE.findall(" ".join(info["headings"]))}
        for term in seen:
            if term.lower() not in names:
                document_frequency[term] += 1
    return [_item("missing_pages", term, 0.0,
                  {"inbound": 0, "document_frequency": count, "heuristic": True})
            for term, count in sorted(document_frequency.items())
            if count >= min_documents]


# ---------------------------------------------------------------------------

def report(cfg: Config, top: int | None = None, heuristic: bool = False,
           now: float | None = None,
           per_signal: int | None = DEFAULT_PER_SIGNAL) -> dict[str, Any]:
    """Ranked research agenda. Deterministic: score desc, then (signal, key).

    `counts` are the TRUE totals; `items` is the ranked shortlist after the
    per-signal ceiling. A truncated signal is named in `truncated` — a shortened
    agenda that looks complete is the same lie as a capped scan reporting
    "no contradictions"."""
    from . import curator

    now = now if now is not None else time.time()
    notes, names, outgoing, incoming = curator.link_graph(cfg)
    min_inbound = int(cfg.get("gap_link_min_inbound"))
    min_age = float(cfg.get("gap_learning_age_days"))

    items: list[dict[str, Any]] = []
    items += _unprocessed_sources(cfg, now)
    items += _missing_pages(notes, names, min_inbound, now)
    items += _open_questions(notes, now)
    items += _undigested_learnings(cfg, notes, min_age, now)
    items += _unlinked_from_index(notes, outgoing, incoming, now)
    items += _decisions_without_review(notes, now)
    if heuristic:
        items += _heuristic_terms(notes, names, min_inbound, now)

    items.sort(key=lambda e: (-e["score"], e["signal"], e["key"]))
    counts: dict[str, int] = dict.fromkeys(SIGNAL_WEIGHTS, 0)
    for entry in items:
        counts[entry["signal"]] += 1

    shortlist = items
    truncated: dict[str, int] = {}
    if per_signal:
        shown: Counter = Counter()
        shortlist = []
        for entry in items:
            signal = entry["signal"]
            if shown[signal] >= per_signal:
                truncated[signal] = counts[signal] - per_signal
                continue
            shown[signal] += 1
            shortlist.append(entry)

    return {"generated_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "notes": len(notes), "heuristic": heuristic, "counts": counts,
            "total": len(items), "truncated": truncated,
            "items": shortlist[:top] if top else shortlist}



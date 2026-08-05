"""What the vault learned over a window of time, in one list.

Every ingredient already existed — notes carry `created:`, learnings accumulate
timestamped `##` sections, the changelog is dated — and none of it could be
asked as a question. "What did we work out last month?" meant opening five
folders and reading by hand, which is why nobody did it.

Two sources, because knowledge arrives in two shapes:

* **Notes** — a decision, a rule, an incident is created once, on a date. Read
  from `created:` in the frontmatter, not from the file's mtime: a note edited
  today did not become knowledge today, and mtime would put every touched file
  at the top of the list.
* **Learning entries** — `learnings/` files are appended to for months, so the
  file's own date says nothing. The unit here is the `##` section, whose
  heading IS its timestamp. These come from the search index, which already
  holds one chunk per section.

Report only. Nothing here writes, moves or deletes: every other curation surface
in this engine proposes and waits for approval, and a "clean up what you learned"
button would be the one place that does not.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..search.chunking import parse_frontmatter

# `2026-08-05`, `2026-08-05 14:09`, `2026-08-05 14:09 — some heading`
_SECTION_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2})?\s*(?:[—–-]\s*(.*))?$")
_CREATED_RE = re.compile(r"^created:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.MULTILINE)

# Folders whose notes are journal machinery rather than knowledge: including
# them would bury the six real decisions of a month under sixty dailies.
_SKIP_PREFIXES = ("daily/", "_moc/", "templates/", "learnings/")


def _parse_day(text: str) -> date | None:
    m = _SECTION_DATE_RE.match(text.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _section_label(text: str) -> str:
    """The part of a learning heading that is not the timestamp, if any."""
    m = _SECTION_DATE_RE.match(text.strip())
    return (m.group(4) or "").strip() if m else text.strip()


def _notes(cfg: Config, since: date, project: str | None) -> list[dict[str, Any]]:
    vault = cfg.vault
    skip_dirs = set(cfg.get("skip_dirs") or ())
    out: list[dict[str, Any]] = []
    for path in vault.rglob("*.md"):
        rel = str(path.relative_to(vault)).replace("\\", "/")
        if any(part in skip_dirs for part in path.relative_to(vault).parts):
            continue
        if rel.startswith(_SKIP_PREFIXES):
            continue
        try:
            head = path.read_text(encoding="utf-8-sig", errors="ignore")[:2000]
        except OSError:
            continue
        m = _CREATED_RE.search(head)
        if not m:
            continue
        day = _parse_day(m.group(1))
        if day is None or day < since:
            continue
        fm, _ = parse_frontmatter(head)
        if project and (fm.get("project") or "general") != project:
            continue
        out.append({"day": day.isoformat(), "kind": str(fm.get("type") or "note"),
                    "title": str(fm.get("title") or path.stem), "path": rel,
                    "project": fm.get("project") or "general"})
    return out


def _learning_entries(cfg: Config, since: date, project: str | None) -> list[dict[str, Any]]:
    """One entry per timestamped `##` section, read from the committed index.

    The index is used rather than the files because it already holds exactly
    this decomposition; re-splitting every learnings file here would be a second
    implementation of chunking that could drift from the first.
    """
    from ..search import indexer

    files = indexer.index_files(cfg.memory_index_dir)
    if files is None:
        return []
    try:
        meta = json.loads(Path(files[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for chunk in meta.get("chunks", []):
        if chunk.get("type") != "learning":
            continue
        section = str(chunk.get("section") or "")
        day = _parse_day(section)
        if day is None or day < since:
            continue
        rel = str(chunk.get("file") or "")
        key = (rel, section)
        if key in seen:          # a long section becomes several chunks
            continue
        seen.add(key)
        proj = chunk.get("project") or "general"
        if project and proj != project:
            continue
        out.append({"day": day.isoformat(), "kind": "learning",
                    "title": str(chunk.get("title") or Path(rel).stem),
                    "label": _section_label(section), "path": rel, "project": proj})
    return out


def collect(cfg: Config, days: int = 30, project: str | None = None,
            today: date | None = None) -> dict[str, Any]:
    """Everything the vault gained in the last `days`, newest day first."""
    end = today or datetime.now().date()
    since = end - timedelta(days=max(1, int(days)) - 1)

    entries = _notes(cfg, since, project) + _learning_entries(cfg, since, project)
    entries.sort(key=lambda e: (e["day"], e["kind"], e["title"]), reverse=True)

    by_kind: dict[str, int] = {}
    for e in entries:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    tasks = {e["title"] for e in entries if e["kind"] == "learning"}

    return {
        "from": since.isoformat(), "to": end.isoformat(), "days": days,
        "project": project, "total": len(entries),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))),
        "learning_tasks": len(tasks),
        "entries": entries,
    }

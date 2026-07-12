"""Project-bound capture: dedup + project binding for `mb learn`/`mb note new`
(T05).

stdlib-only (re, difflib). Dedup is a bounded, deterministic near-duplicate
check over the last N `##` sections of a target file — never a full-file
scan, and never treats a section's raw heading (typically a bare timestamp,
not semantic) as comparison content, only the section body.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, NamedTuple, Optional

DEDUP_WINDOW = 10
SIMILARITY_THRESHOLD = 0.9

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_FM_PROJECT_RE = re.compile(r"(?m)^project:")


def normalize_gist(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the comparison key
    for near-duplicate detection. Never raises."""
    text = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def _split_sections(body: str) -> list[tuple[str, str]]:
    """[(heading, section_body), ...] in file order, from `## heading` splits."""
    parts = _SECTION_RE.split(body)
    sections = []
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        content = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((heading, content.strip()))
    return sections


class DuplicateMatch(NamedTuple):
    heading: str


def find_duplicate(existing_text: str, new_content: str,
                   window: int = DEDUP_WINDOW) -> Optional[DuplicateMatch]:
    """Compare `new_content` against the BODY of the last `window` sections of
    `existing_text` (bounded, not a full-file scan — an older duplicate outside
    the window is intentionally not caught, T05 acceptance). Match on: exact
    normalized gist, normalized prefix containment (retyped near-copy with
    trailing/leading additions), or SequenceMatcher ratio >= 0.9. Returns the
    matched section's heading, or None."""
    new_gist = normalize_gist(new_content)
    if not new_gist:
        return None
    for heading, content in _split_sections(existing_text)[-window:]:
        have_gist = normalize_gist(content)
        if not have_gist:
            continue
        if new_gist == have_gist:
            return DuplicateMatch(heading)
        if new_gist.startswith(have_gist) or have_gist.startswith(new_gist):
            return DuplicateMatch(heading)
        if SequenceMatcher(None, new_gist, have_gist).ratio() >= SIMILARITY_THRESHOLD:
            return DuplicateMatch(heading)
    return None


def resolve_capture_project(cfg: Any, explicit: Optional[str]
                            ) -> tuple[Optional[str], Optional[str]]:
    """(project_or_None, error_or_None). `project=None` means general (the
    frontmatter key should be omitted, matching chunking.infer_project's
    convention). Explicit `--project` is resolved through projects.
    resolve_selector — the SAME resolver `mb search --project` uses (FR-1) —
    but capture needs exactly one concrete target, so a token that expands to
    a group is rejected rather than silently picking one member. Without an
    explicit value, falls back to cwd-based projects.detect_project, which by
    construction can never resolve to "personal" (a reserved id, never a
    registrable path) — the only way to write `project: personal` is to ask
    for it explicitly, which is the whole point of that acceptance criterion."""
    from . import projects

    registry = cfg.get("projects", {}) or {}
    if explicit:
        try:
            ids = projects.resolve_selector(registry, explicit)
        except KeyError as exc:
            return None, str(exc)
        if len(ids) != 1:
            return None, (f"--project '{explicit}' expands to {len(ids)} projects "
                          f"(a group); capture needs exactly one id, or general/personal")
        chosen = ids[0]
        return (None if chosen == "general" else chosen), None
    detected = projects.detect_project(registry, str(Path.cwd()))
    return detected, None


def with_project_frontmatter(body: str, project: Optional[str]) -> str:
    """Insert `project: <id>` into an already-built frontmatter block (works
    uniformly whether `body` came from a user template or the built-in
    fallback). No-op when project is None/general, when `body` has no
    frontmatter fence, or when the block already sets `project:` (never
    clobber a template's own value)."""
    if not project or not body.startswith("---\n"):
        return body
    end = body.find("\n---", 4)
    if end == -1:
        return body
    if _FM_PROJECT_RE.search(body[4:end]):
        return body
    return body[:end] + f"\nproject: {project}" + body[end:]

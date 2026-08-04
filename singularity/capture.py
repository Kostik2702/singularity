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
from typing import Any, NamedTuple

DEDUP_WINDOW = 10
SIMILARITY_THRESHOLD = 0.9

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_FM_PROJECT_RE = re.compile(r"(?m)^project:")
_FM_SOURCES_LINE_RE = re.compile(r"(?m)^sources:[ \t]*(.*)$")


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
                   window: int = DEDUP_WINDOW) -> DuplicateMatch | None:
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


def _under_root(rel_path: str, root: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").strip("/")
    root_norm = str(root).replace("\\", "/").strip("/")
    if not root_norm:
        return False
    return rel_norm == root_norm or rel_norm.startswith(root_norm + "/")


def resolve_capture_project(cfg: Any, explicit: str | None,
                            dest_rel_path: str | None = None
                            ) -> tuple[str | None, str | None]:
    """(project_or_None, error_or_None). `project=None` means general (the
    frontmatter key should be omitted, matching chunking.infer_project's
    convention).

    Priority: explicit --project > dest_rel_path under a configured
    personal_roots entry (T08 retrofit — Open Decision, 2026-07-12: personal
    = frontmatter OR configured personal_roots) > cwd-based detection.

    Explicit `--project` is resolved through projects.resolve_selector — the
    SAME resolver `mb search --project` uses (FR-1) — but capture needs
    exactly one concrete target, so a token that expands to a group is
    rejected rather than silently picking one member.

    `dest_rel_path` is the note's OWN vault-relative destination (e.g.
    `learnings/active/T1.md`), passed by the caller (cmd_learn/cmd_note)
    which already computed it — this mirrors chunking.infer_project's
    personal_roots check on the read side, so a note captured under a
    configured personal root is classified "personal" without needing
    `--project personal` spelled out every time.

    Without an explicit value AND no personal_roots match, falls back to
    cwd-based projects.detect_project, which by construction can never
    resolve to "personal" (a reserved id, never a registrable path) — so
    the only way to land on "personal" is an explicit flag or a configured
    root, never an accidental cwd match."""
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

    personal_roots = cfg.get("personal_roots") or []
    if dest_rel_path and personal_roots:
        for root in personal_roots:
            if isinstance(root, str) and _under_root(dest_rel_path, root):
                return "personal", None

    detected = projects.detect_project(registry, str(Path.cwd()))
    return detected, None


def with_sources_frontmatter(body: str, source_ids: list | None) -> str:
    """Insert `sources: [id, ...]` into an already-built frontmatter block —
    the provenance contract (WIKI-LOOP W04): a note derived from an ingested
    document must say which one, or the claim is unverifiable.

    An EMPTY `sources:` already present (the templates ship `sources: []` to
    document the contract) is filled in rather than left alone -- otherwise
    `mb note new --from-source X` would silently produce a note with no
    provenance, which is the exact failure this contract exists to prevent.
    A non-empty value is never clobbered."""
    if not source_ids or not body.startswith("---\n"):
        return body
    end = body.find("\n---", 4)
    if end == -1:
        return body
    block = body[4:end]
    rendered = "[" + ", ".join(f'"{str(s)}"' for s in source_ids) + "]"
    match = _FM_SOURCES_LINE_RE.search(block)
    if match is None:
        return body[:end] + f"\nsources: {rendered}" + body[end:]
    if match.group(1).strip() not in ("", "[]", "''", '""', "~", "null"):
        return body
    return body[:4] + block[:match.start()] + f"sources: {rendered}" \
        + block[match.end():] + body[end:]


def with_project_frontmatter(body: str, project: str | None) -> str:
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

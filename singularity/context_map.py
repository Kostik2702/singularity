"""Per-project context map: `.mb/context.json` loader + project brain block
builder for SessionStart (T06).

stdlib-only. Every function here is meant to be called from a hook hot path
and fails open by design (missing/malformed/adversarial input -> None/{},
never an exception) — the caller (session_start.build_context) still wraps
calls defensively too, matching the file's existing per-block isolation.

Schema (vault/.mb/context.json), per spec §8.3:
  {"<project-id>": {"brain_note": "<vault-relative path>",
                    "context_map": {"<module>": {"globs": [...], "note": "..."}}}}
`context_map` (T07 module-context) is accepted here but not consumed yet —
T06 only reads `brain_note`. `.mb/context.json` itself is not written by any
code path in this task; it is user/skill-authored (T15's `mb-onboard`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .search.chunking import parse_frontmatter

DEFAULT_BRAIN_MAX_LINES = 40


def load_context_map(vault: Path) -> dict[str, Any]:
    """Tolerant read of vault/.mb/context.json. Missing/malformed/non-object
    -> {} (never raises)."""
    path = vault / ".mb" / "context.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_within_vault(vault: Path, rel: str) -> Path | None:
    """Resolve a vault-relative path, rejecting traversal outside the vault
    (`..`, absolute escapes, symlink escapes). None on an empty string or any
    resolution error, never raises."""
    if not rel or not rel.strip():
        return None
    try:
        vault_r = vault.resolve()
        candidate = (vault / rel).resolve()
        candidate.relative_to(vault_r)
    except (OSError, ValueError):
        return None
    return candidate


def read_note_excerpt(vault: Path, rel_path: Any, max_lines: int, label: str,
                      default_max_lines: int = DEFAULT_BRAIN_MAX_LINES) -> str | None:
    """Shared budgeted-excerpt builder: resolve `rel_path` safely within
    `vault`, strip frontmatter, truncate to `max_lines`, and format with a
    real absolute "Read <path>" hint — never a fabricated line range or
    command. Used by both project_brain_block (T06) and module_context's
    per-module note (T07) so the two budgets/formats can't drift apart. None
    on any failure (missing/non-string path, traversal, missing/unreadable
    file) — every reason fails open silently."""
    if not isinstance(rel_path, str):
        return None
    note_path = _resolve_within_vault(vault, rel_path)
    if note_path is None or not note_path.is_file():
        return None
    try:
        raw = note_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None

    _, body = parse_frontmatter(raw)
    lines = body.strip("\n").splitlines()
    if not isinstance(max_lines, int) or max_lines <= 0:
        max_lines = default_max_lines
    shown = lines[:max_lines]
    more = len(lines) - len(shown)
    header = f"{label} — Read {note_path} for full content:"
    suffix = (f"\n... ({more} more line{'s' if more != 1 else ''} truncated, "
             f"max_lines={max_lines})") if more > 0 else ""
    return "\n".join([header, *shown]) + suffix


def project_brain_block(vault: Path, project: str | None,
                        max_lines: int = DEFAULT_BRAIN_MAX_LINES) -> str | None:
    """Compact, line-budgeted project-brain text for SessionStart, or None
    when there is nothing to show — no project, no context.json entry, no
    `brain_note`, path traversal attempt, or an unreadable/missing file. Every
    reason fails open silently; the caller decides whether a diagnostic is
    worth surfacing (it currently does not, matching the other SessionStart
    blocks' silent-omission convention)."""
    if not project:
        return None
    entry = load_context_map(vault).get(project)
    if not isinstance(entry, dict):
        return None
    return read_note_excerpt(vault, entry.get("brain_note"), max_lines,
                             f"Project brain ({project})")

"""Client-neutral module-context matching + marker/note logic for the
once-per-session PreToolUse hook (T07).

stdlib-only. This module has NO knowledge of Claude's wire format — that
lives entirely in singularity/hooks/module_context.py, the thin PreToolUse
adapter. Gemini support is explicitly deferred (Open Decision, 2026-07-12)
until a proven PreToolUse contract fixture exists; nothing here assumes a
particular client, so a future Gemini adapter can reuse it unchanged.

Module globs in `.mb/context.json`'s `context_map` are PROJECT-relative
(unlike `brain_note`, which is vault-relative) — see spec §8.3.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .context_map import load_context_map, read_note_excerpt

DEFAULT_MODULE_NOTE_MAX_LINES = 60

_SAFE_MARKER_RE = re.compile(r"[^A-Za-z0-9_-]")  # no dots either: ".." must never survive


def _normalize_prefix(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def match_module(context_map_for_project: Any, project_relative_path: str) -> str | None:
    """Longest-matching module prefix, or None. `project_relative_path` is
    relative to the PROJECT's own root (already stripped of any vault/cwd
    prefix), forward-slash normalized. A glob is treated as a path prefix —
    either an exact match or a directory boundary (`target == glob` or
    `target` starts with `glob + "/"`) — not a full glob-wildcard pattern;
    every glob in context.json is an explicit literal prefix (spec §8.3
    examples are directory/file paths, not wildcards). Malformed entries
    (non-dict, non-list globs, non-string glob) are skipped, never raised."""
    if not isinstance(context_map_for_project, dict):
        return None
    target = _normalize_prefix(project_relative_path)
    best_name: str | None = None
    best_len = -1
    for name, entry in context_map_for_project.items():
        if not isinstance(entry, dict):
            continue
        globs = entry.get("globs")
        if not isinstance(globs, list):
            continue
        for g in globs:
            if not isinstance(g, str):
                continue
            gp = _normalize_prefix(g)
            if not gp:
                continue
            if (target == gp or target.startswith(gp + "/")) and len(gp) > best_len:
                best_len = len(gp)
                best_name = name
    return best_name


def module_marker(vault: Path, client: str, session_id: str, project: str, module: str) -> Path:
    """Per-(client, session, project, module) marker so each module note
    injects at most once per session, and switching to a DIFFERENT module
    gets its own fresh injection. Names are sanitized — project/module ids
    ultimately come from context.json, a file this hook only reads, never
    writes; a stray path separator or traversal token in an id must not
    escape the markers directory."""
    def _safe(s: str) -> str:
        return _SAFE_MARKER_RE.sub("_", s)[:128] or "_"

    markers = vault / ".mb" / "markers"
    markers.mkdir(parents=True, exist_ok=True)
    name = f"module-{_safe(client)}-{_safe(session_id)}-{_safe(project)}-{_safe(module)}"
    return markers / name


def module_note_block(vault: Path, project: str, module: str,
                      max_lines: int = DEFAULT_MODULE_NOTE_MAX_LINES) -> str | None:
    """Budgeted excerpt of the module's mapped note, or None (no entry, no
    `note` key, traversal attempt, missing/unreadable file — every reason
    fails open silently, same convention as project_brain_block)."""
    entry = load_context_map(vault).get(project)
    if not isinstance(entry, dict):
        return None
    module_entry = entry.get("context_map", {}).get(module) if isinstance(
        entry.get("context_map"), dict) else None
    if not isinstance(module_entry, dict):
        return None
    return read_note_excerpt(vault, module_entry.get("note"), max_lines,
                             f"Module context ({project}/{module})",
                             default_max_lines=DEFAULT_MODULE_NOTE_MAX_LINES)

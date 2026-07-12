"""Claude PreToolUse hook: inject an architectural module-context note once
per (session, project, module) before the FIRST Edit/Write/MultiEdit that
touches that module (T07).

Claude only for v1 — Open Decision, 2026-07-12: Gemini module_context is
deferred until a proven PreToolUse contract fixture exists (mirroring the
GCLI-00 spike discipline already used for SessionStart/AfterAgent), not
silently ported on the assumption Gemini's PreToolUse hook behaves the same.

Contract: additionalContext ONLY. Never emits permissionDecision, never
denies — the tool call always proceeds under Claude Code's normal permission
flow; this hook can only add information, never gate anything. Reads/no-match
(wrong tool, no project, no module mapped, already injected this session) are
silent — empty `{}` on stdout, exit 0. A crash anywhere in matching/reading
must never block a tool call — fail open all the way through.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# hook runs outside the package context; make the package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}


def build_additional_context(event: dict) -> str | None:
    """Pure-ish orchestration over the client-neutral core (singularity.
    module_context) — returns the additionalContext string, or None for
    every no-op case. Never raises: every failure path returns None; the
    caller (main) still wraps this call defensively as a second line of
    defense."""
    if not isinstance(event, dict) or event.get("tool_name") not in _WRITE_TOOLS:
        return None
    tool_input = event.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    from singularity.config import read_config_tolerant, resolve_vault

    vault = resolve_vault(None)
    if vault is None or not vault.exists():
        return None

    cwd = event.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd.strip() else Path.cwd()
    fp = Path(file_path)
    try:
        abs_file = (fp if fp.is_absolute() else base / fp).resolve()
    except (OSError, ValueError):
        return None

    light_cfg = read_config_tolerant(vault)
    registry = light_cfg.get("projects")
    registry = registry if isinstance(registry, dict) else {}

    from singularity import projects as projects_mod

    # Membership is decided by the FILE's own location, not the session cwd
    # (spec §6.3: "target path resolved and required to be inside a
    # registered project") — reuses detect_project's deepest-match resolver
    # unchanged, just fed an absolute file path instead of a cwd.
    project = projects_mod.detect_project(registry, str(abs_file))
    if not project:
        return None

    project_entry = registry.get(project)
    project_path = project_entry.get("path") if isinstance(project_entry, dict) else None
    if not isinstance(project_path, str):
        return None
    try:
        project_root = Path(project_path).resolve()
        rel = abs_file.relative_to(project_root)
    except (OSError, ValueError):
        return None       # symlink/normalization put the file outside the project after all

    from singularity.context_map import load_context_map
    from singularity.module_context import (DEFAULT_MODULE_NOTE_MAX_LINES, match_module,
                                            module_marker, module_note_block)

    entry = load_context_map(vault).get(project)
    ctx_map = entry.get("context_map") if isinstance(entry, dict) else None
    module = match_module(ctx_map, str(rel).replace("\\", "/"))
    if not module:
        return None

    session_id = str(event.get("session_id", "unknown"))
    marker = module_marker(vault, "claude", session_id, project, module)
    if marker.exists():
        return None       # already injected this (session, project, module) once

    max_lines = light_cfg.get("module_note_max_lines", DEFAULT_MODULE_NOTE_MAX_LINES)
    block = module_note_block(vault, project, module, max_lines)
    if not block:
        return None

    marker.write_text("injected", encoding="utf-8")
    return block


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError, AttributeError):
        print("{}")
        return 0

    try:
        context = build_additional_context(event)
    except Exception:  # noqa: BLE001 — a broken hook must never block/deny a tool call
        context = None

    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": context}}, ensure_ascii=False))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

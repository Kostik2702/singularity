"""Multi-project registry: schema validation + pure cwd resolution (T03).

stdlib-only, no Config/CLI dependency — the registry lives inside the shared
`.mb/config.json["projects"]` key; Config.save()/atomic-write semantics are
reused unchanged from singularity.config, not reimplemented here.

`general` and `personal` are reserved domain ids (fallback / explicit-personal
space, T04/T05) — they are never registrable entries in `projects`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

RESERVED_IDS = {"general", "personal"}
_KNOWN_ENTRY_KEYS = {"path", "group", "about", "ticket_prefix"}


def validate_projects(data: dict[str, Any]) -> dict[str, list[str]]:
    """Validate the `projects` key of a config dict in isolation. Same
    {"errors": [...], "warnings": [...]} shape as config.validate_config, so
    the two merge trivially."""
    errors: list[str] = []
    warnings: list[str] = []
    projects = data.get("projects")
    if projects is None:
        return {"errors": errors, "warnings": warnings}
    if not isinstance(projects, dict):
        return {"errors": [f"projects: must be an object, got {type(projects).__name__}"],
                "warnings": []}

    claimed_prefixes: dict[str, str] = {}
    claimed_paths: dict[str, str] = {}   # normalized path -> first id that claimed it

    for pid, entry in projects.items():
        if not isinstance(pid, str) or not pid.strip():
            errors.append(f"projects: invalid id {pid!r} (must be a non-empty string)")
            continue
        if pid in RESERVED_IDS:
            errors.append(f"projects.{pid}: '{pid}' is a reserved id and cannot be registered")
            continue
        if not isinstance(entry, dict):
            errors.append(f"projects.{pid}: must be an object")
            continue

        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"projects.{pid}.path: must be a non-empty string")
        else:
            norm = _normalize(path)
            if norm in claimed_paths and claimed_paths[norm] != pid:
                errors.append(f"projects.{pid}.path: same path already registered as "
                              f"'{claimed_paths[norm]}'")
            else:
                claimed_paths[norm] = pid

        group = entry.get("group")
        if group is not None:
            if not isinstance(group, str) or not group.strip():
                errors.append(f"projects.{pid}.group: must be a non-empty string or absent")
            elif group in RESERVED_IDS:
                errors.append(f"projects.{pid}.group: '{group}' collides with a reserved id")
            elif group in projects:
                errors.append(f"projects.{pid}.group: '{group}' collides with an existing "
                              f"project id (id beats same-named group — pick another group name)")

        about = entry.get("about")
        if about is not None and not isinstance(about, str):
            errors.append(f"projects.{pid}.about: must be a string or absent")

        prefix = entry.get("ticket_prefix")
        if prefix is not None:
            if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z]{2,6}", prefix):
                errors.append(f"projects.{pid}.ticket_prefix: must be 2-6 capital "
                              f"letters A-Z or absent")
            elif prefix in claimed_prefixes and claimed_prefixes[prefix] != pid:
                errors.append(f"projects.{pid}.ticket_prefix: '{prefix}' already used "
                              f"by '{claimed_prefixes[prefix]}'")
            else:
                claimed_prefixes[prefix] = pid

        unknown = set(entry) - _KNOWN_ENTRY_KEYS
        if unknown:
            warnings.append(f"projects.{pid}: unknown keys ignored: {', '.join(sorted(unknown))}")

    return {"errors": errors, "warnings": warnings}


def _case_insensitive_fs() -> bool:
    return sys.platform in ("win32", "cygwin", "darwin")


def _normalize(path_str: str) -> str:
    """Resolve (symlinks, `..`, relative segments) + platform-aware case-fold
    so containment comparisons are never a naive string prefix. Never raises —
    a missing path still normalizes lexically (strict=False)."""
    try:
        resolved = str(Path(path_str).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        resolved = str(Path(path_str))
    return resolved.lower() if _case_insensitive_fs() else resolved


def detect_project(projects: dict[str, Any] | None, cwd: str) -> str | None:
    """Deepest registered project containing `cwd`, or None (caller falls back
    to 'general'). Pure: no config/CLI I/O beyond path resolution. Unreadable/
    malformed entries are skipped, never raised — runtime detection must not
    crash on a registry another command already flagged via validate."""
    if not projects:
        return None
    target = _normalize(cwd)
    best_id: str | None = None
    best_len = -1
    for pid, entry in projects.items():
        if pid in RESERVED_IDS or not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        norm = _normalize(path)
        if (target == norm or target.startswith(norm.rstrip("/\\") + "/") \
                or target.startswith(norm.rstrip("/\\") + "\\")) and len(norm) > best_len:
            best_len = len(norm)
            best_id = pid
    return best_id


def resolve_selector(projects: dict[str, Any] | None, token: str) -> list[str]:
    """Expand one selector token (project id, group name, or reserved
    general/personal) into concrete project ids. An id match wins over a
    same-named group (id beats group — mirrors the collision check in
    validate_projects). Raises KeyError listing known ids/groups when the
    token matches neither — the single resolver T04's `--project` filter and
    friends must reuse, so every consumer reports unknown selectors the same
    way."""
    projects = projects or {}
    if token in RESERVED_IDS:
        return [token]
    if token in projects:
        return [token]
    groups = sorted({e.get("group") for e in projects.values()
                     if isinstance(e, dict) and isinstance(e.get("group"), str)})
    if token in groups:
        return sorted(pid for pid, e in projects.items()
                      if isinstance(e, dict) and e.get("group") == token)
    known_ids = ", ".join(sorted(projects)) or "(none)"
    known_groups = ", ".join(groups) or "(none)"
    raise KeyError(f"unknown project/group '{token}' — known ids: {known_ids}; "
                   f"known groups: {known_groups}")

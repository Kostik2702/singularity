"""Client-neutral vault guard core (T08): flags new-file creation in managed
knowledge roots and direct `.mb/config.json` writes.

stdlib-only. No knowledge of Claude's wire format — that lives entirely in
singularity/hooks/vault_guard.py, the thin PreToolUse adapter.

Sanctioned CLI writes (`mb note new`, `mb learn`, `mb config set`, `mb
project add`, ...) never go through a client's Write/Edit tool at all — they
run as a separate `mb ...` process (invoked via Bash) that writes files
directly in its own process. Any Write/Edit a client tool sees hitting a
managed root or config.json is therefore, by construction, NOT the
sanctioned path. No command-substring or transcript scanning is needed (a
spoofable signal the AI prompt explicitly warns against) — the mere fact of
the tool call is itself the ownership signal.

Carve-outs `templates/` and `compaction-plans/` are never guarded (user-
customizable / skill-managed one-shot files). Editing an EXISTING note is
always allowed — only NEW-file creation is flagged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

MANAGED_ROOTS = ["learnings/active", "learnings/archive", "decisions", "howto",
                 "incidents", "rules", "reference", "changelog", "_moc", "daily"]
EXEMPT_ROOTS = ["templates", "compaction-plans"]


def vault_relative(vault: Path, abs_path: Path) -> Optional[str]:
    """Forward-slash vault-relative path, or None if `abs_path` resolves
    outside `vault` entirely. Never raises."""
    try:
        rel = abs_path.resolve().relative_to(vault.resolve())
    except (OSError, ValueError):
        return None
    return str(rel).replace("\\", "/")


def is_config_json(vault: Path, abs_path: Path) -> bool:
    return vault_relative(vault, abs_path) == ".mb/config.json"


def new_file_managed_root(vault: Path, abs_path: Path) -> Optional[str]:
    """The matched managed-root prefix if `abs_path` is a NEW file (does not
    already exist on disk) under a managed knowledge root and not under an
    exempt carve-out. None otherwise — including when the file already
    exists (existing notes are always editable)."""
    rel = vault_relative(vault, abs_path)
    if rel is None or abs_path.exists():
        return None
    for exempt in EXEMPT_ROOTS:
        if rel == exempt or rel.startswith(exempt + "/"):
            return None
    for root in MANAGED_ROOTS:
        if rel == root or rel.startswith(root + "/"):
            return root
    return None

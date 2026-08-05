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

MANAGED_ROOTS = ["learnings/active", "learnings/archive", "decisions", "howto",
                 "incidents", "rules", "reference", "changelog", "_moc", "daily"]
EXEMPT_ROOTS = ["templates", "compaction-plans"]
# WIKI-LOOP D6: roots whose contents are immutable by contract. Unlike every
# other check here, a hit is denied even in `vault_guard_mode: audit` — the
# other checks are about conventions (breakable, with a warning), this one is
# about destroying data that has no second copy (D1/R8).
DEFAULT_IMMUTABLE_ROOTS = [".mb/sources"]


def vault_relative(vault: Path, abs_path: Path) -> str | None:
    """Forward-slash vault-relative path, or None if `abs_path` resolves
    outside `vault` entirely. Never raises."""
    try:
        rel = abs_path.resolve().relative_to(vault.resolve())
    except (OSError, ValueError):
        return None
    return str(rel).replace("\\", "/")


def is_config_json(vault: Path, abs_path: Path) -> bool:
    """Is this a direct edit of the config, bypassing `mb config set`?

    Two paths, because a machine mid-upgrade has both: the machine config
    (~/.singularity.json, where it lives from 0.7) and the pre-0.7
    vault-resident copy, which still exists until the migration runs. Dropping
    the second would silently stop guarding it during exactly the window where
    a hand edit is most likely."""
    from ..config import CONFIG_FILE

    if vault_relative(vault, abs_path) == ".mb/config.json":
        return True
    try:
        return abs_path.resolve() == CONFIG_FILE.resolve()
    except OSError:
        return False


def immutable_root(vault: Path, abs_path: Path,
                   roots: list | None = None) -> str | None:
    """The matched immutable-root prefix if `abs_path` falls inside one, else
    None. Applies to EXISTING files as well as new ones — the whole point is
    that an ingested source can never be edited (D6).

    Matching goes through `vault_relative`, which resolves the path first, so
    `.mb/sources/../../x` and a symlink into the tree both normalize before
    comparison instead of sneaking past a string prefix test."""
    rel = vault_relative(vault, abs_path)
    if rel is None:
        return None
    for root in (roots if roots is not None else DEFAULT_IMMUTABLE_ROOTS):
        if not isinstance(root, str):
            continue
        norm = root.replace("\\", "/").strip("/")
        if norm and (rel == norm or rel.startswith(norm + "/")):
            return norm
    return None


def new_file_managed_root(vault: Path, abs_path: Path) -> str | None:
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

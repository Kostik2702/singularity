"""Shared safe-registration primitives for CLI-client integrations (GCLI-01).

Small, focused helpers extracted so the Codex and Gemini registrars cannot drift
apart on the three risky operations: editing a marker-delimited Markdown block in
a user-owned file, mutating a JSON settings file, and owning hooks inside it. This
is deliberately NOT a provider framework — no base classes, no registry. Each
registrar stays a plain module that calls these functions.

- Managed Markdown block  -> merge_managed_block / remove_managed_block
- JSON settings mutation   -> backup_file / read_json_or_raise
- Owned hooks in settings  -> merge_named_hooks / remove_named_hooks  (ownership by
                              the stable inner hook `name`, never a command substring)
- Ownership proof for paths -> resolves_into

Skill directory links are NOT re-implemented here: register_skills._link_dir is the
single source of that junction/symlink/backup logic and is reused directly. Which
skills go to which client is likewise not decided here -- see skill_manifest.py
(T15): each client script calls skill_manifest.skills_for_client(<name>) directly.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable


# --- managed Markdown block ----------------------------------------------------

def merge_managed_block(path: Path, block: str, begin: str, end: str) -> str:
    """Write `block` (which must itself start with `begin` and end with `end`) into
    `path` as a marker-delimited managed section. Returns 'created' | 'refreshed' |
    'appended'. User text outside the markers is preserved verbatim. If the file
    exists but the markers are absent or malformed (only one of them present), the
    block is appended rather than risking a bad splice.

    Byte-for-byte equivalent to the original register_codex._write_profile so the
    Codex golden tests stay green across the extraction.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(block + "\n", encoding="utf-8")
        return "created"
    text = path.read_text(encoding="utf-8")
    if begin in text and end in text:
        pre = text.split(begin, 1)[0].rstrip("\n")
        post = text.split(end, 1)[1].lstrip("\n")
        merged = "\n\n".join(p for p in (pre, block, post) if p).rstrip() + "\n"
        path.write_text(merged, encoding="utf-8")
        return "refreshed"
    path.write_text(text.rstrip() + "\n\n" + block + "\n", encoding="utf-8")
    return "appended"


def remove_managed_block(path: Path, begin: str, end: str) -> bool:
    """Remove the marker-delimited managed section from `path`, preserving all user
    text outside it. Returns True if a block was removed. If the file then contains
    only whitespace (it was our block alone), it is deleted. No-op (False) when the
    file is absent or has no well-formed managed block."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if begin not in text or end not in text:
        return False
    pre = text.split(begin, 1)[0].rstrip("\n")
    post = text.split(end, 1)[1].lstrip("\n")
    remainder = "\n\n".join(p for p in (pre, post) if p).rstrip()
    if remainder:
        path.write_text(remainder + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


# --- JSON settings mutation ----------------------------------------------------

def backup_file(path: Path) -> Path | None:
    """Copy `path` to a timestamped, never-overwritten sibling `<name>.bak-<ts>`
    before it is mutated. Returns the backup path, or None if `path` does not exist
    yet (nothing to back up). Always call this BEFORE writing settings.json."""
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(path.name + f".bak-{ts}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(path.name + f".bak-{ts}-{suffix}")
        suffix += 1
    backup.write_bytes(path.read_bytes())
    return backup


def read_json_or_raise(path: Path) -> dict:
    """Parse a JSON settings file. Missing file -> {} (fresh install). A malformed
    file raises json.JSONDecodeError so the caller can abort registration WITHOUT
    overwriting the user's (recoverable) file.

    Read as utf-8-sig: a UTF-8 BOM (written by Notepad/PowerShell on Windows) is
    stripped instead of poisoning json.loads — the same robustness rule the engine
    applies to hook stdin."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


# --- owned hooks inside a settings `hooks` object ------------------------------

def _def_is_owned(definition: dict, owner_names: Iterable[str]) -> bool:
    owned = set(owner_names)
    return any(h.get("name") in owned for h in definition.get("hooks", []))


def merge_named_hooks(hooks_obj: dict, event: str, entries: list, owner_names: Iterable[str]) -> None:
    """Replace our own hook definitions for `event` with `entries`, keeping every
    foreign definition untouched. Ownership is decided by the stable inner hook
    `name` (e.g. 'singularity-session-start'), never by matching command text.
    Mutates `hooks_obj` in place."""
    foreign = [d for d in hooks_obj.get(event, []) if not _def_is_owned(d, owner_names)]
    merged = foreign + list(entries)
    if merged:
        hooks_obj[event] = merged
    else:
        hooks_obj.pop(event, None)


def remove_named_hooks(hooks_obj: dict, owner_names: Iterable[str]) -> int:
    """Drop every owned hook definition across all events, leaving foreign hooks and
    now-empty events cleaned up. Returns how many definitions were removed."""
    removed = 0
    for event in list(hooks_obj.keys()):
        defs = hooks_obj[event]
        if not isinstance(defs, list):
            continue
        kept = [d for d in defs if not _def_is_owned(d, owner_names)]
        removed += len(defs) - len(kept)
        if kept:
            hooks_obj[event] = kept
        else:
            hooks_obj.pop(event, None)
    return removed


# --- ownership proof for paths (uninstall safety) ------------------------------

def resolves_into(path: Path, root: Path) -> bool:
    """True if `path` (following symlinks/junctions) resolves to a location inside
    `root`. Used before deleting a skill link at uninstall: only remove it when it
    demonstrably points back into the engine source tree."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False

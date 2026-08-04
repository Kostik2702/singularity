"""`{{VAULT}}` — the vault path as a placeholder, substituted at install time.

Agent and skill sources are Markdown INSTRUCTIONS: they cannot read a config at
runtime, so a path they need to name has to be baked in by someone. It used to
be baked in literally — one machine's absolute path, with
`register_agents.TEMPLATE_VAULT` holding the same string so a differing vault
could be substituted back out. That works exactly until the sentinel and the
sources disagree, which is what happened when the authoring machine moved to
another drive: both had to be rewritten together, in ~30 files, or the
substitution silently stopped matching.

A token cannot drift. It is not a path, so it never becomes stale, never names
a drive, and never has to be migrated — the sources say `{{VAULT}}` and the
installer fills in whatever this machine actually configured.

Deployed copies carry an ownership marker so `mb uninstall` can still tell OUR
rendered copy from a user's own directory. Without it a rendered skill would be
indistinguishable from foreign content, and the uninstaller — which deletes
only what resolves back into the engine — would leave it behind forever.
"""
from __future__ import annotations

import json
from pathlib import Path

TOKEN = "{{VAULT}}"
MARKER = ".singularity-owned.json"

# Rendered only for text we author. A binary or an unknown extension is copied
# byte-for-byte: substituting inside one would corrupt it.
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".sh",
                 ".ps1", ".cmd", ".js", ".mjs", ".css", ".html"}


def render_text(text: str, vault: Path) -> str:
    """Every `{{VAULT}}` replaced by this machine's vault path."""
    return text.replace(TOKEN, str(vault))


def file_has_token(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        return TOKEN in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def tree_has_token(root: Path) -> bool:
    """Does anything under `root` need substitution? Decides link vs render."""
    if root.is_file():
        return file_has_token(root)
    return any(file_has_token(p) for p in root.rglob("*") if p.is_file())


def write_marker(dst: Path, src: Path, vault: Path) -> None:
    """Record that WE rendered this directory, from where, for which vault."""
    payload = {"owner": "singularity", "source": str(src), "vault": str(vault)}
    try:
        (dst / MARKER).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    except OSError:
        pass                    # a marker we could not write just means "not ours"


def is_rendered_copy(dst: Path, src: Path) -> bool:
    """Is `dst` a copy WE rendered from `src`?

    Deliberately strict: the marker must name this exact source. A directory
    without one, or naming another engine, is foreign and must never be
    deleted — same rule the symlink check enforces for the linked case."""
    marker = dst / MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("owner") != "singularity":
        return False
    try:
        return Path(str(data.get("source"))).resolve() == src.resolve()
    except OSError:
        return False


def render_tree(src: Path, dst: Path, vault: Path) -> None:
    """Copy `src` to `dst`, substituting the token in text files, and mark it."""
    import shutil

    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.rglob("*")):
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if file_has_token(item):
            # newline="" so a rendered file keeps the line endings it had
            with open(item, encoding="utf-8", newline="") as fh:
                text = fh.read()
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(render_text(text, vault))
        else:
            shutil.copy2(item, target)
    write_marker(dst, src, vault)

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

# `{{AGENTS}}` — where the deployed agent definitions live, for the one thing
# that must NOT be addressed through the vault: the shared preamble.
#
# The preamble is a mechanism, not knowledge. Agents used to read it from
# `{{VAULT}}/rules/`, which made the rules every subagent runs on a property of
# whatever vault happened to be configured — swap the vault and eight agents
# start with no rules at all, silently. Knowledge belongs in the vault and its
# absence must cost only recall; the operating rules ship with the engine.
AGENTS_TOKEN = "{{AGENTS}}"

# `{{WORKFLOW}}` — the phase files of the development flow, for the same reason
# and with the same history. They lived in `{{VAULT}}/workflow/`, which made a
# ten-phase procedure a property of which vault was configured: swap it and the
# whole flow disappears with nothing printed. A phase file is a checkbox gate
# that must execute, so it is mechanism and ships with the engine.
WORKFLOW_TOKEN = "{{WORKFLOW}}"

MARKER = ".singularity-owned.json"

# Rendered only for text we author. A binary or an unknown extension is copied
# byte-for-byte: substituting inside one would corrupt it.
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".sh",
                 ".ps1", ".cmd", ".js", ".mjs", ".css", ".html"}


def default_agents_dir() -> Path:
    """Where `register_agents` deploys agent definitions on this machine."""
    return Path.home() / ".claude" / "agents"


def default_workflow_dir() -> Path:
    """Where the phase files live — inside the engine, never the vault."""
    return Path(__file__).resolve().parents[1] / "singularity" / "workflow"


def render_text(text: str, vault: Path, agents: Path | None = None,
                workflow: Path | None = None) -> str:
    """Every `{{VAULT}}` replaced by this machine's vault path, every
    `{{AGENTS}}` by the deployed-agents directory, every `{{WORKFLOW}}` by the
    engine's phase directory.

    `{{AGENTS}}` and `{{WORKFLOW}}` render via `as_posix()`, `{{VAULT}}` via
    `str()`. `str()` on Windows yields backslashes and produced a MIXED
    separator wherever a template appends `/<file>` (issue #17):
    `\\deployed\\agents/AGENT_SHARED_PREAMBLE.md`. Windows opened it, so rules
    delivery worked, but the delivered text differed per platform and every
    test building its expectation with `Path` joins went red on Windows. Forward
    slashes open on every platform and read the same to the model that follows
    them. `{{VAULT}}` keeps `str()` byte for byte: issue #17 names only
    `{{AGENTS}}`."""
    out = text.replace(TOKEN, str(vault))
    out = out.replace(AGENTS_TOKEN, (agents if agents is not None
                                     else default_agents_dir()).as_posix())
    return out.replace(WORKFLOW_TOKEN, (workflow if workflow is not None
                                        else default_workflow_dir()).as_posix())


def file_has_token(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # Every token render_text substitutes must be listed here. A token missing
    # from this check does not fail — the tree is linked instead of rendered
    # and the literal `{{...}}` reaches the deployed file, where it reads as a
    # path and resolves to nothing.
    return any(t in text for t in (TOKEN, AGENTS_TOKEN, WORKFLOW_TOKEN))


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

"""Deploy Singularity-owned agent definitions into ~/.claude/agents.

Same safety contract as register_skills (AUD-06/AUD-12): the engine repo is the
single source of truth (singularity/agents/<name>.md); each destination is a
symlink back to it where the OS allows, else a copy. Idempotent. A destination
that is byte-identical to the repo source is replaced/left alone; one that
differs (user edits, foreign install) is never deleted — it is moved aside to a
timestamped `<name>.md.backup-<ts>` sibling with a manifest before writing. Only
names in AGENTS are touched; unmanaged agents (e.g. comfyui-*) are left alone.

Agents are single .md FILES, so — unlike skills — a privilege-free Windows
directory junction is not applicable; the fallback is a plain copy.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling install/ imports

import vault_token  # noqa: E402  ({{VAULT}} substitution, shared with skills)

AGENTS_SRC = ENGINE / "singularity" / "agents"

# The agent sources tell the model to read rules out of the vault, so they have
# to name a path. They name `{{VAULT}}` and the installer substitutes it.
#
# This used to be one machine's absolute path, with the same string repeated
# here as a sentinel to substitute back out. That pairing had to be kept in
# sync by hand across every agent and skill source, and it silently stopped
# matching the moment the authoring machine moved to another drive. A token
# cannot drift: it is not a path, so it never goes stale and never needs
# migrating.
TEMPLATE_VAULT = vault_token.TOKEN

# Engine-owned council/workflow agents (source of truth in singularity/agents/).
AGENTS = [
    "investigator",
    "critic",
    "validator",
    "historian",
    "reviewer",
    "coder",
    "web-implementer",
    "web-viewer",
    "scout",
    "advisor",
]

# The shared preamble ships WITH the agents, not through the vault: it is the
# rule set every subagent starts on, and a mechanism may not depend on which
# vault (or whether a vault) is configured. It is deployed under the same name
# the agent sources address as `{{AGENTS}}/AGENT_SHARED_PREAMBLE.md`.
PREAMBLE = "AGENT_SHARED_PREAMBLE"


def _points_to(link: Path, target: Path) -> bool:
    try:
        return link.is_symlink() and link.resolve() == target.resolve()
    except OSError:
        return False


def _files_equal(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _backup_file(dst: Path, reason: str) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = dst.with_name(f"{dst.name}.backup-{ts}")
    suffix = 1
    while backup.exists():
        backup = dst.with_name(f"{dst.name}.backup-{ts}-{suffix}")
        suffix += 1
    shutil.move(str(dst), str(backup))
    backup.with_name(backup.name + ".manifest.txt").write_text(
        f"Moved from: {dst}\nMoved at:   {ts}\nReason:     {reason}\n",
        encoding="utf-8")
    return backup


def _install_file(src: Path, dst: Path) -> str:
    """Make dst mirror src (symlink, else copy). Returns: 'ok' (already correct),
    'linked'/'copied' (created or replaced an identical/link entry),
    'backed-up' (a differing real file was preserved before writing)."""
    if _points_to(dst, src):
        return "ok"
    status = "linked"
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        if _files_equal(src, dst):
            dst.unlink()                    # identical: safe to replace
        else:
            _backup_file(dst, "content differs from engine-owned agent source; "
                              "preserved instead of being overwritten by register_agents")
            status = "backed-up"
    try:
        os.symlink(src, dst)
        return status
    except OSError:
        shutil.copy2(src, dst)              # no privilege-free file link -> copy
        return status if status == "backed-up" else "copied"


def _resolved_vault() -> Path | None:
    """This machine's vault, through the engine's own resolution order. Import
    is local and guarded: registration must still work from a checkout whose
    dependencies are not importable yet."""
    try:
        sys.path.insert(0, str(ENGINE))
        from singularity.config import resolve_vault
        return resolve_vault()
    except Exception:                      # noqa: BLE001 — fall back to linking
        return None


def _render(src: Path, dst: Path, vault: Path | None, agents: Path) -> str:
    """Write `dst` with the tokens resolved for this machine. `{{AGENTS}}` always
    resolves; `{{VAULT}}` is left as the token when no vault is configured, so a
    vaultless install shows an honest placeholder instead of a bogus path.
    Returns 'copied' / 'backed-up', mirroring `_install_file`'s vocabulary."""
    text = vault_token.render_text(src.read_text(encoding="utf-8"),
                                   Path(TEMPLATE_VAULT) if vault is None else vault,
                                   agents)
    status = "copied"
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        if dst.read_text(encoding="utf-8", errors="replace") == text:
            return "ok"                    # already rendered for this vault
        if _files_equal(src, dst):
            dst.unlink()                   # stale verbatim copy: safe to replace
        else:
            _backup_file(dst, "content differs from engine-owned agent source; "
                              "preserved instead of being overwritten by "
                              "register_agents")
            status = "backed-up"
    dst.write_text(text, encoding="utf-8")
    return status


def main() -> int:
    dest_root = Path.home() / ".claude" / "agents"
    dest_root.mkdir(parents=True, exist_ok=True)
    vault = _resolved_vault()
    # The preamble deploys alongside the agents that read it — one write, or the
    # agents point at a file that is not there.
    owned = AGENTS + [PREAMBLE]
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in owned:
        src = AGENTS_SRC / f"{name}.md"
        if not src.is_file():
            print(f"[agents] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        dst = dest_root / f"{name}.md"
        # Render whenever the source names anything this machine has to fill in;
        # otherwise keep the symlink, so the common case stays as it was.
        if vault_token.file_has_token(src):
            status = _render(src, dst, vault, dest_root)
        else:
            status = _install_file(src, dst)
        counts[status] += 1
        if status == "backed-up":
            print(f"[agents] {name}: differing local copy preserved as backup, "
                  f"now points to repo source", file=sys.stderr)
    if vault is not None:
        print(f"[agents] vault path rewritten to {vault} in rendered copies "
              f"(re-run the installer after editing an agent source)")
    else:
        print("[agents] no vault configured: {{VAULT}} left as a placeholder; "
              "the shared rules still deploy — they do not live in the vault")
    print(f"[agents] {len(owned)} owned ({len(AGENTS)} agents + preamble): "
          f"{counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

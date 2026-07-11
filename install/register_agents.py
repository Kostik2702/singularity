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
AGENTS_SRC = ENGINE / "singularity" / "agents"

# Engine-owned council/workflow agents (source of truth in singularity/agents/).
AGENTS = [
    "investigator",
    "critic",
    "validator",
    "historian",
    "reviewer",
    "coder",
]


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


def main() -> int:
    dest_root = Path.home() / ".claude" / "agents"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in AGENTS:
        src = AGENTS_SRC / f"{name}.md"
        if not src.is_file():
            print(f"[agents] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = _install_file(src, dest_root / f"{name}.md")
        counts[status] += 1
        if status == "backed-up":
            print(f"[agents] {name}: differing local copy preserved as backup, "
                  f"now points to repo source", file=sys.stderr)
    print(f"[agents] {len(AGENTS)} owned: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

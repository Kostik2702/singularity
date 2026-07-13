"""Deploy Singularity-owned skills into ~/.claude/skills as live junctions.

The engine repo is the single source of truth (singularity/skills/<name>); each
destination is a directory junction/symlink back to it, so edits in the repo take
effect immediately — same philosophy as register_hooks (which points hook
commands into the engine rather than copying).

Idempotent: a destination already linked to the right source is left alone. A
stale real directory for an owned skill is replaced by a link automatically
ONLY if its content is byte-identical to the repo source; a directory that
differs (user edits, a foreign install, anything unexpected) is never deleted
— it is moved aside to a timestamped `<name>.backup-<ts>/` sibling (with a
manifest) before linking, so no user content is ever silently destroyed. Only
names in SKILLS are touched — unmanaged skills (e.g. comfyui-photorealism)
are never removed.
"""
from __future__ import annotations

import datetime
import filecmp
import os
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILLS_SRC = ENGINE / "singularity" / "skills"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_manifest  # noqa: E402  (T15: single source of client->skills)

# Engine-owned skills deployed to Claude (source of truth: skill_manifest.py).
# Non-engine skills living loose in ~/.claude/skills are intentionally absent
# and left untouched.
SKILLS = skill_manifest.skills_for_client("claude")


def _points_to(link: Path, target: Path) -> bool:
    """True if `link` is a symlink/junction already resolving to `target`."""
    try:
        return link.is_dir() and link.resolve() == target.resolve() and (
            link.is_symlink() or os.path.realpath(link) != str(link))
    except OSError:
        return False


def _dirs_equal(a: Path, b: Path) -> bool:
    """True if a and b hold the same relative filenames with byte-identical
    content, recursively. Used to decide whether a stale real directory can be
    replaced automatically or must be preserved as a backup.

    Deliberately does NOT use filecmp.cmp: it caches outcomes keyed by an
    os.stat (size+mtime) signature, which can serve a stale/wrong verdict for
    a file rewritten with the same size within one mtime tick. Every common
    file is read and compared byte-for-byte instead — no cache, no shortcut."""
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    for name in cmp.common_files:
        if (a / name).read_bytes() != (b / name).read_bytes():
            return False
    return all(_dirs_equal(a / sub, b / sub) for sub in cmp.common_dirs)


def _backup_dir(dst: Path, reason: str) -> Path:
    """Move dst aside to a timestamped, never-overwritten sibling with a small
    manifest explaining why — the caller can then link `dst` freely."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = dst.parent / f"{dst.name}.backup-{ts}"
    suffix = 1
    while backup.exists():
        backup = dst.parent / f"{dst.name}.backup-{ts}-{suffix}"
        suffix += 1
    shutil.move(str(dst), str(backup))
    (backup / ".migration-manifest.txt").write_text(
        f"Moved from: {dst}\nMoved at:   {ts}\nReason:     {reason}\n",
        encoding="utf-8")
    return backup


def _link_dir(src: Path, dst: Path) -> str:
    """Make dst a junction/symlink to src. Returns a short status word:
    'ok' (already linked), 'linked' (created, or replaced an
    identical/link stale entry), 'backed-up' (a differing real directory was
    preserved as a backup before linking), 'copied' (no link support on this
    host)."""
    if _points_to(dst, src):
        return "ok"
    status = "linked"
    if dst.is_symlink():
        dst.unlink()                            # just a link, no content to lose
    elif dst.exists():
        if os.name == "nt" and dst.is_dir() and os.path.realpath(dst) != str(dst):
            dst.rmdir()                         # stale junction: reparse point only
        elif _dirs_equal(src, dst):
            shutil.rmtree(dst)                  # identical content: safe to drop
        else:
            _backup_dir(dst, "content differs from engine-owned skill source; "
                             "preserved instead of being deleted by register_skills")
            status = "backed-up"
    try:
        os.symlink(src, dst, target_is_directory=True)
        return status
    except OSError:
        pass
    if os.name == "nt":                       # junctions need no privilege
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
        return status
    shutil.copytree(src, dst)                  # last resort
    return status if status == "backed-up" else "copied"


def main() -> int:
    dest_root = Path.home() / ".claude" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[skills] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = _link_dir(src, dest_root / name)
        counts[status] += 1
        if status == "backed-up":
            print(f"[skills] {name}: differing local copy preserved as backup, "
                  f"now linked to repo source", file=sys.stderr)
    print(f"[skills] {len(SKILLS)} owned: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

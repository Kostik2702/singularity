"""Deploy Singularity-owned skills into ~/.claude/skills as live junctions.

The engine repo is the single source of truth (singularity/skills/<name>); each
destination is a directory junction/symlink back to it, so edits in the repo take
effect immediately — same philosophy as register_hooks (which points hook
commands into the engine rather than copying).

Idempotent: a destination already linked to the right source is left alone; a
stale real directory for an owned skill is replaced by a link. Only names in
SKILLS are touched — unmanaged skills (e.g. comfyui-photorealism) are never
removed.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILLS_SRC = ENGINE / "singularity" / "skills"

# Engine-owned skills (source of truth in singularity/skills/). Non-engine skills
# living loose in ~/.claude/skills are intentionally absent and left untouched.
SKILLS = [
    "mb-search",
    "mb-memory",
    "mb-llm-delegate",
    "workflow-helper",
    "grill",
    "confidence-calibration",
    "memory-curator",
    "council",
    "planning-council",
    "observation-council",
    "error-reports",
    "report",
    "compaction-plan",
]


def _points_to(link: Path, target: Path) -> bool:
    """True if `link` is a symlink/junction already resolving to `target`."""
    try:
        return link.is_dir() and link.resolve() == target.resolve() and (
            link.is_symlink() or os.path.realpath(link) != str(link))
    except OSError:
        return False


def _link_dir(src: Path, dst: Path) -> str:
    """Make dst a junction/symlink to src. Returns a short status word."""
    if _points_to(dst, src):
        return "ok"
    if dst.exists() or dst.is_symlink():
        # owned skill: the repo copy is now canonical, drop the redundant entry
        if dst.is_symlink() or (os.name == "nt" and dst.is_dir()
                                and os.path.realpath(dst) != str(dst)):
            dst.unlink(missing_ok=True) if dst.is_symlink() else shutil.rmtree(dst, ignore_errors=True)
        else:
            shutil.rmtree(dst)
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "linked"
    except OSError:
        pass
    if os.name == "nt":                       # junctions need no privilege
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
        return "linked"
    shutil.copytree(src, dst)                  # last resort
    return "copied"


def main() -> int:
    dest_root = Path.home() / ".claude" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "missing": 0}
    for name in SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[skills] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = _link_dir(src, dest_root / name)
        counts[status] += 1
    print(f"[skills] {len(SKILLS)} owned: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

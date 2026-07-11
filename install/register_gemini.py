"""Deploy Singularity into the official Google Gemini CLI (~/.gemini).

Gives Gemini the same user-level parity Codex has (GCLI-02), built on the shared
client_registration primitives so the two clients cannot drift:

  1. A managed block in ~/.gemini/GEMINI.md (Gemini's persistent global-instructions
     file, analog of CLAUDE.md / AGENTS.md): identity, vault path, `mb` CLI +
     SEM-FIRST, capture rule, shortcuts, the portable skill list, platform caveats.
     Marker-delimited so re-runs refresh only our section and never touch user text.

  2. Directory junctions ~/.gemini/skills/<name> -> singularity/skills/<name>,
     reusing register_skills._link_dir. Only the portable (non-council) skills, the
     same set Codex gets. Foreign/user skills in ~/.gemini/skills are left untouched.

Safety contract (see tests/fixtures/gemini/README.md, GCLI-00):
  - Absent client (~/.gemini missing AND `gemini` not on PATH) -> SKIP / rc 0,
    nothing created. Never installs the Gemini CLI itself.
  - Version probed with a timeboxed subprocess; an old/unparseable version is a WARN
    (skills may be ignored by that client) but the profile is still useful, so we do
    not fail.
  - Hooks are NOT registered here — SessionStart/AfterAgent adapters come in GCLI-03/04.

Return codes: 0 = skipped (absent) or success; 1 = a real partial failure
(engine-owned skill missing from the repo).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling install/ imports

import client_registration  # noqa: E402  (shared safe-registration primitives)
import register_skills  # noqa: E402  (reuse the junction helpers, single source)
from singularity.config import resolve_vault  # noqa: E402  (stdlib-only, pre-deps safe)

SKILLS_SRC = ENGINE / "singularity" / "skills"
GEMINI_SKILLS = client_registration.PORTABLE_SKILLS

# Documented lower bound for Agent Skills + Hooks (GCLI-00). Below this, skills may
# be ignored by the client — a WARN, not a hard failure for the profile.
MIN_SKILLS_VERSION = (0, 26, 0)

BEGIN = "<!-- SINGULARITY:BEGIN (managed by install/register_gemini.py — edits below are overwritten) -->"
END = "<!-- SINGULARITY:END -->"


def _gemini_present() -> bool:
    return (Path.home() / ".gemini").exists() or shutil.which("gemini") is not None


def _gemini_version() -> tuple[int, ...] | None:
    """Return the CLI version as an int tuple (e.g. (0, 50, 0)), or None if the
    binary is absent, times out, errors, or prints an unparseable version. Never
    raises — a probe failure must not crash the installer."""
    exe = shutil.which("gemini")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (out.stdout or "") + (out.stderr or "")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", blob)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _profile_body(vault: Path) -> str:
    skills = ", ".join(GEMINI_SKILLS)
    return f"""{BEGIN}
# Singularity — persistent memory on this machine

This machine runs **Singularity**, the user's personal AI-memory system.
- **Engine:** `{ENGINE}` — Python, exposes the `mb` CLI (on PATH).
- **Vault (single source of knowledge):** `{vault}` — an Obsidian vault. When the user says "vault" / "memory brain" they mean this.

## SEM-FIRST — search memory before grep/Read
For ANY discovery of prior knowledge, decisions, howtos, incidents, project docs,
run vault search FIRST, only then fall back to grep/Glob/Read:
```
mb search "<query>"          # RU/EN, ~20ms warm; top-5 compact
mb search "<q>" --top 10 --type learning|decision|howto|incident|rule|reference
mb search "<q>" --tags a,b --links Note --json --full
```
Cite results as `file | section`, don't dump raw lines.

## Capture knowledge
Record the non-obvious / contradicting / needed-more-than-one-attempt (code only as `file:line`):
```
mb learn "<insight>" --task <ID>     # decisions carry owner + source
```
Dedup with `mb search` before writing. Maintenance: `mb doctor --fix`.

## Shortcuts (user may type these)
- **um** — update memory (capture insight/decision into the vault; dedup first)
- **fm [kw]** — find in memory: `mb search` first, then code search, report all sources + gaps
- **s** — settings (this profile + the user's CLAUDE.md)

## Skills mirrored into ~/.gemini/skills
{skills}
(Council/multi-agent skills stay Claude-only and are intentionally not shipped here.)
Note: Gemini may require one-time consent when a skill is activated — that is expected.
{END}"""


def _deploy_skills() -> dict:
    dest_root = Path.home() / ".gemini" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in GEMINI_SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[gemini] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = register_skills._link_dir(src, dest_root / name)
        counts[status] += 1
        if status == "backed-up":
            print(f"[gemini] {name}: differing local copy preserved as backup, "
                  f"now linked to repo source", file=sys.stderr)
    return counts


def main() -> int:
    if not _gemini_present():
        print("[gemini] not installed (~/.gemini absent, `gemini` not on PATH) — skipping")
        return 0

    version = _gemini_version()
    if version is None:
        print("[gemini] WARN: could not determine `gemini --version` "
              "(old/unparseable) — deploying profile; skills may be ignored by this client",
              file=sys.stderr)
    elif version < MIN_SKILLS_VERSION:
        print(f"[gemini] WARN: version {'.'.join(map(str, version))} < "
              f"{'.'.join(map(str, MIN_SKILLS_VERSION))} — Agent Skills may be ignored; "
              f"profile is still applied", file=sys.stderr)

    vault = resolve_vault()
    if vault is None:
        print("[gemini] no vault configured — skipping profile", file=sys.stderr)
        return 0

    status = client_registration.merge_managed_block(
        Path.home() / ".gemini" / "GEMINI.md", _profile_body(vault), BEGIN, END)
    print(f"[gemini] profile ~/.gemini/GEMINI.md: {status}")

    counts = _deploy_skills()
    print(f"[gemini] {len(GEMINI_SKILLS)} skills: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

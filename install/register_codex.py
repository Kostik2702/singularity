"""Deploy Singularity into the OpenAI Codex CLI (~/.codex).

Two things make Codex "know Singularity", mirroring what register_hooks +
register_skills do for Claude Code:

  1. A profile file ~/.codex/AGENTS.md (Codex's persistent global-instructions
     mechanism, analog of CLAUDE.md): identity, vault path, `mb` CLI + SEM-FIRST,
     capture rule, shortcuts. Written inside a marker-delimited managed block so
     re-runs refresh only our section and never clobber user-authored text.

  2. Directory junctions ~/.codex/skills/<name> -> singularity/skills/<name>,
     reusing register_skills._link_dir. Only the CLI/self-contained skills are
     mirrored; the three council skills are Claude-only (they spawn Claude
     subagents investigator/critic/... that Codex has no equivalent for), so
     shipping them to Codex would only mislead it.

No-ops with a clear message when Codex is not installed on this machine, so the
installer stays safe to run everywhere.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # import register_skills

import client_registration  # noqa: E402  (shared safe-registration primitives)
import register_skills  # noqa: E402  (reuse the junction helpers, single source)
import skill_manifest  # noqa: E402  (T15: single source of client->skills)
from singularity.config import resolve_vault  # noqa: E402  (stdlib-only, pre-deps safe)
from singularity.hooks_state import CAPTURE_HOOKS_FRAGMENT  # noqa: E402  (T02 shared fragment)

SKILLS_SRC = ENGINE / "singularity" / "skills"

# CLI/self-contained skills that work identically outside Claude Code (source
# of truth: skill_manifest.py; council skills are Claude-only and excluded).
CODEX_SKILLS = skill_manifest.skills_for_client("codex")

BEGIN = "<!-- SINGULARITY:BEGIN (managed by install/register_codex.py — edits below are overwritten) -->"
END = "<!-- SINGULARITY:END -->"


def _codex_present() -> bool:
    return (Path.home() / ".codex").exists() or shutil.which("codex") is not None


def _profile_body(vault: Path) -> str:
    skills = ", ".join(CODEX_SKILLS)
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

{CAPTURE_HOOKS_FRAGMENT}

## Shortcuts (user may type these)
- **um** — update memory (capture insight/decision into the vault; dedup first)
- **fm [kw]** — find in memory: `mb search` first, then code search, report all sources + gaps
- **s** — settings (this profile + the user's CLAUDE.md)

## Skills mirrored into ~/.codex/skills
{skills}
(Council/multi-agent skills stay Claude-only and are intentionally not shipped here.)
{END}"""


def _write_profile(vault: Path) -> str:
    # Managed-block merge lives in the shared primitive so Codex and Gemini can't
    # drift apart; the Codex golden tests pin byte-for-byte equivalence.
    path = Path.home() / ".codex" / "AGENTS.md"
    return client_registration.merge_managed_block(path, _profile_body(vault), BEGIN, END)


def main() -> int:
    if not _codex_present():
        print("[codex] not installed (~/.codex absent, `codex` not on PATH) — skipping")
        return 0

    vault = resolve_vault()
    if vault is None:
        print("[codex] no vault configured — skipping profile", file=sys.stderr)
        return 0

    status = _write_profile(vault)
    print(f"[codex] profile ~/.codex/AGENTS.md: {status}")

    dest_root = Path.home() / ".codex" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in CODEX_SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[codex] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = register_skills._link_dir(src, dest_root / name)
        counts[status] += 1
        if status == "backed-up":
            print(f"[codex] {name}: differing local copy preserved as backup, "
                  f"now linked to repo source", file=sys.stderr)
    print(f"[codex] {len(CODEX_SKILLS)} skills: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")
    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

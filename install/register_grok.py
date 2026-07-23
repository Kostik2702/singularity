"""Deploy Singularity into the xAI Grok Build CLI (GROK_HOME, default ~/.grok).

Gives Grok the same user-level parity Codex/Gemini have (GRK-02), built on the
shared client_registration primitives so the clients cannot drift:

  1. A managed block in <grok-home>/AGENTS.md (Grok's persistent global-rules
     file, loaded into every session as `project_instructions`): identity,
     vault path, `mb` CLI + SEM-FIRST, capture rule, shortcuts, the portable
     skill list. Marker-delimited so re-runs refresh only our section and never
     touch user text. Grok ALSO loads the user's ~/.claude/CLAUDE.md via Claude
     compatibility (additively, no dedup — GRK-00), so this block stays small
     and Grok-specific instead of restating CLAUDE.md.

  2. Directory junctions <grok-home>/skills/<name> -> singularity/skills/<name>,
     reusing register_skills._link_dir. Only the portable skills from
     skill_manifest (GRK-01). Junction-linked skill dirs are discovered by Grok
     (GRK-00), and a native <grok-home>/skills skill wins name-dedup over any
     Claude-compat copy, so this is the one effective source. Foreign/user
     skills in <grok-home>/skills are left untouched.

Safety contract (see tests/fixtures/grok/README.md, GRK-00):
  - The config root comes from _grok_home(): $GROK_HOME when set, else
    ~/.grok — the SAME resolver for install and uninstall (Grok itself
    resolves GROK_HOME first, then the env HOME; observed on 0.2.111).
  - Absent client (config root missing AND `grok` not on PATH) -> SKIP / rc 0,
    nothing created. Never installs the Grok CLI itself.
  - Version probed with a timeboxed subprocess (`grok --version` ->
    "grok 0.2.111 (94172f2aa4)"); an old/unparseable version is a WARN — the
    profile is still useful, so we do not fail.
  - Everything is written BOM-less UTF-8: Grok silently skips BOM'd hook JSON,
    ignores BOM'd SKILL.md frontmatter, and leaks a BOM'd AGENTS.md into the
    model context as mojibake (GRK-00).
  - Hooks and config.toml are NOT touched here — the runtime adapter is
    GRK-04, lifecycle wiring is GRK-03.

Return codes: 0 = skipped (absent) or success; 1 = a real partial failure
(engine-owned skill missing from the repo).
"""
from __future__ import annotations

import os
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
import skill_manifest  # noqa: E402  (T15/GRK-01: single source of client->skills)
from singularity.config import resolve_vault  # noqa: E402  (stdlib-only, pre-deps safe)
from singularity.hooks_state import CAPTURE_HOOKS_FRAGMENT  # noqa: E402  (T02 shared fragment)

SKILLS_SRC = ENGINE / "singularity" / "skills"
GROK_SKILLS = skill_manifest.skills_for_client("grok")

# The only version the GRK-00 contract fixtures are proven against. Older or
# unprobeable versions get the profile anyway (WARN) — it is plain Markdown.
MIN_GROK_VERSION = (0, 2, 111)

BEGIN = "<!-- SINGULARITY:BEGIN (managed by install/register_grok.py — edits below are overwritten) -->"
END = "<!-- SINGULARITY:END -->"


def _grok_home() -> Path:
    """The Grok config root: $GROK_HOME when set, else ~/.grok. Install and
    uninstall MUST both use this — never a hardcoded ~/.grok."""
    env = os.environ.get("GROK_HOME", "").strip()
    return Path(env) if env else Path.home() / ".grok"


def _grok_present() -> bool:
    return _grok_home().exists() or shutil.which("grok") is not None


def _grok_version() -> tuple[int, ...] | None:
    """Return the CLI version as an int tuple (e.g. (0, 2, 111)), or None if the
    binary is absent, times out, errors, or prints an unparseable version. Never
    raises — a probe failure must not crash the installer.

    Format observed on the pinned contract version: `grok 0.2.111 (94172f2aa4)`
    (identical for `--version` and the `version` subcommand). Retries once for
    the cold-start / first-scan case, mirroring the Gemini prober."""
    exe = shutil.which("grok")
    if not exe:
        return None
    for _attempt in range(2):
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (out.stdout or "") + (out.stderr or ""))
        if m:
            return tuple(int(g) for g in m.groups())
    return None


def _profile_body(vault: Path) -> str:
    skills = ", ".join(GROK_SKILLS)
    return f"""{BEGIN}
# Singularity — persistent memory on this machine

This machine runs **Singularity**, the user's personal AI-memory system.
- **Engine:** `{ENGINE}` — Python, exposes the `mb` CLI (on PATH).
- **Vault (single source of knowledge):** `{vault}` — an Obsidian vault. When the user says "vault" / "memory brain" they mean this.
- **At session start:** run `mb search "<current task topic>"` before exploring code — prior decisions/learnings usually exist. (Grok has no context-injecting session hook, so this recall is on you.)

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

## Skills mirrored into the Grok home skills dir ($GROK_HOME or ~/.grok)/skills
{skills}
(Council/multi-agent and visual-gate skills stay Claude-only and are intentionally not shipped here.)
Note: Grok also scans ~/.claude for rules and skills; same-name skills resolve to this native copy.
{END}"""


def _deploy_skills() -> dict:
    dest_root = _grok_home() / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "missing": 0}
    for name in GROK_SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[grok] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = register_skills._link_dir(src, dest_root / name)
        counts[status] += 1
        if status == "backed-up":
            print(f"[grok] {name}: differing local copy preserved as backup, "
                  f"now linked to repo source", file=sys.stderr)
    return counts


def _remove_link(link: Path) -> None:
    """Delete a symlink/junction without touching its target."""
    if link.is_symlink():
        link.unlink()
    elif os.name == "nt" and link.is_dir():
        link.rmdir()                     # junction reparse point, not the target
    else:
        link.unlink()


def uninstall() -> dict:
    """Remove ONLY Singularity-owned Grok artifacts and nothing else:
      - the managed block in <grok-home>/AGENTS.md (user text outside preserved;
        the file is deleted only when our block was its sole content);
      - skill links in <grok-home>/skills that resolve EXACTLY to our engine
        source (register_skills._points_to) — foreign or copied directories at
        an owned name are counted and kept, never auto-deleted.
    Foreign skills, backup dirs, credentials, sessions, config.toml, the Grok
    binary and the vault are never touched. Safe to run when Grok is absent."""
    grok_home = _grok_home()
    result = {"profile": "absent", "skills_removed": 0, "skills_kept": 0}

    if client_registration.remove_managed_block(grok_home / "AGENTS.md", BEGIN, END):
        result["profile"] = "removed"

    skills_dir = grok_home / "skills"
    for name in GROK_SKILLS:
        link = skills_dir / name
        if register_skills._points_to(link, SKILLS_SRC / name):
            _remove_link(link)
            result["skills_removed"] += 1
        elif link.is_symlink() or link.exists():
            result["skills_kept"] += 1     # foreign / copied — never auto-deleted
    return result


def main() -> int:
    if not _grok_present():
        print(f"[grok] not installed ({_grok_home()} absent, `grok` not on PATH) — skipping")
        return 0

    version = _grok_version()
    if version is None:
        print("[grok] WARN: could not determine `grok --version` "
              "(old/unparseable) — deploying profile; contract unproven for this client",
              file=sys.stderr)
    elif version < MIN_GROK_VERSION:
        print(f"[grok] WARN: version {'.'.join(map(str, version))} < "
              f"{'.'.join(map(str, MIN_GROK_VERSION))} — the GRK-00 contract is unproven "
              f"below this; profile is still applied", file=sys.stderr)

    vault = resolve_vault()
    if vault is None:
        print("[grok] no vault configured — skipping profile", file=sys.stderr)
        return 0

    grok_home = _grok_home()
    status = client_registration.merge_managed_block(
        grok_home / "AGENTS.md", _profile_body(vault), BEGIN, END)
    print(f"[grok] profile {grok_home / 'AGENTS.md'}: {status}")

    counts = _deploy_skills()
    print(f"[grok] {len(GROK_SKILLS)} skills: {counts['linked']} linked, "
          f"{counts['ok']} already ok, {counts['copied']} copied, "
          f"{counts['backed-up']} backed up, {counts['missing']} missing")

    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

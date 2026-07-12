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

import json
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
from singularity.config import resolve_vault  # noqa: E402  (stdlib-only, pre-deps safe)
from singularity.hooks_state import CAPTURE_HOOKS_FRAGMENT  # noqa: E402  (T02 shared fragment)

SKILLS_SRC = ENGINE / "singularity" / "skills"
GEMINI_SKILLS = client_registration.PORTABLE_SKILLS

PYTHON = ENGINE / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win")
                             else "bin/python")

# Documented lower bound for Agent Skills + Hooks (GCLI-00). Below this (or when the
# version cannot be probed), skills may be ignored — a WARN, not a hard failure for
# the profile — and hooks are NOT registered (conservative: we can't prove support).
MIN_SKILLS_VERSION = (0, 26, 0)
MIN_HOOKS_VERSION = (0, 26, 0)

# Stable owner names for our nested hooks — uninstall finds them by exact name,
# never by a command substring (GCLI-00: Gemini has no "_mb"-style marker field).
SESSION_START_HOOK_NAME = "singularity-session-start"
AFTER_AGENT_HOOK_NAME = "singularity-after-agent"
OWNED_HOOK_NAMES = {SESSION_START_HOOK_NAME, AFTER_AGENT_HOOK_NAME}

BEGIN = "<!-- SINGULARITY:BEGIN (managed by install/register_gemini.py — edits below are overwritten) -->"
END = "<!-- SINGULARITY:END -->"


def _gemini_present() -> bool:
    return (Path.home() / ".gemini").exists() or shutil.which("gemini") is not None


def _gemini_version() -> tuple[int, ...] | None:
    """Return the CLI version as an int tuple (e.g. (0, 50, 0)), or None if the
    binary is absent, times out, errors, or prints an unparseable version. Never
    raises — a probe failure must not crash the installer.

    Retries once: the first `gemini --version` on a machine is a cold Node start
    (the CLI bundle is tens of MB and Windows Defender may scan it on first spawn),
    which can exceed the timeout; a warm second attempt is ~2s."""
    exe = shutil.which("gemini")
    if not exe:
        return None
    for attempt in range(2):
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

{CAPTURE_HOOKS_FRAGMENT}

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


def _hook_entry(script_name: str, name: str) -> dict:
    script = ENGINE / "singularity" / "hooks" / script_name
    # Gemini executes command hooks through the platform shell.  A quoted
    # executable path is directly invokable by POSIX shells, but PowerShell
    # parses it as a string expression; a second quoted path then becomes an
    # UnexpectedToken.  The call operator is therefore required on Windows:
    #   & "C:\\...\\python.exe" "D:\\...\\hook.py"
    command = f'"{PYTHON}" "{script}"'
    if sys.platform.startswith("win"):
        command = f"& {command}"
    return {
        "hooks": [{
            "type": "command",
            "command": command,
            "name": name,
            "timeout": 15000,
        }]
    }


def _register_hooks() -> str:
    """Merge our named SessionStart + AfterAgent hooks into ~/.gemini/settings.json,
    preserving every foreign key/hook. AfterAgent uses a passive systemMessage nudge
    (GCLI-00: deny-retry is headless-unsafe). A malformed settings file is left
    UNTOUCHED (recoverable) and hooks are skipped. Returns a short status word."""
    settings_path = Path.home() / ".gemini" / "settings.json"
    try:
        data = client_registration.read_json_or_raise(settings_path)
    except json.JSONDecodeError:
        print("[gemini] WARN: ~/.gemini/settings.json is malformed — left untouched, "
              "hooks NOT registered", file=sys.stderr)
        return "skipped-malformed"
    if not isinstance(data, dict):
        print("[gemini] WARN: ~/.gemini/settings.json is not a JSON object — left "
              "untouched, hooks NOT registered", file=sys.stderr)
        return "skipped-malformed"
    client_registration.backup_file(settings_path)     # timestamped, no-op if absent
    hooks = data.setdefault("hooks", {})
    client_registration.merge_named_hooks(
        hooks, "SessionStart",
        [_hook_entry("gemini_session_start.py", SESSION_START_HOOK_NAME)], OWNED_HOOK_NAMES)
    client_registration.merge_named_hooks(
        hooks, "AfterAgent",
        [_hook_entry("gemini_after_agent.py", AFTER_AGENT_HOOK_NAME)], OWNED_HOOK_NAMES)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return "registered"


def _remove_link(link: Path) -> None:
    """Delete a symlink/junction without touching its target."""
    if link.is_symlink():
        link.unlink()
    elif os.name == "nt" and link.is_dir():
        link.rmdir()                     # junction reparse point, not the target
    else:
        link.unlink()


def uninstall() -> dict:
    """Remove ONLY Singularity-owned Gemini artifacts and nothing else:
      - the managed block in ~/.gemini/GEMINI.md (user text outside it preserved);
      - the named `singularity-session-start` hook in ~/.gemini/settings.json
        (backed up first; foreign hooks/keys kept; malformed file left untouched);
      - skill links in ~/.gemini/skills that resolve EXACTLY to our engine source.
    Foreign skills, backup dirs, credentials, Node/Gemini CLI and the vault are
    never touched. Returns a summary dict. Safe to run when Gemini is absent."""
    home_gemini = Path.home() / ".gemini"
    result = {"profile": "absent", "hooks": 0, "skills_removed": 0, "skills_kept": 0}

    if client_registration.remove_managed_block(home_gemini / "GEMINI.md", BEGIN, END):
        result["profile"] = "removed"

    settings_path = home_gemini / "settings.json"
    try:
        data = client_registration.read_json_or_raise(settings_path)
    except json.JSONDecodeError:
        print("[gemini] WARN: ~/.gemini/settings.json malformed — hooks left untouched",
              file=sys.stderr)
        data = None
    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        client_registration.backup_file(settings_path)
        result["hooks"] = client_registration.remove_named_hooks(
            data["hooks"], OWNED_HOOK_NAMES)
        if not data["hooks"]:
            data.pop("hooks")
        settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")

    skills_dir = home_gemini / "skills"
    for name in GEMINI_SKILLS:
        link = skills_dir / name
        if register_skills._points_to(link, SKILLS_SRC / name):
            _remove_link(link)
            result["skills_removed"] += 1
        elif link.is_symlink() or link.exists():
            result["skills_kept"] += 1     # foreign / copied — never auto-deleted
    return result


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

    # SessionStart recall + AfterAgent capture nudge — only when the version is
    # known to support hooks.
    if version is not None and version >= MIN_HOOKS_VERSION:
        print(f"[gemini] hooks (SessionStart + AfterAgent): {_register_hooks()}")
    else:
        print("[gemini] hooks: skipped (version unknown or < "
              f"{'.'.join(map(str, MIN_HOOKS_VERSION))} — hook support unproven)",
              file=sys.stderr)

    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

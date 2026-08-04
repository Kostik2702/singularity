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
  - Every file WE create is BOM-less UTF-8: Grok silently skips BOM'd hook
    JSON, ignores BOM'd SKILL.md frontmatter, and leaks a BOM'd AGENTS.md into
    the model context as mojibake (GRK-00). A pre-existing BOM on a
    user-created AGENTS.md is preserved by merge_managed_block (user bytes win
    over cosmetics).
  - config.toml is NEVER touched. The one runtime hook (GRK-04) is the Stop
    capture nudge — a Singularity-owned <grok-home>/hooks/singularity.json —
    registered only when the probed version proves the stop-gate contract
    (>= MIN_GROK_VERSION). Dynamic SessionStart recall is a documented NO-GO
    (GRK-00: no context-injection channel exists), covered statically by the
    AGENTS.md block instead.

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

import cli_locator  # noqa: E402  (find a CLI that PATH alone cannot see, #10)
import client_registration  # noqa: E402  (shared safe-registration primitives)
import register_skills  # noqa: E402  (reuse the junction helpers, single source)
import skill_manifest  # noqa: E402  (T15/GRK-01: single source of client->skills)
from singularity.config import resolve_vault  # noqa: E402  (stdlib-only, pre-deps safe)
from singularity.hooks_state import (  # noqa: E402  (T02/VSO-01 shared fragments)
    CAPTURE_HOOKS_FRAGMENT,
    VAULT_WRITE_POLICY_FRAGMENT,
)

SKILLS_SRC = ENGINE / "singularity" / "skills"
GROK_SKILLS = skill_manifest.skills_for_client("grok")

PYTHON = ENGINE / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win")
                             else "bin/python")

# The only version the GRK-00 contract fixtures are proven against. Older or
# unprobeable versions get the profile anyway (WARN) — it is plain Markdown —
# but the runtime Stop hook is NOT registered (conservative: the stop-gate
# contract is only proven at this version).
MIN_GROK_VERSION = (0, 2, 111)

# Singularity owns this exact FILE inside <grok-home>/hooks/ (grok loads every
# *.json there). Ownership on uninstall is proven by the file containing our
# hook script marker, never by a name substring elsewhere.
HOOK_FILENAME = "singularity.json"
HOOK_SCRIPT_MARKER = "grok_stop_capture.py"

BEGIN = "<!-- SINGULARITY:BEGIN (managed by install/register_grok.py — edits below are overwritten) -->"
END = "<!-- SINGULARITY:END -->"


def _grok_home() -> Path:
    """The Grok config root: $GROK_HOME when set, else ~/.grok. Install and
    uninstall MUST both use this — never a hardcoded ~/.grok."""
    env = os.environ.get("GROK_HOME", "").strip()
    return Path(env) if env else Path.home() / ".grok"


def _grok_exe() -> tuple[str | None, str | None]:
    """(path, where) for the CLI — PATH first, then the version-manager
    locations `PATH` cannot show a non-interactive process (issue #10). The
    Grok CLI ships via npm too, so it had the same present-by-directory /
    version-by-`which` asymmetry as Gemini."""
    return cli_locator.find_cli("grok")


def _grok_present() -> bool:
    return _grok_home().exists() or _grok_exe()[0] is not None


def _grok_version() -> tuple[int, ...] | None:
    """Return the CLI version as an int tuple (e.g. (0, 2, 111)), or None if the
    binary is absent, times out, errors, or prints an unparseable version. Never
    raises — a probe failure must not crash the installer.

    Format observed on the pinned contract version: `grok 0.2.111 (94172f2aa4)`
    (identical for `--version` and the `version` subcommand). Retries once for
    the cold-start / first-scan case, mirroring the Gemini prober."""
    exe, _where = _grok_exe()
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

{VAULT_WRITE_POLICY_FRAGMENT}

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
    counts = {"ok": 0, "linked": 0, "copied": 0, "backed-up": 0, "rendered": 0,
              "missing": 0}
    # same reason as Gemini: never deploy the raw `{{VAULT}}` token to a client
    vault = resolve_vault()
    for name in GROK_SKILLS:
        src = SKILLS_SRC / name
        if not src.is_dir():
            print(f"[grok] MISSING in repo: {name}", file=sys.stderr)
            counts["missing"] += 1
            continue
        status = register_skills._link_dir(src, dest_root / name, vault)
        counts[status] += 1
        if status == "backed-up":
            print(f"[grok] {name}: differing local copy preserved as backup, "
                  f"now linked to repo source", file=sys.stderr)
    return counts


def _hook_command() -> str:
    """The Stop-hook command line, in the ONLY quoting shapes proven to spawn
    under grok's per-platform hook runner (GRK-00, Windows kill-finding):
    Windows commands run through PowerShell where a bare double-quoted argv0
    is a parse error — the call operator `&` is required; POSIX shells invoke
    a quoted path directly. Never reference env vars in the string (grok's
    load-time $-expansion corrupts them)."""
    script = ENGINE / "singularity" / "hooks" / "grok_stop_capture.py"
    command = f'"{PYTHON}" "{script}"'
    if sys.platform.startswith("win"):
        command = f"& {command}"
    return command


def _register_hook() -> str:
    """Write the Singularity-owned Stop capture hook to
    <grok-home>/hooks/singularity.json (BOM-less UTF-8 — grok silently skips
    BOM'd hook JSON). A foreign file squatting our exact filename is backed up
    first (timestamped, never overwritten silently). Returns a status word."""
    hooks_dir = _grok_home() / "hooks"
    hook_path = hooks_dir / HOOK_FILENAME
    payload = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": _hook_command(),
                            "timeout": 30}]}
            ]
        }
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if hook_path.exists():
        current = hook_path.read_text(encoding="utf-8-sig", errors="ignore")
        if current == text:
            return "ok"
        if HOOK_SCRIPT_MARKER not in current:
            backup = client_registration.backup_file(hook_path)
            print(f"[grok] foreign file at {hook_path} preserved as {backup.name}",
                  file=sys.stderr)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(text, encoding="utf-8")
    return "registered"


def _remove_link(link: Path) -> None:
    """Delete an owned skill entry — a link, a junction, or a copy we rendered.
    Shared with register_skills so the two cannot drift."""
    register_skills.remove_owned(link)


def uninstall() -> dict:
    """Remove ONLY Singularity-owned Grok artifacts and nothing else:
      - the managed block in <grok-home>/AGENTS.md (user text outside preserved;
        the file is deleted only when our block was its sole content);
      - skill links in <grok-home>/skills that resolve EXACTLY to our engine
        source, or a copy we rendered ourselves and marked as ours
        (register_skills.is_owned) — foreign or hand-made directories at
        an owned name are counted and kept, never auto-deleted.
    Foreign skills, backup dirs, credentials, sessions, config.toml, the Grok
    binary and the vault are never touched. Safe to run when Grok is absent."""
    grok_home = _grok_home()
    result = {"profile": "absent", "hooks": 0, "skills_removed": 0, "skills_kept": 0}

    if client_registration.remove_managed_block(grok_home / "AGENTS.md", BEGIN, END):
        result["profile"] = "removed"

    hook_path = grok_home / "hooks" / HOOK_FILENAME
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8-sig", errors="ignore")
        if HOOK_SCRIPT_MARKER in content:
            hook_path.unlink()
            result["hooks"] = 1
        else:
            print(f"[grok] {hook_path} exists but is not ours — left untouched",
                  file=sys.stderr)

    skills_dir = grok_home / "skills"
    for name in GROK_SKILLS:
        link = skills_dir / name
        if register_skills.is_owned(link, SKILLS_SRC / name):
            _remove_link(link)
            result["skills_removed"] += 1
        elif link.is_symlink() or link.exists():
            result["skills_kept"] += 1     # foreign / copied — never auto-deleted
    return result


def main() -> int:
    if not _grok_present():
        print(f"[grok] not installed ({_grok_home()} absent, `grok` not on PATH) — skipping")
        return 0

    exe, where = _grok_exe()
    version = _grok_version()
    if exe is None:
        print(f"[grok] WARN: {_grok_home()} exists but the `grok` binary is not "
              f"reachable from this process (PATH and "
              f"{len(cli_locator.search_locations())} version-manager locations "
              f"checked). If it was installed via nvm/volta/fnm, re-run "
              f"`mb doctor --fix` from an interactive shell", file=sys.stderr)
    elif where != "PATH":
        print(f"[grok] found outside PATH at {exe} (via {where}) — "
              f"a version manager is hiding it from non-interactive processes")
    if version is None and exe is not None:
        print("[grok] WARN: could not determine `grok --version` "
              "(old/unparseable) — deploying profile; contract unproven for this client",
              file=sys.stderr)
    elif version is not None and version < MIN_GROK_VERSION:
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

    # Stop capture nudge — only when the version is proven to honor the
    # stop-gate contract (GRK-00 fixtures). SessionStart recall stays static.
    # Unlike Gemini's inert settings entries, a Stop hook here participates in
    # the stop-gate, so an unproven version stays a genuine skip (GRK-00) — but
    # the message now names the real reason instead of a version constraint the
    # installed client may well satisfy (issue #10).
    if version is not None and version >= MIN_GROK_VERSION:
        print(f"[grok] Stop capture hook: {_register_hook()}")
    elif exe is None:
        print("[grok] Stop capture hook: NOT registered — the binary is "
              "unreachable, so the stop-gate contract cannot be established; "
              "re-run `mb doctor --fix` from a shell where `grok` is on PATH",
              file=sys.stderr)
    elif version is None:
        print("[grok] Stop capture hook: skipped — `grok --version` unreadable, "
              "and the stop-gate contract is only proven from "
              f"{'.'.join(map(str, MIN_GROK_VERSION))}", file=sys.stderr)
    else:
        print(f"[grok] Stop capture hook: skipped (version "
              f"{'.'.join(map(str, version))} < "
              f"{'.'.join(map(str, MIN_GROK_VERSION))} — stop-gate contract unproven)",
              file=sys.stderr)

    return 1 if counts["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())

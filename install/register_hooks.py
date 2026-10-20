"""Register Singularity hooks in ~/.claude/settings.json (idempotent merge).

Every entry we own carries "_mb": true so re-register and uninstall can
find them without touching anything else in the user's settings. Ownership
detection also falls back to matching our own script filename in the
command — see [[Rule - hook ownership detection must tolerate pre-marker
installs]]: a machine can have SessionStart/Stop entries that predate the
`_mb` marker (this repo's own dev machine did). Without the fallback, a
re-register would treat that legacy entry as "foreign" and ADD a second,
duplicate entry alongside it instead of replacing it — confirmed live on
2026-07-12 (T07): running the then-current merge produced two SessionStart
and two Stop entries pointing at the identical command.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
PYTHON = ENGINE / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win")
                             else "bin/python")

def guarded_command(script: Path) -> str:
    """A command that exits 0 whatever happens to `script`.

    `python missing.py` exits 2, and for UserPromptSubmit exit 2 blocks and
    erases the prompt - so no UserPromptSubmit hook can be registered as a bare
    script path: an engine checkout that loses the file would block every
    prompt. The launcher is an inline `-c` (no file of ours to lose), runs
    the script as a child and drops its exit status. Single quotes inside
    double quotes mean the same thing to bash, cmd and PowerShell; forward
    slashes are accepted by Python on Windows."""
    return (f'"{PYTHON}" -c "import subprocess,sys;'
            f"subprocess.call([sys.executable,'{script.as_posix()}']);sys.exit(0)\"")


HOOKS = {
    "SessionStart": [
        {
            "_mb": True,
            "hooks": [{
                "type": "command",
                "command": f'"{PYTHON}" "{ENGINE / "singularity" / "hooks" / "session_start.py"}"',
            }],
        }
    ],
    # The only event that fires when a TASK arrives. SessionStart is too early
    # to classify anything and PreToolUse is too late to route — see
    # singularity/machine/routing.py for why a chain needs its own moment.
    "UserPromptSubmit": [
        {
            "_mb": True,
            "hooks": [{
                "type": "command",
                "command": guarded_command(ENGINE / "singularity" / "hooks" / "user_prompt_submit.py"),
            }],
        },
        # Jev's size hint: its own process and its own 5 s budget, so a slow API
        # can never delay the chain table above. A no-op until `mb jev on`.
        {
            "_mb": True,
            "hooks": [{
                "type": "command",
                "command": guarded_command(ENGINE / "singularity" / "hooks" / "jev_router.py"),
                "timeout": 5,
            }],
        },
    ],
    "Stop": [
        {
            "_mb": True,
            "hooks": [{
                "type": "command",
                "command": f'"{PYTHON}" "{ENGINE / "singularity" / "hooks" / "stop_capture.py"}"',
            }],
        }
    ],
    "PreToolUse": [
        {
            "_mb": True,
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{
                "type": "command",
                "command": f'"{PYTHON}" "{ENGINE / "singularity" / "hooks" / "module_context.py"}"',
            }],
        },
        {
            "_mb": True,
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{
                "type": "command",
                "command": f'"{PYTHON}" "{ENGINE / "singularity" / "hooks" / "vault_guard.py"}"',
            }],
        },
    ],
}


# Fallback ownership signal (script filenames) per event, for entries that
# predate the `_mb` marker — must stay in sync with the script names used in
# HOOKS above. PreToolUse owns two distinct scripts (T07 module_context,
# T08 vault_guard), each its own matcher-group entry.
_OWNED_SCRIPTS = {
    "SessionStart": {"session_start.py"},
    "UserPromptSubmit": {"user_prompt_submit.py", "jev_router.py"},
    "Stop": {"stop_capture.py"},
    "PreToolUse": {"module_context.py", "vault_guard.py"},
}


def _is_owned(entry: dict, event: str) -> bool:
    if entry.get("_mb"):
        return True
    scripts = _OWNED_SCRIPTS.get(event, set())
    return any(script in str(h.get("command", ""))
              for h in entry.get("hooks", []) for script in scripts)


def main() -> int:
    settings_path = Path.home() / ".claude" / "settings.json"
    data = {}
    if settings_path.exists():
        backup = settings_path.with_suffix(
            f".json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
        data = json.loads(settings_path.read_text(encoding="utf-8"))

    hooks = data.setdefault("hooks", {})
    for event, entries in HOOKS.items():
        existing = [e for e in hooks.get(event, []) if not _is_owned(e, event)]
        hooks[event] = existing + entries

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"registered {sum(len(v) for v in HOOKS.values())} hook entr(y/ies); "
          f"settings keys: {sorted(data.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

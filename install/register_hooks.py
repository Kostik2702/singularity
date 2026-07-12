"""Register Singularity hooks in ~/.claude/settings.json (idempotent merge).

Every entry we own carries "_mb": true so re-register and uninstall can
find them without touching anything else in the user's settings.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
PYTHON = ENGINE / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win")
                             else "bin/python")

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
        }
    ],
}


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
        existing = [e for e in hooks.get(event, []) if not e.get("_mb")]
        hooks[event] = existing + entries

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"registered {sum(len(v) for v in HOOKS.values())} hook entr(y/ies); "
          f"settings keys: {sorted(data.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

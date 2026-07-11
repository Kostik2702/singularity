"""Platform-neutral auto-capture decision logic (GCLI-04).

Shared by the Claude Stop hook (stop_capture) and the Gemini AfterAgent hook
(gemini_after_agent). The DECISION is identical across clients: did the session do
substantial work whose insights were never captured, and have we already nudged
once this session? Only the transcript format and the wire output differ — those
stay platform-specific in each hook.
"""
from __future__ import annotations

import json
from pathlib import Path

# A session with at least this many tool uses is "substantial" enough to be worth
# a capture nudge; below it is trivial chat.
MIN_TOOL_USES = 8


def auto_capture_enabled(vault: Path) -> bool:
    """Auto-capture is on by default; honour an explicit auto_capture=false in
    vault/.mb/config.json. A malformed config never silently disables the feature."""
    cfg_path = vault / ".mb" / "config.json"
    if not cfg_path.exists():
        return True
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return bool(cfg.get("auto_capture", True))


def session_marker(vault: Path, session_id: str, client: str = "") -> Path:
    """Per-session (and per-client) marker so each client nudges at most once per
    session and two clients never collide. Claude keeps the historical
    `capture-<id>` name (client=""); Gemini uses `capture-gemini-<id>`."""
    markers = vault / ".mb" / "markers"
    markers.mkdir(parents=True, exist_ok=True)
    name = f"capture-{client}-{session_id}" if client else f"capture-{session_id}"
    return markers / name


def should_remind(tool_uses: int, captured: bool) -> bool:
    """Nudge only when the session is substantial AND nothing was captured yet."""
    return tool_uses >= MIN_TOOL_USES and not captured

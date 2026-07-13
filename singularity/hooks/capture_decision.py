"""Per-session capture marker (T10).

`MIN_TOOL_USES`/`should_remind`/`auto_capture_enabled` (the raw tool-count
threshold and the escaped-JSON-substring "mb learn" search) are retired —
superseded by singularity.session_activity.decide() (T09, code-edit based,
no tool-count) and singularity.hooks_state.capture_config_state() (T01, the
canonical auto_capture reader shared with `mb hooks status`). Only the
marker helper survives unchanged: both stop_capture.py (Claude) and
gemini_after_agent.py (Gemini) still need a per-(client, session) file to
nudge/block at most once per session.
"""
from __future__ import annotations

from pathlib import Path


def session_marker(vault: Path, session_id: str, client: str = "") -> Path:
    """Per-session (and per-client) marker so each client nudges at most once
    per session and two clients never collide. Claude keeps the historical
    `capture-<id>` name (client=""); Gemini uses `capture-gemini-<id>`."""
    markers = vault / ".mb" / "markers"
    markers.mkdir(parents=True, exist_ok=True)
    name = f"capture-{client}-{session_id}" if client else f"capture-{session_id}"
    return markers / name

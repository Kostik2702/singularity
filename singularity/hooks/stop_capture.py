"""Stop hook: auto-capture gate (MB-09, port of Genevra rule C5a as a hook).

When Claude finishes responding, this hook decides whether the session did
substantial work whose insights were never captured into the vault. If so,
it BLOCKS the stop exactly once with a capture instruction — the model then
appends insights via `mb learn` / note edits (or proceeds when there is
nothing worth capturing). Deterministic discipline, LLM does the writing.

Guards against loops / noise:
  * stop_hook_active=true (we already blocked once)  -> allow
  * per-session marker exists                        -> allow
  * auto_capture=false in config                     -> allow
  * fewer than MIN_TOOL_USES tool calls in transcript-> allow (trivial chat)
  * session already captured (mb learn / vault write seen in transcript)
                                                     -> allow
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.hooks.capture_decision import (  # noqa: E402
    MIN_TOOL_USES, auto_capture_enabled, session_marker, should_remind)

REASON = (
    "[Singularity auto-capture] Перед завершением зафиксируй знания сессии в vault: "
    "если появились неочевидные инсайты, решения (с owner/source) или сработавшие/провалившиеся "
    "подходы — запиши их (`mb learn \"<инсайт>\" --task <ID>` или дополни соответствующую заметку; "
    "критерий: неочевидное / противоречащее / потребовавшее >1 попытки; код только как file:line). "
    "Если фиксировать нечего — просто заверши ответ, ничего не записывая."
)


def _transcript_stats(transcript_path: str, vault_str: str) -> tuple[int, bool]:
    """Return (tool_use_count, capture_already_happened). Cheap line scan."""
    tool_uses = 0
    captured = False
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"tool_use"' in line:
                    tool_uses += 1
                    if "mb learn" in line or "singularity" in line and "learn" in line:
                        captured = True
                    elif vault_str and vault_str in line and (
                            '"Write"' in line or '"Edit"' in line):
                        captured = True
    except OSError:
        pass
    return tool_uses, captured


def main() -> int:
    # Windows console defaults stdout to cp1251 → Cyrillic becomes mojibake;
    # Claude Code reads hook stdout as UTF-8 (twin of the stdin utf-8-sig rule)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        # utf-8-sig: tolerate a BOM (PowerShell pipes prepend one)
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (json.JSONDecodeError, OSError):
        return 0

    # never re-block: Claude Code sets this when continuing after our block
    if event.get("stop_hook_active"):
        return 0

    try:
        from singularity.config import resolve_vault

        vault = resolve_vault(None)
        if vault is None or not vault.exists():
            return 0
        if not auto_capture_enabled(vault):
            return 0

        session_id = str(event.get("session_id", "unknown"))
        marker = session_marker(vault, session_id)
        if marker.exists():
            return 0

        transcript = event.get("transcript_path", "")
        vault_json_fragment = json.dumps(str(vault))[1:-1]  # escaped like in transcript
        tool_uses, captured = _transcript_stats(transcript, vault_json_fragment)
        if not should_remind(tool_uses, captured):
            return 0

        marker.write_text("blocked-once", encoding="utf-8")
        print(json.dumps({"decision": "block", "reason": REASON}, ensure_ascii=False))
        return 0
    except Exception:  # noqa: BLE001 — a broken hook must never trap the user in a loop
        return 0


if __name__ == "__main__":
    sys.exit(main())

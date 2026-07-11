"""Gemini CLI AfterAgent auto-capture nudge (GCLI-04).

Ports the Claude Stop-hook capture discipline to Gemini, but SAFELY: the GCLI-00
spike proved that `decision:"deny"` (the retry primitive) mutates headless
`-o json` output (concatenated response + a warnings[] array) and that no payload
field distinguishes interactive from headless — so a deny-based nudge would corrupt
automation. Instead this hook emits a passive `systemMessage`, which the spike
proved leaves headless `-o json`/`-o text` byte-identical to baseline. No retry, no
extra model turn: a once-per-session reminder shown to the user in interactive mode.

Shares the substantial/captured/disabled/once-per-session decision with the Claude
hook via capture_decision; only the transcript parser (Gemini's `toolCalls`, NOT a
`"tool_use"` string) and the wire format are Gemini-specific.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.hooks.capture_decision import (  # noqa: E402
    auto_capture_enabled, session_marker, should_remind)

REMINDER = (
    "[Singularity] Session did substantial work. If it produced non-obvious insights, "
    "decisions (with owner/source) or approaches that worked/failed, capture them to the "
    "vault before moving on — say `um` or run `mb learn \"<insight>\" --task <ID>`. "
    "Nothing worth saving? Ignore this."
)

# Gemini tools that persist edits; a call to one targeting the vault means the
# session already captured something.
_WRITE_TOOLS = {"write_file", "replace", "edit", "create_file"}


def _transcript_stats(transcript_path: str, vault_str: str) -> tuple[int, bool]:
    """Return (tool_use_count, capture_already_happened) from a Gemini transcript.

    Tool calls live in `toolCalls[]` on a `type:"gemini"` message (NOT a top-level
    `"tool_use"` line — see tests/fixtures/gemini/transcript_tooluse.jsonl). Capture
    is inferred from an `mb learn`/`mb note` shell call or a write tool targeting the
    vault. Cheap, tolerant per-line JSON scan; any parse error is skipped."""
    tool_uses = 0
    captured = False
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "gemini":
                    continue
                calls = obj.get("toolCalls") or []
                tool_uses += len(calls)
                for tc in calls:
                    blob = json.dumps(tc, ensure_ascii=False)
                    if "mb learn" in blob or "mb note" in blob:
                        captured = True
                    elif tc.get("name") in _WRITE_TOOLS and vault_str and vault_str in blob:
                        captured = True
    except OSError:
        pass
    return tool_uses, captured


def _emit(obj: dict) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (json.JSONDecodeError, OSError, AttributeError):
        return _emit({})                       # fail open: never disturb the session

    # Defensive: if a retry sequence is ever in play, do not stack nudges.
    if event.get("stop_hook_active"):
        return _emit({})

    try:
        from singularity.config import resolve_vault

        vault = resolve_vault(None)
        if vault is None or not vault.exists() or not auto_capture_enabled(vault):
            return _emit({})

        session_id = str(event.get("session_id", "unknown"))
        marker = session_marker(vault, session_id, client="gemini")
        if marker.exists():
            return _emit({})                   # already nudged once this session

        vault_fragment = json.dumps(str(vault))[1:-1]
        tool_uses, captured = _transcript_stats(event.get("transcript_path", ""), vault_fragment)
        if not should_remind(tool_uses, captured):
            return _emit({})

        marker.write_text("nudged-once", encoding="utf-8")
        return _emit({"systemMessage": REMINDER})
    except Exception:  # noqa: BLE001 — a broken hook must never disturb the session
        return _emit({})


if __name__ == "__main__":
    sys.exit(main())

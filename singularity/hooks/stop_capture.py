"""Stop hook: auto-capture gate (T10, replaces the MB-09 tool-count version).

Decides whether the session did real, uncaptured code-editing work using
singularity.session_activity's normalized SessionActivity + decide() core
(T09) instead of a raw tool-use count and a substring search on an escaped
transcript line. Parses the REAL Claude Code transcript JSONL shape
(verified against a live transcript, 2026-07-12 — Anthropic documents this
format as internal and version-unstable, so this parser is deliberately
conservative: anything it cannot confidently classify is skipped, never
guessed):

  {"type": "assistant", "message": {"content": [
      {"type": "tool_use", "id": "toolu_...", "name": "Edit",
       "input": {"file_path": "...", ...}}]}}
  {"type": "user", "message": {"content": [
      {"type": "tool_result", "tool_use_id": "toolu_...", "content": "...",
       "is_error": true}]}}   # is_error key is ABSENT (not false) on success

A code edit is a successful Edit/Write/MultiEdit whose file_path resolves
inside the session's cwd and outside the vault; a vault write is the same
but inside the vault; a sanctioned capture is a successful shell-tool call
whose command matches `mb learn`/`mb note new` via a parsed-string match,
never a raw-JSON substring search.

Real-machine verification (2026-07-12, this session's own live transcript)
found that Windows Claude Code offers BOTH "Bash" (git-bash) and
"PowerShell" as distinct tool names for shell commands — every `mb learn`
call this session actually went through "PowerShell", not "Bash". A
Bash-only check would silently miss all of them (confirmed: it did, in the
first cut of this parser, before this fix).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.telemetry import session_activity as sa  # noqa: E402
from singularity.hooks.capture_decision import session_marker  # noqa: E402

REASON = (
    "[Singularity auto-capture] Before finishing, record this session's knowledge "
    "in the vault: if non-obvious insights, decisions (with owner/source) or "
    "approaches that worked or failed came up — write them down (`mb learn "
    "\"<insight>\" --task <ID>`, or extend the note they belong to; the criterion "
    "is non-obvious / contradicting expectations / took more than one attempt; "
    "code only as file:line). Nothing worth recording — just finish the reply "
    "and write nothing."
)

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}
_SHELL_TOOLS = {"Bash", "PowerShell"}


def parse_transcript(transcript_path: str, session_id: str, cwd: str,
                     vault: Path) -> sa.SessionActivity:
    activity = sa.SessionActivity(client="claude", session_id=session_id)
    cwd_path = Path(cwd) if cwd else None
    pending: dict[str, tuple[str, dict]] = {}   # tool_use_id -> (name, input)

    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue   # a single bad line is not worth a warning; transcripts are large
                content = obj.get("message", {}).get("content") if isinstance(obj, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name")
                        tid = block.get("id")
                        tool_input = block.get("input")
                        if (isinstance(tid, str) and name in (_WRITE_TOOLS | _SHELL_TOOLS)
                                and isinstance(tool_input, dict)):
                            pending[tid] = (name, tool_input)
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id")
                        if not isinstance(tid, str) or tid not in pending:
                            continue
                        name, tool_input = pending.pop(tid)
                        if block.get("is_error"):
                            continue   # failed call never counts, T09 acceptance
                        if name in _WRITE_TOOLS:
                            file_path = tool_input.get("file_path")
                            if not isinstance(file_path, str) or not file_path.strip():
                                activity.parse_warnings.append(
                                    f"{name} tool_use missing file_path, id={tid}")
                                continue
                            try:
                                fp = Path(file_path)
                                abs_path = fp if fp.is_absolute() else (cwd_path or Path.cwd()) / fp
                            except (OSError, ValueError):
                                activity.parse_warnings.append(f"unresolvable file_path: {file_path!r}")
                                continue
                            if sa.is_vault_write_path(abs_path, vault):
                                activity.vault_writes += 1
                            elif cwd_path is not None and sa.is_code_edit_path(abs_path, cwd_path, vault):
                                activity.code_edits += 1
                        elif name in _SHELL_TOOLS:
                            command = tool_input.get("command")
                            if isinstance(command, str) and sa.is_sanctioned_capture_command(command):
                                activity.sanctioned_captures += 1
    except OSError:
        activity.parse_warnings.append("transcript unreadable")
    return activity


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
    stop_hook_active = bool(event.get("stop_hook_active"))

    try:
        from singularity.config import resolve_vault
        from singularity.machine.hooks_state import capture_config_state

        vault = resolve_vault(None)
        if vault is None or not vault.exists():
            return 0

        session_id = str(event.get("session_id", "unknown"))
        marker = session_marker(vault, session_id)

        state = capture_config_state(vault)
        activity = parse_transcript(str(event.get("transcript_path", "")), session_id,
                                    str(event.get("cwd", "")), vault)
        decision = sa.decide(state["configured"], activity, stop_hook_active, marker.exists())
        if decision.action != "nudge":
            return 0

        marker.write_text("blocked-once", encoding="utf-8")
        print(json.dumps({"decision": "block", "reason": REASON}, ensure_ascii=False))
        return 0
    except Exception:  # noqa: BLE001 — a broken hook must never trap the user in a loop
        return 0


if __name__ == "__main__":
    sys.exit(main())

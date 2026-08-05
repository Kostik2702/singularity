"""Grok Stop hook: auto-capture gate (GRK-04, capture branch = GO).

Thin wire adapter around the shared cores — singularity.session_activity
(decide + path classifiers) and capture_decision.session_marker — for the
Grok Build CLI's Stop event. GRK-00 proved the full Claude-style stop-gate
vocabulary works in Grok (block reason reaches the model, `stopHookActive`
set on the continuation, headless included), so this mirrors the Claude
stop_capture contract: at most ONE block per session, marker client="grok".

Dynamic SessionStart recall stays NO-GO (GRK-00: neither stdout nor
additionalContext is injected on SessionStart/UserPromptSubmit) — recall
lives as a static instruction in the managed <grok-home>/AGENTS.md block.

Wire format (pinned by tests/fixtures/grok/, grok 0.2.111):
  - stdin envelope is camelCase: `hookEventName`, `sessionId`, `cwd`,
    `transcriptPath`, `stopHookActive`, `reason` (snake_case fallbacks are
    read too, in case a future grok aligns with Claude);
  - `reason` is "end_turn" for genuine turn ends; an extra observe-only Stop
    fires at session end with another reason — never nudge on those;
  - `transcriptPath` points at the session's updates.jsonl (ACP event log);
    the model-facing history is the SIBLING chat_history.jsonl with typed
    rows: {"type":"assistant","tool_calls":[{id,name,arguments}]} and
    {"type":"tool_result","tool_call_id","content"};
  - tool failures have fixed content prefixes (both observed live):
    "Tool `<name>` failed: ..." and "Failed to parse arguments for tool ...";
    a shell success is "exit: 0\n..."; an edit success reads like
    "The file <path> has been created." (no structured flag, so edits are
    classified by the failure prefixes).

Fail-open everywhere: any parse/config problem exits 0 and the agent stops
normally. Offline by construction — no xAI API, no network, no direct vault
knowledge writes (only the once-per-session marker file).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.telemetry import session_activity as sa  # noqa: E402
from singularity.hooks.capture_decision import session_marker  # noqa: E402
from singularity.hooks.stop_capture import REASON  # noqa: E402  (one nudge text, all clients)

_EDIT_TOOLS = {"write", "search_replace"}
_SHELL_TOOLS = {"run_terminal_command"}
_FAILURE_PREFIXES = ("Tool `", "Failed to parse arguments")


def _is_failure_result(content: str) -> bool:
    """Grok wraps tool errors in fixed prefixes (observed on 0.2.111):
    'Tool `write` failed: ...' / 'Failed to parse arguments for tool ...'."""
    return content.startswith(_FAILURE_PREFIXES[1]) or (
        content.startswith(_FAILURE_PREFIXES[0]) and "` failed:" in content[:80])


def parse_chat_history(history_path: Path, session_id: str, cwd: str,
                       vault: Path) -> sa.SessionActivity:
    activity = sa.SessionActivity(client="grok", session_id=session_id)
    cwd_path = Path(cwd) if cwd else None
    pending: dict[str, tuple[str, dict]] = {}   # tool_call_id -> (name, args)

    try:
        with open(history_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("type") == "assistant":
                    for call in row.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        name = call.get("name")
                        cid = call.get("id")
                        if not isinstance(cid, str) or name not in (_EDIT_TOOLS | _SHELL_TOOLS):
                            continue
                        try:
                            args = json.loads(call.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            activity.parse_warnings.append(f"unparseable arguments, id={cid}")
                            continue
                        if isinstance(args, dict):
                            pending[cid] = (name, args)
                elif row.get("type") == "tool_result":
                    cid = row.get("tool_call_id")
                    if not isinstance(cid, str) or cid not in pending:
                        continue
                    name, args = pending.pop(cid)
                    content = row.get("content")
                    if not isinstance(content, str) or _is_failure_result(content):
                        continue   # failed call never counts (T09 acceptance)
                    if name in _SHELL_TOOLS:
                        # a shell success is confirmed ONLY by the exit: 0 echo
                        if not content.startswith("exit: 0"):
                            continue
                        command = args.get("command")
                        if isinstance(command, str) and sa.is_sanctioned_capture_command(command):
                            activity.sanctioned_captures += 1
                    else:
                        file_path = args.get("file_path")
                        if not isinstance(file_path, str) or not file_path.strip():
                            activity.parse_warnings.append(
                                f"{name} call missing file_path, id={cid}")
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
    except OSError:
        activity.parse_warnings.append("chat history unreadable")
    return activity


def _field(event: dict, camel: str, snake: str, default=None):
    """camelCase first (grok wire), snake_case fallback (Claude alignment)."""
    return event.get(camel, event.get(snake, default))


def main() -> int:
    # Windows console defaults stdout to cp1251 → Cyrillic becomes mojibake;
    # grok reads hook stdout as UTF-8 (twin of the stdin utf-8-sig rule).
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0

    # Gate only genuine turn ends: the session-end observe fire uses another
    # reason (its output is ignored anyway, but never waste the parse), and a
    # missing reason means an unknown wire — fail open.
    if _field(event, "reason", "reason") != "end_turn":
        return 0
    stop_hook_active = bool(_field(event, "stopHookActive", "stop_hook_active"))

    try:
        from singularity.config import resolve_vault
        from singularity.machine.hooks_state import capture_config_state

        vault = resolve_vault(None)
        if vault is None or not vault.exists():
            return 0

        session_id = str(_field(event, "sessionId", "session_id", "unknown"))
        marker = session_marker(vault, session_id, client="grok")

        transcript = str(_field(event, "transcriptPath", "transcript_path", ""))
        if not transcript:
            return 0
        history = Path(transcript).parent / "chat_history.jsonl"

        state = capture_config_state(vault)
        activity = parse_chat_history(history, session_id,
                                      str(_field(event, "cwd", "cwd", "")), vault)
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

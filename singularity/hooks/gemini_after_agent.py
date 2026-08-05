"""Gemini CLI AfterAgent auto-capture nudge (T10, replaces the GCLI-04
tool-count version).

Ports the Claude Stop-hook capture discipline to Gemini, but SAFELY: the GCLI-00
spike proved that `decision:"deny"` (the retry primitive) mutates headless
`-o json` output (concatenated response + a warnings[] array) and that no payload
field distinguishes interactive from headless — so a deny-based nudge would corrupt
automation. Instead this hook emits a passive `systemMessage`, which the spike
proved leaves headless `-o json`/`-o text` byte-identical to baseline.

Shares the platform-neutral SessionActivity + decide() core (T09) with the Claude
hook; only the transcript parser (Gemini's `toolCalls[]` on a `type:"gemini"`
message, verified against tests/fixtures/gemini/transcript_tooluse.jsonl — a real
redacted gemini-cli capture) and the wire format are Gemini-specific. Success is
`status == "success"` on the toolCall (not a raw JSON-substring search); the exact
arg field name Gemini's write-type tools use for a path is NOT independently
verified against a real fixture (no write-tool call has been captured yet) — this
parser defensively checks a few plausible names and records a parse_warning rather
than guessing when none match, per T09's conservative-under-counting contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.telemetry import session_activity as sa  # noqa: E402
from singularity.hooks.capture_decision import session_marker  # noqa: E402

REMINDER = (
    "[Singularity] Session did substantial work. If it produced non-obvious insights, "
    "decisions (with owner/source) or approaches that worked/failed, capture them to the "
    "vault before moving on — say `um` or run `mb learn \"<insight>\" --task <ID>`. "
    "Nothing worth saving? Ignore this."
)

# Gemini tools that persist edits; args field name for the target path is
# best-effort (see module docstring) — checked against several candidates.
_WRITE_TOOLS = {"write_file", "replace", "edit", "create_file"}
_PATH_ARG_KEYS = ("file_path", "path", "absolute_path")


def parse_transcript(transcript_path: str, session_id: str, cwd: str,
                     vault: Path) -> sa.SessionActivity:
    activity = sa.SessionActivity(client="gemini", session_id=session_id)
    cwd_path = Path(cwd) if cwd else None

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
                if not isinstance(obj, dict) or obj.get("type") != "gemini":
                    continue
                calls = obj.get("toolCalls")
                if not isinstance(calls, list):
                    continue
                for tc in calls:
                    if not isinstance(tc, dict) or tc.get("status") != "success":
                        continue   # failed/pending call never counts, T09 acceptance
                    name = tc.get("name")
                    args = tc.get("args")
                    args = args if isinstance(args, dict) else {}
                    if name in _WRITE_TOOLS:
                        file_path = next((args[k] for k in _PATH_ARG_KEYS
                                         if isinstance(args.get(k), str) and args[k].strip()), None)
                        if file_path is None:
                            activity.parse_warnings.append(
                                f"{name} toolCall missing a recognized path arg")
                            continue
                        try:
                            fp = Path(file_path)
                            abs_path = fp if fp.is_absolute() else (cwd_path or Path.cwd()) / fp
                        except (OSError, ValueError):
                            activity.parse_warnings.append(f"unresolvable path: {file_path!r}")
                            continue
                        if sa.is_vault_write_path(abs_path, vault):
                            activity.vault_writes += 1
                        elif cwd_path is not None and sa.is_code_edit_path(abs_path, cwd_path, vault):
                            activity.code_edits += 1
                    else:
                        command = args.get("command")
                        if isinstance(command, str) and sa.is_sanctioned_capture_command(command):
                            activity.sanctioned_captures += 1
    except OSError:
        activity.parse_warnings.append("transcript unreadable")
    return activity


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
    stop_hook_active = bool(event.get("stop_hook_active"))

    try:
        from singularity.config import resolve_vault
        from singularity.machine.hooks_state import capture_config_state

        vault = resolve_vault(None)
        if vault is None or not vault.exists():
            return _emit({})

        session_id = str(event.get("session_id", "unknown"))
        marker = session_marker(vault, session_id, client="gemini")

        state = capture_config_state(vault)
        activity = parse_transcript(str(event.get("transcript_path", "")), session_id,
                                    str(event.get("cwd", "")), vault)
        decision = sa.decide(state["configured"], activity, stop_hook_active, marker.exists())
        if decision.action != "nudge":
            return _emit({})

        marker.write_text("nudged-once", encoding="utf-8")
        return _emit({"systemMessage": REMINDER})
    except Exception:  # noqa: BLE001 — a broken hook must never disturb the session
        return _emit({})


if __name__ == "__main__":
    sys.exit(main())

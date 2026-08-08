"""Claude UserPromptSubmit hook: push the chain table when a task arrives.

This is the surface that was missing. SessionStart fires before a task exists
and PreToolUse fires after the action is chosen, so nothing in the engine ever
spoke at the one moment a chain can be entered — the second a task arrives.
The workflow router lived as a skill and was therefore never pulled.

What is pushed changed on 2026-08-08. The hook used to classify the prompt itself
by matching word stems, which quietly made the router a function of the language
the prompt was written in. It now injects the table and asks the reader that
already understands every language — the model — to name the class and the words
that chose it. Deciding moved; the push moment did not.

Contract, mirroring module_context (T07): `additionalContext` ONLY. This hook
never emits `decision: block` and never exits 2 — `UserPromptSubmit` is a
blockable event, and a router that can erase the user's prompt is a far worse
failure than a router that stays quiet. Every path fails open: unreadable
stdin, a machine-generated turn, missing vault, broken marker directory and any
unexpected exception all print `{}` and exit 0.

Injection is once per session — the same discipline module_context uses per
(session, project, module). The table restated on every prompt is noise, and
noise is how a gate teaches its reader to skip it.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

def _marker(vault: Path, session_id: str, key: str) -> Path:
    """Per-session touch file. Same directory and sanitising rule as
    module_context — and literally its regex, not a second one that agrees
    today. Session ids arrive from the client: the earlier deny-list of eleven
    characters blocked traversal but let control characters through, and two
    sanitisers for one job are two things to keep in step."""
    from singularity.knowledge.module_context import _SAFE_MARKER_RE

    markers = vault / ".mb" / "markers"
    markers.mkdir(parents=True, exist_ok=True)

    def _safe(s: str, cap: int) -> str:
        return _SAFE_MARKER_RE.sub("_", s)[:cap] or "_"

    return markers / f"route-claude-{_safe(session_id, 128)}-{_safe(key, 64)}"


def build_additional_context(event: dict) -> str | None:
    """The text to inject, or None for silence."""
    from singularity.config import resolve_vault
    from singularity.machine import routing

    if not routing.should_push(event.get("prompt")):
        return None

    # The marker really is a nicety and must be built like one. It was not:
    # `mkdir`/`touch` ran before `render`, so a read-only vault, wrong ownership
    # or a full disk raised OSError, hit the blanket `except` in `main()`, and
    # deleted the feature permanently with no diagnostic — a recoverable branch
    # hiding a total failure ([[A recoverable error hides a total failure]]).
    # Dedup is best-effort; pushing the table is not.
    stamp = None
    try:
        vault = resolve_vault(None)
        if vault is None or not vault.exists():
            vault = Path(tempfile.gettempdir()) / "singularity-route"
        stamp = _marker(vault, str(event.get("session_id", "unknown")), "table")
        if stamp.exists():
            return None
    except OSError:
        stamp = None            # cannot dedup — say it once too often, never zero

    text = routing.render_table()
    if stamp is not None:
        try:
            stamp.touch()       # after a successful render, never before
        except OSError:
            pass
    return text


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError, AttributeError):
        print("{}")
        return 0

    try:
        context = build_additional_context(event)
    except Exception:  # noqa: BLE001 — a broken router must never cost a prompt
        context = None

    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context}}, ensure_ascii=False))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Gemini CLI SessionStart adapter (GCLI-03): inject Singularity memory context
into every Gemini session, reusing the exact same context builder as Claude.

`build_context()` is imported from session_start (single source of truth) — this
module only differs in the wire format. Gemini's SessionStart hook (see
tests/fixtures/gemini/README.md, GCLI-00) is advisory: it reads a JSON payload on
stdin (`source`: startup|resume|clear) and expects a JSON object on stdout whose
`hookSpecificOutput.additionalContext` string is injected as the first turn
(interactive) or prepended to the prompt (headless). continue/decision are ignored,
so a failure here can never block startup — we still emit valid JSON on any error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# hook runs outside the package context; make the package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from singularity.hooks.session_start import build_context  # noqa: E402


def main() -> int:
    # Windows console defaults stdout to cp1251 -> Cyrillic mojibake; Gemini reads
    # hook stdout as UTF-8 (same rationale as the Claude adapter).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    # Consume the payload; tolerate invalid/empty stdin (content not required).
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        context = build_context()
    except Exception as exc:  # noqa: BLE001 — a broken hook must never block startup
        context = f"[Singularity memory] hook error: {exc}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Claude UserPromptSubmit hook: Jev's second opinion on the size of a task.

A second UserPromptSubmit entry, a separate process from user_prompt_submit.py,
so a slow or failing Jev can never delay the chain table.

Contract, stricter than the table hook because this one waits on a network:
`additionalContext` ONLY, never a decision that stops the prompt, never exit
code 2, nothing on stderr. Every failure - flag off, no key, bad key, garbage on
stdin, a Jev that sleeps - ends in exit 0 with no output. A watchdog thread
hard-exits 0 at 4.5 s, under the 5 s the client gives the hook; Jev itself gets
3.5 s and a deadline, so its retries cannot run past it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

WATCHDOG_SECONDS = 4.5
_STARTED = time.monotonic()


def _arm_watchdog() -> threading.Timer:
    # A thread, not SIGALRM: Windows has no SIGALRM, and a blocking socket read
    # is exactly what a signal handler would be waiting behind anyway.
    timer = threading.Timer(WATCHDOG_SECONDS, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    return timer


# Armed before anything heavy is imported, so the budget covers the imports too.
# Only as a script: importing this module (a test, a plant) must not schedule
# the death of the importing process.
_WATCHDOG = _arm_watchdog() if __name__ == "__main__" else None


def build_additional_context(event: dict, started: float) -> str | None:
    """The text to inject, or None for silence."""
    from singularity.config import Config, read_config_tolerant, resolve_vault
    from singularity.jev import enabled, router

    data = read_config_tolerant()
    if data.get("jev_enabled") is not True:
        return None
    vault = resolve_vault(None)
    if vault is None or not vault.is_dir():
        return None
    cfg = Config(vault=vault, data=data)
    if not enabled(cfg):
        return None
    return router.route(cfg, event.get("prompt"), event.get("session_id"), started=started)


def main() -> int:
    started = _STARTED
    watchdog = _WATCHDOG or _arm_watchdog()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return 0
        context = build_additional_context(event, started)
    except BaseException:  # noqa: BLE001 - a broken router must never cost a prompt
        return 0
    finally:
        watchdog.cancel()
    if context:
        try:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context}}, ensure_ascii=False))
            sys.stdout.flush()
        except BaseException:  # noqa: BLE001 - a closed pipe is not worth a traceback
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

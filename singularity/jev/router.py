"""The message router: how big is this task, and is it a reply to the last one.

Pure functions plus one `route()` call. The hook (`hooks/jev_router.py`) owns
the process-level guarantees (watchdog, silence, exit code); this module owns
the decision. Below `THRESHOLD` confidence Jev does not decide and the lead
does: no context is emitted then.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

from ..config import atomic_write_json
from ..knowledge.redact import redact
from . import THRESHOLD, client, enabled, log, noul_confidence

JEV_TIMEOUT = 3.5
MAX_PROMPT_CHARS = 20_000
TRUNCATED_STATE_CHARS = 6_000
KEPT_SESSIONS = 50

# Mirrors team-lead's tier minimums; a hint for the lead, never an order. Opus
# is for checks (team-lead §3), so a large task is built on sonnet and reviewed
# on opus.
ROUTES = {
    "tiny": "answer directly (no executor); any edit is a ticket",
    "small": "haiku·low",
    "medium": "sonnet·medium",
    "large": "sonnet·high executors + opus·high review; plan first",
}

# Turns the harness writes into the prompt slot: background-task notices, hook
# feedback, local-command envelopes. Matched at the START only - a person quoting
# one in the middle of a message is still a person. (The chain-table hook keeps
# its own, substring-based list in machine/routing.py.)
HARNESS_PREFIXES = ("<task-notification", "<system-reminder", "[system notification",
                    "<local-command", "<command-name", "<command-message",
                    "caveat: the messages below", "stop hook feedback:",
                    "hook additional context:")
SLASH_COMMAND = re.compile(r"/[A-Za-z][\w:-]*(\s|$)")

QUESTIONS = {
    "size": {
        "type": "score",
        "instructions": "How large is the task this message asks for?",
        "criteria": [
            "tiny: a one-line answer, lookup or trivial edit; no tools or one tool call",
            "small: a contained change or question in one file or one topic, minutes of work",
            "medium: a feature or investigation across several files that needs a plan",
            "large: a multi-part system change, architecture or research across many components",
        ],
    },
    "followup": {
        "type": "noul",
        "instructions": (
            "Is this message a short reply to the previous exchange (a confirmation, "
            "a correction or a tweak such as 'yes, do it but shorter') rather than a new task?"),
        "criteria": {
            "true": "continues the previous exchange; meaningless without it",
            "false": "a new, self-contained request",
        },
    },
}


def skip_reason(prompt) -> str | None:
    """Why this prompt is not sent at all, or None."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "empty"
    head = prompt.lstrip()
    if SLASH_COMMAND.match(head):
        return "slash command"
    if head.lower().startswith(HARNESS_PREFIXES):
        return "harness turn"
    return None


def state_of(prompt: str) -> str:
    text = prompt if len(prompt) <= MAX_PROMPT_CHARS else prompt[:TRUNCATED_STATE_CHARS]
    return redact(text).text


def decide(answers: dict, previous_route: str | None = None) -> dict:
    """Jev's answers to a decision. Raises KeyError/TypeError/ValueError on a
    shape it cannot read - the caller turns that into a journal error.

    A follow-up is judged first: "yes, do it" says nothing about size, so its
    size confidence would sink exactly the messages this path exists for. With a
    previous route the follow-up inherits it; without one nobody can say what it
    continues, so the lead decides."""
    score = float(answers["size"]["score"])
    confidence = float(answers["size"]["confidence"])
    followup = float(answers["followup"]["noul"])
    if not all(math.isfinite(v) for v in (score, confidence, followup)):
        raise ValueError("non-finite answer")
    level = min(3, max(0, math.floor(score + 0.5)))
    size = log.SIZES[level]
    out = {"size": size, "confidence": confidence, "followup": followup,
           "decided": "self", "route": None}
    if followup >= THRESHOLD:
        if previous_route:
            out.update(decided="jev", route=previous_route,
                       confidence=noul_confidence(followup))
        return out
    if confidence >= THRESHOLD:
        out.update(decided="jev", route=ROUTES[size])
    return out


def context_for(decision: dict) -> str | None:
    if decision["decided"] != "jev":
        return None
    return (f"[jev] size={decision['size']} conf={decision['confidence']:.2f} "
            f"followup={decision['followup']:.2f} → executors: {decision['route']}. "
            "A hint, not an order: below conf 0.6 you decide; Fable only via advisor.")


def _routes_path(cfg):
    return log.jev_dir(cfg) / "last_route.json"


def last_route(cfg, session: str | None) -> str | None:
    if not session:
        return None
    try:
        data = json.loads(_routes_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(session) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def remember_route(cfg, session: str | None, route: str | None) -> None:
    if not session or not route:
        return
    try:
        data = json.loads(_routes_path(cfg).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.pop(session, None)
    data[session] = route
    for stale in list(data)[:-KEPT_SESSIONS]:
        del data[stale]
    try:
        atomic_write_json(_routes_path(cfg), data)
    except OSError:
        pass


def route(cfg, prompt: str, session: str | None, *, started: float | None = None) -> str | None:
    """The context to inject, or None for silence. One journal line per call.
    Never raises on a Jev failure: that is a journal line, not a lost prompt."""
    if not enabled(cfg) or skip_reason(prompt):
        return None
    started = time.monotonic() if started is None else started
    session = session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    key = client.resolve_key()
    if not key:
        log.append(cfg, "router", text=prompt, session=session, error="no_key:null")
        return None
    try:
        reply = client.ask(state_of(prompt), QUESTIONS, timeout=JEV_TIMEOUT,
                           deadline=started + JEV_TIMEOUT, key=key)
    except client.JevError as exc:
        log.append(cfg, "router", text=prompt, session=session, error=exc.tag)
        return None
    except Exception:  # noqa: BLE001 - whatever else: a journal line, never a lost prompt
        log.append(cfg, "router", text=prompt, session=session, error="internal:null")
        return None
    try:
        decision = decide(reply["answers"], last_route(cfg, session))
    except (KeyError, TypeError, ValueError, AttributeError):
        log.append(cfg, "router", text=prompt, session=session, error="bad_json:null",
                   ms=reply["ms"], model=reply["model"], request_id=reply["request_id"])
        return None
    log.append(cfg, "router", text=prompt, session=session, size=decision["size"],
               followup=decision["followup"], confidence=decision["confidence"],
               decided=decision["decided"], route=decision["route"], cost=reply["cost"],
               ms=reply["ms"], model=reply["model"], request_id=reply["request_id"])
    remember_route(cfg, session, decision["route"])
    return context_for(decision)

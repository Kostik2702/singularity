"""The per-spawn pick: which model and effort for an executor brief.

A hint the lead asks for before a spawn (`mb jev pick`); it never rewrites a
spawn. Options run cheapest first on purpose - the vendor documents that a
choice leans to its first option, so the bias favours the minimal tier. Fable is
not an option: it is reached only through the advisor.
"""
from __future__ import annotations

import math
import time

from ..knowledge.redact import redact
from . import THRESHOLD, client, enabled, log

JEV_TIMEOUT = 3.5

TIERS = {
    "haiku·low": "search, reading and summarising files or logs, inventories, "
                 "bulk same-shape edits from an exact pattern",
    "sonnet·medium": "writing code and tests from an explained plan",
    "sonnet·high": "refactoring or hard code from a plan",
    "opus·high": "checking a result: review, critique, validation, or a judgement call",
}

QUESTIONS = {
    "tier": {
        "type": "choice",
        "instructions": "Which executor tier fits the task described in the state?",
        "criteria": TIERS,
    },
}


def pick(cfg, text: str, session: str | None = None) -> str:
    """The one-line answer: `pick: <tier> (conf 0.74)` or `pick: decide yourself (...)`.
    Never raises: any failure is a journal line and "decide yourself"."""
    if not enabled(cfg):
        return "pick: decide yourself (jev off)"
    key = client.resolve_key()
    if not key:
        return "pick: decide yourself (no key)"
    started = time.monotonic()
    try:
        reply = client.ask(redact(text).text, QUESTIONS, timeout=JEV_TIMEOUT,
                           deadline=started + JEV_TIMEOUT, key=key)
    except client.JevError as exc:
        log.append(cfg, "pick", text=text, session=session, error=exc.tag)
        return f"pick: decide yourself (error {exc.kind})"
    except Exception:  # noqa: BLE001 - never a traceback for a hint
        log.append(cfg, "pick", text=text, session=session, error="internal:null")
        return "pick: decide yourself (error internal)"
    try:
        answer = reply["answers"]["tier"]
        tier, confidence = str(answer["choice"]), float(answer["confidence"])
        if tier not in TIERS or not math.isfinite(confidence):
            raise ValueError(tier)
    except (KeyError, TypeError, ValueError, AttributeError):
        log.append(cfg, "pick", text=text, session=session, error="bad_json:null",
                   ms=reply["ms"], model=reply["model"], request_id=reply["request_id"])
        return "pick: decide yourself (error bad_json)"
    decided = "jev" if confidence >= THRESHOLD else "self"
    log.append(cfg, "pick", text=text, session=session, confidence=confidence,
               decided=decided, route=tier if decided == "jev" else None,
               cost=reply["cost"], ms=reply["ms"], model=reply["model"],
               request_id=reply["request_id"])
    if decided == "self":
        return f"pick: decide yourself (conf {confidence:.2f})"
    return f"pick: {tier} (conf {confidence:.2f})"

"""Single source of truth for which engine-owned skill
(singularity/skills/<name>) is deployed to which client, and why not
otherwise (T15).

Before this module, Claude's list (register_skills.SKILLS) and the
Codex/Gemini list (client_registration.PORTABLE_SKILLS) were two
independently-maintained literals that had to be kept in sync by hand --
adding a portable skill meant editing two files, and nothing enforced they
agreed (they happened to hold the same 10 skills in the same order, but
that was accidental, not structural). This is the one place a skill's
client support is declared; every installer computes its own list by
filtering this manifest, so they cannot drift apart again -- and a skill
supported on some but not all clients (impossible to express as a single
shared list before this) is now just a `clients` subset.
"""
from __future__ import annotations

from typing import Any

_ALL_CLIENTS = ["claude", "codex", "gemini"]

# clients: subset of {"claude", "codex", "gemini"} this skill is deployed to.
# tools: informational -- the execution capability the skill's BODY actually
# depends on (e.g. "Agent" = spawns subagents via the Task/Agent tool), not
# enforced at install time; there is no per-client tool-availability probe.
# reason: required (non-None) whenever `clients` is not all three -- a skill
# missing from a client must always have a documented, human-readable why.
SKILL_MANIFEST: dict[str, dict[str, Any]] = {
    "mb-search":             {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-memory":              {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-llm-delegate":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-onboard":             {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-triage":              {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "workflow-helper":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "grill":                  {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "confidence-calibration": {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "memory-curator":         {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "error-reports":          {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "report":                 {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "compaction-plan":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents (investigator/critic/validator/historian) "
                  "via the Agent/Task tool -- Codex/Gemini have no equivalent executor",
    },
    "planning-council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents (investigator/critic/validator/historian) "
                  "via the Agent/Task tool -- Codex/Gemini have no equivalent executor",
    },
    "observation-council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents via the Agent/Task tool -- "
                  "Codex/Gemini have no equivalent executor",
    },
}


def skills_for_client(client: str) -> list[str]:
    """Deterministic, manifest-order list of skill names supported on `client`."""
    return [name for name, meta in SKILL_MANIFEST.items() if client in meta["clients"]]


def exclusion_reason(name: str, client: str) -> str | None:
    """Why `name` is NOT deployed to `client`, or None if it is supported there
    (or `name` is unknown to the manifest -- callers that need "known and
    supported" should check membership in skills_for_client separately)."""
    meta = SKILL_MANIFEST.get(name)
    if meta is None or client in meta["clients"]:
        return None
    return meta.get("reason") or "not supported on this client (no reason recorded)"

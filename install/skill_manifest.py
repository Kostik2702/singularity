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

_ALL_CLIENTS = ["claude", "codex", "gemini", "grok"]

# clients: subset of {"claude", "codex", "gemini", "grok"} this skill is
# deployed to.
# tools: informational -- the execution capability the skill's BODY actually
# depends on (e.g. "Agent" = spawns subagents via the Task/Agent tool), not
# enforced at install time; there is no per-client tool-availability probe.
# reason: required (non-None) whenever `clients` is not all three -- a skill
# missing from a client must always have a documented, human-readable why.
# Skills that used to be here and are not any more
#
# `capcut-editor` and `video-visual-review` moved to `E:\Project\studio-orchestration`
# on 2026-08-06. Both orchestrate MONTAGE, which is not the memory engine's business: shipping
# them from here meant reinstalling memory in order to get a montage skill, and — once the
# orchestration repository also carried `video-visual-review` — meant two copies of one skill with
# nothing diffing them. The engine keeps the skills that ARE its own mechanism (`mb-*`, curator,
# workflow) and nothing else.
SKILL_MANIFEST: dict[str, dict[str, Any]] = {
    "mb-search":             {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-memory":              {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-llm-delegate":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-onboard":             {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-triage":              {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "mb-ingest":              {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "workflow-helper":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "grill":                  {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "confidence-calibration": {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "memory-curator":         {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "error-reports":          {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "report":                 {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "compaction-plan":        {"clients": _ALL_CLIENTS, "tools": ["Bash"], "reason": None},
    "create-image-prompt":   {"clients": _ALL_CLIENTS, "tools": [], "reason": None},
    "council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents (investigator/critic/validator/historian) "
                  "via the Agent/Task tool -- Codex/Gemini have no subagent "
                  "executor; Grok has spawn_subagent but the council personas are "
                  "Claude agent definitions, not adapted or real-client tested there",
    },
    "planning-council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents (investigator/critic/validator/historian) "
                  "via the Agent/Task tool -- Codex/Gemini have no subagent "
                  "executor; Grok has spawn_subagent but the council personas are "
                  "Claude agent definitions, not adapted or real-client tested there",
    },
    "observation-council": {
        "clients": ["claude"], "tools": ["Agent"],
        "reason": "spawns Claude subagents via the Agent/Task tool -- Codex/Gemini "
                  "have no subagent executor; Grok has spawn_subagent but the "
                  "council personas are Claude agent definitions, not adapted or "
                  "real-client tested there",
    },
    "visual-regression-bootstrap": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "acceptance requires the operator to visually review initial "
                  "baselines and drive the mandatory negative tolerance test; "
                  "bound to the local Playwright/browser provisioning conventions "
                  "of the Claude-side toolchain",
    },
    "bg-remove": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "human-gate step requires visually judging cutout edges/hair "
                  "matting from rendered QA images via multimodal Read; also "
                  "depends on the local isolated ML venv only provisioned for "
                  "the Claude-side imgqa toolchain",
    },
    "design-fidelity-audit": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "judgment step requires visually comparing viewport screenshots "
                  "against the design mockup via multimodal Read and consulting the "
                  "vault (mb search) for owner decisions during A/B categorization вЂ” "
                  "that review loop is authored and validated only on Claude Code",
    },
    "website-factory": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "orchestrates the E09 graph across multimodal review skills "
                  "and vault-driven owner gates; acceptance path exercised only "
                  "on Claude Code (WSF-12)",
    },
    "motion-3d-implementer": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "runtime-choice judgment and the evidence loop read rendered "
                  "screenshots multimodally and consult vault decisions; ends "
                  "at the owner visual gate вЂ” GT-precedent (letter-glitch) "
                  "executed only on Claude Code (WSF-E05)",
    },
    "pattern-adapter": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "resolving recipe TODOs (intent, token mapping, fallback "
                  "design) is a judgment layer over the recipe CLI that "
                  "consults the vault and ends at owner acceptance вЂ” GT-tested "
                  "only against Claude Code (WSF-E04)",
    },
    "motion-qa": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "budget findings need A/B triage against owner decisions in "
                  "the vault (a continuous background loop can be a deliberate "
                  "choice) and drive the owner budget sign-off вЂ” GT-tested only "
                  "against Claude Code (WSF-E06)",
    },
    "component-intake": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "quarantine judgment (disputed REJECTs, requirement triage "
                  "into adaptation recipes) consults the vault and drives owner "
                  "gates вЂ” GT-tested only against Claude Code (WSF-E03)",
    },
    "pattern-source-scout": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "query-expansion judgment from the brief and the review-board "
                  "reading before the owner shortlist gate rely on multimodal "
                  "Read and vault consultation вЂ” GT-tested only against Claude "
                  "Code (WSF-E02)",
    },
    "design-analysis": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "one of the two Stage-1 skills with a legitimate LLM judgment "
                  "step: aesthetic/structural mockup reading requires multimodal "
                  "Read anchored to design-metrics numbers, plus the owner brief "
                  "gate вЂ” GT-tested only against Claude Code (WSF-06)",
    },
    "contrast-and-a11y-check": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "judgment step cross-references vault owner decisions (mb "
                  "search) for A/B categorization of violations and reads "
                  "rendered evidence multimodally; keyboard protocol depends on "
                  "the local Playwright provisioning conventions вЂ” GT-tested "
                  "only against Claude Code (WSF-05)",
    },
    "design-reference-scout": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "thin SKILL.md over the design-scout CLI; the query-expansion "
                  "step and the moodboard review before the owner acceptance "
                  "gate rely on multimodal Read of the rendered board and vault "
                  "consultation (mb search/mb learn) вЂ” authored and GT-tested "
                  "only against Claude Code (WSF-09)",
    },
    "image-asset-qa": {
        "clients": ["claude"], "tools": ["Bash"],
        "reason": "thin SKILL.md over the imgqa CLI whose interpretation step "
                  "requires visually reading imgqa's rendered outputs (200% edge "
                  "crops, 3-background composites) as images via multimodal Read "
                  "and driving a human edge-acceptance gate; that inspection + "
                  "dogfood/GT loop is authored and tested only against Claude "
                  "Code -- not validated on Codex/Gemini/Grok",
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

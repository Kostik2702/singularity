"""Task class -> chain of work, and the table the classifier reads.

The delivery architecture had three push moments — SessionStart, PreToolUse,
Stop — and none of them is "a task arrived". SessionStart fires before the task
exists, so it cannot classify anything; PreToolUse fires after the action is
already chosen, so it can only comment. The workflow router therefore lived as
a skill, which is a PULL surface: it ran only when someone remembered it, and
across a full working day it was never pulled once.

A rule is a constraint and can wait in context — "plant the violation" is
evaluated whenever a test gets written, wherever its text happens to sit. A
CHAIN is a procedure: it has to be ENTERED, and entering happens exactly once,
in the first second of a task. So the chain table is pushed here, behind
`UserPromptSubmit` — the one event that fires at that second.

WHO classifies changed on 2026-08-08. Until then this module matched a table of
word stems against the prompt, which made the router a function of the language
the user happened to write in: the stems were Russian, so an English prompt was
never routed and a Russian one was. A deterministic lexical matcher cannot be
language-neutral without enumerating every language, so the classification moved
to the reader that already understands all of them — the model. This module now
ships the table; the model names the class and says which words in the task
chose it. That is the same evidence the marker list used to print, produced by
something that does not care which language the task is written in.

Two properties this module still exists to keep:

1. The chain is ANNOUNCED, not left to be remembered. The table is pushed at the
   moment a task arrives, once per session, whether or not anyone asks for it.
2. Silence on machine turns. A background-task notification arrives through the
   same `UserPromptSubmit` event as a person's message; a machine talking to
   itself is not a task, and spending the table on it teaches the reader to skip
   the announcement.

stdlib only, no config load: this is imported by a hook, and a hook that can
raise is a hook that can end a session ([[Rule - hooks must never use the full
validated Config load path]]).
"""
from __future__ import annotations

from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
WORKFLOW = ENGINE / "singularity" / "workflow"

#: Background-task notifications and other system-generated turns arrive through
#: the same `UserPromptSubmit` event as a person's message. Observed live: a
#: review report was classified as "engine operations" off the words `mb doctor`
#: quoted inside it. A machine talking to itself is not a task.
SYSTEM_PROMPT_SIGNALS = ("[system notification", "<task-notification>",
                         "<local-command-caveat>", "stop hook feedback:",
                         "hook additional context:")

#: Below this a prompt is an acknowledgement or a one-word follow-up, and the
#: table should land on the first real message instead. A LENGTH threshold is
#: the one filter here that stays language-neutral: it counts characters, not
#: words, so a two-letter acknowledgement falls under it in every language.
MIN_PROMPT_CHARS = 10

#: The chains themselves. `entry` is a phase file for the classes that run the
#: full workflow; the rest name the skills that carry the work.
CHAINS: dict[str, dict] = {
    "dev": {
        "title": "development / code fix",
        "entry": "phase-SB-step-back.md",
        "steps": ["SB Step-Back", "0 Approach", "1 Investigation", "1.5 Grill",
                  "2 Planning", "3 Implementation", "4 Review", "5 Verify",
                  "6 Commit", "7 Docs + Self-learn"],
        "note": "skipping a phase is legal (a small edit), but it is recorded [➖] with a reason",
    },
    "research": {
        "title": "research / investigation",
        "entry": "phase-1-investigation.md",
        "steps": ["SB Step-Back", "1 Investigation", "1.5 Grill", "7 Self-learn"],
        "note": "a conclusion is not reported until it has survived an attempt to disprove it (preamble §3)",
    },
    "review": {
        "title": "review / audit",
        "entry": "phase-4-review.md",
        "steps": ["4 Review", "5 Verify"],
        "note": "an old PASS after a complaint is evidence that acceptance is blind, not a defence",
    },
    "web": {
        "title": "web / markup",
        "entry": None,
        "steps": ["website-factory", "design-reference-scout", "design-analysis",
                  "slice implementation", "visual-regression-bootstrap",
                  "design-fidelity-audit", "motion-qa"],
        "note": "Playwright is the only source of truth; an interactive Chrome gives a lead, not a bug",
    },
    "image": {
        "title": "images",
        "entry": None,
        "steps": ["create-image-prompt", "image-asset-qa", "bg-remove"],
        "note": "a frame is accepted by looking at what was rendered, not at the numbers in the plan",
    },
    "memory": {
        "title": "memory / vault",
        "entry": None,
        "steps": ["mb search (dedup before writing)", "mb note new / mb learn",
                  "memory-curator"],
        "note": "a new note comes only from `mb note new`; a direct write into a managed root is flagged by the guard",
    },
    "ops": {
        "title": "engine operations",
        "entry": None,
        "steps": ["mb doctor", "mb status", "show the command's exit code"],
        "note": "the exit code is taken from the command itself, never from the pipe",
    },
}


def phase_file(name: str) -> Path | None:
    """Absolute path to a phase file in the ENGINE, or None if it is gone.

    Engine-resolved on purpose: these files used to live in the vault, which
    made a twelve-phase development flow a property of which vault happened to
    be configured — the same dependency v0.13.0 removed for the agent preamble.
    """
    p = WORKFLOW / name
    return p if p.is_file() else None


def resolve_phase(phase_id: str) -> Path | None:
    """`5` -> phase-5-verify.md, `SB` -> phase-SB-step-back.md, `1.5` -> 1-5.

    Preamble §7 tells every subagent that `mb route` gives the path to phase 5.
    It gave the chain's ENTRY phase only, so the instruction was false — and a
    wrong path is worse than the dead wikilink it replaced, because it reads
    as correct."""
    if not isinstance(phase_id, str) or not phase_id.strip():
        return None
    pid = phase_id.strip().replace(".", "-").upper()
    for p in sorted(WORKFLOW.glob("phase-*.md")):
        stem = p.stem[len("phase-"):]
        if stem.upper() == pid or stem.upper().startswith(pid + "-"):
            return p
    return None


def should_push(prompt: str) -> bool:
    """Whether this turn deserves the table.

    Two language-neutral filters and no third: a machine-generated envelope is
    not a task, and a message shorter than `MIN_PROMPT_CHARS` is an
    acknowledgement. Everything else gets the table exactly once per session,
    whatever language it is written in — which is the property the old stem
    matcher could not have."""
    if not isinstance(prompt, str) or len(prompt.strip()) < MIN_PROMPT_CHARS:
        return False
    low = prompt.lower()
    return not any(sig in low for sig in SYSTEM_PROMPT_SIGNALS)


def render_table() -> str:
    """The chain table, injected once per session at the first real prompt.

    Budgeted: paid once per session, so it states the classes, their chains and
    the entry points, and stops. The instruction is what makes it a chain rather
    than a reference card — a table nobody is told to act on is an archive."""
    lines = ["[mb route] Name this task's class before starting, then follow "
             "that chain. Classify from the MEANING of the request, in whatever "
             "language it is written."]
    for key, chain in CHAINS.items():
        entry = phase_file(chain["entry"]) if chain["entry"] else None
        head = f"- {key} ({chain['title']}): " + " → ".join(chain["steps"])
        lines.append(head)
        if entry is not None:
            lines.append(f"    start at: {entry}")
        lines.append(f"    {chain['note']}")
    lines.append("Say the class and the words in the task that chose it, in one "
                 "line, before starting; announce each phase as you enter it, and "
                 "end every phase with an entry in learnings/active/<TASK>.md. "
                 "No class fits — say so and work your own way.")
    return "\n".join(lines)


def render(key: str) -> str:
    """One chain in full — what `mb route --class <key>` prints."""
    chain = CHAINS[key]
    lines = [f"[mb route] task class: {chain['title'].upper()}",
             "chain: " + " → ".join(chain["steps"])]
    if chain["entry"]:
        entry = phase_file(chain["entry"])
        if entry is not None:
            lines.append(f"start at: {entry}")
    lines.append(chain["note"])
    return "\n".join(lines)

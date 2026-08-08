---
name: workflow-helper
description: Navigate the solo development workflow (Singularity's 12-phase flow). Use when the user says fw, wh, next, "next phase", "what phase", "follow workflow", "start the task through the workflow", or when starting any non-trivial development task. Router + lazy-load - loads ONLY the current phase file, from the ENGINE.
---

# workflow-helper — navigating the solo workflow

The workflow lives in the ENGINE: `{{WORKFLOW}}/` (the router plus the phase files). Lazy-load discipline (rule C9): load ONLY the current phase, never everything at once.

## Protocol

- [ ] 0. Task class and chain: the `UserPromptSubmit` hook pushes the table of chains once per session, and YOU name the class from the meaning of the request — in whatever language it is written — saying in one line which words chose it. `mb route` reprints the table; `mb route --class <key>` prints one chain in full; `mb route --phase <id>` gives the path to a phase file.
- [ ] 1. Read the router: `{{WORKFLOW}}/_router.md` (the phase map plus the cross-cutting rules). If you already read it this session, do not re-read it (cache).
- [ ] 2. Determine the current phase:
    - a new task → phase SB (Step-Back), always;
    - otherwise from the active learnings document (`learnings/active/<TASK>.md`): the last phase entry is the current position; ask the user only if the document leaves it unclear.
- [ ] 3. Read ONLY the current phase's file (`workflow/phase-*.md`) and execute its checkboxes as gates.
- [ ] 4. When a phase completes: auto-append a short entry to the learnings (without asking) → announce the next phase in one line → load its file.
- [ ] 5. `next`/`wh` from the user means "the phase is closed, lead on": check the current phase's unclosed checkboxes (call out failures and skips), then move on.
- [ ] 6. Skipping phases is legal under the router's rules (S size → straight to Phase 3), but it is recorded as `[➖]` with a reason.

## The map (for orientation; the detail is in the phase files)

SB Step-Back → 0 Approach → 1 Investigation → 1.5 Grill → 2 Planning → 3 Implementation → 4 Review (3 passes) → 5 Verify (actually run it) → 6 Commit → 7 Docs + Self-learn (the finale: learnings, changelog, a rule out of the mistakes).

Related skills: `grill` (phase 1.5), `confidence-calibration` (phase 1), `mb-search`/`mb-memory` (cross-cutting), `memory-curator` (phase 7, periodically).

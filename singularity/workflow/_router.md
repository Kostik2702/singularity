---
type: reference
title: "Workflow Router — solo development"
tags: [workflow, router, singularity]
created: 2026-07-03
status: active
source: "COMPLETE_IMPLEMENTATION_WORKFLOW (12 phases -> 8 for solo work)"
---

# Workflow Router — solo development

**Rule C9 (lazy-load):** this file only routes. Load a phase file ONLY when entering that phase — never read all the phases at once. The helper: the `workflow-helper` skill (fw / wh / next).

## Phase map

| # | Phase | File | Entry trigger |
|---|---|---|---|
| SB | Step-Back | `phase-SB-step-back.md` | any new task — ALWAYS first |
| 0 | Choosing the approach | `phase-0-approach.md` | after Step-Back |
| 1 | Investigation | `phase-1-investigation.md` | approach = research/bug |
| 1.5 | Grill | `phase-1-5-grill.md` | before a plan or design (the `grill` skill) |
| 2 | Planning | `phase-2-planning.md` | investigation is closed |
| 3 | Implementation | `phase-3-implementation.md` | the plan is approved |
| 4 | Review | `phase-4-review.md` | the code is written |
| 5 | Verify | `phase-5-verify.md` | review is passed |
| 6 | Commit | `phase-6-commit.md` | verify is green |
| 7 | Docs + Self-learn | `phase-7-docs-selflearn.md` | committed / the task is closed |

## Cross-cutting rules (they hold in every phase)

- [ ] The active learnings document (`learnings/active/<TASK>.md`) exists from phase SB onward; every phase ENDS with an auto-appended short entry in it (without asking the user).
- [ ] [[Core Discipline - SEM-FIRST]] · [[Core Discipline - Never Decide Alone]] · [[Core Discipline - Checkbox Gates]] · [[Core Discipline - Token Economy]].
- [ ] A phase is not "passed" until its checkboxes are `[x]`+evidence or `[➖]`+reason.
- [ ] **Fuel report:** when closing a phase, print one line — `⛽ <phase>: N tokens`. The source is `mb usage --session <id> --brief --from <index of the phase's first turn>`: the command prints one line itself and nothing else. `--session` is MANDATORY: by default it takes the freshest session on the machine, and somebody else's work running in parallel will substitute the figures (check: the id at the start of the line must match the current session). The index comes from the previous phase's `--brief`, so no arithmetic and no intermediate files are needed. Never print `--json` into the context: it weighs ~8k tokens, which makes measuring the spend the largest item of spend.
- [ ] Skipping a phase is legal (a small edit does not need plc), but it is marked `[➖]` with a reason in the learnings.

## Differences from the 12-phase team flow

Removed: the PR description for a team, related repos, persistent QA, demo prep, the memory PR, team review. Added: an explicit verify gate (actually run it) and self-learn at the end. Decision: [[00_Vision_And_Principles]] (owner: user).

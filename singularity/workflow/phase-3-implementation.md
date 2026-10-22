---
type: reference
title: "Phase 3 — Implementation"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 3 — Implementation

Parent: `_router.md`.

- [ ] Who edits: this phase is a ticket for an executor (skill `team-lead`), never the lead's own work; the checklist below is the executor's.
- [ ] Load the repository's rules (its CLAUDE.md / conventions) and [[Core Discipline - Checkbox Gates]]; write in the idiom of the surrounding code.
- [ ] Read-before-edit: the file is read before it is edited; the edit is minimal within the plan's scope.
- [ ] Not one silent change to business logic beyond the plan — noticed that one is needed → stop and record the decision.
- [ ] Verification-after-edit (§17): after "done" — `git diff --stat`, read with your eyes: did exactly the planned thing change?
- [ ] Destructive operations (sed -i, overwriting) only with a backup or through a tmp swap.
- [ ] Cross-platform gates for the code (for Singularity's own code): [[05_Cross_Platform_Strategy]] §4.
- [ ] Auto-append: what changed (file:line), deviations from the plan and why.

**Exit:** the diff matches the plan → `phase-4-review.md`.

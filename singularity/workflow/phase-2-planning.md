---
type: reference
title: "Phase 2 — Planning"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 2 — Planning

Parent: `_router.md`. For S-sized tasks, `[➖]`.

- [ ] Decompose into steps, each with a checkable result (for XL, a full breakdown with dependencies).
- [ ] Risks: find at least 1 gap in your own plan (the ported "try to find at least one gap") — what breaks first? which edge case is uncovered?
- [ ] **Automating something a person does today?** Walk its source (skill, runbook, prose) by the IMPERATIVE VERBS and ask of each: *who does this when nobody is there?* "The assistant, in passing" means the step exists in no task — and the two that hide best are hands ("copy it across", "write it into the file") and eyes ("look at the sheet"). Each such line is an unplanned task with an owner to name, not a detail: three of them were found this way in one pipeline, after it had been decomposed and estimated.
- [ ] Does it touch business logic or behaviour? → an explicit decision with the owner (Never Decide Alone); a refactor preserves behaviour bit for bit.
- [ ] For architectural decisions, a note of type=decision: alternatives, the choice, why, owner + source.
- [ ] The plan is approved (by the user — or, in autonomous mode, marked `ASSUMED plan from investigation`).
- [ ] Auto-append: the plan plus the risks into the learnings.

**Exit:** a plan with risks → `phase-3-implementation.md`.

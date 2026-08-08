---
type: reference
title: "Phase 1 — Investigation"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 1 — Investigation

Parent: `_router.md`.

- [ ] SEM-FIRST: `mb search` on the concept, then targeted Reads of the file:line you found. Do not read files "just in case".
- [ ] Gap analysis: what do I NOT know in order to decide? Every gap is either closed (with evidence) or explicitly OPEN.
- [ ] Verify code first: every claim about the code's behaviour is confirmed by reading the code (file:line), never from memory or assumption.
- [ ] Disproval-first: having formed a conclusion, try to refute it (a counter-example, a neighbouring code path, an edge case). Could not — the conclusion stands.
- [ ] The root-cause question ×3 (for bugs): "is this REALLY the root cause, and not a symptom?"
- [ ] Confidence in a conclusion goes through the `confidence-calibration` skill (never by eye).
- [ ] Auto-append to the learnings: findings (file:line), gaps closed and open, the root-cause hypothesis plus its %.

**Exit:** conclusions with evidence and a calibrated % → `phase-1-5-grill.md` (if a plan or design comes next) or `phase-3-implementation.md`.

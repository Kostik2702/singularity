---
type: reference
title: "Phase 5 — Verify"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 5 — Verify (actually run it)

Parent: `_router.md`. Evidence before assertions: "it works" only after the working was observed.

- [ ] Tests were run (unit/contract) — the output is attached, not retold.
- [ ] The change was run END-TO-END through the real flow (not only tests): the CLI command was invoked, the hook was fed a real event, the search was performed with a real query. (The `/verify` skill, where applicable.)
- [ ] A new gate is declared proven only after a planted violation: add
      a case to `tests/plants.json` and confirm that `pytest tests/test_plants.py`
      goes red on it. This is not discipline but part of the suite — an emptied gate
      fails the ordinary run even when its own test file is green.
- [ ] The negative scenario: break the input or the environment — the behaviour degrades predictably (a fallback or a clear error), never silently.
- [ ] Fail → back to `phase-3-implementation.md` with systematic debugging; do NOT report success on a partial pass.
- [ ] Auto-append: what was run, the results (numbers, outputs), the scenarios left open.

**Exit:** it observably works → `phase-6-commit.md`.

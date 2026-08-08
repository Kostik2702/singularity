---
type: reference
title: "Phase 4 — Review"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 4 — Review (self-review, 3 passes)

Parent: `_router.md`. The QA protocol: 3 full passes over the diff.

- [ ] Pass 1 — **correctness**: logic, error paths, null and empty inputs, conformance to the plan.
- [ ] Pass 2 — **patterns**: the repository's idioms, duplication (could something existing be reused?), naming, superfluous complexity.
- [ ] Pass 3 — **edge cases and gaps**: concurrency, encodings and paths (Win/macOS/Linux!), large inputs, "what if the file / network / process is unavailable". Find at least 1 gap — if "everything is perfect", you did not look hard enough.
- [ ] For anything non-trivial, `/code-review` or the reviewer agent; findings are not accepted without verification (disproval-first).
- [ ] Auto-append: the review findings plus what was fixed.

**Exit:** findings closed → `phase-5-verify.md`.

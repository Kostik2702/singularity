---
name: motion-qa
description: Quality gate for motion/3D on a live page — a scripted scroll timeline, long tasks, CLS, rAF leaks after idle, a census of WebGL contexts plus forced loss and recovery, reduced-motion emulation, and no-JS content survival; budgets come from JSON with an owner sign-off. A thin wrapper over scripts/motion-qa.mjs. Use when "check the animation/perf", motion QA, "why does it heat up / stutter", before merging a motion/3D section, at the acceptance of a pipeline slice.
---

# motion-qa — the cost of movement, measured

The script `scripts/motion-qa.mjs` (envelope motion-qa/1; Playwright from the project's node_modules). Evaluative: exit 0 = budgets green, 1 = FAIL (the list is in verdict.reason), 2 = tool error. **An unsupported metric becomes a typed `unavailable` and the outcome is not PASS** (at most PASS-WITH-UNAVAILABLE) — what was under-measured does not pass in silence.

> **Zero-code rule:** timeline, long tasks, CLS, rAF, WebGL, reduced and no-JS are already in the script. 0 new scripts.

```bash
node "E:\Singularity\singularity\skills\motion-qa\scripts\motion-qa.mjs" \
  --project "<project root>" --url http://localhost:PORT/index.html \
  --out "<evidence OUTSIDE the project>" [--budgets budgets.json]
# waitUntil=load (not networkidle — streaming media is never idle, the NIKA lesson)
```

## Budgets (decision §9: the numbers are frozen by the OWNER after the baseline)

The defaults are generous smoke guards: long tasks ≤5/≤200ms, CLS ≤0.1, rAF after
idle ≤2/500ms, WebGL contexts ≤2, recovery mandatory, a reduced-motion variant
mandatory, no-JS content mandatory. The project's `budgets.json` is the only way to
change them; editing it takes an owner sign-off and is recorded in the vault.

## How to read it

- A `raf-after-settle` FAIL on a live background is not always a bug: a continuous
  background animation may be a category B owner decision; carry it with evidence
  to an owner budget (e.g. "pause when out of viewport or idle") rather than
  muting the gate.
- `webgl-recovery` v1 proves recovery of the BROWSER context; recovery of the
  application's SCENE is v2 (no typed unavailable will appear for it — check the
  recovery listeners by hand until v2).
- The gate's negative fixtures live in `fixtures/` (leak-raf, no-reduced,
  many-contexts, clean): running them proves the gate is alive, which comes before
  trusting its PASS on a project.
- The verdict and the findings go into the learnings; report.json is the evidence
  for the WSF-11 viewer.

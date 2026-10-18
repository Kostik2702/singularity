---
name: pattern-adapter
description: 'Adapting an external component into a project through an adaptation recipe — carrying over the pattern''s design/motion DNA without the demo blob: scaffolding the recipe from the intake report, resolving the TODOs by the method (tokens, timing, breakpoints, reduced/static fallback, cleanup, lazy), validation, and a re-intake after adaptation. A thin wrapper over `component-intake recipe`. Use when "adapt the component", adaptation, "port the pattern into the project", after a quarantine ADAPT verdict, before any code in the implementation branch.'
---

# pattern-adapter — the pattern's DNA, not the demo blob

CLI: `component-intake recipe` (contract adaptation-recipe/1 in `singularity/intake/recipe.py`). Rule §7 of the decision: a component is never copied as-is — a recipe is created, and **a recipe with TODOs does not pass validation** → only a complete one reaches the implementation branch.

## The order

```bash
# 1. Only after an ADAPT intake verdict (a REJECT has no recipe — the validator will not allow it)
component-intake recipe --report <workspace>/intake-report.json \
  --out <evidence>/recipe-<candidate>.json --json
# facts (provenance sha256, license, intake requirements) are filled in; judgements are TODO

# 2. Resolve the TODOs by the method below (by hand or by the skill), against the project manifest

# 3. Completeness gate: exit 0 = the recipe is ready, 1 = TODOs or holes remain
component-intake recipe --validate <evidence>/recipe-<candidate>.json

# 4. Implementation from the recipe in a clean branch/worktree; upstream code and local
#    edits stay distinguishable (patch_set); then RE-INTAKE the adapted result
#    (E03 --runtime) plus visual acceptance
```

## Method for resolving the TODOs

- **preserved_intent** — one sentence: which design function we are taking (e.g. "a progressive blur of the content on scroll"). Not "the whole component".
- **replacements** — colours, fonts and sizes map to the project's design tokens; demo texts and assets are NOT carried over (the licence of demo content is not the licence of the code).
- **timing** — durations and easings map to the project's motion tokens (a single grammar, not random values from the demo).
- **breakpoints** — desktop and mobile behaviour stated explicitly; "as in the demo" is not an answer.
- **reduced_motion** — from the intake requirements; the variant must preserve the content (never opacity:0 on something important).
- **static_fallback** — what a no-JS client or a bot sees: the meaning and the CTA survive.
- **cleanup** — listeners, rAF, observers and contexts on unmount (motion-qa checks this: rAF-after-settle).
- **lazy_strategy** — from the intake (heavy deps → an island); the chunk budget.
- **patch_set** — the list of differences from upstream: file, what changed, why.
- **acceptance** — the paths to the screenshots and perf evidence that prove the adaptation.

## Gates

- [ ] A cross-framework port (react → static and the like) is a SEPARATE owner decision, never the default.
- [ ] The licence's attribution obligations go into license.obligations and into the implementation.
- [ ] After adaptation — a green re-intake (E03 --runtime) plus the owner's visual acceptance.
- [ ] The recipe and the verdicts go into the learnings; the recipe enters the golden registry on promotion (E07).

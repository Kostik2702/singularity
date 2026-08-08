---
name: design-fidelity-audit
description: Audit how faithfully a live site implementation matches the design mockup — screenshots across a viewport matrix in deterministic mode, DOM metrics (section heights, proportions), comparison against the mockup and A/B categorisation of the divergences. Use when "compare it with the design", "does it look like the mockup", "visual audit", design fidelity, "why does it not look like the mockup", acceptance of the visual part, before/after redesign comparison.
---

# design-fidelity-audit — implementation vs mockup

The script gathers EVIDENCE (numbers plus screenshots); the skill delivers the JUDGEMENT (A/B categorisation, verdict). Do not mix them: judgement without numbers is "by eye" (it has been wrong before: contrast 4.2 against the real 4.93); numbers without A/B categories is a war against the owner's decisions.

> **Zero-code rule:** measurements, screenshots and diffs come only from `scripts/fidelity-audit.mjs` and `imgqa compare`. A new one-off script during an audit session is a regression.

## Preconditions

- [ ] The audited project runs locally (`npx http-server`, NOT `python -m http.server` — Range/video).
- [ ] The project has its own Playwright (harness) — the script resolves it from the project's `node_modules`.
- [ ] `IMGQA_PROTECTED_ROOTS=<project root>` is set for the session — no output can land in the audited tree.
- [ ] The page has a `?visual-test=1` mode (canvas t=0, animation off, lazy→eager). The script adds the flag itself; if the project does not support the mode, run [[visual-regression-bootstrap]] first, otherwise the screenshots are non-deterministic and the comparison is meaningless.
- [ ] **Playwright is the only source of truth.** An interactive Chrome tab (claude-in-chrome) is forbidden in this skill as grounds for a verdict: 3 anomalies were measured in one session, 2 of which did not reproduce in a clean context (tab rAF throttling, extension injection). Saw something odd in the tab — that is a lead: reproduce it with a minimal Playwright run, and let the verdict rest on that. Stray console/network errors are first checked for belonging to an extension rather than to the page.

## Pipeline

```bash
node "%USERPROFILE%\.claude\skills\design-fidelity-audit\scripts\fidelity-audit.mjs" ^
  --project "D:\path\to\site" --url http://localhost:8743 ^
  --design "D:\path\to\mockup.png" --out "%TEMP%\fidelity-<task>"
```

1. The script → `fidelity-metrics.json` + `fidelity.<viewport>.png` (1440x900 / 768x1024 / 390x844 by default). Pin `schema_version == "fidelity-audit/1"`; status=error / exit 2 means the tool did not run, NOT "everything matches".
2. Read the metrics: `pageHeight`, `proportion` (height/width) against `design.proportion`, section heights, `horizontalOverflow`, `consoleErrors`, `visualTestMode` (must be true — otherwise stop, the mode did not engage).
3. Read the screenshots and the mockup with your eyes (multimodal Read), section by section.
4. Pixel comparisons of equally sized artefacts (before/after, two runs) go through `imgqa compare a.png b.png --json` (mae/diff_ratio metrics), never a hand-written diff.
5. Report from the template in [`references/report-template.md`](references/report-template.md), with the mandatory A/B categorisation.

## A/B categorisation (the core of the skill)

Every implementation↔mockup divergence gets exactly one category:

- **A — not built, or built differently from the intent.** Goes into the fix list with specifics (selector, number, expectation).
- **B — deliberately different**: truth policy (no fake prices, reviews or contacts), owner decisions, recorded constraints. It does NOT enter the fix list — it is listed in a separate block with a link to the decision (vault: decisions/backlog). Precedent: the "it looks COMPLETELY different" conflict in NIKA was resolved by exactly this split.

Before categorising — `mb search` the project for recorded decisions and constraints; if you do not know why a section differs, that is a question for the owner, not automatically a category A.

## Calibrating judgement about proportions

The mockup is art direction, not a pixel spec (data and figures in it are UNVERIFIED). Signals of a real problem: the page `proportion` is several times the mockup's (NIKA before the rebuild: desktop 5.1 against the mockup's 1.78 — "too long and too empty"), giant sections (a 1013px hero when the first screen is 900px), horizontal overflow, empty bands on the screenshots. NOT signals: heights not matching exactly (responsive ≠ artboard), different content under the truth policy (category B).

## Recording

The verdict plus the top divergences plus the path to the report → `mb learn --task <TASK>`. Defects found on dogfood projects become regression cases (rule §9 of the imgqa contract).

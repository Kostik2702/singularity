---
name: contrast-and-a11y-check
description: Precise accessibility checking of a site — WCAG contrast by exact calculation (imgqa contrast), an axe run against a live URL (scripts/a11y-audit.mjs), and a keyboard-navigation protocol with documented WebKit workarounds. Use when "check the contrast", "is there enough contrast", "a11y", accessibility check, WCAG, axe, keyboard navigation, before a release or a site's visual baseline.
---

# contrast-and-a11y-check — precise accessibility, not "by eye"

Two deterministic tools plus one manual protocol. The judgement "fix it, or is this an owner decision" belongs to the human or the skill, never to the tools.

> **Contrast is NEVER assessed by eye.** NIKA precedent: "by eye" gave a false fail of 4.2 when the real value was 4.93:1 (GT-C1 imgqa: `C9586B` on `070506` → 4.93). Only `imgqa contrast`.

## 1. Contrast (evaluative, exit 0/1/2)

```bash
imgqa contrast "#RRGGBB" "#RRGGBB" --json          # --min 4.5 by default
# verdict.result PASS/FAIL, data.ratio_raw — the gate is on the raw value, not on the rounding
```
- AA normal ≥4.5, AA large ≥3.0, AAA ≥7.0 — read the typed fields `aa_normal/aa_large/aaa_normal`.
- Take the colours from the project's computed styles or tokens, never "roughly" from the mockup.

## 2. An axe run against a live URL (evaluative)

```bash
# Server: ONLY npx http-server (python http.server does not serve Range — a vault rule)
node "E:\Singularity\singularity\skills\contrast-and-a11y-check\scripts\a11y-audit.mjs" \
  --project "<project root>" --url http://localhost:8743 \
  --out "<evidence dir OUTSIDE the project>" [--page /index.html] [--fail-impact serious]
```
- Playwright and axe-core are taken from the PROJECT's node_modules (no network, no CDN); no axe-core means a typed exit 2 with the remediation `npm i -D axe-core`.
- exit 0 = no violations at or above fail-impact; 1 = there are some (the list is in verdict.reason plus `a11y-report.json`); 2 = the tool did not run (≠ "everything is fine").
- `?visual-test=1` is added automatically (determinism: canvas t=0, animation off); `--visual-test 0` turns it off.
- `data.incomplete` means axe is unsure: check those by hand, do not ignore them silently.

## 3. Keyboard navigation protocol (manual, Playwright-only)

An interactive Chrome is not a source of truth (the vault rule "an anomaly in interactive Chrome is a lead"). Check with a Playwright script in the project's context: Tab through every interactive element, a visible focus ring, Escape closing modals, Enter/Space activating.

**WebKit traps (vault [[WEBKIT-TRAPS]], do NOT re-research them):**
- a `button` in WebKit does not take focus on click — verify by Tab order, not by clicking;
- synthetic Tab/Space in headless WebKit are unreliable — run keyboard tests in Chromium and keep WebKit as smoke only;
- `page.route` does not intercept media requests (.mp4) in WebKit — a "media 404" test silently does not work there.

## Gates

- [ ] Every text colour pair goes through `imgqa contrast`, with the numbers in the report.
- [ ] axe exit 2 ≠ PASS; tell "clean" apart from "did not run".
- [ ] A violation may be a category B (a deliberate owner decision) — check the vault before "fixing"; the final verdict is a human gate.
- [ ] Results go into the learnings (`mb learn`), with a11y-report.json as evidence.

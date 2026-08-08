---
name: visual-regression-bootstrap
description: Deploy a ready Playwright visual-regression harness into a static-site project — deterministic screenshots (?visual-test=1), byte-identity checking, the owner's baseline policy and a mandatory negative test of the tolerance. Use when "add visual tests", "set up visual regression", "screenshot tests", new site scaffold testing, "compare against baseline", or before the first visual baseline of any site.
---

# visual-regression-bootstrap — a portable visual harness

Source: the production NIKA harness (198/198 green, every trap already eaten there). The skill is one deployment script plus the preserved lessons.

## Deployment (one command)

```bash
node "%USERPROFILE%\.claude\skills\visual-regression-bootstrap\scripts\bootstrap.mjs" ^
  --project "D:\path\to\site" [--port 8788]
```

It lays down: `tests/playwright.config.js` (workers:2, tolerance 0.0015, http-server webServer), `tests/visual/visual.spec.js` (3 viewports, scroll-through, fonts.ready), `tools/check-visual-determinism.mjs` (byte-identity of 2 runs), `BASELINE-POLICY.md`; and patches `package.json` (scripts + devDeps). Existing files are NOT overwritten (exit 2) without `--force`. After that follow `next_steps` from the output, in order.

## Mandatory gates (the order is strict)

- [ ] **The page implements the `?visual-test=1` contract**: `html[data-visual-test]`, canvas frozen on a deterministic frame (t=0/seed), all JS/CSS animation off, lazy→eager. No such mode — add it to the page first; the harness is meaningless without it.
- [ ] **`PLAYWRIGHT_BROWSERS_PATH=D:\playwright-browsers`** before `npx playwright install` — the browsers (~1GB) do not go on C: (owner's rule).
- [ ] **`npm run check:visual-determinism` → STABLE across every viewport** before generating a baseline. UNSTABLE means fix the page, not the tolerance.
- [ ] **Initial baseline** (`test:update-baseline`) — the owner looks at the screenshots.
- [ ] **NEGATIVE TEST — without it the harness is not accepted:** make a visible change (swap the accent colour) → `test:visual` MUST go red → revert → green. The 0.0015 tolerance was chosen exactly this way (0.02 swallowed a full repaint of a button on a full-page screenshot). "A suite that has never failed has proven nothing."

## Traps already encoded in the templates (do not rediscover them)

1. `python -m http.server` does not serve Range → video hangs; use `http-server` only (it is in the config).
2. CDP fullPage does NOT scroll → lazy content and reveals below the fold come out empty; the spec does a real scroll-through.
3. WebKit headless does not deliver synthetic Tab/Space; `page.route` does not intercept `.mp4` (intercept the manifest) — for e2e layers on top.
4. An anomaly in an interactive Chrome tab is a lead to re-check with clean Playwright, not a bug.
5. workers:2 — full parallelism kills the static server (false timeouts).
6. `networkidle` NEVER arrives on a page with a genuinely loading `<video>`: Chromium pulls media in chunks and keeps the request open. The test dies on a timeout rather than on an assert — take `waitUntil: "load"` plus a poll of a concrete condition (`data-state`, a resource counter).
7. Byte-identity of screenshots is run with `--disable-gpu`: initialising the GPU process gives a uniform ±1 LSB and a false UNSTABLE. Tolerance tests (`toHaveScreenshot`) absorb it — the GPU does not bother them. The tell-tale of a rasteriser rather than content: a diff that is "uniform everywhere, max_delta=1, structural diff_pixels(>2)=0".

## Baseline policy

`BASELINE-POLICY.md` is placed in the project and is law: updates only by the owner through `test:update-baseline` after review; red after a deliberate redesign is the correct state until acceptance; changing the tolerance means repeating the negative test; defects caught and missed go into the regression log (§9).

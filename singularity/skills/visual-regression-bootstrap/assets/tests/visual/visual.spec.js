// @ts-check
import { test, expect } from "@playwright/test";

// Visual-regression baseline (deployed by visual-regression-bootstrap).
//
// Pages load with ?visual-test=1 — the page MUST implement the determinism
// contract: html[data-visual-test] set, canvas frozen at a deterministic
// frame, all JS/CSS animations disabled, lazy media eager. Verify with
// `npm run check:visual-determinism` (byte-identical repeat runs) BEFORE
// trusting any baseline.
//
// Baselines live in tests/visual/visual.spec.js-snapshots/ and are updated
// ONLY via `npm run test:update-baseline` after owner approval — never
// silently, never just to turn the suite green (see BASELINE-POLICY.md).
const VIEWPORTS = [
  { name: "mobile-390", width: 390, height: 844 },
  { name: "tablet-768", width: 768, height: 1024 },
  { name: "desktop-1440", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`page @ ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/index.html?visual-test=1");
    await page.waitForLoadState("networkidle");
    // fonts are the one remaining async layout dependency
    await page.evaluate(() => document.fonts.ready);
    // real scroll-through: CDP fullPage does NOT scroll, so lazy content and
    // scroll-reveals below the fold never trigger without this (NIKA lesson)
    await page.evaluate(async () => {
      const step = window.innerHeight;
      for (let y = 0; y <= document.documentElement.scrollHeight; y += step) {
        window.scrollTo(0, y);
        await new Promise((r) => setTimeout(r, 40));
      }
      window.scrollTo(0, 0);
    });
    await expect(page).toHaveScreenshot(`page-${vp.name}.png`, { fullPage: true });
  });
}

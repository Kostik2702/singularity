// @ts-check
import { defineConfig, devices } from "@playwright/test";

// Deployed by the visual-regression-bootstrap skill (Singularity WEBSITE-FACTORY).
// Deterministic local static server, no build step.
export default defineConfig({
  testDir: ".",
  // http-server (Node), NOT `python -m http.server`: python's server has no
  // Range support -> <video> stalls forever (NIKA finding).
  // Workers capped at 2: a plain static server under full parallel load was
  // observed to time out (NIKA-05A).
  fullyParallel: true,
  workers: 2,
  forbidOnly: !!process.env.CI,
  retries: 1,
  reporter: [["list"]],
  timeout: 45_000,
  expect: {
    // 0.02 was proven TOO LOOSE in a live intentional-diff test (a full
    // button color change passed unnoticed on a full-page shot); 0.0015
    // still passes clean runs but reliably catches a real change. If you
    // touch this number, re-run the negative tolerance test (see
    // BASELINE-POLICY.md) — a tolerance that has never failed is unproven.
    toHaveScreenshot: { maxDiffPixelRatio: 0.0015 },
  },
  use: {
    baseURL: "http://127.0.0.1:__PORT__",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npx http-server -p __PORT__ -c-1 --cors",
    url: "http://127.0.0.1:__PORT__/index.html",
    reuseExistingServer: true,
    timeout: 15_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
